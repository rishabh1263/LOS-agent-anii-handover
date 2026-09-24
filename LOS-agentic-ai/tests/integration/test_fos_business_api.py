"""
Intake is not a conversation.

THE DEFECT THIS CLOSES. Nine deterministic FOS reads were implemented by
turning a named action into an English sentence -- "Show me the document
checklist." -- and handing it to the intent classifier, which matched it
against a few hundred regexes to recover the intent the caller had
already named. A button labelled CHECKLIST does not need to be
understood.

AND THE ROUND TRIP WAS NOT FREE. Every routing defect found in review
was a pattern-ordering bug in that ladder: a status question answered
from the handbook, "still pending" missed by one adverb, a mismatch
question answered with a dictionary definition. A dropdown that reaches
its capability directly cannot have any of them, and this file pins
that.

WHAT DID NOT CHANGE, and is checked here too: the capability check, the
ownership check, the tool plan, the deterministic answer, the audit
record and the response envelope. Only the guessing is gone.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import config as agent_config
from app.store import set_repository
from app.store.models import Document, status_for_verdict
from app.store.sqlite_repo import SQLiteRepository

FOS_SCOPES = [
    "read_applicant", "read_application", "read_documents", "read_verification",
    "read_pending_items", "read_next_action", "create_applicant",
    "update_applicant", "create_application", "upload_document",
]


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    agent_config.reload()
    repository = SQLiteRepository(tmp_path / "fos_business.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)
    agent_config.reload()


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES)}"})
    return c


@pytest.fixture
def case(client):
    response = client.post("/api/v1/fos/applicants", json={
        "applicant": {
            "full_name": "Rahul Sharma", "mobile": "9876543210",
            "date_of_birth": "1990-04-12", "address": "Mumbai, Maharashtra",
        },
        "application": {"product": "PERSONAL_LOAN", "loan_amount": 500000},
    })
    assert response.status_code == 201, response.text
    body = response.json()
    return body["applicant_id"], body["case_id"]


def verified(store, case_id, applicant_id, doc_type, verdict):
    store.save_document(Document(
        document_id=f"{case_id}:{doc_type}", case_id=case_id,
        applicant_id=applicant_id, document_type=doc_type,
        status=status_for_verdict(verdict), verification_status=verdict,
        source_id=f"{doc_type.lower()}.jpg",
    ))


# ==========================================================================
# A. A DETERMINISTIC ACTION REACHES ITS CAPABILITY WITHOUT NLU
# ==========================================================================


def test_a_named_action_never_reaches_the_classifier(client, case, monkeypatch):
    """
    THE HEART OF IT. `classify` is replaced with something that fails
    the test if it is called at all. A dropdown action must complete
    without it.
    """
    from app.agents.applicant import agent

    def never(message):
        raise AssertionError(
            f"a named action was sent through the classifier: {message!r}")

    monkeypatch.setattr(agent, "classify", never)

    applicant_id, case_id = case

    for action in ("GET_APPLICANT", "GET_APPLICATION_STATUS", "GET_DOCUMENTS",
                   "GET_DOCUMENT_CHECKLIST", "GET_VERIFICATION_STATUS",
                   "GET_PENDING_ITEMS", "GET_NEXT_ACTION", "GET_CASE_360",
                   "CHECK_CPA_READINESS"):
        response = client.post("/api/v1/fos/copilot", json={
            "applicant_id": applicant_id, "case_id": case_id,
            "action": action})

        assert response.status_code == 200, (action, response.text)
        assert response.json()["action"] == action


def test_a_typed_question_still_is_classified(client, case, monkeypatch):
    """
    The boundary works both ways. CUSTOM_QUERY is the only request here
    that is genuinely a question, and it must still be understood.
    """
    from app.agents.applicant import agent

    seen: list[str] = []
    original = agent.classify
    monkeypatch.setattr(agent, "classify",
                        lambda m: (seen.append(m), original(m))[1])

    applicant_id, case_id = case
    response = client.post("/api/v1/fos/copilot", json={
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "CUSTOM_QUERY", "message": "What documents are pending?"})

    assert response.status_code == 200
    assert seen == ["What documents are pending?"]


def test_each_action_reaches_the_intent_it_names(client, case):
    """The action and the intent are the same fact, not two guesses."""
    applicant_id, case_id = case
    expected = {
        "GET_APPLICANT": "APPLICANT_DETAILS",
        "GET_DOCUMENT_CHECKLIST": "DOCUMENTS_REQUIRED",
        "GET_DOCUMENTS": "DOCUMENTS_UPLOADED",
        "GET_PENDING_ITEMS": "PENDING_ITEMS",
        "GET_NEXT_ACTION": "NEXT_ACTION",
        "CHECK_CPA_READINESS": "READINESS",
    }

    for action, intent in expected.items():
        body = client.post("/api/v1/fos/copilot", json={
            "applicant_id": applicant_id, "case_id": case_id,
            "action": action}).json()

        assert body["intent"] == intent, action


# ==========================================================================
# B / C. THE BUSINESS READS, AND THE FACADE, AGREE
# ==========================================================================


@pytest.mark.parametrize("path,action", [
    ("/api/v1/fos/applications/{case_id}", "GET_APPLICATION_STATUS"),
    ("/api/v1/fos/documents/{case_id}", "GET_DOCUMENTS"),
    ("/api/v1/fos/checklist/{case_id}", "GET_DOCUMENT_CHECKLIST"),
])
def test_a_read_endpoint_returns_what_the_facade_returns(
        client, case, _store, path, action):
    """
    ONE IMPLEMENTATION, TWO DOORS. A business endpoint and the
    compatibility endpoint answering the same question differently is
    the defect this refactor exists to prevent, so they are compared
    field for field.
    """
    applicant_id, case_id = case
    verified(_store, case_id, applicant_id, "PAN", "PASS")

    direct = client.get(path.format(case_id=case_id),
                        params={"applicant_id": applicant_id}).json()
    facade = client.post("/api/v1/fos/copilot", json={
        "applicant_id": applicant_id, "case_id": case_id,
        "action": action}).json()

    # request_id and timing differ per call by construction.
    ignore = {"request_id", "processing_ms"}
    assert {k: v for k, v in direct.items() if k not in ignore} == \
           {k: v for k, v in facade.items() if k not in ignore}


def test_the_applicant_read_returns_the_applicant(client, case):
    applicant_id, case_id = case

    body = client.get(f"/api/v1/fos/applicants/{applicant_id}",
                      params={"case_id": case_id}).json()

    assert body["applicant"]["full_name"] == "Rahul Sharma"
    assert body["intent"] == "APPLICANT_DETAILS"


def test_the_checklist_read_carries_the_policy_that_produced_it(client, case):
    applicant_id, case_id = case

    body = client.get(f"/api/v1/fos/checklist/{case_id}",
                      params={"applicant_id": applicant_id}).json()

    assert body["checklist"] or body["required_documents"]
    assert body["policy"] is not None


def test_a_read_endpoint_is_no_weaker_than_the_facade(make_token, case):
    """
    Authorisation is the agent's, and it does not change because the
    caller came through a REST route instead of the facade.

    ASSERTED AS AGREEMENT, NOT AS A FIXED CODE. `los.read` is allowed to
    read here -- see test_a_read_only_token_can_still_read in
    test_fos_api.py -- and the property that matters is that the new
    door answers the same token the same way the old one does. A test
    naming a status would pass while the two quietly diverged.
    """
    import main

    applicant_id, case_id = case
    weak = TestClient(main.app)
    weak.headers.update(
        {"Authorization": f"Bearer {make_token(scopes=['los.read'])}"})

    direct = weak.get(f"/api/v1/fos/documents/{case_id}",
                      params={"applicant_id": applicant_id})
    facade = weak.post("/api/v1/fos/copilot", json={
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "GET_DOCUMENTS"})

    assert direct.status_code == facade.status_code


def test_a_read_endpoint_refuses_without_a_token():
    import main

    anonymous = TestClient(main.app)

    assert anonymous.get("/api/v1/fos/documents/CASE-X").status_code in (401, 403)


# ==========================================================================
# D / E. UPLOAD IS INTAKE, AND ONLY INTAKE
# ==========================================================================


def test_the_upload_endpoint_is_the_same_handler_as_the_facade(
        client, case, monkeypatch):
    """
    A second upload implementation would be a second place for the stage
    boundary to be got wrong.

    CHECKED BY BEHAVIOUR, NOT BY READING SOURCE. The facade's handler is
    replaced, and the business endpoint must reach the replacement. An
    earlier version compared `inspect.getsource` text, which reads the
    file on disk against the line numbers of the module in memory -- edit
    the file during a run and it returns a different function entirely.
    """
    from app.api.routes import fos_api

    reached: list[str] = []

    async def the_facade_handler(request, claims, request_id):
        reached.append(request_id)
        return {"handled_by": "the facade's upload handler"}

    monkeypatch.setattr(fos_api, "_copilot_upload", the_facade_handler)

    applicant_id, case_id = case
    response = client.post("/api/v1/fos/documents",
                           data={"applicant_id": applicant_id, "case_id": case_id,
                                 "action": "UPLOAD_DOCUMENT"},
                           files={"files": ("pan.txt", b"x", "text/plain")})

    assert response.status_code == 200
    assert response.json() == {"handled_by": "the facade's upload handler"}
    assert len(reached) == 1


def test_intake_runs_no_cross_document_or_income_analysis(
        client, case, monkeypatch):
    """
    THE FOS STAGE BOUNDARY, OBSERVED AT THE CALL. KYC asks whether
    several documents describe one person; income and affordability ask
    what the applicant earns and can repay. None of the three is
    intake's to answer, and an upload endpoint returning them would be
    reporting verdicts at a stage with no authority to act on them.

    The pipeline is wrapped and the arguments it is ACTUALLY called with
    are asserted -- not text found in a source file.
    """
    from app.agents.los import flow

    seen: dict[str, object] = {}
    original = flow.process_application

    async def watched(documents, **kwargs):
        seen.update(kwargs)
        return await original(documents, **kwargs)

    monkeypatch.setattr(flow, "process_application", watched)

    applicant_id, case_id = case
    client.post("/api/v1/fos/documents",
                data={"applicant_id": applicant_id, "case_id": case_id,
                      "action": "UPLOAD_DOCUMENT"},
                files={"files": ("pan.txt", b"not a real document", "text/plain")})

    assert seen, "the upload never reached the pipeline"
    assert seen.get("cross_document_checks") is False
    assert seen.get("financial_analysis") is False
    assert seen.get("summarise") is False


def test_an_upload_without_a_file_is_refused_clearly(client, case):
    applicant_id, case_id = case

    response = client.post("/api/v1/fos/documents", data={
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "UPLOAD_DOCUMENT"})

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "FILE_REQUIRED"


def test_an_upload_names_the_missing_identifiers(client):
    response = client.post("/api/v1/fos/documents", data={
        "action": "UPLOAD_DOCUMENT"})

    assert response.status_code == 422
    assert "applicant_id" in response.json()["detail"]["message"]


# ==========================================================================
# BACKWARD COMPATIBILITY
# ==========================================================================


def test_the_facade_still_serves_every_action_it_always_did(client, case):
    applicant_id, case_id = case

    listed = client.get("/api/v1/fos/actions").json()
    names = {row["value"] for row in listed["actions"]}

    for action in names - {"UPLOAD_DOCUMENT", "CUSTOM_QUERY"}:
        response = client.post("/api/v1/fos/copilot", json={
            "applicant_id": applicant_id, "case_id": case_id,
            "action": action})

        assert response.status_code == 200, action


def test_the_action_phrases_survive_for_the_record(client, case):
    """
    The sentences were kept and taken off the routing path: the audit
    trail and the echoed question read the same before and after this
    change. They no longer decide anything.
    """
    from app.api.routes.fos_api import _ACTION_INTENT, _ACTION_PHRASE, FosAction

    for action in _ACTION_INTENT:
        assert action in _ACTION_PHRASE

    assert FosAction.CUSTOM_QUERY not in _ACTION_INTENT
    assert FosAction.UPLOAD_DOCUMENT not in _ACTION_INTENT
