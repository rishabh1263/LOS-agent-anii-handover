"""
FIELD VALUES ENCRYPTED AT REST (2026-10-06).

The case store holds what documents SAID -- PAN numbers, names, dates of birth,
addresses, income figures -- in `case_findings.payload`. Masking happens on the
API response; until now the database itself held the raw values, so anyone with
read access to Postgres (a backup, a replica, a support query) read them too.

WHAT IS ENCRYPTED. The payload of the finding kinds that carry document values
(EXTRACTION, KYC, FINANCIAL by default; LOS_ENCRYPTED_FINDING_KINDS overrides).
Codes, statuses, scores and ids stay in their own columns, in clear, so every
query, index and idempotency key works unchanged -- no SQL reads payload content.

HOW. Fernet (AES-128-CBC + HMAC-SHA256, from `cryptography`). The stored JSON is
{"_enc": "fernet.v1", "data": "<token>"}: still valid JSON, recognisable, and a
row written before this change (plain JSON) still reads.

KEYS. LOS_DATA_ENCRYPTION_KEY: one Fernet key, or several comma-separated for
ROTATION -- the first encrypts, every one decrypts. Production refuses to start
without it (app/store/__init__.py). Development and tests fall back to a FIXED
DEVELOPMENT KEY so the encrypted path is the one exercised; it protects nothing
and is never accepted in production.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from functools import lru_cache
from typing import Any

MARKER = "fernet.v1"
ENV_KEY = "LOS_DATA_ENCRYPTION_KEY"
_DEFAULT_KINDS = ("EXTRACTION", "KYC", "FINANCIAL")
#: development only -- derived from a public string, so it is NOT a secret
_DEV_KEY = base64.urlsafe_b64encode(hashlib.sha256(b"los-development-only-key").digest()).decode()


class EncryptionConfigError(RuntimeError):
    """Production without a data encryption key."""


def _production() -> bool:
    return os.getenv("ENVIRONMENT", "development").strip().lower() in {"production", "prod"}


def configured() -> bool:
    return bool(os.getenv(ENV_KEY, "").strip())


def require_in_production() -> None:
    if _production() and not configured():
        raise EncryptionConfigError(
            f"{ENV_KEY} is required in production: document values in the case store are encrypted at rest.")


@lru_cache(maxsize=4)
def _fernet(raw: str):
    from cryptography.fernet import Fernet, MultiFernet

    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return MultiFernet([Fernet(k.encode()) for k in keys])


def _cipher():
    raw = os.getenv(ENV_KEY, "").strip()
    if not raw:
        require_in_production()
        raw = _DEV_KEY
    return _fernet(raw)


def encrypted_kinds() -> frozenset[str]:
    raw = os.getenv("LOS_ENCRYPTED_FINDING_KINDS")
    kinds = [k.strip().upper() for k in raw.split(",")] if raw is not None else list(_DEFAULT_KINDS)
    return frozenset(k for k in kinds if k)


def seal(kind: str, payload: dict[str, Any]) -> str:
    """The JSON stored for a payload: encrypted when its kind carries document values."""
    text = json.dumps(payload or {})
    if str(kind).upper() not in encrypted_kinds() or not payload:
        return text
    return json.dumps({"_enc": MARKER, "data": _cipher().encrypt(text.encode()).decode()})


def open_(stored: Any) -> Any:
    """A stored payload back as JSON text: decrypted if sealed, untouched if plain (older rows)."""
    if not isinstance(stored, str) or MARKER not in stored:
        return stored
    try:
        wrapper = json.loads(stored)
    except ValueError:
        return stored
    if not isinstance(wrapper, dict) or wrapper.get("_enc") != MARKER:
        return stored
    return _cipher().decrypt(wrapper["data"].encode()).decode()


__all__ = ["seal", "open_", "encrypted_kinds", "require_in_production", "configured", "EncryptionConfigError"]
