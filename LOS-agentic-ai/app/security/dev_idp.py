"""
Dev-only local Identity Provider -- single process, same port as the main app.

`app/security/auth.py` is a resource server: it only VALIDATES tokens against
a JWKS URL, it never issues them. In production that JWKS URL points at a
real IdP (Keycloak, Auth0, Cognito, etc). This module plays the IdP role
locally, for development only:

    - generates an RSA keypair once per process
    - exposes it as a JWKS document (GET /.well-known/jwks.json)
    - issues short-lived RS256 access tokens shaped exactly the way
      app/security/auth.require_jwt expects
    - issues and rotates refresh tokens, stored hashed in a local sqlite file
      (never the raw token), single-use: each refresh invalidates the token
      used and returns a new one

Nothing here is used to VALIDATE access tokens -- app/security/auth.py still
owns that entirely. This only stands in for the IdP side during development.

DO NOT point JWT_JWKS_URL at this in a real deployment; swap in the real
IdP's JWKS URL there and stop including auth_api's router.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, status
from jwt.algorithms import RSAAlgorithm

_KEY_ID = "los-dev-key-1"

ACCESS_TOKEN_TTL_SECONDS = int(os.getenv("DEV_IDP_ACCESS_TOKEN_TTL_SECONDS", "900"))  # 15 min
REFRESH_TOKEN_TTL_SECONDS = int(os.getenv("DEV_IDP_REFRESH_TOKEN_TTL_SECONDS", "604800"))  # 7 days

_DB_PATH = Path(os.getenv("DEV_IDP_DB_PATH", "dev_idp_refresh_tokens.sqlite3"))


class _DevKeyPair:
    """One RSA keypair, generated once and held for the life of the process."""

    def __init__(self) -> None:
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = self.private_key.public_key()
        self.private_pem = self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )


@lru_cache(maxsize=1)
def _keypair() -> _DevKeyPair:
    return _DevKeyPair()


def get_jwks() -> dict[str, Any]:
    """The JWKS document that JWT_JWKS_URL should resolve to."""
    jwk = json.loads(RSAAlgorithm.to_jwk(_keypair().public_key))
    jwk["kid"] = _KEY_ID
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return {"keys": [jwk]}


def issue_access_token(
    *,
    subject: str,
    issuer: str,
    audience: str,
    scopes: Iterable[str] | None = None,
    roles: Iterable[str] | None = None,
) -> str:
    """Issue a short-lived RS256 access token that require_jwt will accept."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": subject,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "nbf": now,
        "exp": now + ACCESS_TOKEN_TTL_SECONDS,
    }
    if scopes:
        payload["scope"] = " ".join(scopes)
    if roles:
        payload["roles"] = list(roles)

    return jwt.encode(payload, _keypair().private_pem, algorithm="RS256", headers={"kid": _KEY_ID})


# ============================================================================
# REFRESH TOKENS
#
# The refresh token itself is a random opaque string, never a JWT. Only its
# SHA-256 hash is stored, so reading the database doesn't hand out usable
# tokens. Each refresh call revokes the token it was given and issues a new
# one (rotation) -- reusing an old, already-rotated token is rejected, which
# is what catches a stolen refresh token being replayed after the legitimate
# client has already rotated past it.
# ============================================================================

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS refresh_tokens (
            token_hash TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            issued_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    return conn


def _hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_refresh_token(subject: str) -> str:
    token = secrets.token_urlsafe(48)
    now = int(time.time())
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO refresh_tokens (token_hash, subject, issued_at, expires_at, revoked) "
            "VALUES (?, ?, ?, ?, 0)",
            (_hash_refresh_token(token), subject, now, now + REFRESH_TOKEN_TTL_SECONDS),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def rotate_refresh_token(old_token: str) -> tuple[str, str]:
    """Validates and revokes old_token, returns (subject, new_refresh_token)."""
    token_hash = _hash_refresh_token(old_token)
    now = int(time.time())
    conn = _db()
    try:
        row = conn.execute(
            "SELECT subject, expires_at, revoked FROM refresh_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()

        if not row:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

        subject, expires_at, revoked = row

        if revoked:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token already used")

        if expires_at <= now:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired")

        conn.execute("UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?", (token_hash,))
        conn.commit()
    finally:
        conn.close()

    return subject, issue_refresh_token(subject)


def revoke_refresh_token(token: str) -> None:
    """Used by /logout. Silently no-ops if the token doesn't exist."""
    conn = _db()
    try:
        conn.execute(
            "UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?",
            (_hash_refresh_token(token),),
        )
        conn.commit()
    finally:
        conn.close()