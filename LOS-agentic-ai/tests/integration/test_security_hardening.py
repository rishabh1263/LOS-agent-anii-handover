"""
Production security hardening: who may touch which case, with which scope.

THE AUDIT FINDINGS THESE HOLD:

  1. OWNERSHIP proved only that a case belonged to the applicant named in
     the request -- two ids the CALLER chose. Nothing bound the caller to
     them: any token with a read scope could read any applicant's case.
  2. SCOPE was not enforced on the processing routes (/los/process, /kyc,
     /verify, /financial, /document-agent, /extract-document,
     /agents/execute): a valid token of any kind was enough.
  3. The confirmation of a proposed write trusted the tool name it was sent.
  4. A development identity provider was mounted unconditionally.
  5. The audit log kept the first 120 characters of every question verbatim,
     identifiers and all.

THE OWNERSHIP MODEL (app/security/access.py): a service principal holding
`los.read` (read) or `los.write` (read + write) may access any case -- the
existing service-account scopes. Everyone else only what their own JWT
subject created, recorded as a grant when they created it. Fail closed.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository

FOS_READ = ["read_applicant", "read_application", "read_documents",
            "read_verification", "read_pending_items", "read_next_action"]
FOS_WRITE = ["create_applicant", "update_applicant", "create_application",
             "upload_document"]
FOS = FOS_READ + FOS_WRITE
COPILOT = "/api/v1/copilot/query"


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "security.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


@pytest.fixture(autouse=True)
def llm_off(monkeypatch):
    from app.agents.applicant import config as agent_config

    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    agent_config.reload()
    yield
    monkeypatch.undo()
    agent_config.reload()


@pytest.fixture
def app_client():
    import main

    return TestClient(main.app)


def headers(make_token, subject: str, scopes) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(subject=subject, scopes=scopes)}"}


def open_case(client, auth, name="Asha Rao") -> dict:
    """An FOS officer opens a case -- and so owns it."""
    response = client.post("/api/v1/fos/applicants", headers=auth, json={
        "applicant": {"full_name": name, "mobile": "9876543210"},
        "application": {"product": "PERSONAL_LOAN"}})
    assert response.status_code in (200, 201), response.text
    body = response.json()
    return {"applicant_id": body["applicant_id"], "case_id": body["case_id"]}


def ask(client, auth, case, message="What is my application status?"):
    return client.post(COPILOT, headers=auth, json={**case, "message": message})


# ==========================================================================
# 1. OWNERSHIP
# ==========================================================================

def test_the_owner_reads_their_own_case(app_client, repo, make_token):
    alice = headers(make_token, "alice", FOS)
    case = open_case(app_client, alice)

    response = ask(app_client, alice, case)

    assert response.status_code == 200, response.text
    assert response.json()["intent"] == "APPLICATION_STATUS"


def test_another_officer_cannot_read_it(app_client, repo, make_token):
    case = open_case(app_client, headers(make_token, "alice", FOS))

    response = ask(app_client, headers(make_token, "bob", FOS), case)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "CASE_ACCESS_DENIED"


def test_a_fake_applicant_id_is_refused(app_client, repo, make_token):
    alice = headers(make_token, "alice", FOS)
    case = open_case(app_client, alice)

    response = ask(app_client, alice,
                   {"applicant_id": "APP-NOT-HERS", "case_id": case["case_id"]})

    assert response.status_code == 403


def test_a_case_of_another_applicant_is_refused(app_client, repo, make_token):
    alice = headers(make_token, "alice", FOS)
    mine = open_case(app_client, alice)
    theirs = open_case(app_client, headers(make_token, "bob", FOS), "Bob Das")

    # Alice's own applicant id, Bob's case id.
    response = ask(app_client, alice,
                   {"applicant_id": mine["applicant_id"],
                    "case_id": theirs["case_id"]})

    assert response.status_code == 403


def test_an_applicant_level_question_is_owned_too(app_client, repo, make_token):
    case = open_case(app_client, headers(make_token, "alice", FOS))

    response = app_client.post(COPILOT, headers=headers(make_token, "bob", FOS),
                               json={"applicant_id": case["applicant_id"],
                                     "message": "how many cases do I have?"})

    assert response.status_code == 403


def test_the_denial_is_the_same_for_missing_and_foreign_cases(app_client, repo,
                                                             make_token):
    alice = headers(make_token, "alice", FOS)
    mine = open_case(app_client, alice)
    theirs = open_case(app_client, headers(make_token, "bob", FOS), "Bob Das")

    foreign = ask(app_client, alice, theirs)
    missing = ask(app_client, alice, {"applicant_id": mine["applicant_id"],
                                      "case_id": "CASE-DOES-NOT-EXIST"})

    assert foreign.status_code == missing.status_code == 403
    assert foreign.json()["detail"]["message"] == missing.json()["detail"]["message"]


def test_a_service_reader_may_read_any_case_but_not_write(app_client, repo,
                                                         make_token):
    case = open_case(app_client, headers(make_token, "alice", FOS))
    service = headers(make_token, "svc-reporting", ["los.read"])

    assert ask(app_client, service, case).status_code == 200
    upload = app_client.post("/api/v1/fos/documents", headers=service, data={
        "applicant_id": case["applicant_id"], "case_id": case["case_id"],
        "document_types": "PAN"},
        files={"files": ("pan.jpg", b"not-an-image", "image/jpeg")})
    assert upload.status_code == 403


@pytest.mark.parametrize("path", [
    "/api/v1/fos/applications/{case_id}",
    "/api/v1/fos/checklist/{case_id}",
    "/api/v1/fos/documents/{case_id}",
])
def test_the_fos_read_routes_are_owned(app_client, repo, make_token, path):
    case = open_case(app_client, headers(make_token, "alice", FOS))
    url = path.format(**case) + f"?applicant_id={case['applicant_id']}"

    assert app_client.get(url, headers=headers(make_token, "alice", FOS)).status_code == 200
    assert app_client.get(url, headers=headers(make_token, "bob", FOS)).status_code == 403


def test_eligibility_is_owned(app_client, repo, make_token):
    case = open_case(app_client, headers(make_token, "alice", FOS))
    url = f"/api/v1/eligibility/{case['case_id']}?applicant_id={case['applicant_id']}"

    denied = app_client.get(url, headers=headers(make_token, "bob",
                                                 FOS + ["read_eligibility"]))
    assert denied.status_code == 403


def test_no_token_is_401(app_client, repo):
    response = app_client.post(COPILOT, json={"applicant_id": "A", "case_id": "C",
                                              "message": "status?"})
    assert response.status_code == 401


@pytest.mark.parametrize("token", ["not-a-jwt", "a.b.c",
                                   "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0."])
def test_an_invalid_token_is_401(app_client, repo, token):
    response = app_client.post(COPILOT, headers={"Authorization": f"Bearer {token}"},
                               json={"applicant_id": "A", "case_id": "C",
                                     "message": "status?"})
    assert response.status_code == 401


def test_an_expired_token_is_401(app_client, repo, make_token):
    token = make_token(subject="alice", scopes=FOS, expires_in=-120)
    response = app_client.post(COPILOT, headers={"Authorization": f"Bearer {token}"},
                               json={"applicant_id": "A", "case_id": "C",
                                     "message": "status?"})
    assert response.status_code == 401


def test_a_token_without_a_subject_owns_nothing(repo):
    from app.security import access

    repo.save_applicant(__import__("app.store.models", fromlist=["Applicant"])
                        .Applicant(applicant_id="APP-1"))
    with pytest.raises(access.AccessDenied):
        access.authorize(None, set(FOS), applicant_id="APP-1")


def test_a_backend_without_grants_fails_closed():
    from app.store.repository import Repository

    assert Repository.has_access(object(), "alice", "CASE", "C-1") is False


# ==========================================================================
# 2. /los/process -- scope AND ownership
# ==========================================================================

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def _process(client, auth, **fields):
    return client.post("/api/v1/los/process", headers=auth, data={
        "operation": "PROCESS", "expected_types": "PAN", **fields},
        files={"files": ("pan.png", _PNG, "image/png")})


def test_processing_needs_a_write_scope(app_client, repo, make_token):
    response = _process(app_client, headers(make_token, "alice", FOS_READ),
                        applicant_id="APP-NEW-1", case_id="CASE-NEW-1")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"


def test_processing_a_new_case_makes_the_caller_its_owner(app_client, repo,
                                                          make_token):
    alice = headers(make_token, "alice", ["documents:write"])

    first = _process(app_client, alice, applicant_id="APP-NEW-2",
                     case_id="CASE-NEW-2")

    assert first.status_code == 200, first.text
    assert repo.has_access("alice", "CASE", "CASE-NEW-2")
    assert repo.has_access("alice", "APPLICANT", "APP-NEW-2")


def test_processing_into_someone_elses_case_is_refused(app_client, repo,
                                                       make_token):
    case = open_case(app_client, headers(make_token, "alice", FOS))

    response = _process(app_client, headers(make_token, "bob", ["documents:write"]),
                        **case)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "CASE_ACCESS_DENIED"


def test_a_service_writer_may_process_any_case(app_client, repo, make_token):
    case = open_case(app_client, headers(make_token, "alice", FOS))

    response = _process(app_client, headers(make_token, "svc-pipeline",
                                            ["los.write"]), **case)

    assert response.status_code == 200, response.text


# ==========================================================================
# 3. ROUTE SCOPES
# ==========================================================================

@pytest.mark.parametrize("method,path,accepted", [
    ("post", "/api/v1/document-agent", ["documents:write"]),
    ("post", "/api/v1/extract-document", ["documents:write"]),
    ("post", "/api/v1/extract-document/batch", ["documents:write"]),
    ("post", "/api/v1/financial/extract", ["documents:write"]),
    ("post", "/api/v1/financial/verify", ["documents:write"]),
    ("post", "/api/v1/verify", ["documents:write"]),
    ("post", "/api/v1/kyc", ["kyc:read"]),
    ("post", "/api/v1/agents/execute", ["agents:execute"]),
])
def test_every_processing_route_enforces_its_scope(app_client, repo, make_token,
                                                  method, path, accepted):
    bare = getattr(app_client, method)(
        path, headers=headers(make_token, "alice", ["read_applicant"]))
    allowed = getattr(app_client, method)(
        path, headers=headers(make_token, "alice", accepted))

    assert bare.status_code == 403, bare.text
    assert bare.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"
    # Past the scope check (the empty request then fails validation).
    assert allowed.status_code != 403, allowed.text


def test_a_read_service_scope_does_not_open_a_write_route(app_client, repo,
                                                          make_token):
    response = app_client.post("/api/v1/document-agent",
                               headers=headers(make_token, "svc", ["los.read"]))

    assert response.status_code == 403


# ==========================================================================
# 4. TOOL AUTHORISATION (the in-process capability layer)
# ==========================================================================

def _caller(scopes, subject="alice"):
    from app.agents.applicant.permissions import Caller

    return Caller(subject=subject, scopes=frozenset(scopes), roles=frozenset())


def test_an_authorised_read_tool_passes():
    from app.agents.applicant import permissions

    permissions.check_tool(_caller(["read_application"]), "application.get")


def test_a_read_tool_without_its_scope_is_refused():
    from app.agents.applicant import permissions

    with pytest.raises(permissions.PermissionDenied):
        permissions.check_tool(_caller(["read_documents"]), "application.get")


def test_an_authorised_write_tool_passes():
    from app.agents.applicant import permissions

    permissions.check_tool(_caller(["create_applicant"]), "applicant.create")


def test_a_read_all_token_cannot_write():
    from app.agents.applicant import permissions

    with pytest.raises(permissions.PermissionDenied):
        permissions.check_tool(_caller(["los.read"]), "applicant.create")


def test_an_unknown_tool_is_refused():
    from app.agents.applicant import permissions

    with pytest.raises(permissions.PermissionDenied):
        permissions.check_tool(_caller(["los.read", "los.write"] + FOS),
                               "applicant.delete_everything")


def test_a_tool_without_a_contract_is_refused(monkeypatch):
    from app.agents.applicant import permissions
    from app.mcp import contracts

    monkeypatch.delitem(contracts.CONTRACTS, "application.get")
    with pytest.raises(permissions.PermissionDenied):
        permissions.check_tool(_caller(FOS + ["los.read"]), "application.get")


def test_a_tool_whose_scope_does_not_resolve_is_refused(monkeypatch):
    from app.agents.applicant import permissions
    from app.mcp import contracts

    monkeypatch.setattr(contracts, "required_scope", lambda _name: None)
    with pytest.raises(permissions.PermissionDenied):
        permissions.check_tool(_caller(FOS + ["los.read"]), "application.get")


async def test_a_confirmation_cannot_swap_in_another_tool(repo):
    from app.agents.applicant.agent import AgentError, confirm_action

    with pytest.raises(AgentError) as refused:
        await confirm_action(
            action={"type": "UPDATE_APPLICANT", "tool": "applicant.create",
                    "arguments": {"full_name": "X"}},
            claims={"sub": "alice", "scope": " ".join(FOS)})

    assert refused.value.code == "INVALID_ACTION"


async def test_a_confirmed_write_to_anothers_applicant_is_refused(repo):
    from app.agents.applicant.agent import AgentError, confirm_action
    from app.store.models import Applicant

    repo.save_applicant(Applicant(applicant_id="APP-BOB"))
    repo.grant_access("bob", "APPLICANT", "APP-BOB")

    with pytest.raises(AgentError) as refused:
        await confirm_action(
            action={"type": "UPDATE_APPLICANT", "tool": "applicant.update",
                    "arguments": {"applicant_id": "APP-BOB",
                                  "mobile": "9000000000"}},
            claims={"sub": "alice", "scope": " ".join(FOS)})

    assert refused.value.http_status == 403


async def test_a_confirmed_create_is_owned_by_its_creator(repo):
    from app.agents.applicant.agent import confirm_action

    done = await confirm_action(
        action={"type": "CREATE_APPLICANT", "tool": "applicant.create",
                "arguments": {"full_name": "New Person"}},
        claims={"sub": "alice", "scope": " ".join(FOS)})

    created = done["result"]["applicant"]["applicant_id"]
    assert repo.has_access("alice", "APPLICANT", created)
    assert not repo.has_access("bob", "APPLICANT", created)


def test_the_deprecated_create_route_checks_ownership(app_client, repo,
                                                      make_token):
    case = open_case(app_client, headers(make_token, "alice", FOS))

    response = app_client.post(
        "/api/v1/applicant-agent/applications",
        headers=headers(make_token, "bob", FOS),
        json={"applicant_id": case["applicant_id"], "product": "PERSONAL_LOAN"})

    assert response.status_code == 403


# ==========================================================================
# 5. THE DEVELOPMENT IDENTITY PROVIDER
# ==========================================================================

@pytest.mark.parametrize("flag,environment,expected", [
    ("true", "development", True),
    ("true", "local", True),
    ("true", "production", False),
    ("true", None, False),            # unset environment counts as production
    ("false", "development", False),
    (None, "development", False),     # no explicit opt-in
])
def test_the_dev_idp_needs_an_opt_in_and_a_dev_environment(
        monkeypatch, flag, environment, expected):
    from app.security.dev_idp import dev_idp_enabled

    for name, value in (("LOS_DEV_IDP_ENABLED", flag),
                        ("ENVIRONMENT", environment)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    assert dev_idp_enabled() is expected


def test_production_mode_mounts_no_token_issuer(monkeypatch):
    import main

    monkeypatch.setenv("LOS_DEV_IDP_ENABLED", "true")
    monkeypatch.setenv("ENVIRONMENT", "production")
    app = FastAPI()

    assert main.mount_dev_identity_provider(app) is False
    client = TestClient(app)
    assert client.post("/api/v1/auth/login", json={}).status_code == 404
    assert client.get("/.well-known/jwks.json").status_code == 404


def test_development_mode_mounts_it(monkeypatch):
    import main

    monkeypatch.setenv("LOS_DEV_IDP_ENABLED", "true")
    monkeypatch.setenv("ENVIRONMENT", "development")
    app = FastAPI()

    assert main.mount_dev_identity_provider(app) is True
    assert TestClient(app).get("/.well-known/jwks.json").status_code == 200


# ==========================================================================
# 6. SENSITIVE DATA
# ==========================================================================

@pytest.mark.parametrize("typed,hidden", [
    ("is my PAN ABCDE1234F verified?", "ABCDE1234F"),
    ("aadhaar 1234 5678 9012 status", "1234 5678 9012"),
    ("call me on 9876543210", "9876543210"),
    ("mail a.person@example.com", "a.person@example.com"),
    ("token eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl", "eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl"),
])
def test_the_audit_excerpt_masks_identifiers(tmp_path, monkeypatch, typed, hidden):
    from app.agents.applicant import audit

    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("APPLICANT_AGENT_AUDIT_PATH", str(path))
    monkeypatch.setenv("APPLICANT_AGENT_AUDIT_ENABLED", "true")

    audit.record(request_id="r1", subject="alice", applicant_id="APP-1",
                 case_id="CASE-1", intent="DOCUMENT_VERIFICATION", tools=[],
                 status="OK", message=typed)

    line = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert hidden not in line["message_excerpt"]
    # What makes the audit useful is kept.
    for key in ("request_id", "subject", "case_id", "intent", "status"):
        assert line[key]


def test_no_bearer_token_reaches_the_logs(app_client, repo, make_token, caplog):
    import logging

    alice_token = make_token(subject="alice", scopes=FOS)
    auth = {"Authorization": f"Bearer {alice_token}"}
    case = open_case(app_client, auth)

    with caplog.at_level(logging.DEBUG):
        ask(app_client, auth, case)
        ask(app_client, headers(make_token, "bob", FOS), case)

    assert alice_token not in caplog.text
    assert alice_token.split(".")[2] not in caplog.text
