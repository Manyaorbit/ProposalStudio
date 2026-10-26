"""Login hashing/verification, brute-force throttling and a small rate limiter."""
import base64
import hashlib
import hmac
import os
import struct
import threading
import time
from collections import defaultdict, deque

import bcrypt

from config import cfg

# Used so a wrong username takes as long to reject as a wrong password.
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt(rounds=12))


def verify_login(username: str, password: str) -> bool:
    user_ok = hmac.compare_digest(username.encode(), cfg.user.encode())
    hashed = cfg.password_hash or _DUMMY_HASH
    try:
        pw_ok = bcrypt.checkpw(password.encode(), hashed)
    except ValueError:
        pw_ok = False
    return bool(user_ok and pw_ok and cfg.password_hash)


def _hotp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    num = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{num:06d}"


_totp_lock = threading.Lock()
_totp_last_counter = 0


def verify_totp(code: str) -> bool:
    """Standard 6-digit authenticator code (RFC 6238). Each code works once, with +-30s clock slack.
    When no TOTP_SECRET is configured, the check is skipped (local mode only)."""
    global _totp_last_counter
    if not cfg.totp_secret:
        return not cfg.production
    code = "".join(str(code).split())
    if len(code) != 6 or not code.isdigit():
        return False
    now = int(time.time() // 30)
    with _totp_lock:
        for counter in (now - 1, now, now + 1):
            if hmac.compare_digest(_hotp(cfg.totp_secret, counter), code) and counter > _totp_last_counter:
                _totp_last_counter = counter          # replaying a used code fails
                return True
    return False


def new_totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


class LoginThrottle:
    """After a few failures, lock out with exponential back-off (30s, 60s, 2m ... up to 15m)."""

    FREE_TRIES = 4
    BASE = 30
    CAP = 15 * 60

    def __init__(self):
        self._lock = threading.Lock()
        self._fails = defaultdict(int)
        self._until = {}

    def blocked_for(self, key) -> int:
        with self._lock:
            remaining = self._until.get(key, 0) - time.monotonic()
            return int(remaining) + 1 if remaining > 0 else 0

    def failure(self, key, free_tries=None):
        with self._lock:
            self._fails[key] += 1
            n = self._fails[key] - (self.FREE_TRIES if free_tries is None else free_tries)
            if n > 0:
                self._until[key] = time.monotonic() + min(self.CAP, self.BASE * 2 ** (n - 1))

    def success(self, key):
        with self._lock:
            self._fails.pop(key, None)
            self._until.pop(key, None)


class RateLimiter:
    """Sliding window: at most `limit` calls per `window` seconds."""

    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self._lock = threading.Lock()
        self._hits = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._hits and now - self._hits[0] > self.window:
                self._hits.popleft()
            if len(self._hits) >= self.limit:
                return False
            self._hits.append(now)
            return True


SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
}
