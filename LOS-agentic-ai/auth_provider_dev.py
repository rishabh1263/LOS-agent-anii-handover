from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import jwt
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import JSONResponse

BASE_DIR = Path(__file__).resolve().parent
KEY_DIR = BASE_DIR / "auth_keys"
DB_PATH = BASE_DIR / "auth_provider.sqlite3"

KEY_DIR.mkdir(parents=True, exist_ok=True)

PRIVATE_KEY_PATH = KEY_DIR / "jwt_signing_private.pem"
PUBLIC_KEY_PATH = KEY_DIR / "jwt_signing_public.pem"

JWT_ALGORITHM = "RS256"
JWT_ISSUER = os.getenv("JWT_ISSUER", "http://127.0.0.1:8020")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "los-agentic-ai")
JWT_KEY_ID = os.getenv("JWT_KEY_ID", "los-rs256-1")

ACCESS_TOKEN_TTL_SECONDS = int(
    os.getenv("ACCESS_TOKEN_TTL_SECONDS", "900")
)

REFRESH_TOKEN_TTL_SECONDS = int(
    os.getenv("REFRESH_TOKEN_TTL_SECONDS", "604800")
)

CLIENT_ID = os.getenv("AUTH_CLIENT_ID", "los-demo-client")
CLIENT_SECRET = os.getenv(
    "AUTH_CLIENT_SECRET",
    "change-me-local-only",
)


def ensure_signing_keypair() -> None:
    if PRIVATE_KEY_PATH.exists() and PUBLIC_KEY_PATH.exists():
        return

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    PRIVATE_KEY_PATH.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    PUBLIC_KEY_PATH.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def load_private_key() -> str:
    return PRIVATE_KEY_PATH.read_text(encoding="utf-8")


def load_public_key():
    return serialization.load_pem_public_key(
        PUBLIC_KEY_PATH.read_bytes()
    )


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS refresh_tokens (
            token_hash TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            client_id TEXT NOT NULL,
            issued_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    conn.commit()
    return conn


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()


def issue_refresh_token(
    subject: str,
    client_id: str,
) -> str:
    token = secrets.token_urlsafe(48)

    now = int(time.time())
    expires_at = now + REFRESH_TOKEN_TTL_SECONDS

    conn = db()

    try:
        conn.execute(
            """
            INSERT INTO refresh_tokens
            (
                token_hash,
                subject,
                client_id,
                issued_at,
                expires_at,
                revoked
            )
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (
                hash_refresh_token(token),
                subject,
                client_id,
                now,
                expires_at,
            ),
        )

        conn.commit()

    finally:
        conn.close()

    return token


def rotate_refresh_token(
    old_token: str,
) -> tuple[str, str]:

    token_hash = hash_refresh_token(old_token)
    now = int(time.time())

    conn = db()

    try:
        row = conn.execute(
            """
            SELECT
                subject,
                client_id,
                expires_at,
                revoked
            FROM refresh_tokens
            WHERE token_hash = ?
            """,
            (token_hash,),
        ).fetchone()

        if not row:
            raise HTTPException(
                status_code=401,
                detail="Invalid refresh credential",
            )

        subject, client_id, expires_at, revoked = row

        if revoked:
            raise HTTPException(
                status_code=401,
                detail="Refresh credential revoked",
            )

        if expires_at <= now:
            raise HTTPException(
                status_code=401,
                detail="Refresh credential expired",
            )

        conn.execute(
            """
            UPDATE refresh_tokens
            SET revoked = 1
            WHERE token_hash = ?
            """,
            (token_hash,),
        )

        conn.commit()

    finally:
        conn.close()

    return (
        subject,
        issue_refresh_token(subject, client_id),
    )


def issue_access_token(
    subject: str,
    client_id: str,
) -> str:

    now = int(time.time())

    payload = {
        "sub": subject,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + ACCESS_TOKEN_TTL_SECONDS,
        "jti": uuid.uuid4().hex,
        "client_id": client_id,
        "scope": "los.read los.write",
        "role": "los-service",
    }

    return jwt.encode(
        payload,
        load_private_key(),
        algorithm=JWT_ALGORITHM,
        headers={"kid": JWT_KEY_ID},
    )


def b64url_int(value: int) -> str:
    raw = value.to_bytes(
        (value.bit_length() + 7) // 8,
        "big",
    )

    return (
        base64.urlsafe_b64encode(raw)
        .rstrip(b"=")
        .decode("ascii")
    )


def public_jwk() -> dict[str, Any]:
    public_key = load_public_key()
    numbers = public_key.public_numbers()

    return {
        "kty": "RSA",
        "use": "sig",
        "alg": JWT_ALGORITHM,
        "kid": JWT_KEY_ID,
        "n": b64url_int(numbers.n),
        "e": b64url_int(numbers.e),
    }


ensure_signing_keypair()

app = FastAPI(
    title="LOS Local Identity Provider",
    version="1.0.0",
)


@app.post("/oauth/token")
def token(
    grant_type: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
):
    if grant_type != "client_credentials":
        raise HTTPException(
            status_code=400,
            detail="Only client_credentials is supported.",
        )

    if (
        client_id != CLIENT_ID
        or client_secret != CLIENT_SECRET
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid client credentials",
        )

    access_token = issue_access_token(
        subject=client_id,
        client_id=client_id,
    )

    refresh_token = issue_refresh_token(
        subject=client_id,
        client_id=client_id,
    )

    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token": refresh_token,
            "refresh_expires_in": REFRESH_TOKEN_TTL_SECONDS,
        }
    )


@app.post("/oauth/refresh")
def refresh(
    refresh_token: str = Form(...),
):
    subject, new_refresh_token = rotate_refresh_token(
        refresh_token
    )

    access_token = issue_access_token(
        subject=subject,
        client_id=CLIENT_ID,
    )

    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token": new_refresh_token,
            "refresh_expires_in": REFRESH_TOKEN_TTL_SECONDS,
        }
    )


@app.get("/.well-known/jwks.json")
def jwks():
    return {
        "keys": [
            public_jwk()
        ]
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "issuer": JWT_ISSUER,
        "audience": JWT_AUDIENCE,
        "algorithm": JWT_ALGORITHM,
        "access_token_ttl_seconds": ACCESS_TOKEN_TTL_SECONDS,
    }


if __name__ == "__main__":
    uvicorn.run(
        "auth_provider_dev:app",
        host="127.0.0.1",
        port=8020,
        reload=False,
    )
