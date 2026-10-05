"""
QUERIES AND DEVIATIONS over the real HTTP endpoints -- the frontend's Raise Query
button and the Copilot's RAISE_QUERY action share one service
(app/agents/los/queries.py) and one vocabulary (app/config/queries.yaml).

    the chat OFFERS the structured action; nothing is created until it is posted
    posting the action creates the query, persisted, on the timeline, idempotent
    an open query blocks the gate out of the stage (configured, UNCONFIRMED)
    the lifecycle is the configured one; RESOLVED needs the reviewer-side scope
    a deviation is never approved by its raiser, nor without the authority
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import config as agent_config
from app.agents.applicant.copilot.capabilities import gates
from app.agents.los import queries
from app.store import get_repository, set_repository
from app.store.models import (Applicant, Application, ApplicationStatus, CaseFinding, Document,
                              DocumentStatus, FindingKind)
from app.store.sqlite_repo import SQLiteRepository

APP, CASE = "APP-QRY00000001", "CASE-QRY-000001"
OTHER_APP, OTHER_CASE = "APP-QRY00000002", "CASE-QRY-000002"
OWNER, STRANGER = "qry-owner", "qry-stranger"
DOC = f"{CASE}:{APP}:pan.jpg"
FOREIGN_DOC = f"{OTHER_CASE}:{OTHER_APP}:pan.jpg"
FOS = ["read_applicant", "read_application", "read_documents", "read_verification",
       "read_pending_items", "read_next_action", "upload_document"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    agent_config.reload()
    queries.reload()
    repository = SQLiteRepository(tmp_path / "queries.sqlite3")
    repository.initialise()
    set_repository(repository)
    for app_id, case_id in ((APP, CASE), (OTHER_APP, OTHER_CASE)):
        repository.save_applicant(Applicant(applicant_id=app_id, full_name="Query Person"))
        repository.save_application(Application(case_id=case_id, applicant_id=app_id,
                                                status=ApplicationStatus.BASIC_DOCUMENT_VERIFICATION,
                                                product="PERSONAL_LOAN"))
    for doc_id, case_id, app_id in ((DOC, CASE, APP), (FOREIGN_DOC, OTHER_CASE, OTHER_APP)):
        repository.save_document(Document(document_id=doc_id, case_id=case_id, applicant_id=app_id,
                                          document_type="PAN", party_id=app_id, status=DocumentStatus.REVIEW,
                                          source_id="pan.jpg", verification_status="REVIEW",
                                          reason_codes=["PAN_NAME_MISMATCH"]))
    repository.grant_access(OWNER, "APPLICANT", APP)
    repository.grant_access(STRANGER, "APPLICANT", OTHER_APP)
    yield repository
    set_repository(None)
    agent_config.reload()
    queries.reload()


def _client(make_token, scopes, subject=OWNER) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers["Authorization"] = "Bearer " + make_token(subject=subject, scopes=scopes)
    return c


def _ask(client, message):
    return client.post("/api/v1/fos/copilot", json={"applicant_id": APP, "case_id": CASE,
                                                    "action": "CUSTOM_QUERY", "message": message})


def test_the_chat_offers_the_structured_action_and_creates_nothing(repo, make_token):
    body = _ask(_client(make_token, FOS + ["documents:write"]), "query raise kar do").json()
    assert body["response_type"] == "QUERY_PROPOSED"
    action = body["raise_query_action"]
    assert action["action_id"] == "RAISE_QUERY" and action["available"] and action["requires_confirmation"]
    assert action["target_type"] == "DOCUMENT" and action["target_id"] == DOC
    assert action["endpoint"] == f"/api/v1/los/cases/{CASE}/queries" and action["body"]["text"]
    assert action in body["actions"]                                   # renderable without parsing text
    assert queries.list_queries(CASE) == []                            # offered, not created


def test_posting_the_offered_action_creates_one_query_and_blocks_the_gate(repo, make_token):
    client = _client(make_token, FOS + ["documents:write"])
    action = _ask(client, "raise a query for this").json()["raise_query_action"]
    first = client.post(action["endpoint"], json=action["body"])
    assert first.status_code == 201, first.text
    query = first.json()["query"]
    assert query["status"] == "OPEN" and query["result"] == "CREATED" and query["raised_by"] == OWNER
    assert query["query_id"].startswith("QRY-")
    assert [b["id"] for b in first.json()["gate"]["blockers"]].count("OPEN_QUERIES") == 1
    # a second click is the same query, never a duplicate
    again = client.post(action["endpoint"], json=action["body"]).json()["query"]
    assert again["result"] == "EXISTING" and again["query_id"] == query["query_id"]
    assert len(queries.list_queries(CASE)) == 1
    assert any(e.event_type == "QUERY_RAISED" for e in get_repository().get_case_timeline(CASE))
    # and the chat now says one is open instead of offering another
    said = _ask(client, "query raise kar do").json()
    assert said["response_type"] == "QUERY_EXISTS" and query["query_id"] in said["answer"]


def test_the_lifecycle_is_the_configured_one_and_resolving_needs_the_reviewer(repo, make_token):
    officer = _client(make_token, FOS + ["documents:write"])
    created = officer.post(f"/api/v1/los/cases/{CASE}/queries", json={
        "target_type": "DOCUMENT", "target_id": DOC, "query_type": "DOCUMENT_DISCREPANCY",
        "text": "Please clarify the name difference between the PAN and the bank statement."}).json()["query"]
    url = f"/api/v1/los/cases/{CASE}/queries/{created['query_id']}/status"
    denied = officer.post(url, json={"status": "RESOLVED"})
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "QUERY_NOT_PERMITTED"
    assert officer.post(url, json={"status": "RESPONDED", "note": "Updated statement uploaded."}).status_code == 200
    illegal = officer.post(url, json={"status": "OPEN"})
    assert illegal.status_code == 409 and illegal.json()["detail"]["code"] == "INVALID_QUERY_TRANSITION"
    reviewer = _client(make_token, ["los.stage:write"] + FOS)
    resolved = reviewer.post(url, json={"status": "RESOLVED", "note": "Name verified."})
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["query"]["status"] == "RESOLVED"
    assert [h["status"] for h in body["query"]["history"]] == ["OPEN", "RESPONDED", "RESOLVED"]
    assert body["open_items"]["queries"] == []
    assert "OPEN_QUERIES" not in [b["id"] for b in body["gate"]["blockers"]]
    # RESOLVED IS NOT A STAGE PASS: the gate still reads the configured criteria
    assert body["gate"]["status"] != "PASS"


@pytest.mark.parametrize("payload, code, status", [
    ({"target_type": "SPACESHIP", "query_type": "CLARIFICATION", "text": "x"}, "INVALID_TARGET_TYPE", 422),
    ({"target_type": "CASE", "query_type": "MAKE_IT_PASS", "text": "x"}, "INVALID_QUERY_TYPE", 422),
    ({"target_type": "CASE", "query_type": "CLARIFICATION", "text": "   "}, "QUERY_TEXT_REQUIRED", 422),
    ({"target_type": "DOCUMENT", "target_id": FOREIGN_DOC, "query_type": "CLARIFICATION", "text": "x"},
     "TARGET_NOT_FOUND", 404),
])
def test_a_malformed_or_foreign_target_is_refused(repo, make_token, payload, code, status):
    response = _client(make_token, FOS + ["documents:write"]).post(f"/api/v1/los/cases/{CASE}/queries", json=payload)
    assert response.status_code == status and response.json()["detail"]["code"] == code
    assert queries.list_queries(CASE) == []


def test_another_customers_case_is_refused_before_anything_is_read(repo, make_token):
    stranger = _client(make_token, FOS + ["documents:write"], subject=STRANGER)
    assert stranger.get(f"/api/v1/los/cases/{CASE}/queries").status_code == 403
    assert stranger.post(f"/api/v1/los/cases/{CASE}/queries", json={
        "target_type": "CASE", "query_type": "CLARIFICATION", "text": "x"}).status_code == 403
    assert queries.list_queries(CASE) == []


def test_raising_needs_the_query_permission(repo, make_token):
    read_only = _client(make_token, ["read_applicant", "read_documents"])
    response = read_only.post(f"/api/v1/los/cases/{CASE}/queries", json={
        "target_type": "CASE", "query_type": "CLARIFICATION", "text": "x"})
    assert response.status_code == 403


def _deviation(repo, raised_by="credit-analyst"):
    repo.save_finding(CaseFinding(
        finding_id="F-DEV-1", case_id=CASE, finding_kind=FindingKind.DEVIATION, status="PENDING_APPROVAL",
        source_type="DEVIATION", source_id="DEV-1", stage="FOS",
        payload={"record": "DEVIATION", "rule": "CONFIGURED_RULE", "raised_by": raised_by, "history": []}))


def test_a_pending_deviation_blocks_and_only_the_authority_other_than_the_raiser_decides(repo, make_token):
    _deviation(repo, raised_by=OWNER)
    url = f"/api/v1/los/cases/{CASE}/deviations/DEV-1/decision"
    gate = gates.evaluate("FOS", gates.read_sources(CASE, results={}, repository=repo))
    assert "PENDING_DEVIATIONS" in [b["id"] for b in gate["blockers"]]
    no_authority = _client(make_token, ["los.stage:write"] + FOS)
    assert no_authority.post(url, json={"decision": "APPROVED", "justification": "ok"}).json()["detail"]["code"] \
        == "DEVIATION_APPROVAL_NOT_PERMITTED"
    self_approval = _client(make_token, ["los.deviation:approve"] + FOS)          # the raiser, with authority
    assert self_approval.post(url, json={"decision": "APPROVED", "justification": "ok"}).json()["detail"]["code"] \
        == "SELF_APPROVAL_FORBIDDEN"
    approver = _client(make_token, ["los.deviation:approve", "los.write"] + FOS, subject="credit-head")
    missing = approver.post(url, json={"decision": "APPROVED"})
    assert missing.json()["detail"]["code"] == "JUSTIFICATION_REQUIRED"
    done = approver.post(url, json={"decision": "APPROVED", "justification": "Within delegated authority."})
    assert done.status_code == 200, done.text
    assert done.json()["deviation"]["status"] == "APPROVED"
    assert done.json()["open_items"]["deviations"] == []


def test_no_deviation_rule_is_configured_so_it_is_a_configuration_gap(repo, make_token):
    body = _client(make_token, FOS).get(f"/api/v1/los/cases/{CASE}/queries").json()
    assert body["deviation_rules"] == "CONFIGURATION_GAP" and body["deviations"] == []
    said = _ask(_client(make_token, FOS), "koi deviation hai?").json()
    assert said["response_type"] == "CASE_QUERIES" and said["answer"].startswith("No deviation")


def test_a_review_document_card_carries_the_raise_query_button(repo, make_token):
    body = _ask(_client(make_token, FOS + ["documents:write"]), "is my PAN verified?").json()
    cards = (body.get("verification") or {}).get("documents") or []
    buttons = [a for c in cards for a in c.get("actions") or [] if a.get("action_id") == "RAISE_QUERY"]
    assert buttons and buttons[0]["target_id"] == DOC and buttons[0]["requires_confirmation"]


def test_queries_are_not_explained_as_findings(repo, make_token):
    from app.agents.applicant import case_memory_facts

    queries.raise_query(CASE, target_type="CASE", query_type="CLARIFICATION", text="Please clarify income.",
                        actor=OWNER, scopes={"documents:write"})
    kinds = [f["finding_kind"] for f in case_memory_facts.case_memory(CASE)["findings"]]
    assert "QUERY" not in kinds
