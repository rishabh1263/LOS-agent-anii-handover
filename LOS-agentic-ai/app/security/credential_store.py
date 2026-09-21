"""
Password hashing and login rate-limiting.

Uses hashlib.pbkdf2_hmac (Python stdlib) rather than bcrypt/argon2 so no new
dependency is needed beyond what's already in requirements.txt. PBKDF2-SHA256
with a high iteration count is still an accepted, NIST-recommended choice for
password storage.

Nothing here talks to a database. Swap in a real user table later by
replacing _verify_password's lookup in auth_api.py -- this module's hashing
and rate-limiting functions stay the same.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time

PBKDF2_ITERATIONS = 260_000
_SALT_BYTES = 16


def hash_password(plain_password: str) -> str:
    """
    Returns a string of the form "pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>".
    Store this in place of the plaintext password (e.g. as DUMMY_PASSWORD_HASH).
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(plain_password: str, stored_hash: str) -> bool:
    """
    Recomputes the hash with the stored salt/iteration count and compares in
    constant time. Returns False (never raises) on any malformed input so a
    bad config fails closed instead of throwing a 500.
    """
    try:
        algorithm, iterations_str, salt_hex, hash_hex = stored_hash.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False

    candidate = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


class LoginRateLimiter:
    """
    In-memory sliding lockout: after `max_attempts` failures for a given key
    (e.g. "username:ip"), further attempts are rejected for `lockout_seconds`.

    In-memory and per-process -- fine for a single dev instance. If this ever
    runs behind multiple workers/processes, back this with Redis instead so
    the counters are shared.
    """

    def __init__(self, max_attempts: int = 5, lockout_seconds: int = 300) -> None:
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self._lock = threading.Lock()
        # key -> (failure_count, locked_until_epoch_or_0)
        self._state: dict[str, tuple[int, float]] = {}

    def check(self, key: str) -> tuple[bool, int]:
        """Returns (allowed, seconds_remaining_if_locked)."""
        with self._lock:
            count, locked_until = self._state.get(key, (0, 0.0))
            now = time.time()
            if locked_until and now < locked_until:
                return False, int(locked_until - now) + 1
            return True, 0

    def record_failure(self, key: str) -> None:
        with self._lock:
            count, _ = self._state.get(key, (0, 0.0))
            count += 1
            locked_until = time.time() + self.lockout_seconds if count >= self.max_attempts else 0.0
            self._state[key] = (count, locked_until)

    def record_success(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)


_default_max_attempts = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
_default_lockout_seconds = int(os.getenv("LOGIN_LOCKOUT_SECONDS", "300"))

login_rate_limiter = LoginRateLimiter(
    max_attempts=_default_max_attempts,
    lockout_seconds=_default_lockout_seconds,
)