"""End-to-end backend checks for the console's auth gate and model catalogue.

Run with the project venv:

    .venv/bin/python tests/test_backend.py

Everything runs against the real ASGI app with a throwaway data dir, so the
credential store is seeded exactly as it would be on a fresh install. The
gateway URL points at a closed port on purpose: the console must stay fully
usable (and the forced-rotation gate must still hold) with no gateway running.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

DATA = Path(tempfile.mkdtemp(prefix="squilla-test-"))
os.environ["SQUILLA_CONSOLE_DATA"] = str(DATA)
os.environ.pop("SQUILLA_CONSOLE_PASSWORD", None)  # force the generated-secret path
# Unreachable on purpose: nothing here may depend on a live gateway.
os.environ["SQUILLA_GATEWAY_WS"] = "ws://127.0.0.1:9/ws"
os.environ["SQUILLA_GATEWAY_HTTP"] = "http://127.0.0.1:9"

from starlette.testclient import TestClient  # noqa: E402

import app as console  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILURES.append(label)


def section(title: str) -> None:
    print(f"\n--- {title} ---")


NEW_PW = "Rosetta-Stele-42!x"


def main() -> int:
    client = TestClient(console.app)

    section("1. 全新安装：自动生成密码 + 强制改密 + 提示文件")
    check("must_change", console.auth.must_change, True)
    boot = console.auth.bootstrap_password
    check("bootstrap 密码已生成", bool(boot), True)
    # Everything below logs in with it, so stop here rather than trip over None
    # in a dozen confusing ways.
    assert boot, "未生成初始密码，后续断言无从进行"
    hint = DATA / "bootstrap-password.txt"
    check("提示文件已写", hint.exists(), True)
    check("auth.json 权限 600", oct(os.stat(DATA / "auth.json").st_mode & 0o777), "0o600")
    stored = json.loads((DATA / "auth.json").read_text())
    check("明文密码未落盘", boot not in json.dumps(stored), True)

    section("2. 未登录：一律拦下")
    r = client.get("/api/state")
    check("/api/state 状态码", r.status_code, 401)
    r = client.get("/", follow_redirects=False)
    check("/ 重定向到登录页", (r.status_code, r.headers.get("location")), (302, "/login"))
    check("/login 可达", client.get("/login").status_code, 200)
    check("/healthz 可达", client.get("/healthz").status_code, 200)
    check("图标免鉴权", client.get("/favicon.ico").status_code, 200)
    r = client.get("/static/app.css")
    check("/static 仍锁着", r.status_code, 401)

    section("3. 错密码：拒绝且不泄露信息")
    r = client.post("/api/login", json={"password": "wrong-password-xyz"})
    check("错密码状态码", r.status_code, 401)
    check("错密码未发 cookie", "squilla_session" not in r.cookies, True)

    section("4. 用初始密码登录：拿到会话但被标记必须改密")
    r = client.post("/api/login", json={"password": boot})
    check("登录成功", r.status_code, 200)
    check("登录响应带 must_change", r.json().get("must_change"), True)
    check("已下发会话 cookie", bool(client.cookies.get("squilla_session")), True)

    section("5. 关键闸门：未改密的会话不能碰任何业务接口")
    for path in ("/api/state", "/api/models", "/api/keys", "/api/opensquilla",
                 "/api/providers", "/api/usage", "/api/routing"):
        r = client.get(path)
        detail = r.json() if r.status_code == 403 else r.status_code
        blocked = r.status_code == 403 and bool((r.json() or {}).get("must_change"))
        check(f"GET {path} 被拦", blocked, True)
    r = client.post("/api/opensquilla/install", json={"version": "0.5.4"})
    check("POST 安装接口被拦", r.status_code, 403)
    r = client.post("/api/keys", json={"note": "x", "provider": "custom"})
    check("POST 新增凭据被拦", r.status_code, 403)

    section("6. 闸门放行的四个：改密所必需")
    check("/api/auth 放行", client.get("/api/auth").status_code, 200)
    check("/api/auth 报告 must_change", client.get("/api/auth").json().get("must_change"), True)
    check("/healthz 放行", client.get("/healthz").status_code, 200)
    r = client.get("/", follow_redirects=False)
    check("/ 放行（渲染改密界面）", r.status_code, 200)

    section("7. WS 事件流：未改密不给连")
    ws_rejected = False
    try:
        with client.websocket_connect("/ws"):
            pass
    except Exception:
        ws_rejected = True
    check("未改密时 /ws 被拒", ws_rejected, True)

    section("8. 改密校验：弱密码/错误当前密码一律拒绝")
    cases = [
        ("太短", {"current": boot, "new_password": "Ab1!x"}, 400),
        ("两类字符", {"current": boot, "new_password": "abcdefghijklmn"}, 400),
        ("含禁用词", {"current": boot, "new_password": "MyConsole-Pw-1!"}, 400),
        ("当前密码错", {"current": "nope-nope-nope", "new_password": NEW_PW}, 401),
    ]
    for label, body, want in cases:
        r = client.post("/api/password", json=body)
        check(f"{label} -> {r.json().get('detail', '')[:24]}", r.status_code, want)
    check("失败后仍未改密", console.auth.must_change, True)

    section("9. 正确改密：闸门解除 + 旧会话失效")
    old_cookie = client.cookies.get("squilla_session")
    r = client.post("/api/password", json={"current": boot, "new_password": NEW_PW})
    check("改密成功", r.status_code, 200)
    check("must_change 已清除", console.auth.must_change, False)
    check("rotations 递增", r.json()["auth_info"]["rotations"], 1)
    new_cookie = client.cookies.get("squilla_session")
    check("已换发新 cookie", new_cookie != old_cookie, True)
    check("旧 cookie 已失效", console._session_valid(old_cookie), False)
    check("新 cookie 有效", console._session_valid(new_cookie), True)
    check("提示文件已清理", hint.exists(), False)

    section("10. 改密后：业务接口恢复")
    r = client.get("/api/state")
    check("/api/state 可用", r.status_code, 200)
    check("/api/state 带 keys 字段", "keys" in r.json(), True)
    r = client.get("/api/models")
    check("/api/models 可用", r.status_code, 200)
    body = r.json()
    check("目录返回缓存结构", all(k in body for k in ("models", "count", "stale", "source")), True)
    check("无凭据无网关时目录为空而非报错", body["count"], 0)

    section("11. 旧密码彻底作废")
    client.cookies.clear()
    check("旧密码登录失败", client.post("/api/login", json={"password": boot}).status_code, 401)
    r = client.post("/api/login", json={"password": NEW_PW})
    check("新密码登录成功", r.status_code, 200)
    check("登录后不再要求改密", r.json().get("must_change"), False)

    section("12. 重启后状态保留（不会再次强制改密）")
    reloaded = console.AuthStore(DATA / "auth.json", env_password="", env_user="operator")
    check("重载 must_change", reloaded.must_change, False)
    check("重载后新密码可用", reloaded.verify(NEW_PW), True)
    check("重载后旧密码无效", reloaded.verify(boot), False)

    section("13. 安装接口的版本校验（防注入/防降级）")
    for bad in ("latest", "v0.5.4", "0.5.4; rm -rf /", "../../etc/passwd", ""):
        r = client.post("/api/opensquilla/install", json={"version": bad})
        check(f"拒绝非法版本 {bad!r}", r.status_code in (400, 422), True)

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项:")
        for f in FAILURES:
            print("  -", f)
    else:
        print("全部通过")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(DATA, ignore_errors=True)
    sys.exit(code)
