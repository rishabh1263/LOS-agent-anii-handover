"""
Security utilities: JWT token creation/verification and credential checking.

Kept separate from route logic so it's reusable across the app
(e.g. other protected routes can import verify_token from here).
"""

from datetime import datetime, timedelta, timezone
import secrets

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.core.config import Settings, get_settings

# Matches the frontend authClient path used by Swagger "Authorize"
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/v1/auth/login")

# In-memory refresh-token store (replace with Redis/DB in production).
# Maps refresh_token -> {"sub": username, "exp": unix_ts}
_refresh_store: dict[str, dict] = {}


def verify_credentials(username: str, password: str, settings: Settings) -> bool:
    """
    Checks username/password against the configured dummy user.
    Replace this function's internals later with a real DB lookup +
    hashed password check (e.g. passlib/bcrypt) without touching any route code.
    """
    return username == settings.dummy_username and password == settings.dummy_password


def create_access_token(
    subject: str,
    settings: Settings,
    *,
    role: str | None = None,
    scope: str | None = None,
) -> str:
    """
    Creates an RS256-signed JWT matching the LOS token shape:

    Header:  { "alg": "RS256", "kid": "...", "typ": "JWT" }
    Payload: sub, iss, aud, iat, nbf, exp, jti, scope, role
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.access_token_expire_minutes)
    iat = int(now.timestamp())
    jti = f"{subject}-{iat}"

    payload = {
        "sub": subject,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": iat,
        "nbf": iat,
        "exp": int(expire.timestamp()),
        "jti": jti,
        "scope": scope if scope is not None else settings.jwt_default_scope,
        "role": role if role is not None else settings.jwt_default_role,
    }

    headers = {
        "kid": settings.jwt_kid,
        "typ": "JWT",
    }

    return jwt.encode(
        payload,
        settings.get_private_key(),
        algorithm=settings.jwt_algorithm,
        headers=headers,
    )


def create_refresh_token(subject: str, settings: Settings) -> str:
    """Opaque refresh token stored server-side (not a JWT)."""
    token = secrets.token_urlsafe(48)
    exp = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    _refresh_store[token] = {
        "sub": subject,
        "exp": int(exp.timestamp()),
    }
    return token


def rotate_refresh_token(old_token: str, settings: Settings) -> tuple[str, str] | None:
    """
    Validates old refresh token, issues a new access + refresh pair.
    Returns (access_token, new_refresh_token) or None if invalid/expired.
    """
    entry = _refresh_store.pop(old_token, None)
    if entry is None:
        return None
    now = int(datetime.now(timezone.utc).timestamp())
    if entry["exp"] < now:
        return None
    subject = entry["sub"]
    access = create_access_token(subject=subject, settings=settings)
    new_refresh = create_refresh_token(subject=subject, settings=settings)
    return access, new_refresh


def revoke_refresh_token(token: str) -> None:
    _refresh_store.pop(token, None)


def decode_access_token(token: str, settings: Settings) -> dict:
    """
    Decodes and validates a JWT using the RSA public key (RS256).
    Enforces issuer and audience claims.
    """
    return jwt.decode(
        token,
        settings.get_public_key(),
        algorithms=[settings.jwt_algorithm],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
    )


def get_current_user(
    token: str = Depends(oauth2_scheme),
    settings: Settings = Depends(get_settings),
) -> str:
    """
    Dependency to protect routes. Use like:

        @router.get("/protected")
        def protected_route(user: str = Depends(get_current_user)):
            ...

    Returns the username stored in the token, or raises 401.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token, settings)
        username: str | None = payload.get("sub")
        if username is None:
            raise credentials_exception
        return username
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError:
        raise credentials_exception