"""Admin authentication: scrypt password hash, HMAC-signed session cookies,
login rate limiting and a custom-header CSRF guard."""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

import yaml

COOKIE = "phos_session"
CSRF_HEADER = "x-phos"
SESSION_TTL = 12 * 3600
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(pw: str, salt: bytes | None = None) -> str:
    if len(pw) < 8:
        raise ValueError("password must have at least 8 characters")
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(h)}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, h = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(pw.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, _unb64(h))
    except (ValueError, TypeError):
        return False


class Sessions:
    def __init__(self, secret: str) -> None:
        # Without a configured secret, sessions survive only until restart (random key).
        self.key = hashlib.sha256((secret or secrets.token_hex(32)).encode() + b"|phos-session").digest()
        self.revoked: dict[str, float] = {}

    def issue(self, user: str = "admin") -> str:
        body = _b64(json.dumps({"u": user, "exp": int(time.time()) + SESSION_TTL,
                                "n": secrets.token_hex(8)}).encode())
        sig = _b64(hmac.new(self.key, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def verify(self, token: str | None) -> dict | None:
        if not token or "." not in token:
            return None
        body, sig = token.rsplit(".", 1)
        expect = _b64(hmac.new(self.key, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expect):
            return None
        try:
            data = json.loads(_unb64(body))
        except ValueError:
            return None
        if data.get("exp", 0) < time.time() or data.get("n") in self.revoked:
            return None
        return data

    def revoke(self, token: str | None) -> None:
        d = self.verify(token)
        if d:
            self.revoked[d["n"]] = d["exp"]
        now = time.time()
        self.revoked = {k: v for k, v in self.revoked.items() if v > now}


class RateLimiter:
    def __init__(self, attempts: int = 5, window: float = 60.0) -> None:
        self.attempts, self.window = attempts, window
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.attempts:
            return False
        q.append(now)
        return True


def update_secrets(path: Path, **values: str) -> None:
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
    data.update(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def passwd_main() -> None:
    """`phosphor-passwd`: set the admin password (writes the hash to secrets.yaml)."""
    from backend.config import ETC_DIR
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ETC_DIR / "secrets.yaml"
    pw = os.environ.get("PHOSPHOR_ADMIN_PASSWORD") or getpass.getpass("new admin password: ")
    if not os.environ.get("PHOSPHOR_ADMIN_PASSWORD") and pw != getpass.getpass("repeat: "):
        sys.exit("passwords differ")
    try:
        h = hash_password(pw)
    except ValueError as exc:
        sys.exit(str(exc))
    update_secrets(path, admin_password_hash=h)
    print(f"admin password updated in {path}")
