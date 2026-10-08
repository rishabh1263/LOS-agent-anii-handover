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

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

# THE SUITE RUNS ON THE CODE DEFAULTS, NOT ON THE DEPLOYMENT'S .env.
#
# `main` calls load_dotenv(), and the deployment .env switches case memory
# and the background worker ON. python-dotenv never overrides a variable
# that is already set, so pinning both here -- before any test imports
# `main` -- keeps whichever test happens to import it first from turning
# them on for the rest of the run. Both default OFF in code; a test that
# needs one sets it explicitly.
for _flag in ("LOS_CASE_MEMORY_ENABLED", "LOS_OCR_WORKER_ENABLED"):
    os.environ[_flag] = "false"

# THE SAME FOR RETRIEVAL. The deployment .env points at an embedded Qdrant
# store and Ollama embeddings. The suite keeps the code defaults -- an
# in-memory store and the local hashing embedder -- so it never takes the
# embedded store's exclusive lock, never depends on a model server, and
# never reads the demo corpus. A test that needs retrieval builds its own.
for _name, _value in (("QDRANT_PATH", ""), ("EMBEDDING_PROVIDER", "hashing"),
                      ("LOS_DEMO_INDEX_ENABLED", "false")):
    os.environ[_name] = _value

# AND THE MCP RUNTIME. The deployment .env may select the MCP protocol; the
# suite runs the capability layer in process unless a test selects protocol
# mode itself (tests/integration/test_mcp_runtime.py does, over real
# transports).
os.environ["LOS_MCP_MODE"] = "in_process"
# THE AUDIENCE THESE SUITES WERE WRITTEN FOR. Production speaks to the LOAN AGENT
# ("The customer's PAN number is ...", app/agents/applicant/copilot/answering/
# voice.py); the existing suites assert the customer channel's wording ("Your PAN
# number is ..."), which APPLICANT_AGENT_AUDIENCE=customer still serves. The agent
# voice is pinned by tests/agents/test_answer_voice.py.
os.environ.setdefault("APPLICANT_AGENT_AUDIENCE", "customer")

# AND AUTHENTICATION: ON, explicitly, whatever a developer's .env says. A test
# that exercises the local no-auth mode switches it off itself.
os.environ["AUTH_ENABLED"] = "true"

# AND THE COPILOT ACCESS POLICY: a STAFF deployment, explicitly. The production
# default is customer-facing (service scopes open only OWNED cases -- see
# access.conversation_service_access). Most suites below use service tokens
# (los.read / los.write) against seeded demo cases no test subject owns: that
# is an officer desk, so the test environment declares it, the same way it
# pins AUTH_ENABLED. The customer-facing default is proven where it matters:
# tests/integration/test_copilot_policy_lock.py switches this off (and deletes
# it to check the code default) and asserts every cross-customer path fails.
os.environ["COPILOT_SERVICE_SCOPE_ACCESS"] = "true"

# AND THE CASE STORE. A test that does not build its own repository used the
# default path -- the deployment's LIVE ./runtime/los_store.sqlite3 -- so the
# suite wrote its fixtures (APP-E2E, CASE-DEFAULT ...) into real data, and
# read back whatever the last run had left there. One temporary store and
# document store per test session instead.
import tempfile as _tempfile

_SESSION_STORE = _tempfile.mkdtemp(prefix="los-tests-")
os.environ["LOS_DOCUMENT_STORE_PATH"] = os.path.join(_SESSION_STORE, "documents")

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
def no_real_jev_provider_by_default(monkeypatch, request):
    """
    No test reaches a real JEV provider unless it is the live suite.

    .env points the app at the local Unsloth Decision API; without this every
    upload in the suite would call it from the background trigger. Contract
    tests set their own JEV_BASE_URL and stub the HTTP reply; test_jev_live.py
    (marker live_jev) keeps the real configuration.
    """
    if request.node.get_closest_marker("live_jev") is None:
        monkeypatch.setenv("JEV_BASE_URL", "")
        monkeypatch.delenv("JEV_API_KEY", raising=False)


@pytest.fixture(scope="session", autouse=True)
def postgres_case_store_for_the_session():
    """
    THE CASE STORE IN TESTS IS POSTGRESQL, as in production. Tests that build
    their own store use app.store.testing.fresh_repository(); code that asks
    get_repository() itself gets this session's database -- never the
    development one (the embedded dev server is switched off here).
    """
    import os

    from app.store.testing import session_dsn

    os.environ["LOS_DEV_EMBEDDED_PG"] = "false"
    os.environ["LOS_STORE_DSN"] = session_dsn()
    yield


@pytest.fixture(autouse=True)
def maker_checker_off_by_default(monkeypatch, request):
    """
    Four-eyes is ON in the shipped config (config/maker_checker.yaml). Tests
    written for the controlled endpoints' own mechanics (gates, override
    permission, deviation authority) run with it off; the maker/checker tests
    (marker or module name maker_checker) switch it on themselves.
    """
    if "maker_checker" not in request.node.nodeid:
        monkeypatch.setenv("MAKER_CHECKER_ENABLED", "false")


@pytest.fixture(autouse=True)
def stage_rules_off_by_default(monkeypatch):
    """
    The Phase 3 stage rules are OFF in every test unless the test opts in
    (monkeypatch.setenv(...) inside the test, as test_fos_cpa_kyc_rule.py does):

      LOS_STAGE_GATE_IN_SERVICE  gate enforced inside stage_lifecycle.transition
      LOS_FOS_CPA_KYC_RULE       FOS -> CPA also requires full KYC

    main.py loads .env at import, so a developer's local flag would otherwise
    reach every test -- and the suites that set up later-stage fixtures with a
    bare OVERRIDE (maker-checker is off above) would be refused. Pinned here,
    existing suites keep testing what they were written for.
    """
    monkeypatch.setenv("LOS_STAGE_GATE_IN_SERVICE", "false")
    monkeypatch.setenv("LOS_FOS_CPA_KYC_RULE", "false")


#: Phase 3 feature flags a developer's .env may turn on (README_CHATBOT "Demo setup"). main.py loads .env
#: at import, so without this every "flag off" test would see them ON. Each test that needs one sets it.
_PHASE3_FLAGS = ("COPILOT_CASE_WORKSPACE", "COPILOT_CASE_ACTIONS", "COPILOT_RESPONSE_STYLE", "COPILOT_VERIFY_DIAGNOSE",
                 "COPILOT_DOCUMENT_ACTIONS", "COPILOT_TERMS_KNOWLEDGE", "COPILOT_LOCALIZED_KYC_REASONS",
                 "COPILOT_SINGLE_CASE_RESOLVE", "COPILOT_EMPHASIS", "COPILOT_STREAMING", "COPILOT_SESSION_MEMORY",
                 "LOS_COAPP_IDENTITY", "COPILOT_PARTY_RECOGNITION", "COPILOT_GUARDRAIL_HARDENING",
                 "LOS_COAPP_MANDATORY_DOCS", "LOS_SIGNATURE_MANDATORY",
                 "COPILOT_LANGUAGE_LOCK", "COPILOT_PROFESSIONAL_FORMAT", "COPILOT_KYC_TABLE",
                 "COPILOT_READINESS_REPORT", "COPILOT_SNAPSHOT_QA",
                 "COPILOT_COUNT_ANSWERS", "COPILOT_HANDOFF_NOTE",
                 "COPILOT_SMART_UPLOAD", "COPILOT_CASE_TIMELINE",
                 "COPILOT_WHAT_IF", "COPILOT_AUTOPILOT_REVIEW", "COPILOT_STATUS_TABLES", "COPILOT_CUSTOMER_MESSAGE", "COPILOT_VISIT_CHECKLIST",
                 # MASTER SPEC: the markdown + tts reply contract (config default ON; the older tests read the
                 # full envelope), the old login self-grant (default off)
                 "COPILOT_MD_TTS_CONTRACT", "LOS_LOGIN_SELF_GRANT_LEGACY", "COPILOT_CASE_LIST_PAGING", "COPILOT_FAQ",
                 "COPILOT_ABUSE_GUARD", "COPILOT_PRODUCT_FLOW", "COPILOT_CHAT_CASE_CREATE")


@pytest.fixture(autouse=True)
def phase3_flags_off_by_default(monkeypatch):
    # pinned to "false", not deleted: ocr.py calls load_dotenv() mid-test, which would put a deleted
    # flag back from the deployment .env (load_dotenv never overrides a variable that is set)
    for flag in _PHASE3_FLAGS:
        monkeypatch.setenv(flag, "false")


@pytest.fixture(autouse=True)
def llm_router_off_by_default(monkeypatch):
    """
    The LLM router (step 6b) is ON by default in the service, so a test run on a
    machine with Ollama up would call the real model: slow, nondeterministic,
    and it breaks the rule never to run tests beside the live model. Pinned OFF
    here; the router tests opt in with a fake model (test_step6b_llm_router.py).
    The decision and knowledge caches are emptied so no test sees another's.
    """
    monkeypatch.setenv("COPILOT_LLM_ROUTER", "false")
    # the question rewrite (general layer slow path) likewise: off unless a test / the eval gate opts in
    monkeypatch.setenv("COPILOT_LLM_REWRITE", "false")
    monkeypatch.delenv("COPILOT_UNDERSTANDING_LLM", raising=False)
    from app.agents.applicant.copilot import agent as _agent
    from app.agents.applicant.copilot.semantics import llm_router as _router

    _router.clear_cache()
    _agent.KNOWLEDGE_ANSWERS.clear()


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
    # THE CODE DEFAULTS, NOT THE DEPLOYMENT'S. `main` loads the deployment
    # .env, which turns both of these ON. SET, NOT DELETED: python-dotenv
    # fills in any variable that is absent, so deleting one here let the
    # first test to `import main` switch it straight back on. A test that
    # needs either sets it explicitly.
    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "false")
    monkeypatch.setenv("LOS_OCR_WORKER_ENABLED", "false")
    monkeypatch.setenv("QDRANT_PATH", "")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hashing")
    monkeypatch.setenv("LOS_DEMO_INDEX_ENABLED", "false")

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
