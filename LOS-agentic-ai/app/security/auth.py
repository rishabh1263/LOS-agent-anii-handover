"""
Production JWT authentication and authorization for LOS Agentic AI.

The Python LOS service is a RESOURCE SERVER.

It:
- validates short-lived access JWTs
- resolves signing keys from JWKS
- validates kid / signature / exp / nbf / iat
- validates issuer and audience
- supports scope and role authorization
- never stores refresh tokens
- never stores access tokens
- never holds the IdP private signing key

Environment:
    JWT_ALGORITHM=RS256
    JWT_ISSUER=los-local
    JWT_AUDIENCE=los-agentic-ai
    JWT_JWKS_URL=http://127.0.0.1:8020/.well-known/jwks.json
    JWT_LEEWAY_SECONDS=30
    JWT_JWKS_CACHE_SECONDS=300
"""

from __future__ import annotations

import os
import time
from functools import lru_cache
from typing import Any

import jwt
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


# ============================================================================
# BEARER SCHEME
# ============================================================================

bearer_scheme = HTTPBearer(auto_error=False)


# ============================================================================
# CONFIGURATION
# ============================================================================

JWT_ALGORITHM = (
    os.getenv("JWT_ALGORITHM", "RS256")
    or "RS256"
).strip().upper()

JWT_ISSUER = (
    os.getenv("JWT_ISSUER") or ""
).strip()

JWT_AUDIENCE = (
    os.getenv("JWT_AUDIENCE") or ""
).strip()

JWT_JWKS_URL = (
    os.getenv("JWT_JWKS_URL") or ""
).strip()

JWT_LEEWAY_SECONDS = max(
    0,
    int(os.getenv("JWT_LEEWAY_SECONDS", "30")),
)

JWT_JWKS_CACHE_SECONDS = max(
    30,
    int(os.getenv("JWT_JWKS_CACHE_SECONDS", "300")),
)

ALLOWED_ALGORITHMS = {
    "RS256",
    "RS384",
    "RS512",
    "ES256",
    "ES384",
    "ES512",
}


# ============================================================================
# STARTUP VALIDATION
# ============================================================================

def validate_auth_configuration() -> None:
    if not JWT_JWKS_URL:
        raise RuntimeError(
            "JWT_JWKS_URL is not configured."
        )

    if not JWT_ISSUER:
        raise RuntimeError(
            "JWT_ISSUER is not configured."
        )

    if not JWT_AUDIENCE:
        raise RuntimeError(
            "JWT_AUDIENCE is not configured."
        )

    if JWT_ALGORITHM not in ALLOWED_ALGORITHMS:
        raise RuntimeError(
            f"Unsupported JWT_ALGORITHM={JWT_ALGORITHM!r}. "
            f"Allowed algorithms: {sorted(ALLOWED_ALGORITHMS)}"
        )


# ============================================================================
# HTTP ERRORS
# ============================================================================

def unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={
            "WWW-Authenticate": "Bearer"
        },
    )


def forbidden(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=detail,
    )


# ============================================================================
# JWKS
# ============================================================================

class JWKSProvider:
    """
    Resolve public signing keys from the Identity Provider.

    The JWT kid selects the correct public key.

    Private signing keys never enter this service.
    """

    def __init__(self) -> None:
        self.client = jwt.PyJWKClient(
            JWT_JWKS_URL,
            cache_jwk_set=True,
            lifespan=JWT_JWKS_CACHE_SECONDS,
            cache_keys=True,
        )

    def get_signing_key(self, token: str):
        try:
            return self.client.get_signing_key_from_jwt(token)

        except Exception as exc:
            raise unauthorized(
                "Unable to resolve JWT signing key."
            ) from exc


@lru_cache(maxsize=1)
def get_jwks_provider() -> JWKSProvider:
    return JWKSProvider()


# ============================================================================
# HEADER
# ============================================================================

def _validate_header(token: str) -> None:
    try:
        header = jwt.get_unverified_header(token)

    except jwt.InvalidTokenError as exc:
        raise unauthorized(
            "Invalid JWT header."
        ) from exc

    algorithm = header.get("alg")
    key_id = header.get("kid")

    if algorithm != JWT_ALGORITHM:
        raise unauthorized(
            "JWT algorithm is not allowed."
        )

    if not key_id:
        raise unauthorized(
            "JWT kid is missing."
        )


# ============================================================================
# CLAIM HELPERS
# ============================================================================

def _get_scopes(
    claims: dict[str, Any],
) -> set[str]:

    raw = claims.get(
        "scope",
        claims.get("scp", ""),
    )

    if isinstance(raw, str):
        return {
            item.strip()
            for item in raw.split()
            if item.strip()
        }

    if isinstance(raw, list):
        return {
            str(item).strip()
            for item in raw
            if str(item).strip()
        }

    return set()


def _get_roles(
    claims: dict[str, Any],
) -> set[str]:

    raw = claims.get(
        "roles",
        claims.get("role", []),
    )

    if isinstance(raw, str):
        return {
            item.strip()
            for item in raw.replace(",", " ").split()
            if item.strip()
        }

    if isinstance(raw, list):
        return {
            str(item).strip()
            for item in raw
            if str(item).strip()
        }

    return set()


# ============================================================================
# TOKEN VALIDATION
# ============================================================================

def validate_token(
    token: str,
) -> dict[str, Any]:

    _validate_header(token)

    signing_key = (
        get_jwks_provider()
        .get_signing_key(token)
    )

    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER,
            audience=JWT_AUDIENCE,
            leeway=JWT_LEEWAY_SECONDS,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_iss": True,
                "verify_aud": True,
                # PRESENT, NOT ONLY VALID WHEN PRESENT. `verify_exp` checks an
                # `exp` that exists; without `require`, a signed token with no
                # expiry was accepted forever, and one with no subject named
                # nobody for ownership to bind to. Both IdPs this service
                # knows issue both.
                "require": ["exp", "sub"],
            },
        )

    except jwt.MissingRequiredClaimError as exc:
        raise unauthorized(
            f"JWT is missing a required claim: {exc.claim}."
        ) from exc

    except jwt.ExpiredSignatureError as exc:
        raise unauthorized(
            "JWT token expired."
        ) from exc

    except jwt.ImmatureSignatureError as exc:
        raise unauthorized(
            "JWT token is not active yet."
        ) from exc

    except jwt.InvalidIssuedAtError as exc:
        raise unauthorized(
            "JWT issued-at claim is invalid."
        ) from exc

    except jwt.InvalidIssuerError as exc:
        raise unauthorized(
            "JWT issuer is invalid."
        ) from exc

    except jwt.InvalidAudienceError as exc:
        raise unauthorized(
            "JWT audience is invalid."
        ) from exc

    except jwt.InvalidSignatureError as exc:
        raise unauthorized(
            "JWT signature is invalid."
        ) from exc

    except jwt.InvalidTokenError as exc:
        raise unauthorized(
            "Invalid JWT token."
        ) from exc

    if not isinstance(claims, dict):
        raise unauthorized(
            "JWT payload is invalid."
        )

    return claims


# ============================================================================
# AUTHENTICATION ON / OFF -- local testing only
# ============================================================================
#
# AUTH_ENABLED=false lets a developer call the API without a token. It is
# honoured ONLY when ENVIRONMENT is development / dev / local / test -- the
# same convention the dev IdP uses, where an UNSET environment counts as
# production. Anywhere else the flag is ignored and authentication stays ON
# (fail closed), and startup refuses outright (`validate_auth_mode`).
#
# DISABLING AUTHENTICATION DISABLES NOTHING ELSE. The request still gets an
# identity (`local_claims`: a subject and scopes, never a token), and
# ownership, scope checks, the MCP server's re-authorisation and the
# guardrails all run exactly as they do with a real token.
# ============================================================================

_DEV_ENVIRONMENTS = frozenset({"development", "dev", "local", "test"})
_FALSE = frozenset({"false", "0", "no", "off"})


def environment() -> str:
    return (os.getenv("ENVIRONMENT") or "production").strip().lower()


def auth_disabled_requested() -> bool:
    return (os.getenv("AUTH_ENABLED") or "true").strip().lower() in _FALSE


def auth_enabled() -> bool:
    """True unless AUTH_ENABLED=false AND this is a development environment."""
    return not (auth_disabled_requested()
                and environment() in _DEV_ENVIRONMENTS)


def validate_auth_mode() -> None:
    """Refuse to start when authentication is switched off outside dev."""
    if auth_disabled_requested() and environment() not in _DEV_ENVIRONMENTS:
        raise RuntimeError(
            "AUTH_ENABLED=false is only permitted when ENVIRONMENT is one of "
            f"{sorted(_DEV_ENVIRONMENTS)}; ENVIRONMENT={environment()!r}. "
            "Refusing to start without authentication.")


def local_claims() -> dict[str, Any]:
    """
    The identity a request carries when authentication is OFF (dev only).
    A subject and scopes from configuration -- no token, no secret. Every
    authorisation check still runs against them.
    """
    # READ-ONLY BY DEFAULT. `los.write` is the write-all service scope; a
    # developer who needs writes with authentication off grants it explicitly
    # (AUTH_LOCAL_SCOPES="los.read los.write") rather than getting it silently.
    return {
        "sub": (os.getenv("AUTH_LOCAL_SUBJECT") or "local-developer").strip(),
        "scope": (os.getenv("AUTH_LOCAL_SCOPES") or "los.read").strip(),
        "auth_disabled": True,
    }


# ============================================================================
# PRIMARY AUTHENTICATION
# ============================================================================

class VerifiedClaims(dict):
    """
    The validated claims -- and, as an ATTRIBUTE, the credential they came
    from.

    WHY IT TRAVELS. The MCP server re-authenticates every call itself
    (app/mcp/case_server.py): it is handed the caller's own signed token and
    validates it with `validate_token`, rather than trusting an identity the
    client asserts. So the token must reach the MCP client.

    WHY AN ATTRIBUTE, NOT A KEY. Claims are logged, audited, copied and
    serialised; a key would go wherever they go. An attribute is invisible
    to json.dumps, to dict(), to `**claims` and to every `.items()` -- and a
    copy simply loses it, which fails closed.
    """

    __slots__ = ("credential", "auth_ms")

    def __init__(self, claims: dict[str, Any], credential: str | None = None,
                 auth_ms: float | None = None):
        super().__init__(claims)
        self.credential = credential
        #: How long authentication took (a timing, never a claim value).
        self.auth_ms = auth_ms

    def __repr__(self) -> str:  # never print the credential
        return f"VerifiedClaims({dict.__repr__(self)})"


def require_jwt(
    credentials: HTTPAuthorizationCredentials | None = Security(
        bearer_scheme
    ),
) -> dict[str, Any]:

    # AUTHENTICATION OFF (development only -- see `auth_enabled`): the
    # configured local identity, and nothing else changes.
    from app.observability.tracing import span

    if not auth_enabled():
        with span("auth.authenticate", auth_enabled=False, outcome="LOCAL_IDENTITY"):
            return VerifiedClaims(local_claims(), credential=None)

    # The span carries the outcome only -- never the token, a header or a claim
    # value (tracing.safe_attributes drops anything credential-shaped anyway).
    auth_started = time.perf_counter()
    with span("auth.authenticate", auth_enabled=True) as current:
        if credentials is None:
            current and current.set_attribute("outcome", "MISSING")
            raise unauthorized(
                "Missing Bearer token."
            )

        if credentials.scheme.lower() != "bearer":
            current and current.set_attribute("outcome", "WRONG_SCHEME")
            raise unauthorized(
                "Authorization scheme must be Bearer."
            )

        try:
            claims = validate_token(credentials.credentials)
        except Exception:
            current and current.set_attribute("outcome", "REJECTED")
            raise
        current and current.set_attribute("outcome", "VERIFIED")
        return VerifiedClaims(
            claims, credential=credentials.credentials,
            auth_ms=round((time.perf_counter() - auth_started) * 1000, 2))


# ============================================================================
# BACKWARD-COMPATIBLE NAME
# ============================================================================
#
# Older V21 routes still import verify_api_key.
#
# It is now ONLY a compatibility alias to JWT authentication.
# No X-API-Key authentication remains here.
# ============================================================================

def verify_api_key(
    credentials: HTTPAuthorizationCredentials | None = Security(
        bearer_scheme
    ),
) -> dict[str, Any]:

    return require_jwt(
        credentials
    )


# ============================================================================
# SCOPE AUTHORIZATION
# ============================================================================

def require_scope(
    scope: str,
):
    def dependency(
        claims: dict[str, Any] = Security(
            require_jwt
        ),
    ) -> dict[str, Any]:

        if scope not in _get_scopes(claims):
            raise forbidden(
                f"Required scope missing: {scope}"
            )

        return claims

    return dependency


def require_any_scope(
    scopes_required: list[str]
    | tuple[str, ...]
    | set[str],
):
    allowed = set(scopes_required)

    def dependency(
        claims: dict[str, Any] = Security(
            require_jwt
        ),
    ) -> dict[str, Any]:

        if not _get_scopes(claims).intersection(
            allowed
        ):
            raise forbidden(
                "None of the required scopes are present."
            )

        return claims

    return dependency


def require_all_scopes(
    scopes_required: list[str]
    | tuple[str, ...]
    | set[str],
):
    required = set(scopes_required)

    def dependency(
        claims: dict[str, Any] = Security(
            require_jwt
        ),
    ) -> dict[str, Any]:

        missing = (
            required - _get_scopes(claims)
        )

        if missing:
            raise forbidden(
                "Required scopes missing: "
                + ", ".join(
                    sorted(missing)
                )
            )

        return claims

    return dependency


# ============================================================================
# ROLE AUTHORIZATION
# ============================================================================

def require_role(
    role: str,
):
    def dependency(
        claims: dict[str, Any] = Security(
            require_jwt
        ),
    ) -> dict[str, Any]:

        if role not in _get_roles(claims):
            raise forbidden(
                f"Required role missing: {role}"
            )

        return claims

    return dependency


def require_any_role(
    roles_required: list[str]
    | tuple[str, ...]
    | set[str],
):
    allowed = set(roles_required)

    def dependency(
        claims: dict[str, Any] = Security(
            require_jwt
        ),
    ) -> dict[str, Any]:

        if not _get_roles(claims).intersection(
            allowed
        ):
            raise forbidden(
                "None of the required roles are present."
            )

        return claims

    return dependency


# ============================================================================
# CLAIM ACCESS
# ============================================================================

def get_subject(
    claims: dict[str, Any],
) -> str | None:
    return claims.get("sub")


def get_client_id(
    claims: dict[str, Any],
) -> str | None:
    return (
        claims.get("client_id")
        or claims.get("azp")
        or claims.get("appid")
    )


def get_scopes(
    claims: dict[str, Any],
) -> set[str]:
    return _get_scopes(claims)


def get_roles(
    claims: dict[str, Any],
) -> set[str]:
    return _get_roles(claims)


# ============================================================================
# SAFE DIAGNOSTIC
# ============================================================================

def auth_health() -> dict[str, Any]:
    return {
        "enabled": True,
        "algorithm": JWT_ALGORITHM,
        "issuer": JWT_ISSUER,
        "audience": JWT_AUDIENCE,
        "jwks_configured": bool(
            JWT_JWKS_URL
        ),
        "jwks_cache_seconds": JWT_JWKS_CACHE_SECONDS,
        "leeway_seconds": JWT_LEEWAY_SECONDS,
    }
