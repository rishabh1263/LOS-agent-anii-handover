"""
The Universal Copilot's first real question: "why is this case in review?"

WHAT MAKES THIS ANSWERABLE. Until case memory existed, it was not. The
live store says a case IS in review; the reasons were computed by the
pipeline, returned once and lost. They are now recorded, and this reads
them back.

THE FAILURE THIS MUST NOT HAVE. A confident explanation assembled from
nothing. "Probably the PAN failed" is indistinguishable from a real
answer to the officer reading it, and it is the one output that cannot
be checked. So when no findings were recorded, the answer says exactly
that -- and there is no model anywhere on this path to decide otherwise.

AND THE OTHER ONE. Case memory read for a case the caller has not been
cleared for. Ownership is checked before anything is read, and several
tests below exist only to keep that ordering.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.store import set_repository
from app.store.models import (
    Applicant,
    Application,
    CaseDecision,
    CaseFinding,
    FindingKind,
)
from app.store.sqlite_repo import SQLiteRepository

ENDPOINT = "/api/v1/copilot/query"


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "copilot.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


@pytest.fixture(autouse=True)
def memory_on():
    """Case memory is off by default; these questions need it on."""
    from unittest.mock import patch

    with patch("app.agents.los.config.case_memory_enabled", return_value=True):
        yield


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    # `los.read` satisfies every read scope -- see permissions.read_all_scope
    # in applicant_agent.yaml. The narrower-scope case is its own test below.
    c.headers.update(
        {"Authorization": f"Bearer {make_token(scopes=['los.read'])}"})
    return c


def seed(repo, case_id="CASE-1", applicant_id="APP-1", party_id="APP-1",
         codes=("DOCUMENT_TYPE_MISMATCH",), kind=FindingKind.VERIFICATION,
         decision="REVIEW", status="PARTIAL"):
    repo.save_applicant(Applicant(applicant_id=applicant_id))
    repo.save_application(Application(case_id=case_id,
                                      applicant_id=applicant_id))
    if codes:
        repo.save_finding(CaseFinding(
            finding_id=f"f-{case_id}-{party_id}", case_id=case_id,
            party_id=party_id, finding_kind=kind, status="FAIL",
            reason_codes=list(codes), source_id="pan.jpg",
            document_id=f"{case_id}:{party_id}:pan.jpg",
            content_hash=f"h-{case_id}-{party_id}"))
    if decision:
        repo.save_decision(CaseDecision(
            decision_id=f"d-{case_id}", case_id=case_id, decision=decision,
            next_action="MANUAL_REVIEW", status=status))


def ask(client, message="Why is this case in review?", **body):
    body.setdefault("case_id", "CASE-1")
    body.setdefault("applicant_id", "APP-1")
    return client.post(ENDPOINT, json={"message": message, **body})


# ==========================================================================
# 1-2. CASE_FACT REACHES CASE MEMORY, AND THE ANSWER IS GROUNDED
# ==========================================================================


def test_the_question_routes_to_case_history(client, repo):
    seed(repo)

    body = ask(client).json()

    assert body["intent"] == "CASE_HISTORY"
    assert body["category"] == "CASE_ONLY"


def test_the_answer_names_the_recorded_finding(client, repo):
    """
    THE POINT OF THE WHOLE SLICE. The reason in the answer is the reason
    code the pipeline wrote down.
    """
    seed(repo, codes=("DOCUMENT_TYPE_MISMATCH",))

    body = ask(client).json()

    assert "not the type it was declared as" in body["answer"]
    assert "REVIEW" in body["answer"]


def test_several_findings_are_all_reported(client, repo):
    seed(repo, codes=("DOCUMENT_TYPE_MISMATCH", "NAME_MISMATCH",
                      "INSUFFICIENT_SOURCES"))

    answer = ask(client).json()["answer"]

    assert "not the type it was declared as" in answer
    assert "name differs" in answer
    assert "only one document to compare" in answer


def test_the_sources_cite_the_findings(client, repo):
    seed(repo, codes=("NAME_MISMATCH",))

    sources = ask(client).json()["sources"]

    finding = next(s for s in sources if s["kind"] == "case_finding")
    assert finding["reason_code"] == "NAME_MISMATCH"
    assert finding["finding_kind"] == "VERIFICATION"
    assert finding["source_id"] == "pan.jpg"
    assert any(s["kind"] == "case_decision" for s in sources)


def test_the_answer_is_deterministic(client, repo):
    """
    No model on this path at any setting. STRUCTURED is the existing
    vocabulary for "the store answered it" -- as opposed to KNOWLEDGE,
    MIXED or a model.
    """
    from app.agents.applicant.routing import ResponseSource

    seed(repo)

    body = ask(client).json()

    assert body["response_source"] == ResponseSource.STRUCTURED.value


# ==========================================================================
# 3/12. NOTHING RECORDED -> SAY SO, NEVER INVENT
# ==========================================================================


def test_no_findings_produces_an_explicit_insufficient_answer(client, repo):
    seed(repo, codes=(), decision=None)

    body = ask(client).json()

    assert "No findings have been recorded" in body["answer"]
    assert body["sources"] == []


def test_nothing_is_invented_when_there_is_nothing_to_say(client, repo):
    """
    A confident explanation assembled from nothing is the one output a
    reviewer cannot check.
    """
    seed(repo, codes=(), decision=None)

    answer = ask(client).json()["answer"].lower()

    for invented in ("probably", "likely", "appears to", "may have",
                     "mismatch", "failed"):
        assert invented not in answer


def test_a_decision_with_no_findings_says_exactly_that(client, repo):
    seed(repo, codes=(), decision="REVIEW")

    answer = ask(client).json()["answer"]

    assert "No individual findings were recorded" in answer


def test_case_memory_off_reports_nothing_rather_than_guessing(client, repo):
    from unittest.mock import patch

    seed(repo)

    with patch("app.agents.los.config.case_memory_enabled",
               return_value=False):
        answer = ask(client).json()["answer"]

    assert "No findings have been recorded" in answer


# ==========================================================================
# 4-6. ISOLATION
# ==========================================================================


def test_a_party_scoped_question_sees_only_that_party(client, repo):
    seed(repo, party_id="APP-1", codes=("NAME_MISMATCH",))
    repo.save_finding(CaseFinding(
        finding_id="f-co", case_id="CASE-1", party_id="COAPP-9",
        finding_kind=FindingKind.KYC, status="FAIL",
        reason_codes=["DOB_MISMATCH"], content_hash="h-co"))

    primary = ask(client, party_id="APP-1").json()
    co = ask(client, party_id="COAPP-9").json()

    assert "name differs" in primary["answer"]
    assert "date of birth differs" not in primary["answer"]
    assert "date of birth differs" in co["answer"]


def test_one_case_never_answers_with_anothers_findings(client, repo):
    seed(repo, case_id="CASE-1", codes=("NAME_MISMATCH",))
    seed(repo, case_id="CASE-2", applicant_id="APP-1", party_id="APP-1",
         codes=("PAN_MISMATCH",))

    first = ask(client, case_id="CASE-1").json()["answer"]
    second = ask(client, case_id="CASE-2").json()["answer"]

    assert "name differs" in first
    assert "PAN differs" not in first
    assert "PAN differs" in second


def test_another_applicants_case_is_refused(client, repo):
    """
    OWNERSHIP BEFORE RETRIEVAL. The case exists and has findings; it
    belongs to somebody else, so nothing about it comes back.
    """
    seed(repo, case_id="CASE-OTHER", applicant_id="APP-2",
         party_id="APP-2", codes=("NAME_MISMATCH",))
    repo.save_applicant(Applicant(applicant_id="APP-1"))

    response = ask(client, case_id="CASE-OTHER", applicant_id="APP-1")

    assert response.status_code == 403
    assert "name differs" not in response.text


# ==========================================================================
# 7. THE ORDER OF THE CHECKS
# ==========================================================================


def test_ownership_is_checked_before_case_memory_is_read(client, repo):
    """
    Not merely that the answer is refused -- that the read never
    happened. A refusal issued after the data was fetched is a refusal
    that already touched it.
    """
    from unittest.mock import patch

    seed(repo, case_id="CASE-OTHER", applicant_id="APP-2", party_id="APP-2")
    repo.save_applicant(Applicant(applicant_id="APP-1"))

    with patch("app.agents.applicant.case_memory_facts.case_memory") as read:
        response = ask(client, case_id="CASE-OTHER", applicant_id="APP-1")

    assert response.status_code == 403
    read.assert_not_called()


def test_case_memory_needs_the_verification_scope(make_token, repo):
    """
    Recorded findings are verification output. A caller who may read
    documents but not verification may not read the reasons behind a
    verdict either.
    """
    import main

    seed(repo)
    client = TestClient(main.app)
    client.headers.update(
        {"Authorization": f"Bearer {make_token(scopes=['read_documents'])}"})

    response = client.post(ENDPOINT, json={
        "message": "Why is this case in review?",
        "case_id": "CASE-1", "applicant_id": "APP-1"})

    assert response.status_code == 403


def test_the_verification_scope_is_sufficient(make_token, repo):
    import main

    seed(repo)
    client = TestClient(main.app)
    client.headers.update(
        {"Authorization": f"Bearer {make_token(scopes=['read_verification'])}"})

    response = client.post(ENDPOINT, json={
        "message": "Why is this case in review?",
        "case_id": "CASE-1", "applicant_id": "APP-1"})

    assert response.status_code == 200
    assert response.json()["intent"] == "CASE_HISTORY"


# ==========================================================================
# 10. AUTHENTICATION
# ==========================================================================


def test_the_endpoint_requires_a_token(repo):
    import main

    seed(repo)

    response = TestClient(main.app).post(ENDPOINT, json={
        "message": "Why is this case in review?", "case_id": "CASE-1"})

    assert response.status_code in (401, 403)


def test_a_case_id_is_required(client, repo):
    seed(repo)

    response = client.post(ENDPOINT, json={"message": "Why is this in review?"})

    assert response.status_code == 422


# ==========================================================================
# 11. NO LEAKAGE
# ==========================================================================


def test_the_response_carries_only_the_published_keys(client, repo):
    seed(repo)

    body = ask(client).json()

    assert set(body) == {
        "request_id", "case_id", "applicant_id", "party_id",
        "conversation_id", "category", "intent", "answer",
        "response_source", "sources", "tool_invoked", "errors",
        # Which LOS desk this answer was scoped to, and where that came
        # from. `status` carries CAPABILITY_UNAVAILABLE for a stage with
        # nothing registered; it is null on an answered question.
        "stage", "stage_resolution", "status"}


def test_no_payload_or_internals_reach_the_caller(client, repo):
    seed(repo, codes=("NAME_MISMATCH",))

    blob = json.dumps(ask(client).json())

    for forbidden in ("payload", "content_hash", "prompt", "Traceback",
                      "bbox", "raw_text", "ocr", "tokens", "file_path",
                      "samples/", "trace", "claims"):
        assert forbidden not in blob


def test_the_conversation_id_is_echoed_not_stored(client, repo):
    seed(repo)

    body = ask(client, conversation_id="conv-42").json()

    assert body["conversation_id"] == "conv-42"


# ==========================================================================
# 8-9/13-14. NOTHING EXISTING MOVED
# ==========================================================================


def test_the_fos_surface_is_untouched():
    import main

    paths = set(main.app.openapi()["paths"])

    for path in ("/api/v1/fos/copilot", "/api/v1/fos/applicants",
                 "/api/v1/fos/actions", "/api/v1/fos/config",
                 "/api/v1/fos/tools"):
        assert path in paths


def test_the_applicant_agent_surface_is_untouched():
    import main

    paths = set(main.app.openapi()["paths"])

    assert any("applicant-agent" in p for p in paths)


def test_the_routing_categories_are_unchanged():
    from app.agents.applicant.routing import QueryCategory

    assert {c.value for c in QueryCategory} == {
        "CASE_ONLY", "KNOWLEDGE_ONLY", "MIXED", "DOWNSTREAM", "UNSUPPORTED"}


def test_case_history_is_a_case_only_question():
    from app.agents.applicant import routing
    from app.agents.applicant.intents import Intent

    assert routing.category_for(Intent.CASE_HISTORY) is \
        routing.QueryCategory.CASE_ONLY


def test_the_mcp_tool_registry_is_unchanged():
    """No tool was added, removed or renamed for this slice."""
    from app.mcp.contracts import catalogue

    names = {tool["name"] for tool in catalogue()}

    assert "applicant.get" in names
    assert "documents.verification" in names
    assert not any("copilot" in name or "case_memory" in name
                   for name in names)


def test_existing_intents_still_classify_the_same_way():
    from app.agents.applicant.intents import Intent, classify

    assert classify("what documents are pending?").intent is \
        Intent.DOCUMENTS_PENDING
    assert classify("what documents are required for a personal loan?").intent \
        is Intent.FOS_KNOWLEDGE
    assert classify("is the PAN verified?").intent is \
        Intent.DOCUMENT_VERIFICATION
