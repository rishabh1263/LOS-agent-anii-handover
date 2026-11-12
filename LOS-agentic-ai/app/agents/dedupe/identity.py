"""
Dedupe parameters -> normalised values -> keyed-HMAC match tokens.

THE PARAMETERS (owner, 2026-10-22): PAN, MOBILE, AP_CODE, PR_CODE -- nothing else.
  - PAN and MOBILE are stored by the LOS (PAN encrypted, mobile in clear on `applicants`): matched as keyed HMACs.
  - AP_CODE and PR_CODE are NOT stored by this LOS. dedupe_policy.yaml `parameters` maps each to a store column; while
    the mapping is null, a search on it is INSUFFICIENT_DATA -- never a guessed field.

WHY A KEYED HMAC. A PAN has a small keyspace (~3x10^12): a plain SHA-256 of it is brute-forced from a database copy.
HMAC-SHA256 under a key held OUTSIDE the database is useless without the key. The key is NOT the data-encryption key
(app/store/crypto.py): leaking one does not expose the other.

KEYS. LOS_IDENTITY_HMAC_KEY = "key_id:secret[,key_id2:secret2]". The first key writes; every key is tried when
matching (rotation). Production refuses to run without it. Development and tests use a FIXED, PUBLIC
development-only key; it protects nothing and is never accepted in production.

AADHAAR IS NEVER HANDLED (owner rule).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import unicodedata
from functools import lru_cache

ENV_KEY = "LOS_IDENTITY_HMAC_KEY"
_DEV_KEYS = (("dev1", hashlib.sha256(b"los-identity-hmac-development-only").hexdigest()),)
PARAMETERS = ("PAN", "MOBILE", "AP_CODE", "PR_CODE")

_PAN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_CODE = re.compile(r"^[A-Z0-9][A-Z0-9_.:/-]{0,63}$")


class IdentityKeyError(RuntimeError):
    """Production without an identity HMAC key, or a malformed key."""


def _production() -> bool:
    return (os.getenv("ENVIRONMENT") or "development").strip().lower() in {"production", "prod"}


def require_in_production() -> None:
    if _production() and not (os.getenv(ENV_KEY) or "").strip():
        raise IdentityKeyError(f"{ENV_KEY} is required in production: dedupe match tokens are keyed HMACs.")


@lru_cache(maxsize=4)
def _parse(raw: str) -> tuple[tuple[str, bytes], ...]:
    keys = []
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        kid, sep, secret = part.partition(":")
        if not sep or not kid.strip() or len(secret.strip()) < 16:
            raise IdentityKeyError(f"{ENV_KEY}: each key is key_id:secret (secret 16+ characters).")
        keys.append((kid.strip(), secret.strip().encode()))
    if not keys:
        raise IdentityKeyError(f"{ENV_KEY} names no key.")
    return tuple(keys)


def _keys() -> tuple[tuple[str, bytes], ...]:
    raw = (os.getenv(ENV_KEY) or "").strip()
    if not raw:
        require_in_production()
        return tuple((kid, secret.encode()) for kid, secret in _DEV_KEYS)
    return _parse(raw)


def _mac(secret: bytes, kind: str, value: str) -> str:
    # the PARAMETER is in the message: equal strings of two parameters never share a token
    return hmac.new(secret, f"{kind}\x1f{value}".encode(), hashlib.sha256).hexdigest()


def token(kind: str, value: str | None) -> str | None:
    """The token WRITTEN for a normalised value (first key), or None."""
    if not value:
        return None
    kid, secret = _keys()[0]
    return f"{kid}:{_mac(secret, kind, value)}"


def tokens(kind: str, value: str | None) -> set[str]:
    """Every token a normalised value may be stored under (all keys: rotation)."""
    if not value:
        return set()
    return {f"{kid}:{_mac(secret, kind, value)}" for kid, secret in _keys()}


def _upper(text: str | None) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")).upper())


def pan(value: str | None) -> str | None:
    v = re.sub(r"[^A-Z0-9]", "", _upper(value))
    return v if _PAN.match(v) else None


def mobile(value: str | None) -> str | None:
    """A 10-digit Indian mobile (+91 / 0 prefix dropped), or None."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if re.fullmatch(r"[6-9]\d{9}", digits) else None


def code(value: str | None) -> str | None:
    """An AP / PR code as typed: upper-cased, spaces dropped."""
    v = _upper(value)
    return v if _CODE.match(v) else None


NORMALISE = {"PAN": pan, "MOBILE": mobile, "AP_CODE": code, "PR_CODE": code}


def normalise(kind: str, value: str | None) -> str | None:
    return NORMALISE[kind](value)


__all__ = ["ENV_KEY", "PARAMETERS", "IdentityKeyError", "code", "mobile", "normalise", "pan",
           "require_in_production", "token", "tokens"]
