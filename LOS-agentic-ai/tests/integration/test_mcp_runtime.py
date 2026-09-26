"""
The Copilot's case tools over the REAL MCP protocol -- Slice 5.

Every protocol test crosses an MCP transport: a real `mcp.ClientSession`
sending JSON-RPC to the FastMCP case server (app/mcp/case_server.py) over
in-memory streams, a child process on stdio, or a real HTTP server. Nothing
is mocked between the client and the server.

IDENTITY IS PROVED, NOT ASSERTED. Every caller here authenticates with a
real RS256 token (conftest `make_token`), which the server re-validates.
The unsigned `_meta.caller` identity the runtime used to send is gone, and
the server ignores one if it is sent -- see the attack tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

from app.agents.applicant.permissions import Caller
from app.mcp import applicant as capabilities
from app.mcp import runtime
from app.mcp.contracts import CONTRACTS
from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository

CASE, APP = "case_mcp000000000000000000000000001", "APP-MCPRUNTIME01"
COAPP = "COAPP-MCPRUNTIME1"
SCOPES = ["read_applicant", "read_application", "read_documents",
          "read_verification", "read_pending_items", "read_next_action"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    path = tmp_path / "mcp.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialise()
    set_repository(repository)
    # A stdio server is another process: it opens the same store by path.
    monkeypatch.setenv("LOS_STORE_PATH", str(path))
    yield repository
    runtime.reset()
    set_repository(None)


@pytest.fixture
def case(repo):
    from app.store.ingest import persist_los_result

    persist_los_result({
        "request_id": "r-mcp", "applicant_id": APP, "co_applicant_id": COAPP,
        "case_id": CASE, "status": "SUCCESS", "decision": "PASS",
        "next_action": "PROCEED",
        "documents": [
            {"source_id": "pan.jpg", "type": "PAN", "party_id": APP,
             "verification": "PASS", "reason_codes": []},
            {"source_id": "pan.jpg", "type": "PAN", "party_id": COAPP,
             "party_role": "CO_APPLICANT", "verification": "REVIEW",
             "reason_codes": []}],
    })
    repo.grant_access("mcp-officer", "APPLICANT", APP)
    return CASE


@pytest.fixture
def protocol(monkeypatch):
    def use(transport="memory", **extra):
        monkeypatch.setenv("LOS_MCP_MODE", "protocol")
        monkeypatch.setenv("LOS_MCP_TRANSPORT", transport)
        for key, value in extra.items():
            monkeypatch.setenv(f"LOS_MCP_{key.upper()}", str(value))
        runtime.reset()
    yield use
    runtime.reset()


@pytest.fixture
def signed(make_token):
    """A Caller carrying a real signed token, exactly as a request would."""
    def build(subject="mcp-officer", scopes=SCOPES, **claims):
        token = make_token(subject=subject, scopes=list(scopes), **claims)
        return Caller(subject, frozenset(scopes), frozenset(),
                      credential=token)
    return build


@pytest.fixture
def officer(signed):
    return signed()


def call(tool, caller, **ids):
    return asyncio.run(runtime.call(
        tool, applicant_id=ids.get("applicant_id", APP),
        case_id=ids.get("case_id", CASE), document_type=ids.get("document_type"),
        caller=caller, request_id="req-test", stage="FOS", intent="TEST"))


def _raw(name, arguments, meta=None):
    """A call straight through a fresh client session -- no runtime help."""
    from mcp.shared.memory import create_connected_server_and_client_session

    from app.mcp.case_server import server

    async def go():
        async with create_connected_server_and_client_session(server) as session:
            return await session.call_tool(name, arguments, meta=meta)
    return asyncio.run(go())


def _code(result, name="application.get"):
    return runtime._envelope_from(name, result).error.code


# ==========================================================================
# THE SERVER AND ITS CONTRACTS
# ==========================================================================

def test_the_server_publishes_every_read_contract_and_no_write():
    from app.mcp.case_server import server

    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) == set(capabilities.READ_TOOLS)
    assert not set(tools) & set(capabilities.WRITE_TOOLS)
    for name, tool in tools.items():
        assert tool.description == CONTRACTS[name].summary
        assert set(tool.inputSchema.get("required") or ()) == set(
            CONTRACTS[name].input_schema.get("required") or ())


def test_every_contract_names_a_provider_and_an_output_schema():
    for name, contract in CONTRACTS.items():
        assert contract.provider, name
        assert "ok" in contract.output_schema["properties"]


def test_configuration_can_narrow_the_allowlist_but_never_widen_it(
        monkeypatch):
    from app.mcp import case_server

    monkeypatch.setenv("LOS_MCP_ALLOWED_TOOLS",
                       "application.get,applicant.create,os.system")
    assert case_server.allowed_tools() == frozenset({"application.get"})


# ==========================================================================
# PROTOCOL CALLS -- in-memory transport
# ==========================================================================

def test_a_call_crosses_the_protocol_and_returns_the_real_result(
        case, protocol, officer):
    protocol("memory")
    envelope, trace = call("application.get", officer)

    assert envelope.ok
    assert envelope.result["application"]["case_id"] == CASE
    assert trace["transport"] == "memory" and trace["protocol"] == "mcp"
    assert trace["provider"] == "case_store"
    assert trace["authorized"] is True
    assert trace["request_id"] == "req-test" and trace["stage"] == "FOS"
    assert trace["server_ms"] >= 0 and trace["transport_ms"] >= 0


@pytest.mark.parametrize("tool", sorted(capabilities.READ_TOOLS))
def test_protocol_and_in_process_return_the_same_result(
        case, protocol, officer, monkeypatch, tool):
    """PARITY, for every read tool: the business result is identical."""
    kwargs = {"document_type": "PAN"} if tool == "documents.verification" \
        else {}
    monkeypatch.setenv("LOS_MCP_MODE", "in_process")
    direct, direct_trace = call(tool, officer, **kwargs)
    protocol("memory")
    carried, carried_trace = call(tool, officer, **kwargs)

    assert direct_trace["transport"] == "in_process"
    assert carried_trace["transport"] == "memory"
    assert carried.ok == direct.ok
    assert carried.result == direct.result
    assert (carried.error.code if carried.error else None) == \
        (direct.error.code if direct.error else None)


# ==========================================================================
# AUTHENTICATION -- the signed token is the only identity
# ==========================================================================

def test_a_call_without_a_credential_is_refused(case, protocol):
    protocol("memory")
    unsigned = Caller("mcp-officer", frozenset(SCOPES), frozenset())
    envelope, trace = call("application.get", unsigned)
    assert envelope.error.code == "UNAUTHENTICATED"
    assert trace["authorized"] is False


@pytest.mark.parametrize("claims", [
    {"expires_in": -3600},                          # expired
    {"issuer": "https://evil.example/"},            # wrong issuer
    {"audience": "some-other-api"},                 # wrong audience
    {"subject": None},                              # no subject
])
def test_an_invalid_token_is_refused(case, protocol, signed, claims):
    protocol("memory")
    envelope, _ = call("application.get", signed(**claims))
    assert envelope.error.code == "UNAUTHENTICATED"


def test_a_token_with_no_expiry_is_refused(case, protocol, private_key_pem):
    import app.security.auth as auth

    protocol("memory")
    now = datetime.now(timezone.utc)
    token = jwt.encode({"sub": "mcp-officer", "iss": auth.JWT_ISSUER,
                        "aud": auth.JWT_AUDIENCE, "iat": now, "nbf": now,
                        "scope": " ".join(SCOPES)},
                       private_key_pem, algorithm="RS256",
                       headers={"kid": "test-signing-key"})
    caller = Caller("mcp-officer", frozenset(SCOPES), frozenset(),
                    credential=token)
    assert call("application.get", caller)[0].error.code == "UNAUTHENTICATED"


def test_a_token_signed_by_another_key_is_refused(case, protocol):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    import app.security.auth as auth

    protocol("memory")
    rogue = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = rogue.private_bytes(serialization.Encoding.PEM,
                              serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption())
    now = datetime.now(timezone.utc)
    token = jwt.encode({"sub": "mcp-officer", "iss": auth.JWT_ISSUER,
                        "aud": auth.JWT_AUDIENCE, "iat": now, "nbf": now,
                        "exp": now + timedelta(minutes=5),
                        "scope": " ".join(SCOPES)},
                       pem, algorithm="RS256",
                       headers={"kid": "test-signing-key"})
    caller = Caller("mcp-officer", frozenset(SCOPES), frozenset(),
                    credential=token)
    assert call("application.get", caller)[0].error.code == "UNAUTHENTICATED"


def test_asserted_caller_metadata_is_ignored_the_signed_identity_wins(
        case, make_token):
    """THE ATTACK: metadata says `mcp-officer`; the token is a stranger's."""
    stranger = make_token(subject="someone-else", scopes=SCOPES)
    result = _raw("application.get", {"case_id": CASE}, meta={
        "authorization": f"Bearer {stranger}",
        "caller": {"subject": "mcp-officer", "scopes": SCOPES},
        "caller_id": "mcp-officer"})
    assert _code(result) == "CASE_ACCESS_DENIED"


def test_asserted_caller_metadata_alone_authenticates_nobody(case):
    result = _raw("application.get", {"case_id": CASE}, meta={
        "caller": {"subject": "mcp-officer", "scopes": SCOPES},
        "caller_id": "mcp-officer"})
    assert _code(result) == "UNAUTHENTICATED"


# ==========================================================================
# AUTHORISATION -- the server re-checks everything itself
# ==========================================================================

def test_the_server_refuses_a_scope_the_caller_lacks(case, protocol, signed):
    protocol("memory")
    envelope, trace = call("application.get",
                           signed(scopes=["read_documents"]))
    assert envelope.error.code == "INSUFFICIENT_SCOPE"
    assert trace["authorized"] is False


def test_the_server_refuses_a_case_the_caller_does_not_own(
        case, protocol, signed):
    protocol("memory")
    envelope, trace = call("application.get", signed(subject="someone-else"))
    assert envelope.error.code == "CASE_ACCESS_DENIED"
    assert trace["authorized"] is False


def test_the_server_refuses_an_applicant_the_caller_does_not_own(
        case, protocol, signed):
    protocol("memory")
    envelope, _ = call("applicant.get", signed(subject="someone-else"))
    assert envelope.error.code == "CASE_ACCESS_DENIED"


def test_a_co_applicant_is_reachable_only_through_the_owned_case(
        case, protocol, officer, signed):
    protocol("memory")
    documents, _ = call("documents.get", officer)
    assert {d["party_id"] for d in documents.result["documents"]} == \
        {APP, COAPP}
    refused, _ = call("documents.get", signed(subject="someone-else"))
    assert refused.error.code == "CASE_ACCESS_DENIED"


def test_an_unknown_tool_is_refused(case, officer):
    result = _raw("applicant.delete_everything", {"case_id": CASE},
                  meta={"authorization": f"Bearer {officer.credential}"})
    assert result.isError
    assert not runtime._envelope_from("applicant.delete_everything",
                                      result).ok


def test_a_write_tool_is_not_reachable_over_mcp(case, officer):
    result = _raw("applicant.create", {"full_name": "X"},
                  meta={"authorization": f"Bearer {officer.credential}"})
    assert result.isError


def test_a_tool_outside_the_allowlist_is_refused_even_if_registered(
        case, officer, monkeypatch):
    from app.mcp import case_server

    monkeypatch.setenv("LOS_MCP_ALLOWED_TOOLS", "documents.get")
    result = _raw("application.get", {"case_id": CASE},
                  meta={"authorization": f"Bearer {officer.credential}"})
    assert _code(result) == "TOOL_NOT_ALLOWED"
    assert "application.get" not in case_server.allowed_tools()


def test_malformed_input_is_rejected(case, officer):
    meta = {"authorization": f"Bearer {officer.credential}"}
    missing = _raw("application.get", {}, meta=meta)
    assert missing.isError and _code(missing) == "MCP_TOOL_ERROR"
    injected = _raw("application.get", {"case_id": "x' OR 1=1 --"}, meta=meta)
    assert _code(injected) == "INVALID_INPUT"


# ==========================================================================
# RESULTS, TIMEOUTS, FAILURE
# ==========================================================================

class _Result:
    def __init__(self, payload=None, text=None, error=False):
        self.structuredContent = payload
        self.isError = error
        self.content = [type("B", (), {"text": text})()] if text else []


def test_a_malformed_result_is_rejected_not_trusted():
    assert runtime._envelope_from(
        "application.get", _Result({"ok": True, "capability": "documents.get",
                                    "status": "OK", "result": {}})
    ).error.code == "MCP_MALFORMED_RESULT"      # another tool's envelope
    assert runtime._envelope_from(
        "application.get", _Result({"ok": "maybe", "status": 7})
    ).error.code == "MCP_MALFORMED_RESULT"      # not an envelope at all


def test_an_oversized_result_is_rejected(monkeypatch):
    monkeypatch.setenv("LOS_MCP_MAX_RESULT_BYTES", "2048")
    assert runtime._envelope_from(
        "application.get", _Result(text="x" * 5000)
    ).error.code == "MCP_MALFORMED_RESULT"


def test_a_slow_tool_times_out_without_breaking_the_session(
        case, protocol, officer, monkeypatch):
    protocol("memory", call_timeout_seconds="0.3")

    async def slow(case_id):
        await asyncio.sleep(2)
    monkeypatch.setitem(capabilities.READ_TOOLS, "application.get", slow)

    envelope, trace = call("application.get", officer)
    assert envelope.error.code == "MCP_TIMEOUT"
    assert trace["timed_out"] is True
    assert call("workflow.pending_items", officer)[0].ok


def test_a_provider_failure_is_a_structured_error(case, protocol, officer,
                                                  monkeypatch):
    protocol("memory")

    def broken():
        raise RuntimeError("store is down")
    monkeypatch.setattr(capabilities, "_repo", broken)

    envelope, _ = call("application.get", officer)
    assert envelope.error.code == "CAPABILITY_FAILED"
    assert "store is down" not in json.dumps(envelope.model_dump())


def test_an_unreachable_server_fails_closed_or_falls_back_as_configured(
        case, protocol, officer):
    protocol("http", server_url="http://127.0.0.1:9/mcp",
             connect_timeout_seconds="3")
    envelope, trace = call("application.get", officer)
    assert envelope.error.code == "MCP_UNAVAILABLE"

    protocol("http", server_url="http://127.0.0.1:9/mcp",
             connect_timeout_seconds="3", fallback="in_process")
    envelope, trace = call("application.get", officer)
    assert envelope.ok and trace["transport"] == "in_process"
    assert trace["fallback"]


# ==========================================================================
# CREDENTIALS NEVER LEAK; TELEMETRY HAS A HOOK
# ==========================================================================

def test_no_log_line_carries_the_token(case, protocol, officer, caplog):
    protocol("memory")
    with caplog.at_level(logging.DEBUG):
        call("application.get", officer)
        call("application.get", Caller("x", frozenset(), frozenset(),
                                       credential=officer.credential + "x"))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert officer.credential not in text
    assert officer.credential.split(".")[1] not in text      # the claims part
    line = next(r.getMessage() for r in caplog.records
                if r.getMessage().startswith("mcp_call "))
    trace = json.loads(line[len("mcp_call "):])
    for field in ("request_id", "case_id", "caller", "stage", "intent", "tool",
                  "provider", "authorized", "timed_out", "started_at",
                  "duration_ms", "status", "transport"):
        assert field in trace
    assert "bearer" not in line.lower() and "authorization" not in line.lower()


def test_a_telemetry_listener_gets_every_call_without_the_credential(
        case, protocol, officer):
    protocol("memory")
    seen = []
    runtime.add_listener(seen.append)
    try:
        call("application.get", officer)
    finally:
        runtime.remove_listener(seen.append)
    assert seen and seen[0]["tool"] == "application.get"
    assert officer.credential not in json.dumps(seen)


# ==========================================================================
# REAL TRANSPORTS -- HTTP and stdio
# ==========================================================================

@pytest.fixture
def http_server(case):
    """The case MCP server on a real HTTP port, in this process."""
    import uvicorn

    from app.mcp.case_server import server

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(server.streamable_http_app(), host="127.0.0.1",
                            port=port, log_level="warning")
    running = uvicorn.Server(config)
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not running.started and time.time() < deadline:
        time.sleep(0.05)
    assert running.started, "the HTTP MCP server did not start"
    yield f"http://127.0.0.1:{port}/mcp"
    runtime.reset()
    running.should_exit = True
    thread.join(timeout=10)


def test_http_the_server_authenticates_and_authorises_every_call(
        http_server, protocol, officer, signed):
    protocol("http", server_url=http_server, connect_timeout_seconds="15")
    envelope, trace = call("application.get", officer)
    assert envelope.ok, envelope
    assert trace["transport"] == "http" and trace["protocol"] == "mcp"

    assert call("application.get", signed(subject="someone-else")
                )[0].error.code == "CASE_ACCESS_DENIED"
    assert call("application.get", signed(expires_in=-60)
                )[0].error.code == "UNAUTHENTICATED"
    assert call("application.get", Caller("mcp-officer", frozenset(SCOPES),
                                          frozenset())
                )[0].error.code == "UNAUTHENTICATED"


@pytest.fixture
def jwks_file(signing_keypair, monkeypatch):
    """
    The session public key, served as a JWKS endpoint the child process
    fetches over HTTP -- exactly how it reaches a real Identity Provider
    (PyJWKClient accepts only http(s) URIs).
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import app.security.auth as auth

    _private, public = signing_keypair
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public))
    jwk.update({"kid": "test-signing-key", "use": "sig", "alg": "RS256"})
    body = json.dumps({"keys": [jwk]}).encode()

    class JWKS(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    idp = ThreadingHTTPServer(("127.0.0.1", 0), JWKS)
    threading.Thread(target=idp.serve_forever, daemon=True).start()
    monkeypatch.setenv("JWT_JWKS_URL",
                       f"http://127.0.0.1:{idp.server_address[1]}/jwks.json")
    monkeypatch.setenv("JWT_ISSUER", auth.JWT_ISSUER)
    monkeypatch.setenv("JWT_AUDIENCE", auth.JWT_AUDIENCE)
    yield idp
    idp.shutdown()


def test_stdio_the_server_runs_in_another_process_and_still_authenticates(
        case, jwks_file, protocol, officer, signed):
    protocol("stdio", connect_timeout_seconds="60", call_timeout_seconds="20")

    envelope, trace = call("application.get", officer)
    assert envelope.ok, envelope
    assert envelope.result["application"]["case_id"] == CASE
    assert trace["transport"] == "stdio" and trace["protocol"] == "mcp"

    assert call("application.get", signed(subject="someone-else")
                )[0].error.code == "CASE_ACCESS_DENIED"
    assert call("application.get", signed(audience="some-other-api")
                )[0].error.code == "UNAUTHENTICATED"


# ==========================================================================
# READINESS
# ==========================================================================

def test_readiness_in_process_needs_no_mcp_server(monkeypatch):
    monkeypatch.setenv("LOS_MCP_MODE", "in_process")
    assert runtime.health()["status"] == "NOT_REQUIRED"


def test_readiness_reports_a_misconfiguration(monkeypatch):
    monkeypatch.setenv("LOS_MCP_MODE", "protcol")          # a typo
    report = runtime.health()
    assert report["status"] == "MISCONFIGURED"
    assert report["mode"] == "in_process"                  # safe fallback
    assert report["problems"]


def test_readiness_reports_available_and_unavailable(case, protocol):
    protocol("memory")
    report = runtime.health()
    assert report["status"] == "AVAILABLE"
    assert set(report["tools"]) == set(capabilities.READ_TOOLS)

    protocol("http", server_url="http://127.0.0.1:9/mcp",
             connect_timeout_seconds="2")
    assert runtime.health()["status"] == "UNAVAILABLE"


def test_the_ready_probe_refuses_traffic_when_protocol_mcp_is_down(
        case, protocol):
    import main

    protocol("http", server_url="http://127.0.0.1:9/mcp",
             connect_timeout_seconds="2")
    response = TestClient(main.app).get("/ready")
    assert response.status_code == 503
    assert response.json()["reason"] == "mcp_unavailable"

    protocol("http", server_url="http://127.0.0.1:9/mcp",
             connect_timeout_seconds="2", fallback="in_process")
    response = TestClient(main.app).get("/ready")
    assert response.status_code == 200
    assert response.json()["mcp"]["degraded"] is True


# ==========================================================================
# THE COPILOT, END TO END, IN PROTOCOL MODE
# ==========================================================================

@pytest.fixture
def copilot(make_token):
    import main

    c = TestClient(main.app)
    c.headers["Authorization"] = "Bearer " + make_token(
        subject="mcp-officer", scopes=SCOPES)
    return c


def ask(client, message, status=200):
    response = client.post("/api/v1/copilot/query", json={
        "applicant_id": APP, "case_id": CASE, "message": message})
    assert response.status_code == status, response.text
    return response.json()


@pytest.mark.parametrize("question", [
    "What is my application status?",
    "What documents are pending?",
    "what doc is pending?",
    "Are both applicants verified?",
])
def test_the_copilot_answers_the_same_over_mcp(case, protocol, copilot,
                                               monkeypatch, caplog, question):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    monkeypatch.setenv("LOS_MCP_MODE", "in_process")
    direct = ask(copilot, question)

    protocol("memory")
    with caplog.at_level(logging.INFO, logger="app.mcp.runtime"):
        carried = ask(copilot, question)

    assert carried["answer"] == direct["answer"]
    assert carried["tool_invoked"] == direct["tool_invoked"]
    traces = [json.loads(r.getMessage()[len("mcp_call "):])
              for r in caplog.records
              if r.getMessage().startswith("mcp_call ")]
    assert traces and {t["tool"] for t in traces} == set(carried["tool_invoked"])
    assert all(t["transport"] == "memory" and t["protocol"] == "mcp"
               and t["authorized"] for t in traces)
    assert carried["answer_basis"]["tool_transport"] == ["memory"]
    assert carried["timings"]["mcp_ms"] >= 0


def test_a_server_side_refusal_reaches_the_caller_as_403(
        case, protocol, copilot, monkeypatch):
    # The Copilot's own checks pass; the MCP SERVER says no. Published as a
    # refusal, never as an empty answer.
    protocol("memory")
    monkeypatch.setenv("LOS_MCP_ALLOWED_TOOLS", "applicant.get")
    body = ask(copilot, "What documents are pending?", status=403)
    assert body["detail"]["code"] == "CASE_ACCESS_DENIED"
