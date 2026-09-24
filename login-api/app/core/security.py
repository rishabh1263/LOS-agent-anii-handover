"""
Security utilities: JWT token creation/verification and credential checking.

Kept separate from route logic so it's reusable across the app
(e.g. other protected routes can import verify_token from here).
"""

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.core.config import Settings, get_settings

# This just tells FastAPI/Swagger where to send username+password to get a token.
# It does not enforce OAuth2 flows, it's only used for the "Authorize" button in docs
# and for extracting the Bearer token from the Authorization header.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")


def verify_credentials(username: str, password: str, settings: Settings) -> bool:
    """
    Checks username/password against the configured dummy user.
    Replace this function's internals later with a real DB lookup +
    hashed password check (e.g. passlib/bcrypt) without touching any route code.
    """
    return username == settings.dummy_username and password == settings.dummy_password


def create_access_token(subject: str, settings: Settings) -> str:
    """
    Creates an RS256-signed JWT with sub, iss, aud, iat, nbf, exp, jti,
    scope and role claims, and a `kid` header.
    """
    now = datetime.now(timezone.utc)
    iat = int(now.timestamp())
    exp = int((now + timedelta(minutes=settings.access_token_expire_minutes)).timestamp())

    payload = {
        "sub": subject,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": iat,
        "nbf": iat,
        "exp": exp,
        "jti": f"{settings.dummy_role}-{iat}",
        "scope": settings.dummy_scope,
        "role": settings.dummy_role,
    }
    return jwt.encode(
        payload,
        settings.private_key,
        algorithm=settings.jwt_algorithm,
        headers={"kid": settings.jwt_kid},  # "typ": "JWT" PyJWT khud add karta hai
    )


def decode_access_token(token: str, settings: Settings) -> dict:
    """
    Decodes and validates a JWT (signature, exp, nbf, iss, aud).
    Raises jwt exceptions on failure, which get translated to HTTP errors
    by get_current_user below.
    """
    return jwt.decode(
        token,
        settings.public_key,
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