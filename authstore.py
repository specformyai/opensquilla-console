"""Operator credentials that the operator can actually change.

The console used to read its password straight from
``SQUILLA_CONSOLE_PASSWORD`` in a systemd drop-in. That works for a single
hand-configured host but is wrong for something people install themselves:

* the password lives in the unit file, so changing it means editing root-owned
  config and restarting the service — nothing an operator can do from the UI;
* whatever the install instructions suggest as a first password stays valid
  forever, and shared setup guides mean shared passwords.

So credentials move to ``data/auth.json`` (0600) holding a salted PBKDF2 hash,
and the bootstrap credential is explicitly marked ``must_change``. Until the
operator replaces it, every route except the change-password flow is refused —
a fresh install cannot be left sitting on its setup password.

The env var is still honoured, but only to *seed* the store on first boot; it is
never the live secret afterwards.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import string
import time
from pathlib import Path
from typing import Any

# PBKDF2 rather than a bare sha256: the stored value is a password verifier, so
# it should be expensive to attack offline if data/auth.json ever leaks.
KDF_ITERATIONS = 210_000
KDF_ALGO = "sha256"

MIN_PASSWORD_LEN = 12
MAX_PASSWORD_LEN = 200
MIN_USER_LEN = 3
MAX_USER_LEN = 64

USER_RE = re.compile(r"^[A-Za-z0-9._@-]+$")

# Substrings that make a password guessable for *this* application regardless of
# how many character classes it mixes.
_BANNED_FRAGMENTS = (
    "squilla", "console", "password", "passwd", "admin", "root", "operator",
    "123456", "qwerty", "letmein", "changeme", "welcome", "iloveyou",
)


class AuthError(Exception):
    """Rejected credential change; ``message`` is safe to show the operator."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _now() -> int:
    return int(time.time())


def _hash(password: str, salt: bytes, iterations: int = KDF_ITERATIONS) -> str:
    dk = hashlib.pbkdf2_hmac(KDF_ALGO, password.encode("utf-8"), salt, iterations)
    return dk.hex()


def generate_password(length: int = 20) -> str:
    """A bootstrap password strong enough that leaving it set is not a breach.

    It still gets flagged ``must_change`` — the point is that the window between
    install and first login is not a soft spot.
    """
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        # Rejection-sample instead of forcing characters at fixed positions,
        # which would leak structure.
        if (any(c.islower() for c in candidate)
                and any(c.isupper() for c in candidate)
                and any(c.isdigit() for c in candidate)
                and any(not c.isalnum() for c in candidate)):
            return candidate


def validate_password(password: str, *, user: str = "") -> None:
    """Reject weak passwords at the point of change, not at the point of breach."""
    pw = password or ""
    if len(pw) < MIN_PASSWORD_LEN:
        raise AuthError(f"密码至少 {MIN_PASSWORD_LEN} 位")
    if len(pw) > MAX_PASSWORD_LEN:
        raise AuthError(f"密码最长 {MAX_PASSWORD_LEN} 位")
    if pw.strip() != pw:
        raise AuthError("密码首尾不能有空格")
    classes = sum([
        any(c.islower() for c in pw),
        any(c.isupper() for c in pw),
        any(c.isdigit() for c in pw),
        any(not c.isalnum() for c in pw),
    ])
    if classes < 3:
        raise AuthError("密码需包含小写、大写、数字、符号中的至少三类")
    low = pw.lower()
    for frag in _BANNED_FRAGMENTS:
        if frag in low:
            raise AuthError(f"密码不能包含常见词「{frag}」")
    if user and user.lower() in low:
        raise AuthError("密码不能包含账号名")
    if len(set(pw)) < 6:
        raise AuthError("密码字符种类太少")
    # "abcdefgh" / "aaaa1111" style runs pass the class check but are trivial.
    if re.search(r"(.)\1{3,}", pw):
        raise AuthError("密码不能有 4 个以上连续重复字符")
    return None


def validate_user(user: str) -> str:
    u = (user or "").strip()
    if len(u) < MIN_USER_LEN:
        raise AuthError(f"账号至少 {MIN_USER_LEN} 位")
    if len(u) > MAX_USER_LEN:
        raise AuthError(f"账号最长 {MAX_USER_LEN} 位")
    if not USER_RE.match(u):
        raise AuthError("账号只能用字母、数字和 . _ @ - ")
    return u


class AuthStore:
    """File-backed operator credential with a forced-rotation flag."""

    def __init__(self, path: Path, *, env_password: str = "", env_user: str = "operator") -> None:
        self.path = Path(path)
        self._env_password = env_password or ""
        self._env_user = (env_user or "operator").strip() or "operator"
        self._data: dict[str, Any] = {}
        self.bootstrap_password: str | None = None
        self._load_or_seed()

    # -- persistence -------------------------------------------------------
    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        os.chmod(self.path, 0o600)
        self._data = data

    def _load_or_seed(self) -> None:
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text("utf-8"))
                if isinstance(loaded, dict) and loaded.get("password_hash"):
                    self._data = loaded
                    self._data.setdefault("user", self._env_user)
                    self._data.setdefault("must_change", False)
                    self._data.setdefault("iterations", KDF_ITERATIONS)
                    return
            except Exception:  # noqa: BLE001 - corrupt store: reseed below
                pass
        self._seed()

    def _seed(self) -> None:
        """First boot: adopt the env password if given, else mint one.

        Either way ``must_change`` is set. An env-supplied password is assumed
        to have been copy-pasted from setup notes and shared, so it is treated
        as a bootstrap value, not a chosen one.
        """
        if self._env_password:
            password = self._env_password
            source = "env"
            self.bootstrap_password = None
        else:
            password = generate_password()
            source = "generated"
            # Surfaced once so an unattended install is still reachable.
            self.bootstrap_password = password
        salt = secrets.token_bytes(16)
        self._write({
            "version": 1,
            "user": self._env_user,
            "salt": salt.hex(),
            "iterations": KDF_ITERATIONS,
            "password_hash": _hash(password, salt),
            "must_change": True,
            "bootstrap_source": source,
            "created_at": _now(),
            "updated_at": _now(),
            "rotations": 0,
        })
        if self.bootstrap_password:
            hint = self.path.parent / "bootstrap-password.txt"
            try:
                hint.write_text(
                    "初始密码（登录后会强制要求修改，改完可删除本文件）:\n"
                    f"{password}\n",
                    "utf-8",
                )
                os.chmod(hint, 0o600)
            except Exception:  # noqa: BLE001 - the console still starts
                pass

    # -- reads -------------------------------------------------------------
    @property
    def user(self) -> str:
        return self._data.get("user") or self._env_user

    @property
    def must_change(self) -> bool:
        return bool(self._data.get("must_change"))

    def public(self) -> dict[str, Any]:
        return {
            "user": self.user,
            "must_change": self.must_change,
            "bootstrap_source": self._data.get("bootstrap_source"),
            "updated_at": self._data.get("updated_at"),
            "rotations": self._data.get("rotations", 0),
        }

    def verify(self, password: str) -> bool:
        salt = bytes.fromhex(self._data.get("salt") or "")
        iterations = int(self._data.get("iterations") or KDF_ITERATIONS)
        expected = self._data.get("password_hash") or ""
        if not salt or not expected:
            return False
        return hmac.compare_digest(_hash(password or "", salt, iterations), expected)

    def session_secret(self) -> bytes:
        """Signing key bound to the stored hash.

        Deriving the key from the current verifier means a password change
        invalidates every outstanding cookie with no server-side session
        registry to purge.
        """
        material = f"squilla-console-session/{self._data.get('password_hash', '')}"
        return hashlib.sha256(material.encode()).digest()

    # -- writes ------------------------------------------------------------
    def change(self, *, current: str, new_password: str, new_user: str | None = None) -> dict[str, Any]:
        """Rotate the credential after re-checking the current password."""
        if not self.verify(current):
            raise AuthError("当前密码不正确")
        user = validate_user(new_user) if new_user else self.user
        validate_password(new_password, user=user)
        if self.verify(new_password):
            raise AuthError("新密码不能与当前密码相同")
        salt = secrets.token_bytes(16)
        data = dict(self._data)
        data.update({
            "user": user,
            "salt": salt.hex(),
            "iterations": KDF_ITERATIONS,
            "password_hash": _hash(new_password, salt),
            "must_change": False,
            "updated_at": _now(),
            "rotations": int(self._data.get("rotations") or 0) + 1,
        })
        self._write(data)
        # The setup hint is meaningless now and should not linger on disk.
        hint = self.path.parent / "bootstrap-password.txt"
        try:
            if hint.exists():
                hint.unlink()
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass
        self.bootstrap_password = None
        return self.public()
