"""SQUILLA CONSOLE — a thin operator console for a local OpenSquilla gateway.

The gateway speaks JSON-RPC over a single WebSocket (`/ws`) after a `connect`
handshake. This service owns exactly one upstream connection, multiplexes RPC
calls onto it, and fans `session.event.*` frames out to browser clients.

Everything it adds on top of the gateway is deliberately small:

* a local, file-backed credential book (many keys, each with a note),
* one-click activation of a stored key as the gateway's primary provider,
* model discovery + switching,
* a liveness probe that always asks the same question, so answers are
  comparable across keys and models.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
ICON_DIR = STATIC_DIR / "icon"
DATA_DIR = Path(os.environ.get("SQUILLA_CONSOLE_DATA", BASE_DIR / "data"))
KEYS_FILE = DATA_DIR / "keys.json"

GATEWAY_WS = os.environ.get("SQUILLA_GATEWAY_WS", "ws://127.0.0.1:18791/ws")
GATEWAY_HTTP = os.environ.get("SQUILLA_GATEWAY_HTTP", "http://127.0.0.1:18791")
GATEWAY_TOKEN = os.environ.get("SQUILLA_GATEWAY_TOKEN", "")

# The liveness question is fixed on purpose: identical prompt for every key and
# every model makes the answers directly comparable.
PROBE_QUESTION = "罗塞塔石碑是什么?"

SESSION_KEY = "agent:main:webchat:default"
RPC_TIMEOUT = 180.0
# The gateway drops a client that sends no text frame for
# `client_ws_keepalive_timeout` seconds (default 120). Ping at well under half
# that so a single lost frame cannot trip the deadline.
KEEPALIVE_INTERVAL = 45.0

# Last non-empty model catalogue, replayed when an upstream 503 empties a read.
_models_cache: dict[str, Any] = {}

# Provider ids the gateway will accept in `onboarding.provider.configure`.
# A stored credential whose `provider` is not in this set fails activation with
# `onboarding.provider.invalid "unknown provider: ..."` — which is exactly what
# happens when the field is a free-text box and someone types a label into it.
# The catalogue is static per gateway build, so one successful read is cached
# for the process lifetime and reused as validation input.
_providers_cache: dict[str, Any] = {}


async def provider_catalog(refresh: bool = False) -> list[dict[str, Any]]:
    """Provider ids/labels from `onboarding.catalog`, cached after first read."""
    if not refresh and _providers_cache.get("rows"):
        return list(_providers_cache["rows"])
    payload = await link.rpc("onboarding.catalog", None, timeout=60)
    raw = (payload or {}).get("providers") or []
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = item.get("providerId") or ""
        if not pid:
            continue
        rows.append({
            "id": pid,
            "label": item.get("label") or pid,
            "requires_base_url": bool(item.get("requiresBaseUrl")),
            "requires_api_key": bool(item.get("requiresApiKey")),
            "default_base_url": item.get("defaultBaseUrl") or "",
            "default_model": item.get("defaultModel") or item.get("defaultDirectModel") or "",
        })
    rows.sort(key=lambda r: r["label"].lower())
    if rows:
        _providers_cache["rows"] = rows
    return rows


async def validate_provider_id(provider: str | None) -> None:
    """Reject an unknown provider id at write time, not at activation time.

    Storing a bad id and only failing later on 「激活」 hides the cause behind an
    unrelated action. If the catalogue cannot be read (gateway down) the write
    is allowed through rather than blocked on an unavailable dependency.
    """
    pid = (provider or "").strip()
    if not pid:
        return
    try:
        rows = await provider_catalog()
    except Exception:  # noqa: BLE001 - gateway unreachable: do not block writes
        return
    if not rows:
        return
    known = {r["id"] for r in rows}
    if pid not in known:
        hint = ", ".join(sorted(known)[:8])
        raise HTTPException(
            400,
            f"提供方 ID「{pid}」网关不认识。请从下拉列表里选，例如: {hint} …",
        )

# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
# The console holds live API keys and can spend money, so it must not be
# reachable unauthenticated. The password is supplied out of band; only its
# hash is compared, and sessions live in memory so a restart logs everyone out.

AUTH_PASSWORD = os.environ.get("SQUILLA_CONSOLE_PASSWORD", "")
AUTH_USER = os.environ.get("SQUILLA_CONSOLE_USER", "operator")
SESSION_COOKIE = "squilla_session"
SESSION_TTL = 12 * 3600
# Brute-force damping: a wrong password costs an escalating delay per client.
LOGIN_LOCK_AFTER = 5
LOGIN_LOCK_SECONDS = 300

# token -> expiry epoch
_sessions: dict[str, float] = {}
# client ip -> [fail_count, locked_until_epoch]
_login_fails: dict[str, list[float]] = {}

# Paths reachable without a session; everything else requires one.
PUBLIC_PATHS = frozenset({"/login", "/api/login", "/api/auth", "/healthz"})

# Favicons are served from their own unauthenticated prefix rather than /static,
# which stays gated. The login page and the browser tab both need them before a
# session exists, and an app icon reveals nothing.
PUBLIC_PREFIXES = ("/icon/", "/favicon.ico", "/apple-touch-icon")


def _auth_enabled() -> bool:
    return bool(AUTH_PASSWORD)


def _issue_session() -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    # Opportunistically drop expired tokens so the dict cannot grow unbounded.
    for old, expiry in list(_sessions.items()):
        if expiry <= now:
            del _sessions[old]
    _sessions[token] = now + SESSION_TTL
    return token


def _session_valid(token: str | None) -> bool:
    if not token:
        return False
    expiry = _sessions.get(token)
    if expiry is None:
        return False
    if expiry <= time.time():
        del _sessions[token]
        return False
    return True


def _client_ip(request: Request) -> str:
    """Identify the caller for rate-limiting purposes.

    Behind the reverse proxy every request arrives from the proxy's own
    address, so keying the lockout on `request.client.host` would let one
    attacker lock out everybody. `X-Forwarded-For` is only trustworthy because
    this service binds to loopback and is unreachable except through the proxy
    that sets the header; the left-most entry is the original client.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real
    return request.client.host if request.client else "unknown"


def _password_ok(candidate: str) -> bool:
    # compare_digest on the hashes keeps the check constant-time regardless of
    # how the two strings differ in length.
    return secrets.compare_digest(
        hashlib.sha256(candidate.encode()).hexdigest(),
        hashlib.sha256(AUTH_PASSWORD.encode()).hexdigest(),
    )

DATA_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Credential book
# --------------------------------------------------------------------------


def _mask(secret: str) -> str:
    s = secret or ""
    if len(s) <= 10:
        return (s[:2] + "…") if s else ""
    return f"{s[:6]}…{s[-4:]}"


class KeyStore:
    """JSON-file credential book. Written 0600; never leaves this host."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {"keys": [], "active_id": None}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text("utf-8"))
            except Exception:
                self._data = {"keys": [], "active_id": None}
        self._data.setdefault("keys", [])
        self._data.setdefault("active_id", None)

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), "utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        os.chmod(self.path, 0o600)

    # -- reads ------------------------------------------------------------
    def raw(self, key_id: str) -> dict[str, Any] | None:
        return next((k for k in self._data["keys"] if k["id"] == key_id), None)

    @property
    def active_id(self) -> str | None:
        return self._data.get("active_id")

    def public(self) -> list[dict[str, Any]]:
        out = []
        for k in self._data["keys"]:
            item = {kk: vv for kk, vv in k.items() if kk != "api_key"}
            item["api_key_masked"] = _mask(k.get("api_key", ""))
            item["active"] = k["id"] == self._data.get("active_id")
            out.append(item)
        return out

    # -- writes -----------------------------------------------------------
    async def add(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            entry = {
                "id": uuid.uuid4().hex[:12],
                "note": payload.get("note") or "未命名凭据",
                "provider": payload.get("provider") or "custom",
                "base_url": (payload.get("base_url") or "").rstrip("/"),
                "api_key": payload.get("api_key") or "",
                "model": payload.get("model") or "",
                "created_at": int(time.time()),
                "last_test": None,
            }
            self._data["keys"].append(entry)
            if self._data.get("active_id") is None:
                self._data["active_id"] = entry["id"]
            self._flush()
            return entry

    async def update(self, key_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            entry = self.raw(key_id)
            if entry is None:
                raise KeyError(key_id)
            for field in ("note", "provider", "base_url", "model"):
                if payload.get(field) is not None:
                    entry[field] = payload[field]
            # An empty api_key means "keep the stored secret".
            if payload.get("api_key"):
                entry["api_key"] = payload["api_key"]
            entry["base_url"] = (entry.get("base_url") or "").rstrip("/")
            self._flush()
            return entry

    async def delete(self, key_id: str) -> None:
        async with self._lock:
            self._data["keys"] = [k for k in self._data["keys"] if k["id"] != key_id]
            if self._data.get("active_id") == key_id:
                self._data["active_id"] = (
                    self._data["keys"][0]["id"] if self._data["keys"] else None
                )
            self._flush()

    async def set_active(self, key_id: str) -> None:
        async with self._lock:
            if self.raw(key_id) is None:
                raise KeyError(key_id)
            self._data["active_id"] = key_id
            self._flush()

    async def record_test(self, key_id: str, result: dict[str, Any]) -> None:
        async with self._lock:
            entry = self.raw(key_id)
            if entry is not None:
                entry["last_test"] = result
                self._flush()

    async def record_model(self, key_id: str, model: str) -> None:
        async with self._lock:
            entry = self.raw(key_id)
            if entry is not None:
                entry["model"] = model
                self._flush()


store = KeyStore(KEYS_FILE)


# --------------------------------------------------------------------------
# Gateway link: one upstream WebSocket, many callers
# --------------------------------------------------------------------------


def _llm_from_effective(payload: Any) -> dict[str, Any]:
    """Pull provider/model/base_url out of a `config.effective` reply.

    The wire shape is provenance-wrapped and flat-dotted:
    ``{"fields": {"llm.model": {"value": ..., "source": ...}, ...}}`` — not a
    nested config object. Older/other shapes are tolerated as a fallback.
    """
    fields = (payload or {}).get("fields")
    if isinstance(fields, dict):
        def val(path: str) -> Any:
            rec = fields.get(path)
            return rec.get("value") if isinstance(rec, dict) else rec

        return {
            "provider": val("llm.provider"),
            "model": val("llm.model"),
            "base_url": val("llm.base_url"),
        }
    llm = ((payload or {}).get("config") or payload or {}).get("llm") or {}
    return {
        "provider": llm.get("provider"),
        "model": llm.get("model"),
        "base_url": llm.get("base_url"),
    }


def _provider_from_status(payload: Any) -> dict[str, Any]:
    """Best-effort provider/model out of the fast `providers.status` reply."""
    if not isinstance(payload, dict):
        return {}
    active = str(payload.get("activeProvider") or "")
    if not active:
        return {}
    snap: dict[str, Any] = {"provider": active}
    for row in payload.get("providers") or []:
        if isinstance(row, dict) and row.get("providerId") == active:
            if row.get("model"):
                snap["model"] = row["model"]
            break
    return snap


def _status_row(payload: Any, provider_id: str) -> dict[str, Any]:
    """The `providers.status` row for one provider id, or an empty dict."""
    if not isinstance(payload, dict):
        return {}
    for row in payload.get("providers") or []:
        if isinstance(row, dict) and row.get("providerId") == provider_id:
            return row
    return {}


class GatewayLink:
    """Single multiplexed WebSocket connection to the OpenSquilla gateway."""

    def __init__(self) -> None:
        self.ws: Any = None
        self.hello: dict[str, Any] | None = None
        self.connected = False
        self.last_error: str | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.browsers: set[WebSocket] = set()
        self._send_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop = False
        # Cached provider/model view, refreshed off the request path.
        self._provider: dict[str, Any] = {}
        self._provider_task: asyncio.Task | None = None

    # -- provider snapshot -------------------------------------------------
    def provider_snapshot(self) -> dict[str, Any]:
        return dict(self._provider)

    async def _pull_provider(self, *, status_timeout: float, cfg_timeout: float) -> None:
        """Refresh the cached provider view, publishing partial data as it lands.

        Order matters. `providers.status` answers from cached state in
        milliseconds and already carries the active provider id plus its model,
        so it runs first and is merged immediately — the UI gets a populated
        header without waiting. `config.effective` resolves the model catalog
        and has been measured at ~180s on a cold live-catalog fetch, so it runs
        second, with a timeout generous enough to actually succeed, and only
        enriches what is already there (notably `base_url`).
        """
        snap: dict[str, Any] = dict(self._provider)
        with contextlib.suppress(Exception):
            status = await self.rpc("providers.status", None, timeout=status_timeout)
            snap["providers"] = status
            snap.update(_provider_from_status(status))
            self._provider = dict(snap)
        with contextlib.suppress(Exception):
            cfg = await self.rpc("config.effective", None, timeout=cfg_timeout)
            snap.update({k: v for k, v in _llm_from_effective(cfg).items() if v})
            self._provider = dict(snap)

    async def refresh_provider_now(self) -> dict[str, Any]:
        """Force one provider/model read, e.g. right after a config write."""
        if not self.connected:
            return self.provider_snapshot()
        await self._pull_provider(status_timeout=25, cfg_timeout=25)
        return self.provider_snapshot()

    async def _refresh_provider(self) -> None:
        """Poll the provider/model view in the background.

        Never call these RPCs inline from a polled HTTP endpoint:
        `config.effective` can block for minutes while the model catalog is
        fetched, which would hang `/api/state` for its whole timeout.
        """
        while not self._stop:
            if not self.connected:
                # The link needs a few seconds to handshake at boot. Poll for it
                # instead of burning a full interval, or the console would show
                # an empty provider header for the first 20s of its life.
                await asyncio.sleep(1)
                continue
            await self._pull_provider(status_timeout=20, cfg_timeout=240)
            await asyncio.sleep(20)

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = False
            self._task = asyncio.create_task(self._supervise())
        if self._provider_task is None or self._provider_task.done():
            self._provider_task = asyncio.create_task(self._refresh_provider())

    async def stop(self) -> None:
        self._stop = True
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()
        for task in (self._task, self._provider_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _supervise(self) -> None:
        backoff = 1.0
        while not self._stop:
            try:
                await self._run_once()
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI
                self.last_error = f"{type(exc).__name__}: {exc}"
            self.connected = False
            self.hello = None
            self._fail_pending("gateway link dropped")
            await self._broadcast({"type": "gateway", "connected": False,
                                   "error": self.last_error})
            if self._stop:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.7, 15.0)

    async def _run_once(self) -> None:
        async with websockets.connect(
            GATEWAY_WS, max_size=26_214_400, ping_interval=20, ping_timeout=20
        ) as ws:
            self.ws = ws

            # The gateway drives the handshake: it emits `connect.challenge`
            # first, and only then accepts our `connect` request. Sending
            # eagerly and reading one frame would mis-read the challenge as the
            # hello response, so wait for it explicitly.
            challenge = await self._await_challenge(ws)

            auth = {"token": GATEWAY_TOKEN} if GATEWAY_TOKEN else {}
            handshake = {
                "type": "req",
                "id": "handshake",
                "method": "connect",
                "params": {
                    "minProtocol": 1,
                    "maxProtocol": 3,
                    "role": "operator",
                    "auth": auth,
                    "nonce": challenge.get("nonce"),
                    "client": {
                        "id": "squilla-console",
                        "display_name": "Squilla Console",
                        "version": "1.0.0",
                        "platform": "web",
                        "mode": "operator",
                    },
                    "caps": ["chat", "models", "onboarding"],
                },
            }
            await ws.send(json.dumps(handshake))

            hello = await self._await_hello(ws)
            self.hello = hello
            self.connected = True
            self.last_error = None

            await self._broadcast({
                "type": "gateway",
                "connected": True,
                "version": (hello.get("server") or {}).get("version"),
            })

            # Subscriptions and the app-level keepalive are ordinary RPCs whose
            # replies arrive as frames, so they can only complete once the read
            # loop below is consuming. Awaiting them here would deadlock until
            # each one's timeout expires (measured: 90s each, delaying the first
            # usable RPC by ~180s), so they run as tasks alongside the reader.
            helpers = [
                asyncio.create_task(self._subscribe()),
                asyncio.create_task(self._keepalive()),
            ]
            try:
                async for message in ws:
                    await self._on_frame(message)
            finally:
                for task in helpers:
                    task.cancel()
                for task in helpers:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

    async def _subscribe(self) -> None:
        """Register session subscriptions once the read loop is live."""
        with contextlib.suppress(Exception):
            await self.rpc("sessions.messages.subscribe", {"key": SESSION_KEY})
        with contextlib.suppress(Exception):
            await self.rpc("sessions.subscribe", None)

    async def _keepalive(self) -> None:
        """Send an application-level ping well inside the gateway's deadline.

        The gateway arms `client_ws_keepalive_timeout` (default 120s) on
        `receive_text()`, so it only counts JSON *text* frames as liveness. The
        websockets library's `ping_interval` sends protocol-level control
        frames, which that timer never sees — an idle console was being dropped
        with `gateway.client_ws_keepalive_timeout timeout_s=120.0` every two
        minutes. The gateway replies `{"type":"pong"}` to a `ping` frame.
        """
        while not self._stop:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if not self.connected or self.ws is None:
                return
            try:
                async with self._send_lock:
                    await self.ws.send(json.dumps({"type": "ping"}))
            except Exception:
                return

    # -- handshake ---------------------------------------------------------
    @staticmethod
    async def _await_challenge(ws: Any, timeout: float = 20.0) -> dict[str, Any]:
        """Read frames until the gateway's `connect.challenge` arrives."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("网关未在 20s 内下发 connect.challenge")
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            try:
                frame = json.loads(raw)
            except Exception:
                continue
            if frame.get("type") == "event" and frame.get("event") == "connect.challenge":
                return frame.get("payload") or {}
            if frame.get("type") == "res" and frame.get("error"):
                raise RuntimeError(f"握手被拒: {json.dumps(frame['error'], ensure_ascii=False)[:200]}")

    @staticmethod
    async def _await_hello(ws: Any, timeout: float = 25.0) -> dict[str, Any]:
        """Read frames until `hello-ok`, surfacing an auth rejection verbatim."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("网关未在 25s 内回 hello-ok")
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            try:
                frame = json.loads(raw)
            except Exception:
                continue
            if frame.get("type") == "hello-ok":
                return frame
            err = frame.get("error")
            if err:
                code = err.get("code") if isinstance(err, dict) else None
                msg = err.get("message") if isinstance(err, dict) else str(err)
                if code == "UNAUTHORIZED":
                    raise RuntimeError(
                        "网关鉴权失败：请在 SQUILLA_GATEWAY_TOKEN 里填入网关 token"
                    )
                raise RuntimeError(f"握手被拒 [{code}]: {msg}")

    # -- frames -----------------------------------------------------------
    async def _on_frame(self, raw: str | bytes) -> None:
        try:
            frame = json.loads(raw)
        except Exception:
            return
        kind = frame.get("type")
        if kind == "res":
            fut = self.pending.pop(str(frame.get("id")), None)
            if fut is not None and not fut.done():
                fut.set_result(frame)
        elif kind == "event":
            name = frame.get("event") or ""
            if name == "tick":
                return
            await self._broadcast({
                "type": "event",
                "event": name,
                "payload": frame.get("payload"),
            })
        elif kind == "ping":
            async with self._send_lock:
                with contextlib.suppress(Exception):
                    await self.ws.send(json.dumps({"type": "pong"}))

    def _fail_pending(self, reason: str) -> None:
        for fut in list(self.pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
        self.pending.clear()

    # -- rpc --------------------------------------------------------------
    async def rpc(self, method: str, params: Any = None, timeout: float = RPC_TIMEOUT) -> Any:
        if not self.connected or self.ws is None:
            raise HTTPException(503, f"网关未连接: {self.last_error or 'not connected'}")
        req_id = secrets.token_hex(8)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[req_id] = fut
        payload = {"type": "req", "id": req_id, "method": method, "params": params}
        async with self._send_lock:
            await self.ws.send(json.dumps(payload))
        try:
            frame = await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError as exc:
            self.pending.pop(req_id, None)
            raise HTTPException(504, f"{method} 超时") from exc
        if not frame.get("ok"):
            err = frame.get("error") or {}
            raise HTTPException(
                502, f"{method} 失败: {err.get('code', '')} {err.get('message', '')}".strip()
            )
        return frame.get("payload")

    # -- browser fanout ---------------------------------------------------
    async def _broadcast(self, message: dict[str, Any]) -> None:
        dead = []
        for sock in list(self.browsers):
            try:
                await sock.send_json(message)
            except Exception:
                dead.append(sock)
        for sock in dead:
            self.browsers.discard(sock)


link = GatewayLink()


# --------------------------------------------------------------------------
# Liveness probe (direct to the provider endpoint)
# --------------------------------------------------------------------------


def _is_anthropic_style(base_url: str, provider: str) -> bool:
    b = (base_url or "").lower()
    p = (provider or "").lower()
    return "anthropic" in b or b.rstrip("/").endswith("/anthropic") or p in {
        "anthropic",
        "custom_anthropic",
    }


async def probe_endpoint(base_url: str, api_key: str, model: str, provider: str) -> dict[str, Any]:
    """Ask PROBE_QUESTION straight at the provider endpoint and time it."""
    base = (base_url or "").rstrip("/")
    if not base:
        return {"ok": False, "error": "缺少 base_url", "at": int(time.time())}
    if not model:
        return {"ok": False, "error": "缺少模型名", "at": int(time.time())}

    anthropic = _is_anthropic_style(base, provider)
    if anthropic:
        url = f"{base}/v1/messages" if not base.endswith("/v1") else f"{base}/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": 300,
            "messages": [{"role": "user", "content": PROBE_QUESTION}],
        }
    else:
        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
        body = {
            "model": model,
            "max_tokens": 300,
            "messages": [{"role": "user", "content": PROBE_QUESTION}],
        }

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(url, headers=headers, json=body)
        latency = int((time.perf_counter() - started) * 1000)
        if resp.status_code >= 400:
            return {
                "ok": False,
                "status": resp.status_code,
                "latency_ms": latency,
                "error": resp.text[:400],
                "model": model,
                "question": PROBE_QUESTION,
                "at": int(time.time()),
            }
        data = resp.json()
        if anthropic:
            blocks = data.get("content") or []
            answer = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
            usage = data.get("usage") or {}
            tokens = (usage.get("input_tokens", 0) or 0) + (usage.get("output_tokens", 0) or 0)
        else:
            choices = data.get("choices") or []
            message = (choices[0].get("message") if choices else {}) or {}
            answer = message.get("content") or ""
            if isinstance(answer, list):  # some gateways return content parts
                answer = "".join(
                    part.get("text", "") for part in answer if isinstance(part, dict)
                )
            tokens = (data.get("usage") or {}).get("total_tokens", 0) or 0
        answer = (answer or "").strip()
        return {
            "ok": bool(answer),
            "status": resp.status_code,
            "latency_ms": latency,
            "answer": answer[:1200],
            "chars": len(answer),
            "tokens": tokens,
            "model": data.get("model") or model,
            "question": PROBE_QUESTION,
            "error": None if answer else "响应为空",
            "at": int(time.time()),
        }
    except Exception as exc:  # noqa: BLE001 - reported verbatim to the operator
        return {
            "ok": False,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
            "model": model,
            "question": PROBE_QUESTION,
            "at": int(time.time()),
        }


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------

app = FastAPI(title="Squilla Console", docs_url=None, redoc_url=None)


@app.middleware("http")
async def require_session(request: Request, call_next: Any) -> Any:
    """Gate every route behind a session cookie.

    Denying by default means a new endpoint is protected the moment it is
    added, instead of being forgotten in an allow-list. Static assets stay
    behind the gate too: the login page is self-contained, so nothing the
    unauthenticated visitor needs lives under /static.
    """
    path = request.url.path
    if not _auth_enabled() or path in PUBLIC_PATHS:
        return await call_next(request)
    if path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)

    if _session_valid(request.cookies.get(SESSION_COOKIE)):
        return await call_next(request)

    # XHR and asset requests get a status they can act on; a browser asking for
    # a page gets bounced to the form.
    if path.startswith("/api/") or path == "/ws":
        return JSONResponse({"detail": "未登录"}, status_code=401)
    if path.startswith("/static/"):
        return JSONResponse({"detail": "未登录"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


class LoginPayload(BaseModel):
    password: str = Field(min_length=1)


@app.get("/login")
async def login_page() -> Any:
    if not _auth_enabled():
        return RedirectResponse("/", status_code=302)
    return FileResponse(STATIC_DIR / "login.html", headers={"cache-control": "no-store"})


@app.get("/api/auth")
async def api_auth(request: Request) -> JSONResponse:
    """Let the front end tell 'no auth configured' from 'not logged in'."""
    return JSONResponse({
        "enabled": _auth_enabled(),
        "authenticated": (not _auth_enabled())
        or _session_valid(request.cookies.get(SESSION_COOKIE)),
        "user": AUTH_USER,
    })


@app.post("/api/login")
async def api_login(payload: LoginPayload, request: Request) -> JSONResponse:
    if not _auth_enabled():
        return JSONResponse({"ok": True, "note": "未启用登录"})

    client = _client_ip(request)
    state = _login_fails.get(client) or [0.0, 0.0]
    now = time.time()
    if state[1] > now:
        wait = int(state[1] - now)
        return JSONResponse(
            {"ok": False, "detail": f"尝试次数过多，请 {wait} 秒后再试"},
            status_code=429,
        )

    if not _password_ok(payload.password):
        state[0] += 1
        if state[0] >= LOGIN_LOCK_AFTER:
            state[1] = now + LOGIN_LOCK_SECONDS
            state[0] = 0.0
        _login_fails[client] = state
        # Blunt the timing signal and slow scripted guessing.
        await asyncio.sleep(1.0)
        return JSONResponse({"ok": False, "detail": "密码错误"}, status_code=401)

    _login_fails.pop(client, None)
    token = _issue_session()
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        # Set Secure only behind TLS; on plain http the cookie would be dropped.
        secure=request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto") == "https",
        path="/",
    )
    return response


@app.post("/api/logout")
async def api_logout(request: Request) -> JSONResponse:
    _sessions.pop(request.cookies.get(SESSION_COOKIE) or "", None)
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


class KeyPayload(BaseModel):
    note: str | None = None
    provider: str | None = "custom"
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class ChatPayload(BaseModel):
    message: str = Field(min_length=1)


class ModelPayload(BaseModel):
    model: str = Field(min_length=1)


@app.on_event("startup")
async def _startup() -> None:
    link.start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await link.stop()


@app.get("/api/state")
async def api_state() -> JSONResponse:
    gateway: dict[str, Any] = {
        "connected": link.connected,
        "ws": GATEWAY_WS,
        "error": link.last_error,
        "probe_question": PROBE_QUESTION,
    }
    if link.hello:
        server = link.hello.get("server") or {}
        snapshot = link.hello.get("snapshot") or {}
        gateway.update(
            version=server.get("version"),
            conn_id=server.get("conn_id"),
            auth_mode=snapshot.get("auth_mode"),
            config_path=snapshot.get("config_path"),
        )
    # Provider/model come from a background refresher, never inline: these two
    # RPCs can sit unanswered for their full timeout on a gateway with no
    # provider wired, and /api/state is polled on every render.
    gateway.update(link.provider_snapshot())
    return JSONResponse({
        "gateway": gateway,
        "keys": store.public(),
        "active_id": store.active_id,
    })


@app.get("/api/models")
async def api_models(refresh: int = 0) -> JSONResponse:
    """Model catalog for the gateway's active provider.

    Upstream `/models` endpoints flap: tokenrhythm.studio was observed
    answering 503 on three consecutive probes while the gateway's own live
    catalog still held 13 entries, so `models.list` alternates between a full
    list and an empty one with `provider_overloaded`. Blanking the catalogue on
    such a blip is worse than showing slightly stale data, so the last
    non-empty result is kept and replayed when a fresh read comes back empty.
    """
    payload = await link.rpc("models.list", None, timeout=60)
    models = (payload or {}).get("models") or []
    errors = (payload or {}).get("errors") or []

    if not models:
        # models.list only reports what the live selector knows. Fall back to
        # provider-side discovery using the active key's own credentials.
        active = store.raw(store.active_id) if store.active_id else None
        if active:
            with contextlib.suppress(Exception):
                discovered = await link.rpc(
                    "onboarding.models.discover",
                    {
                        "providerId": active["provider"],
                        "apiKey": active["api_key"],
                        "baseUrl": active["base_url"],
                    },
                    timeout=60,
                )
                for item in (discovered or {}).get("models") or []:
                    models.append({
                        "id": item.get("id") or item.get("model") or "",
                        "name": item.get("name") or item.get("id") or "",
                        "provider": active["provider"],
                        "source": "discover",
                    })

    if models:
        _models_cache["models"] = models
        return JSONResponse({"models": models, "errors": errors, "stale": False})

    cached = _models_cache.get("models") or []
    return JSONResponse({"models": cached, "errors": errors, "stale": bool(cached)})


@app.get("/api/models/endpoint")
async def api_models_endpoint(key_id: str) -> JSONResponse:
    """Pull /models straight from a stored key's endpoint (no gateway needed)."""
    entry = store.raw(key_id)
    if entry is None:
        raise HTTPException(404, "凭据不存在")
    base = (entry["base_url"] or "").rstrip("/")
    if not base:
        raise HTTPException(400, "该凭据没有 base_url")
    url = f"{base}/models"
    headers = {"Authorization": f"Bearer {entry['api_key']}"}
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"拉取失败: {type(exc).__name__}: {exc}") from exc
    rows = data.get("data") if isinstance(data, dict) else data
    models = [
        {
            "id": row.get("id", ""),
            "name": row.get("id", ""),
            "provider": row.get("owned_by") or entry["provider"],
            "source": "endpoint",
        }
        for row in (rows or [])
        if isinstance(row, dict) and row.get("id")
    ]
    models.sort(key=lambda m: (m["provider"], m["id"]))
    return JSONResponse({"models": models, "count": len(models)})


@app.post("/api/model/switch")
async def api_model_switch(payload: ModelPayload) -> JSONResponse:
    """Switch the gateway's primary model, keeping the stored credential."""
    result = await link.rpc(
        "onboarding.provider.configure",
        {
            "providerId": (await _active_provider_id()),
            "model": payload.model,
            "preserveApiKey": True,
            "routerAction": "preserve",
        },
        timeout=90,
    )
    if store.active_id:
        await store.record_model(store.active_id, payload.model)
    return JSONResponse({"ok": True, "model": payload.model, "result": result})


async def _active_provider_id() -> str:
    entry = store.raw(store.active_id) if store.active_id else None
    if entry:
        return entry["provider"]
    snap = link.provider_snapshot()
    if snap.get("provider"):
        return str(snap["provider"])
    return "custom"


@app.get("/api/providers")
async def api_providers(refresh: int = 0) -> JSONResponse:
    """Provider ids the gateway accepts, for the credential form's picker."""
    try:
        rows = await provider_catalog(refresh=bool(refresh))
    except Exception as exc:  # noqa: BLE001 - surface as empty, not a 500
        return JSONResponse({"providers": [], "error": f"{type(exc).__name__}: {exc}"})
    return JSONResponse({"providers": rows, "count": len(rows)})


@app.get("/api/keys")
async def api_keys() -> JSONResponse:
    return JSONResponse({"keys": store.public(), "active_id": store.active_id})


@app.post("/api/keys")
async def api_keys_add(payload: KeyPayload) -> JSONResponse:
    if not payload.api_key:
        raise HTTPException(400, "api_key 必填")
    await validate_provider_id(payload.provider)
    entry = await store.add(payload.model_dump())
    return JSONResponse({"ok": True, "id": entry["id"], "keys": store.public()})


@app.put("/api/keys/{key_id}")
async def api_keys_update(key_id: str, payload: KeyPayload) -> JSONResponse:
    await validate_provider_id(payload.provider)
    try:
        await store.update(key_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(404, "凭据不存在") from exc
    return JSONResponse({"ok": True, "keys": store.public()})


@app.delete("/api/keys/{key_id}")
async def api_keys_delete(key_id: str) -> JSONResponse:
    await store.delete(key_id)
    return JSONResponse({"ok": True, "keys": store.public()})


@app.post("/api/keys/{key_id}/activate")
async def api_keys_activate(key_id: str) -> JSONResponse:
    """Write a stored credential into the gateway as its primary provider."""
    entry = store.raw(key_id)
    if entry is None:
        raise HTTPException(404, "凭据不存在")
    params: dict[str, Any] = {
        "providerId": entry["provider"],
        "apiKey": entry["api_key"],
        "routerAction": "preserve",
    }
    if entry.get("base_url"):
        params["baseUrl"] = entry["base_url"]
    if entry.get("model"):
        params["model"] = entry["model"]
    result = await link.rpc("onboarding.provider.configure", params, timeout=90)

    # `onboarding.provider.configure` echoes the endpoint back in its `entry`
    # but does not always persist `llm.base_url`, and a provider that requires
    # one (`custom`, `vllm`, `azure`, ...) then fails every call with
    # "requires an explicit base_url". Confirm against providers.status — which
    # answers from cached state in milliseconds, unlike config.effective — and
    # pin the endpoint only when it really is missing.
    want = entry.get("base_url") or ""
    if want:
        row: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            status = await link.rpc("providers.status", None, timeout=25)
            row = _status_row(status, entry["provider"])
        if row and not row.get("baseUrlConfigured"):
            with contextlib.suppress(Exception):
                await link.rpc(
                    "config.set", {"path": "llm.base_url", "value": want}, timeout=60
                )
    await link.refresh_provider_now()
    await store.set_active(key_id)
    return JSONResponse({"ok": True, "active_id": key_id, "result": result})


@app.post("/api/keys/{key_id}/test")
async def api_keys_test(key_id: str) -> JSONResponse:
    entry = store.raw(key_id)
    if entry is None:
        raise HTTPException(404, "凭据不存在")
    result = await probe_endpoint(
        entry["base_url"], entry["api_key"], entry.get("model") or "", entry["provider"]
    )
    await store.record_test(key_id, result)
    return JSONResponse({"ok": True, "result": result, "keys": store.public()})


@app.post("/api/test/all")
async def api_test_all() -> JSONResponse:
    """Probe every stored credential concurrently with the same question."""
    entries = [store.raw(k["id"]) for k in store.public()]
    entries = [e for e in entries if e]
    results = await asyncio.gather(*[
        probe_endpoint(e["base_url"], e["api_key"], e.get("model") or "", e["provider"])
        for e in entries
    ])
    for entry, result in zip(entries, results, strict=True):
        await store.record_test(entry["id"], result)
    return JSONResponse({
        "ok": True,
        "results": {e["id"]: r for e, r in zip(entries, results, strict=True)},
        "keys": store.public(),
    })


@app.post("/api/chat")
async def api_chat(payload: ChatPayload) -> JSONResponse:
    result = await link.rpc(
        "chat.send", {"message": payload.message, "sessionKey": SESSION_KEY}, timeout=60
    )
    return JSONResponse({"ok": True, "result": result})


@app.post("/api/chat/abort")
async def api_chat_abort() -> JSONResponse:
    result = await link.rpc(
        "chat.abort", {"sessionKey": SESSION_KEY, "source": "webui_stop"}, timeout=30
    )
    return JSONResponse({"ok": True, "result": result})


@app.get("/api/chat/history")
async def api_chat_history(limit: int = 60) -> JSONResponse:
    payload = await link.rpc(
        "chat.history", {"sessionKey": SESSION_KEY, "limit": limit}, timeout=45
    )
    return JSONResponse(payload or {})


@app.post("/api/gateway/reconnect")
async def api_gateway_reconnect() -> JSONResponse:
    await link.stop()
    link.start()
    for _ in range(40):
        if link.connected:
            break
        await asyncio.sleep(0.25)
    return JSONResponse({"ok": link.connected, "error": link.last_error})


@app.websocket("/ws")
async def ws_bridge(sock: WebSocket) -> None:
    # An @app.middleware("http") hook never sees the websocket scope, so the
    # session check has to be repeated here or the event stream would be open
    # to anyone.
    if _auth_enabled() and not _session_valid(sock.cookies.get(SESSION_COOKIE)):
        await sock.close(code=1008)
        return
    await sock.accept()
    link.browsers.add(sock)
    await sock.send_json({
        "type": "gateway",
        "connected": link.connected,
        "error": link.last_error,
    })
    try:
        while True:
            await sock.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        link.browsers.discard(sock)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


class NoCacheStatic(StaticFiles):
    """Serve assets with revalidation forced.

    The console is a single ES module; a stale cached copy silently reverts
    behaviour while the file on disk is already fixed, which is impossible to
    diagnose from the UI. Correctness beats a few saved bytes on a local tool.
    """

    def is_not_modified(self, response_headers: Any, request_headers: Any) -> bool:
        return False

    async def get_response(self, path: str, scope: Any) -> Any:
        response = await super().get_response(path, scope)
        response.headers["cache-control"] = "no-store, must-revalidate"
        for stale in ("etag", "last-modified"):
            if stale in response.headers:
                del response.headers[stale]
        return response


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico() -> FileResponse:
    """Browsers request this path directly, ignoring the <link> tags."""
    return FileResponse(ICON_DIR / "favicon.ico", media_type="image/x-icon")


@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
async def apple_touch_icon() -> FileResponse:
    """iOS probes these two fixed paths when a page is added to the home screen."""
    return FileResponse(ICON_DIR / "icon-180.png", media_type="image/png")


# Icons are cacheable (unlike the app shell) and must stay outside the auth gate,
# so they get their own mount instead of living under /static.
app.mount("/icon", StaticFiles(directory=ICON_DIR), name="icon")
app.mount("/static", NoCacheStatic(directory=STATIC_DIR), name="static")

