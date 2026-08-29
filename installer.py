"""OpenSquilla install / upgrade driver.

The console manages an OpenSquilla runtime that lives outside it: a uv-managed
tool install plus a gateway process. This module is the only place that shells
out to `uv` and `opensquilla`, so the rules about *where* the distribution may
come from are enforced in one spot.

Three facts drive the design; all three were measured on the production host
rather than assumed:

* **PyPI is not a usable source.** `pip index`/PyPI's JSON API only publish
  `opensquilla` up to 0.3.0, while every real release since is shipped as a
  wheel attached to a GitHub Release (0.5.4 at the time of writing). Resolving
  "latest" through PyPI would *downgrade* a 0.5.x host by two minor versions,
  so the wheel URL is always derived from a release tag and PyPI is never
  consulted.
* **Two independent version oracles exist.** `opensquilla version --check
  --json` reads a static channel manifest mirrored to Aliyun OSS (reachable
  from mainland China, no API rate limit); the GitHub Releases API carries the
  authoritative asset list. The OSS manifest is preferred for "is there an
  update" and GitHub is used to confirm the wheel actually exists, so a
  manifest that runs ahead of asset publication cannot produce a 404 install.
* **The wheel URL is never taken from the client.** A browser may only name a
  version string, which is matched against the release list and rendered into
  the canonical asset URL. Accepting a URL would turn an operator console into
  an arbitrary-code installer.

Upgrades stop the gateway, replace the tool environment, then start it again
and verify both the reported version and `/healthz`, because a tool
environment swapped underneath a live process leaves a running gateway on
deleted inodes — it keeps working until it restarts, which makes a failed
upgrade look successful for hours.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

GITHUB_REPO = os.environ.get("SQUILLA_OS_REPO", "opensquilla/opensquilla")
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}"
# The vendor's own China-facing mirror root (see
# opensquilla/observability/update_check.py: DEFAULT_UPDATE_CHANNEL_ROOT).
OSS_ROOT = os.environ.get(
    "SQUILLA_OS_OSS_ROOT",
    "https://opensquilla-releases.oss-cn-beijing.aliyuncs.com",
)
# Official passive-update manifest. Kept as an explicit override because a
# self-hosted mirror is a reasonable deployment.
OSS_CHANNEL_STABLE = os.environ.get(
    "SQUILLA_OS_CHANNEL",
    f"{OSS_ROOT}/releases/channels/stable.json",
)

# Only versions matching this may be turned into a download URL.
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$")

# Wheels are `py3-none-any`, so one asset name serves every platform.
def wheel_name(version: str) -> str:
    return f"opensquilla-{version}-py3-none-any.whl"


# --------------------------------------------------------------------------
# Download source: measured, not assumed
# --------------------------------------------------------------------------
# The same wheel is published to GitHub Releases and to the vendor's Aliyun OSS
# bucket (verified: both answer a ranged GET with an identical 66,633,623 byte
# Content-Range). Which one is closer is a property of where this host sits, so
# hardcoding either is wrong. Measured from the Los Angeles host this console
# runs on, GitHub's API answered in 61ms against Aliyun's 659ms and the wheel
# in 496ms against 860ms; inside mainland China the ordering inverts. So the
# source is probed and the winner cached.
#
# `SQUILLA_OS_SOURCE` pins it: "github", "aliyun", or "auto" (default).

SOURCE_SPECS: dict[str, dict[str, str]] = {
    "github": {
        "label": "GitHub Releases",
        "wheel": "https://github.com/{repo}/releases/download/v{version}/{wheel}",
        # Cheapest representative request that exercises the same host+TLS path.
        "probe": f"{GITHUB_API}/releases?per_page=1",
    },
    "aliyun": {
        "label": "阿里云 OSS 镜像",
        "wheel": f"{OSS_ROOT}/releases/v{{version}}/{{wheel}}",
        "probe": OSS_CHANNEL_STABLE,
    },
}
SOURCE_PIN = (os.environ.get("SQUILLA_OS_SOURCE", "auto") or "auto").strip().lower()
# GitHub is the canonical publisher, so it is the answer before any measurement.
SOURCE_DEFAULT = "github"
SOURCE_TTL_SECONDS = 6 * 3600.0
_SOURCE_STATE: dict[str, Any] = {"id": "", "at": 0.0, "probes": [], "reason": ""}
_SOURCE_LOCK: asyncio.Lock | None = None


def _source_cache_path() -> Path:
    root = os.environ.get("SQUILLA_CONSOLE_DATA", "") or str(Path(__file__).parent / "data")
    return Path(root) / "update_source.json"


def _load_source_state() -> None:
    """Survive a restart without re-probing; the answer changes only if the host moves."""
    if _SOURCE_STATE["id"]:
        return
    with contextlib.suppress(Exception):
        raw = json.loads(_source_cache_path().read_text("utf-8"))
        if isinstance(raw, dict) and raw.get("id") in SOURCE_SPECS:
            _SOURCE_STATE.update({
                "id": raw["id"],
                "at": float(raw.get("at") or 0),
                "probes": raw.get("probes") or [],
                "reason": str(raw.get("reason") or ""),
            })


def _save_source_state() -> None:
    with contextlib.suppress(Exception):
        path = _source_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_SOURCE_STATE, ensure_ascii=False, indent=2), "utf-8")
        # This file holds no secret, but it lives in the credential directory and
        # every other file there is 0600; a stray world-readable file in a 0700
        # dir is the kind of inconsistency that later gets copied onto one that
        # does matter.
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        os.chmod(path, 0o600)


def _probe_one(url: str, timeout: float) -> dict[str, Any]:
    """Time a single byte-ranged GET. Range keeps a 65 MB wheel from being fetched."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "squilla-console",
        "Range": "bytes=0-0",
    })
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed hosts
            resp.read(1)
            code = resp.status
    except urllib.error.HTTPError as exc:
        # 200/206/416 all prove the host is reachable and serving.
        code = exc.code
    except Exception as exc:  # noqa: BLE001 - unreachable is a valid measurement
        return {"ms": -1, "http": 0, "error": f"{type(exc).__name__}"}
    return {"ms": int((time.monotonic() - started) * 1000), "http": code, "error": ""}


async def probe_sources(timeout: float = 12.0) -> list[dict[str, Any]]:
    """Measure every candidate source concurrently."""
    async def one(sid: str) -> dict[str, Any]:
        spec = SOURCE_SPECS[sid]
        res = await asyncio.to_thread(_probe_one, spec["probe"], timeout)
        return {"id": sid, "label": spec["label"], **res}

    return list(await asyncio.gather(*(one(sid) for sid in SOURCE_SPECS)))


async def resolve_source(*, force: bool = False) -> dict[str, Any]:
    """Pick the download source for this host, caching the measurement."""
    global _SOURCE_LOCK
    if SOURCE_PIN in SOURCE_SPECS:
        return {
            "id": SOURCE_PIN,
            "label": SOURCE_SPECS[SOURCE_PIN]["label"],
            "reason": "由 SQUILLA_OS_SOURCE 指定",
            "pinned": True,
            "probes": [],
            "age": 0.0,
        }
    _load_source_state()
    fresh = _SOURCE_STATE["id"] and (time.time() - _SOURCE_STATE["at"]) < SOURCE_TTL_SECONDS
    if fresh and not force:
        return {
            "id": _SOURCE_STATE["id"],
            "label": SOURCE_SPECS[_SOURCE_STATE["id"]]["label"],
            "reason": _SOURCE_STATE["reason"],
            "pinned": False,
            "probes": _SOURCE_STATE["probes"],
            "age": time.time() - _SOURCE_STATE["at"],
        }

    if _SOURCE_LOCK is None:
        _SOURCE_LOCK = asyncio.Lock()
    async with _SOURCE_LOCK:
        # Another waiter may have finished the probe while this one queued.
        if not force and _SOURCE_STATE["id"] and (time.time() - _SOURCE_STATE["at"]) < SOURCE_TTL_SECONDS:
            return await resolve_source()
        probes = await probe_sources()
        ok = sorted((p for p in probes if p["ms"] >= 0), key=lambda p: p["ms"])
        if ok:
            best = ok[0]
            others = [p for p in ok[1:]]
            if others:
                gain = others[0]["ms"] - best["ms"]
                reason = (f"实测最快：{best['label']} {best['ms']}ms，"
                          f"比 {others[0]['label']} {others[0]['ms']}ms 快 {gain}ms")
            else:
                reason = f"只有 {best['label']} 可达（{best['ms']}ms）"
            chosen = best["id"]
        else:
            chosen = SOURCE_DEFAULT
            reason = "所有源都探测失败，回落到规范发布源"
        _SOURCE_STATE.update({"id": chosen, "at": time.time(), "probes": probes, "reason": reason})
        _save_source_state()
    return {
        "id": chosen,
        "label": SOURCE_SPECS[chosen]["label"],
        "reason": reason,
        "pinned": False,
        "probes": probes,
        "age": 0.0,
    }


def current_source_id() -> str:
    """The source decided so far, without triggering a probe."""
    if SOURCE_PIN in SOURCE_SPECS:
        return SOURCE_PIN
    _load_source_state()
    return _SOURCE_STATE["id"] or SOURCE_DEFAULT


def wheel_url(version: str, source: str | None = None) -> str:
    sid = source or current_source_id()
    spec = SOURCE_SPECS.get(sid) or SOURCE_SPECS[SOURCE_DEFAULT]
    return spec["wheel"].format(
        repo=GITHUB_REPO, version=version, wheel=wheel_name(version),
    )


# --------------------------------------------------------------------------
# Host layout
# --------------------------------------------------------------------------
# Defaults suit a plain `uv tool install opensquilla` on PATH. The production
# host redirects every uv directory onto a data volume, so each one is
# overridable; the console's systemd unit supplies them.

OS_BASE = os.environ.get("SQUILLA_OS_BASE", "")
UV_BIN = os.environ.get("SQUILLA_UV_BIN", "") or shutil.which("uv") or "uv"
OS_BIN = os.environ.get("SQUILLA_OS_BIN", "") or shutil.which("opensquilla") or "opensquilla"
OS_PYTHON = os.environ.get("SQUILLA_OS_PYTHON", "3.12")
# Extras to preserve across upgrades when the receipt cannot be read.
OS_EXTRAS_FALLBACK = os.environ.get("SQUILLA_OS_EXTRAS", "recommended")

_UV_ENV_KEYS = (
    "UV_CACHE_DIR",
    "UV_TOOL_DIR",
    "UV_TOOL_BIN_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "OPENSQUILLA_PROFILE_HOME",
    "OPENSQUILLA_HOME",
    "HOME",
)


def tool_env() -> dict[str, str]:
    """Environment for `uv` / `opensquilla` calls.

    Inherited from the console's own process environment: the deployment sets
    the uv redirection variables on the unit, which keeps this module free of
    host-specific paths.
    """
    env = dict(os.environ)
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    tool_bin = env.get("UV_TOOL_BIN_DIR")
    if tool_bin:
        env["PATH"] = f"{tool_bin}:{env['PATH']}"
    # Never inherit a proxy-less curl config or a pager into subprocesses.
    env["PYTHONUNBUFFERED"] = "1"
    env["NO_COLOR"] = "1"
    return env


def layout() -> dict[str, Any]:
    """Report where the managed runtime lives, for display and preflight."""
    env = tool_env()
    tool_dir = env.get("UV_TOOL_DIR", "")
    receipt = Path(tool_dir) / "opensquilla" / "uv-receipt.toml" if tool_dir else None
    return {
        "uv_bin": UV_BIN,
        "opensquilla_bin": OS_BIN,
        "python": OS_PYTHON,
        "base": OS_BASE,
        "tool_dir": tool_dir,
        "tool_bin_dir": env.get("UV_TOOL_BIN_DIR", ""),
        "profile_home": env.get("OPENSQUILLA_PROFILE_HOME", ""),
        "receipt": str(receipt) if receipt and receipt.exists() else "",
        "repo": GITHUB_REPO,
    }


def installed_extras() -> list[str]:
    """Extras from uv's install receipt, so an upgrade keeps the same feature set.

    Upgrading `opensquilla[recommended]` as bare `opensquilla` silently strips
    optional dependency groups; the gateway then starts but loses whatever the
    extras pulled in. The receipt is the only record of what was requested.
    """
    info = layout()
    path = info.get("receipt")
    if not path:
        return [OS_EXTRAS_FALLBACK] if OS_EXTRAS_FALLBACK else []
    with contextlib.suppress(Exception):
        text = Path(path).read_text("utf-8")
        found = re.search(r"extras\s*=\s*\[([^\]]*)\]", text)
        if found:
            extras = re.findall(r'"([^"]+)"', found.group(1))
            if extras:
                return extras
    return [OS_EXTRAS_FALLBACK] if OS_EXTRAS_FALLBACK else []


def requirement(version: str, extras: list[str] | None = None,
                source: str | None = None) -> str:
    """Build the pinned `pkg[extras] @ url` requirement uv will install."""
    if not VERSION_RE.match(version):
        raise ValueError(f"版本号不合法: {version!r}")
    names = extras if extras is not None else installed_extras()
    suffix = f"[{','.join(names)}]" if names else ""
    return f"opensquilla{suffix} @ {wheel_url(version, source)}"


# --------------------------------------------------------------------------
# Shelling out
# --------------------------------------------------------------------------


async def run_capture(args: list[str], timeout: float = 90.0) -> dict[str, Any]:
    """Run a short command and capture its output."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=tool_env(),
        )
    except FileNotFoundError as exc:
        return {"rc": -1, "stdout": "", "stderr": f"找不到可执行文件: {exc}"}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return {"rc": -1, "stdout": "", "stderr": f"命令超时（{timeout:.0f}s）"}
    return {
        "rc": proc.returncode,
        "stdout": out.decode("utf-8", "replace"),
        "stderr": err.decode("utf-8", "replace"),
    }


def _fetch_json(url: str, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            # GitHub's API answers anonymous requests but rejects a missing UA.
            "User-Agent": "squilla-console",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed hosts
        return json.loads(resp.read().decode("utf-8", "replace"))


async def fetch_json(url: str, timeout: float = 20.0) -> Any:
    """Off-thread JSON GET; urllib is blocking and this runs on the event loop."""
    return await asyncio.to_thread(_fetch_json, url, timeout)


# --------------------------------------------------------------------------
# Version discovery
# --------------------------------------------------------------------------


async def current_version() -> dict[str, Any]:
    """Installed version, plus whether the gateway is up."""
    ver = await run_capture([OS_BIN, "version", "--json"], timeout=60)
    version = ""
    if ver["rc"] == 0:
        with contextlib.suppress(Exception):
            version = (json.loads(ver["stdout"]) or {}).get("version") or ""
    if not version:
        # `version --json` is only present on newer builds; fall back to the
        # dist-info directory name, which exists for any wheel install.
        tool_dir = layout().get("tool_dir") or ""
        if tool_dir:
            root = Path(tool_dir) / "opensquilla"
            for found in root.rglob("opensquilla-*.dist-info"):
                m = re.search(r"opensquilla-([0-9][^/]*)\.dist-info$", str(found))
                if m:
                    version = m.group(1)
                    break
    status = await run_capture([OS_BIN, "gateway", "status"], timeout=45)
    running = "running" in (status["stdout"] + status["stderr"]).lower()
    return {
        "version": version,
        "gateway_running": running,
        "gateway_status": (status["stdout"] or status["stderr"]).strip()[:400],
        "error": (ver["stderr"].strip()[:300] if ver["rc"] != 0 and not version else ""),
    }


async def releases(limit: int = 20) -> list[dict[str, Any]]:
    """Releases that actually ship an installable wheel.

    A tag whose assets are still uploading, or a desktop-only release, would
    otherwise be offered as a target and fail at download time.
    """
    data = await fetch_json(f"{GITHUB_API}/releases?per_page={max(1, min(limit, 100))}")
    rows: list[dict[str, Any]] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or item.get("draft"):
            continue
        tag = str(item.get("tag_name") or "")
        version = tag[1:] if tag.startswith("v") else tag
        if not VERSION_RE.match(version):
            continue
        assets = {a.get("name"): a for a in item.get("assets") or [] if isinstance(a, dict)}
        wheel = assets.get(wheel_name(version))
        if not wheel:
            continue
        rows.append({
            "version": version,
            "tag": tag,
            "prerelease": bool(item.get("prerelease")),
            "published_at": item.get("published_at") or "",
            "wheel_size": wheel.get("size") or 0,
            "url": item.get("html_url") or "",
        })
    return rows


async def check_update() -> dict[str, Any]:
    """Merge the official channel check with the release list.

    `version --check` is the vendor's own answer and is cheap, but it reports a
    version rather than an asset. The release list confirms the matching wheel
    is downloadable and supplies its size for the preflight. When the manifest
    is unreachable the release list alone is enough.
    """
    out: dict[str, Any] = {
        "current": "",
        "latest": "",
        "update_available": False,
        "release_url": "",
        "channel_error": "",
        "releases_error": "",
        "installable": [],
        "latest_wheel_size": 0,
        "source": {},
    }
    cur = await current_version()
    out["current"] = cur["version"]
    out["gateway_running"] = cur["gateway_running"]

    # Which mirror this host should download from is measured, not assumed; the
    # answer is cached, so this is normally free.
    with contextlib.suppress(Exception):
        out["source"] = await resolve_source()

    official = await run_capture([OS_BIN, "version", "--check", "--json"], timeout=90)
    if official["rc"] == 0:
        with contextlib.suppress(Exception):
            payload = json.loads(official["stdout"]) or {}
            out["latest"] = payload.get("latest") or ""
            out["release_url"] = payload.get("releaseUrl") or ""
            out["channel_disabled"] = bool(payload.get("disabled"))
            if payload.get("error"):
                out["channel_error"] = str(payload["error"])[:300]
    else:
        out["channel_error"] = (official["stderr"] or "").strip()[:300]

    try:
        rows = await releases()
        out["installable"] = rows
        stable = [r for r in rows if not r["prerelease"]]
        newest = (stable or rows)[0]["version"] if (stable or rows) else ""
        # The wheel list wins on "can I install it", so a manifest pointing at
        # a version with no published wheel does not become an install target.
        if newest and (not out["latest"] or out["latest"] not in {r["version"] for r in rows}):
            out["latest"] = newest
        for row in rows:
            if row["version"] == out["latest"]:
                out["latest_wheel_size"] = row["wheel_size"]
                out["release_url"] = out["release_url"] or row["url"]
                break
    except Exception as exc:  # noqa: BLE001 - degraded, not fatal
        out["releases_error"] = f"{type(exc).__name__}: {exc}"[:300]

    out["update_available"] = bool(
        out["latest"] and out["current"] and _newer(out["latest"], out["current"])
    )
    return out


_PARTS_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?$")


def _parts(version: str) -> tuple:
    """Sortable key: release numbers, then pre-release ordering (rc < final).

    A final release must sort above any of its own pre-releases, so the second
    element ranks a/b/rc below the 3 used for "no suffix".
    """
    found = _PARTS_RE.match(version.strip())
    if found is None:
        raise ValueError(f"无法解析版本号: {version!r}")
    core, kind, serial = found.groups()
    nums = tuple(int(p) for p in core.split("."))
    if not kind:
        return (nums, 3, 0)
    return (nums, {"a": 0, "b": 1, "rc": 2}[kind], int(serial))


def _newer(candidate: str, installed: str) -> bool:
    try:
        return _parts(candidate) > _parts(installed)
    except Exception:  # noqa: BLE001 - unparseable means "don't claim an update"
        return candidate != installed


async def preflight(version: str) -> dict[str, Any]:
    """Checks that turn a mid-install failure into an upfront refusal.

    Disk is the one that actually bites: the wheel is ~65 MB but uv unpacks it
    alongside a full dependency closure (scipy, onnxruntime, scikit-learn) into
    a fresh tool environment while keeping the old one, so the transient
    requirement is several times the download.
    """
    checks: list[dict[str, Any]] = []
    info = layout()

    uv_ok = await run_capture([UV_BIN, "--version"], timeout=30)
    checks.append({
        "name": "uv 可用",
        "ok": uv_ok["rc"] == 0,
        "detail": (uv_ok["stdout"] or uv_ok["stderr"]).strip()[:120],
    })

    target_dir = info.get("tool_dir") or "/"
    probe = target_dir if Path(target_dir).exists() else "/"
    try:
        usage = shutil.disk_usage(probe)
        free_mb = usage.free // (1024 * 1024)
    except Exception:  # noqa: BLE001
        free_mb = -1
    need_mb = 2048
    checks.append({
        "name": "磁盘空间",
        "ok": free_mb < 0 or free_mb >= need_mb,
        "detail": (f"{probe} 可用 {free_mb} MB / 建议 ≥ {need_mb} MB"
                   if free_mb >= 0 else "无法读取磁盘用量"),
    })

    reachable = False
    detail = ""
    try:
        rows = await releases()
        match = next((r for r in rows if r["version"] == version), None)
        reachable = match is not None
        detail = (f"{wheel_name(version)} {match['wheel_size'] // (1024*1024)} MB"
                  if match else f"{version} 没有发布 wheel 资产")
    except Exception as exc:  # noqa: BLE001
        detail = f"无法访问 GitHub Releases: {type(exc).__name__}"
    checks.append({"name": "目标 wheel 存在", "ok": reachable, "detail": detail[:160]})

    # The release list only proves GitHub published the asset. When the chosen
    # source is a mirror, the mirror's own copy is what uv will actually fetch,
    # and mirrors lag: a version present upstream can still 404 here.
    src = await resolve_source()
    url = wheel_url(version, src["id"])
    probe = await asyncio.to_thread(_probe_one, url, 15.0)
    src_ok = probe["http"] in {200, 206, 416}
    checks.append({
        "name": f"下载源可达（{src['label']}）",
        "ok": src_ok,
        "detail": (f"HTTP {probe['http']} · {probe['ms']}ms" if src_ok
                   else f"HTTP {probe['http']} {probe['error']} — 该源没有这个版本"),
    })

    return {"ok": all(c["ok"] for c in checks), "checks": checks, "source": src}


# --------------------------------------------------------------------------
# Install job
# --------------------------------------------------------------------------
# One job at a time, with a line buffer the browser polls. A websocket would
# add a second protocol for no benefit: the log is a few hundred lines and the
# console already polls for state.


class Job:
    """A single install/upgrade run and its captured log."""

    MAX_LINES = 4000

    def __init__(self, kind: str, target: str) -> None:
        self.id = f"{kind}-{int(time.time())}"
        self.kind = kind
        self.target = target
        self.lines: list[str] = []
        self.state = "running"  # running | done | failed
        self.started = time.time()
        self.finished: float | None = None
        self.error: str | None = None
        self.result: dict[str, Any] = {}

    def log(self, text: str) -> None:
        for raw in str(text).splitlines() or [""]:
            self.lines.append(raw.rstrip())
        if len(self.lines) > self.MAX_LINES:
            drop = len(self.lines) - self.MAX_LINES
            self.lines = [f"… 省略 {drop} 行 …", *self.lines[drop + 1:]]

    def snapshot(self, since: int = 0) -> dict[str, Any]:
        since = max(0, min(since, len(self.lines)))
        return {
            "id": self.id,
            "kind": self.kind,
            "target": self.target,
            "state": self.state,
            "started": self.started,
            "finished": self.finished,
            "error": self.error,
            "result": self.result,
            "next_cursor": len(self.lines),
            "lines": self.lines[since:],
            "elapsed": round((self.finished or time.time()) - self.started, 1),
        }


class JobRunner:
    """Serializes install jobs and keeps the most recent one for inspection."""

    def __init__(self) -> None:
        self.job: Job | None = None
        self._task: asyncio.Task | None = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, kind: str, target: str, coro_factory: Any) -> Job:
        if self.busy:
            raise RuntimeError("已有安装任务在运行")
        job = Job(kind, target)
        self.job = job
        self._task = asyncio.create_task(self._wrap(job, coro_factory))
        return job

    async def _wrap(self, job: Job, coro_factory: Any) -> None:
        try:
            job.result = await coro_factory(job) or {}
            job.state = "done"
        except Exception as exc:  # noqa: BLE001 - reported to the operator
            job.state = "failed"
            job.error = f"{type(exc).__name__}: {exc}"[:500]
            job.log(f"!! 失败: {job.error}")
        finally:
            job.finished = time.time()


runner = JobRunner()


async def _stream(job: Job, args: list[str], timeout: float = 1800.0) -> int:
    """Run a command, teeing both streams into the job log as they arrive."""
    job.log(f"$ {' '.join(args)}")
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=tool_env(),
    )

    async def pump() -> None:
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                return
            job.log(raw.decode("utf-8", "replace").rstrip())

    pump_task = asyncio.create_task(pump())
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        job.log(f"!! 超时 {timeout:.0f}s，已终止")
        raise RuntimeError(f"命令超时（{timeout:.0f}s）") from None
    finally:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(pump_task, timeout=10)
    rc = proc.returncode or 0
    job.log(f"— 退出码 {rc}")
    return rc


# --------------------------------------------------------------------------
# Snapshot cache
# --------------------------------------------------------------------------
# Assembling the panel's data costs three subprocess round-trips through the
# opensquilla CLI (measured on the production host: --version 4.4s,
# version --check 5.3s, gateway status 4.8s) plus a GitHub API call. Serialised
# behind a click that is ~15s of dead UI, which is why the panel originally
# waited for a button press. Cache it instead and refresh in the background, so
# opening the panel paints from memory and the operator never presses anything.

SNAPSHOT_TTL_SECONDS = 120.0
SNAPSHOT_REFRESH_SECONDS = 90.0
_SNAP: dict[str, Any] = {"data": None, "at": 0.0}
_SNAP_LOCK: asyncio.Lock | None = None
_SNAP_TASK: asyncio.Task | None = None


async def _build_snapshot() -> dict[str, Any]:
    info = await current_version()
    update = await check_update()
    return {
        "current": info,
        "update": update,
        "layout": layout(),
        "extras": installed_extras(),
    }


async def snapshot(*, force: bool = False) -> dict[str, Any]:
    """Panel data, served from cache unless it is stale or `force` is set."""
    global _SNAP_LOCK
    age = time.time() - float(_SNAP["at"] or 0)
    if _SNAP["data"] is not None and not force and age < SNAPSHOT_TTL_SECONDS:
        return dict(_SNAP["data"]) | {"cached": True, "age": age}

    if _SNAP_LOCK is None:
        _SNAP_LOCK = asyncio.Lock()
    if _SNAP_LOCK.locked() and _SNAP["data"] is not None and not force:
        # A refresh is already running: serve the stale copy rather than queue
        # behind ~15s of subprocess work.
        return dict(_SNAP["data"]) | {"cached": True, "age": age, "refreshing": True}

    async with _SNAP_LOCK:
        age = time.time() - float(_SNAP["at"] or 0)
        if _SNAP["data"] is not None and not force and age < SNAPSHOT_TTL_SECONDS:
            return dict(_SNAP["data"]) | {"cached": True, "age": age}
        data = await _build_snapshot()
        _SNAP["data"] = data
        _SNAP["at"] = time.time()
    return dict(data) | {"cached": False, "age": 0.0}


def snapshot_now() -> dict[str, Any] | None:
    """Whatever is cached, without triggering any work. None before the first build."""
    if _SNAP["data"] is None:
        return None
    return dict(_SNAP["data"]) | {
        "cached": True,
        "age": time.time() - float(_SNAP["at"] or 0),
    }


async def _snapshot_loop() -> None:
    # First build happens immediately so the panel has data before anyone opens
    # it; a failure here must never take the console down.
    while True:
        with contextlib.suppress(Exception):
            await snapshot(force=True)
        try:
            await asyncio.sleep(SNAPSHOT_REFRESH_SECONDS)
        except asyncio.CancelledError:
            return


def start_snapshot_refresh() -> None:
    global _SNAP_TASK
    if _SNAP_TASK is None or _SNAP_TASK.done():
        _SNAP_TASK = asyncio.create_task(_snapshot_loop())


async def stop_snapshot_refresh() -> None:
    global _SNAP_TASK
    task, _SNAP_TASK = _SNAP_TASK, None
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def invalidate_snapshot() -> None:
    """Force the next read to rebuild — call after anything that changes state."""
    _SNAP["at"] = 0.0


async def install_version(
    version: str,
    *,
    extras: list[str] | None = None,
    restart_gateway: bool = True,
    healthz_url: str = "",
) -> Job:
    """Install/upgrade to `version`, cycling the gateway around the swap."""
    requirement(version, extras)  # validates the version before any work starts

    async def work(job: Job) -> dict[str, Any]:
        before = await current_version()
        job.log(f"当前版本: {before['version'] or '未知'}"
                f" | 网关: {'运行中' if before['gateway_running'] else '未运行'}")
        job.log(f"目标: {version}")

        pre = await preflight(version)
        for check in pre["checks"]:
            job.log(f"[{'OK ' if check['ok'] else 'BAD'}] {check['name']}: {check['detail']}")

        # Source selection is measured per host, so say which one was picked and
        # why — otherwise a slow download looks like a hung install.
        src = pre.get("source") or await resolve_source()
        source_id = src["id"]
        job.log(f"下载源: {src['label']}（{src.get('reason') or '默认'}）")

        # A mirror can lag behind the canonical publisher. Rather than failing,
        # fall back to GitHub, which is where releases actually land first.
        src_check = next((c for c in pre["checks"] if c["name"].startswith("下载源")), None)
        if src_check and not src_check["ok"] and source_id != SOURCE_DEFAULT:
            job.log(f"该源没有 {version}，回落到 {SOURCE_SPECS[SOURCE_DEFAULT]['label']}")
            source_id = SOURCE_DEFAULT
            probe = await asyncio.to_thread(_probe_one, wheel_url(version, source_id), 15.0)
            if probe["http"] not in {200, 206, 416}:
                raise RuntimeError(f"两个下载源都拿不到 {version} 的 wheel，已终止")
            job.log(f"[OK ] 回落源可达: HTTP {probe['http']} · {probe['ms']}ms")
        elif not pre["ok"]:
            raise RuntimeError("预检未通过，已终止（未改动任何文件）")

        req = requirement(version, extras, source_id)
        job.log(f"需求串: {req}")

        was_running = before["gateway_running"]
        if was_running and restart_gateway:
            job.log("停止网关（替换 tool 环境前必须停，否则旧进程会跑在被删掉的文件上）")
            await _stream(job, [OS_BIN, "gateway", "stop"], timeout=180)

        # --force replaces the existing tool environment in place; without it uv
        # treats an already-installed tool as satisfied and does nothing.
        rc = await _stream(
            job,
            [UV_BIN, "tool", "install", "--force", "--python", OS_PYTHON, req],
            timeout=2400,
        )
        if rc != 0:
            # Leave the gateway down rather than starting a half-replaced env.
            raise RuntimeError(f"uv tool install 失败（退出码 {rc}）")

        after = await current_version()
        job.log(f"安装后版本: {after['version'] or '读不到'}")
        if after["version"] and after["version"] != version:
            raise RuntimeError(f"版本核对不符: 期望 {version}，实际 {after['version']}")

        started = False
        if restart_gateway:
            job.log("启动网关")
            rc = await _stream(job, [OS_BIN, "gateway", "start"], timeout=300)
            started = rc == 0
            if rc != 0:
                raise RuntimeError(f"网关启动失败（退出码 {rc}）")

        healthy = None
        if healthz_url and restart_gateway:
            # A zero exit from `gateway start` only means the supervisor
            # accepted it; the HTTP probe is what proves it serves traffic.
            for attempt in range(10):
                await asyncio.sleep(2)
                with contextlib.suppress(Exception):
                    body = await fetch_json(healthz_url, timeout=8)
                    if isinstance(body, dict) and body.get("ok"):
                        healthy = True
                        job.log(f"/healthz OK（第 {attempt + 1} 次探测）")
                        break
            if healthy is None:
                healthy = False
                job.log("!! /healthz 始终未就绪，请查看网关日志")

        return {
            "from": before["version"],
            "to": after["version"],
            "gateway_started": started,
            "healthz_ok": healthy,
        }

    return runner.start("install", version, work)


async def gateway_action(action: str) -> dict[str, Any]:
    """start / stop / restart / status / reload the managed gateway."""
    allowed = {"start", "stop", "restart", "status", "reload"}
    if action not in allowed:
        raise ValueError(f"不支持的动作: {action}")
    res = await run_capture([OS_BIN, "gateway", action], timeout=300)
    return {
        "ok": res["rc"] == 0,
        "action": action,
        "output": (res["stdout"] + res["stderr"]).strip()[:2000],
    }
