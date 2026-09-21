"""
Shared test fixtures, principally authentication.

Every business route is protected by RS256 JWT verified against a JWKS
endpoint. Tests must therefore present a real, signed, verifiable token --
production auth is not disabled, bypassed or monkeypatched away, because a
test suite that switches auth off cannot tell you whether auth works.

What IS replaced is the network: an RSA keypair is generated per session, and
the JWKS provider is pointed at that public key instead of at an Identity
Provider over HTTP. The token still has to carry the right algorithm, kid,
issuer, audience and expiry, and `validate_token` still verifies the
signature. No key material is committed and no token is hardcoded.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

TEST_KID = "test-signing-key"

# Used only when the environment does not define them, so the fixture works on
# a bare checkout as well as against a configured .env.
FALLBACK_ISSUER = "los-test-issuer"
FALLBACK_AUDIENCE = "los-test-audience"


class _StubSigningKey:
    """What PyJWKClient would return: an object carrying the public key."""

    def __init__(self, key: Any) -> None:
        self.key = key


class _StubJWKSProvider:
    """
    Stands in for the Identity Provider's JWKS endpoint.

    Returns the session's public key for any token. Signature verification,
    claim validation and expiry are all still performed by production code --
    only the key lookup is local.
    """

    def __init__(self, public_key: Any) -> None:
        self._public_key = public_key

    def get_signing_key(self, token: str) -> _StubSigningKey:
        return _StubSigningKey(self._public_key)


@pytest.fixture(scope="session")
def signing_keypair() -> tuple[Any, Any]:
    """A throwaway RSA keypair, generated fresh for this test session."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(scope="session")
def private_key_pem(signing_keypair) -> bytes:
    private_key, _public = signing_keypair
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )



@pytest.fixture(autouse=True)
def deterministic_summary_by_default(monkeypatch):
    """
    No test reaches a real language model unless it asks to.

    The suite must not depend on Ollama being installed, running, or holding
    any particular model -- and it must not get SLOWER because a model became
    responsive. Both happened: with an unreachable model the summary stage
    cost nothing, and switching to one that answers added roughly a second to
    every test that ran the LOS flow.

    Production keeps the summary ON; this only changes the default inside the
    suite. Tests that exercise the model enable it explicitly and stub the
    generation call, which continues to work because this is only a default.
    """
    monkeypatch.setenv("LOS_LLM_SUMMARY_ENABLED", "false")

    from app.agents.los import config
    from app.llm import availability

    config.reload()
    availability.reset()
    yield
    config.reload()
    availability.reset()


@pytest.fixture(autouse=True)
def stub_jwks(monkeypatch, signing_keypair):
    """
    Point the JWKS provider at the session key, for every test.

    Autouse so no test accidentally reaches for a real Identity Provider over
    the network. get_jwks_provider is lru_cached in production, so the cache is
    cleared around the patch to keep a real provider from leaking in or out.
    """
    import app.security.auth as auth

    _private, public_key = signing_keypair

    # Held so teardown can clear the REAL cache: by then the module attribute
    # is still the stub, which has no cache to clear.
    original = auth.get_jwks_provider
    original.cache_clear()

    monkeypatch.setattr(
        auth, "get_jwks_provider", lambda: _StubJWKSProvider(public_key)
    )

    # Tokens must match whatever the app was configured with. Where the
    # environment supplies nothing, pin both sides to a known value so the
    # fixture still produces a verifiable token.
    if not auth.JWT_ISSUER:
        monkeypatch.setattr(auth, "JWT_ISSUER", FALLBACK_ISSUER)
    if not auth.JWT_AUDIENCE:
        monkeypatch.setattr(auth, "JWT_AUDIENCE", FALLBACK_AUDIENCE)

    yield

    original.cache_clear()


@pytest.fixture
def make_token(private_key_pem) -> Callable[..., str]:
    """
    Mint a signed token.

    Every claim is overridable so a test can build an expired token, one for
    the wrong audience, or one carrying particular scopes, without another
    fixture per case.
    """

    def _make(
        subject: str = "test-subject",
        scopes: str | list[str] | None = None,
        roles: list[str] | None = None,
        issuer: str | None = None,
        audience: str | None = None,
        expires_in: int = 900,
        issued_at: datetime | None = None,
        algorithm: str = "RS256",
        kid: str | None = TEST_KID,
        **extra: Any,
    ) -> str:
        import app.security.auth as auth

        now = issued_at or datetime.now(timezone.utc)

        claims: dict[str, Any] = {
            "sub": subject,
            "iss": issuer if issuer is not None else auth.JWT_ISSUER,
            "aud": audience if audience is not None else auth.JWT_AUDIENCE,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(seconds=expires_in),
            "jti": uuid.uuid4().hex,
        }

        if scopes is not None:
            claims["scope"] = (
                scopes if isinstance(scopes, str) else " ".join(scopes)
            )
        if roles is not None:
            claims["roles"] = roles

        claims.update(extra)

        headers = {"kid": kid} if kid else {}

        return jwt.encode(
            claims, private_key_pem, algorithm=algorithm, headers=headers
        )

    return _make


@pytest.fixture
def auth_token(make_token) -> str:
    """One ordinary valid token."""
    return make_token(
        scopes=["documents:read", "documents:write", "kyc:read", "agents:execute"],
        roles=["loan_officer"],
    )


@pytest.fixture
def auth_headers(auth_token) -> dict[str, str]:
    """Authorization header for a protected route."""
    return {"Authorization": f"Bearer {auth_token}"}


@pytest.fixture
def app_client(auth_headers) -> TestClient:
    """
    The whole application, authenticated.

    Use `unauthenticated_client` where the point of the test is that a request
    without credentials is refused.
    """
    from main import app

    client = TestClient(app)
    client.headers.update(auth_headers)
    return client


@pytest.fixture
def unauthenticated_client() -> TestClient:
    """The whole application with no credentials attached."""
    from main import app

    return TestClient(app)


@pytest.fixture
def authenticate() -> Callable[[TestClient, dict[str, str]], TestClient]:
    """
    Attach credentials to a client a test built itself.

    Several suites assemble a cut-down app from individual routers rather than
    importing the whole one; this lets them stay as they are.
    """

    def _authenticate(client: TestClient, headers: dict[str, str]) -> TestClient:
        client.headers.update(headers)
        return client

    return _authenticate
