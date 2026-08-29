"""模型目录：多源获取 + 磁盘缓存 + TTL + 负缓存 + 定时刷新。

策略照搬 wb-pool 的 `app/upstream_sync.py`（那套在生产跑了两周，三层降级被
实测验证过），但数据源换成本项目实际能用的四路：

    1. gateway `models.list`        —— 网关自己的 live catalog
    2. gateway `onboarding.models.discover` —— 用激活凭据做 provider 侧发现
    3. 凭据端点 `GET {base_url}/models` —— **逐把 key 轮询，不止激活那把**
    4. 磁盘缓存回放                 —— 上面全挂时不让目录空掉

第 3 路是这次改动的核心。原实现只用「当前激活凭据」拉端点，激活那把一旦被上游
冻结（实测 2026-08-29 主号 `403 ACCOUNT_SUSPENDED`），整个模型目录就空了，而池里
另外两把号明明 `200` 各拉回 20 个模型。所以这里改成按 base_url 分组、组内逐把 key
尝试直到有一把成功，任何一把好 key 都能撑住目录。

缓存写盘的意义：进程内字典重启即失，而 systemd restart 是部署常态；落盘后重启
后首屏立刻有目录，不用等任何一路网络。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

# 正缓存有效期 / 失败后冷却。冷却期内不再打上游，避免一个坏 key 被反复重试。
CACHE_TTL_SECONDS = 3600.0
FAIL_COOLDOWN_SECONDS = 300.0
# 后台定时刷新间隔。比 TTL 略短，保证前端永远读到热缓存而不是自己触发同步拉取。
REFRESH_INTERVAL_SECONDS = 1800.0
# 单个端点的拉取超时。上游偶发 5s+，给足但不至于拖死整轮。
ENDPOINT_TIMEOUT = 20.0

# 上游 vendor 名各家写法不一，按 id 前缀给人类可读厂商名（照 wb-pool vendor_of）。
_VENDOR_BY_PREFIX = [
    ("glm-", "Zhipu"),
    ("kimi-", "Moonshot"),
    ("deepseek-", "DeepSeek"),
    ("minimax-", "MiniMax"),
    ("qwen", "Qwen"),
    ("seed-", "ByteDance"),
    ("mimo-", "Xiaomi"),
    ("longcat", "Meituan"),
    ("hunyuan", "Tencent"),
    ("hy3", "Tencent"),
    ("gpt-", "OpenAI"),
    ("o1", "OpenAI"),
    ("o3", "OpenAI"),
    ("claude", "Anthropic"),
    ("gemini", "Google"),
    ("grok", "xAI"),
    ("auto", "Auto"),
]


def vendor_of(model_id: str) -> str:
    mid = (model_id or "").lower()
    for prefix, name in _VENDOR_BY_PREFIX:
        if mid.startswith(prefix):
            return name
    return ""


def _empty() -> dict[str, Any]:
    return {
        "models": [],
        "timestamp": 0.0,
        "source": None,
        "sources_tried": [],
        "last_fail": 0.0,
        "last_error": None,
        "synced_at": None,
    }


class ModelCatalog:
    """模型目录的唯一入口。所有读都走 `resolve()`，所有写都经 `refresh()`。"""

    def __init__(self, cache_path: Path) -> None:
        self.path = cache_path
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = _empty()
        self._task: asyncio.Task | None = None
        self._load()

    # -- 磁盘 -------------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except Exception:  # noqa: BLE001 - 坏缓存不该让服务起不来
            return
        if not isinstance(raw, dict):
            return
        models = raw.get("models")
        if not isinstance(models, list):
            raw["models"] = []
        # 兼容早期格式：models 曾经是 list[str]
        elif models and isinstance(models[0], str):
            raw["models"] = [{"id": m, "name": m} for m in models]
        self._data = _empty() | raw

    def _flush(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), "utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)
            os.chmod(self.path, 0o600)
        except Exception:  # noqa: BLE001 - 缓存写失败不影响本次返回
            pass

    # -- 读 ---------------------------------------------------------------
    @property
    def expired(self) -> bool:
        if not self._data.get("models"):
            return True
        return (time.time() - float(self._data.get("timestamp") or 0)) > CACHE_TTL_SECONDS

    @property
    def in_cooldown(self) -> bool:
        last_fail = float(self._data.get("last_fail") or 0)
        return bool(last_fail) and (time.time() - last_fail) < FAIL_COOLDOWN_SECONDS

    def resolve(self) -> dict[str, Any]:
        """只读快照，永不打网络。前端轮询走这里。"""
        data = dict(self._data)
        models = list(data.get("models") or [])
        age = time.time() - float(data.get("timestamp") or 0) if data.get("timestamp") else None
        return {
            "models": models,
            "count": len(models),
            "source": data.get("source"),
            "sources_tried": data.get("sources_tried") or [],
            "synced_at": data.get("synced_at"),
            "age_seconds": int(age) if age is not None else None,
            # 目录过期但仍在用 = 前端要显示「可能过时」，而不是清屏。
            "stale": bool(models) and self.expired,
            "last_error": data.get("last_error"),
            "cooldown": self.in_cooldown,
        }

    # -- 归一化 -----------------------------------------------------------
    @staticmethod
    def _normalize(rows: Any, provider: str, source: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in rows or []:
            if isinstance(row, str):
                row = {"id": row}
            if not isinstance(row, dict):
                continue
            mid = row.get("id") or row.get("model") or row.get("name") or ""
            if not mid:
                continue
            out.append({
                "id": mid,
                "name": row.get("display_name") or row.get("name") or mid,
                "provider": row.get("owned_by") or row.get("provider") or provider or "",
                "vendor_label": vendor_of(mid),
                "source": source,
                "ctx": row.get("context_length") or row.get("ctx"),
            })
        return out

    @staticmethod
    def _merge(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按 id 去重，先到先留（调用方按数据源可信度排序）。"""
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for group in groups:
            for item in group:
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                merged.append(item)
        merged.sort(key=lambda m: (m.get("vendor_label") or "zzz", m["id"]))
        return merged

    # -- 端点直拉 ---------------------------------------------------------
    async def _from_endpoints(self, entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        """逐把凭据打 `{base_url}/models`，同一 base_url 只要有一把成功就够。

        这是修 403 空目录的关键：按 base_url 分组，组内顺序尝试，第一把成功就
        跳出。冻结/欠费的 key 只会让它自己失败，不再拖垮整个目录。
        """
        notes: list[str] = []
        by_base: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            base = (entry.get("base_url") or "").rstrip("/")
            if base and entry.get("api_key"):
                by_base.setdefault(base, []).append(entry)

        collected: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=ENDPOINT_TIMEOUT) as client:
            for base, group in by_base.items():
                for entry in group:
                    label = entry.get("note") or entry.get("id") or "?"
                    try:
                        resp = await client.get(
                            f"{base}/models",
                            headers={"Authorization": f"Bearer {entry['api_key']}"},
                        )
                    except Exception as exc:  # noqa: BLE001
                        notes.append(f"{label}: {type(exc).__name__}")
                        continue
                    if resp.status_code != 200:
                        # 403/401 = 这把 key 的账号问题（冻结/欠费/被禁），不是端点问题。
                        detail = ""
                        try:
                            detail = (resp.json() or {}).get("code") or ""
                        except Exception:  # noqa: BLE001
                            detail = ""
                        notes.append(f"{label}: HTTP {resp.status_code} {detail}".strip())
                        continue
                    try:
                        payload = resp.json()
                    except Exception as exc:  # noqa: BLE001
                        notes.append(f"{label}: 响应非 JSON ({type(exc).__name__})")
                        continue
                    rows = payload.get("data") if isinstance(payload, dict) else payload
                    models = self._normalize(rows, entry.get("provider") or "", "endpoint")
                    if models:
                        notes.append(f"{label}: {len(models)} 个")
                        collected.extend(models)
                        break  # 同一端点已拿到清单，不必再耗其他 key
        return collected, notes

    # -- 刷新 -------------------------------------------------------------
    async def refresh(
        self,
        *,
        rpc: Callable[..., Awaitable[Any]] | None,
        entries: list[dict[str, Any]],
        active: dict[str, Any] | None,
        force: bool = False,
    ) -> dict[str, Any]:
        """跑一轮四源获取并落盘。返回 `resolve()` 形状 + `ok` / `errors`。"""
        async with self._lock:
            if not force and not self.expired:
                return self.resolve() | {"ok": True, "skipped": "cache_fresh"}
            if not force and self.in_cooldown:
                return self.resolve() | {"ok": False, "skipped": "fail_cooldown"}

            tried: list[str] = []
            errors: list[str] = []
            gw_models: list[dict[str, Any]] = []
            discover_models: list[dict[str, Any]] = []

            if rpc is not None:
                tried.append("gateway")
                try:
                    payload = await rpc("models.list", None, timeout=60)
                    gw_models = self._normalize(
                        (payload or {}).get("models"), "", "gateway",
                    )
                    for err in (payload or {}).get("errors") or []:
                        kind = err.get("kind") if isinstance(err, dict) else str(err)
                        if kind:
                            errors.append(f"gateway: {kind}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"gateway: {type(exc).__name__}")

                if active and active.get("api_key"):
                    tried.append("discover")
                    try:
                        payload = await rpc(
                            "onboarding.models.discover",
                            {
                                "providerId": active.get("provider"),
                                "apiKey": active.get("api_key"),
                                "baseUrl": active.get("base_url"),
                            },
                            timeout=60,
                        )
                        discover_models = self._normalize(
                            (payload or {}).get("models"),
                            active.get("provider") or "",
                            "discover",
                        )
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"discover: {type(exc).__name__}")

            tried.append("endpoint")
            ep_models, ep_notes = await self._from_endpoints(entries)
            errors.extend(f"endpoint {n}" for n in ep_notes if "个" not in n)

            # 可信度顺序：端点直拉 > 网关 live > provider discover。
            # 端点是上游权威清单（实测 20 条），网关 live catalog 会被上游 503 清零。
            merged = self._merge(ep_models, gw_models, discover_models)

            if not merged:
                self._data["last_fail"] = time.time()
                self._data["last_error"] = "; ".join(errors[:6]) or "所有数据源均未返回模型"
                self._data["sources_tried"] = tried
                self._flush()
                return self.resolve() | {"ok": False, "errors": errors}

            source = "endpoint" if ep_models else ("gateway" if gw_models else "discover")
            self._data.update({
                "models": merged,
                "timestamp": time.time(),
                "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": source,
                "sources_tried": tried,
                "last_fail": 0.0,
                "last_error": "; ".join(errors[:6]) or None,
            })
            self._flush()
            return self.resolve() | {"ok": True, "errors": errors}

    # -- 后台定时 ---------------------------------------------------------
    def start(self, supplier: Callable[[], Awaitable[dict[str, Any]]]) -> None:
        """起后台刷新循环。`supplier` 每轮提供 rpc/entries/active。

        用 supplier 回调而不是直接持引用，是因为凭据库和网关连接都会在运行期
        变化（增删 key、网关重连），每轮都要读当时的真实状态。
        """
        if self._task is not None:
            return

        async def loop() -> None:
            # 起步先等一下：网关握手 + provider 快照要几秒才就绪，太早跑第一轮
            # 会白白记一次失败并进 5 分钟冷却。
            await asyncio.sleep(20)
            while True:
                try:
                    ctx = await supplier()
                    await self.refresh(
                        rpc=ctx.get("rpc"),
                        entries=ctx.get("entries") or [],
                        active=ctx.get("active"),
                        force=False,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - 后台循环绝不能因单轮异常退出
                    pass
                await asyncio.sleep(REFRESH_INTERVAL_SECONDS)

        self._task = asyncio.create_task(loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
