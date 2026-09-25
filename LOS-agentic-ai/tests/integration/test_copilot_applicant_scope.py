"""
One applicant, several cases, one Copilot.

THE GAP THIS CLOSES. `case_id` was required, so the Copilot could only
ever answer about one case -- and `applications.list`, the only tool
that reads an applicant's whole portfolio, was wired into the dispatcher
but planned by no intent, so it was unreachable. "How many cases does
this applicant have?" had nowhere to go.

THE BOUNDARY THAT DID NOT MOVE. There is no unscoped read in this
service and this adds none: every MCP tool requires a `case_id` or an
`applicant_id`, and `applications.list` takes the applicant's own id.
With a case, `applicant_owns_case` still gates it. Without one, the
scope is the applicant. The caller -> applicant model is unchanged.

CASES ARE COUNTED, NEVER MERGED. Two cases are two answers. A sentence
blending them would report a status no single case holds, and a
reviewer acting on "the application is in review" would not know which.
"""

from __future__ import annotations

import re
import pytest
from fastapi.testclient import TestClient

from app.store import set_repository
from app.store.models import Applicant, Application, ApplicationStatus
from app.store.sqlite_repo import SQLiteRepository

ENDPOINT = "/api/v1/copilot/query"


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "scope.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update(
        {"Authorization": f"Bearer {make_token(scopes=['los.read'])}"})
    return c


def seed(repo, applicant_id="APP-1", cases=(("CASE-1", "DOCUMENT_COLLECTION"),)):
    repo.save_applicant(Applicant(applicant_id=applicant_id))
    for case_id, status in cases:
        repo.save_application(Application(
            case_id=case_id, applicant_id=applicant_id,
            status=ApplicationStatus(status)))


def ask(client, message, **body):
    body.setdefault("applicant_id", "APP-1")
    return client.post(ENDPOINT, json={"message": message, **body})


THREE = (("CASE-1", "DOCUMENT_COLLECTION"),
         ("CASE-2", "READY_FOR_CPA"),
         ("CASE-3", "BASIC_DOCUMENT_VERIFICATION"))


# ==========================================================================
# APPLICANT-SCOPED: NO CASE SUPPLIED
# ==========================================================================


def test_a_question_about_the_applicant_needs_no_case(client, repo):
    """THE DEFECT. `case_id` was required, so this was a 422."""
    seed(repo, cases=THREE)

    response = ask(client, "How many cases does this applicant have?")

    assert response.status_code == 200, response.text


def test_the_answer_counts_the_applicants_cases(client, repo):
    seed(repo, cases=THREE)

    body = ask(client, "How many cases does this applicant have?").json()

    assert body["intent"] == "CASE_PORTFOLIO"
    assert body["answer"].startswith("Across 3 cases:")


def test_every_case_is_named_with_its_own_status(client, repo):
    """
    NEVER MERGED. Each case keeps its own verdict; nothing is blended
    into a single status the application does not have.
    """
    seed(repo, cases=THREE)

    answer = ask(client, "what happened across my previous cases?").json()["answer"]

    # One numbered entry per case, each with its own status -- and no case
    # id in the sentence (the ids are in the structured response; an
    # internal identifier is never part of a natural-language answer).
    # The store's listing order is not insertion order, so each status is
    # matched to SOME numbered entry, and every number 1..3 is used once.
    for case_id, status in THREE:
        readable = status.replace("_", " ").title().replace("Cpa", "CPA")
        assert re.search(rf"\b[1-3]\) application, {readable}\b", answer), answer
        assert case_id not in answer
    for number in (1, 2, 3):
        assert answer.count(f"{number}) application,") == 1


def test_one_case_reads_as_one_case(client, repo):
    seed(repo, cases=(("CASE-ONLY", "DOCUMENT_COLLECTION"),))

    answer = ask(client, "list all my applications").json()["answer"]

    assert answer.startswith("Across 1 case:")


def test_an_applicant_with_nothing_on_file_is_told_so(client, repo):
    """Not an error, and not an invented case."""
    repo.save_applicant(Applicant(applicant_id="APP-1"))

    answer = ask(client, "how many cases do I have?").json()["answer"]

    assert answer == "No applications are on file for this applicant."


def test_the_answer_is_deterministic(client, repo):
    seed(repo, cases=THREE)

    first = ask(client, "my cases").json()["answer"]
    second = ask(client, "my cases").json()["answer"]

    assert first == second


def test_the_portfolio_answer_uses_no_model(client, repo):
    seed(repo, cases=THREE)

    body = ask(client, "my cases").json()

    # `STRUCTURED` is this service's word for "read from stored
    # records", as opposed to a sentence a model wrote.
    assert body["response_source"] == "STRUCTURED"


# ==========================================================================
# ONLY THIS APPLICANT'S CASES
# ==========================================================================


def test_another_applicants_cases_are_not_returned(client, repo):
    """
    NO GLOBAL RETRIEVAL. `applications.list` is scoped to one
    applicant_id at the tool; a second applicant's cases exist in the
    same store and must not appear.
    """
    seed(repo, "APP-1", cases=(("MINE-1", "DOCUMENT_COLLECTION"),))
    seed(repo, "APP-2", cases=(("THEIRS-1", "READY_FOR_CPA"),
                               ("THEIRS-2", "DOCUMENT_COLLECTION")))

    answer = ask(client, "my cases", applicant_id="APP-1").json()["answer"]

    # Scoping is proved by the count and by the other applicant's
    # READY_FOR_CPA case being absent; no id is named in the sentence.
    assert answer.startswith("Across 1 case:")
    assert "Document Collection" in answer
    assert "Ready For CPA" not in answer
    for case_id in ("MINE-1", "THEIRS-1", "THEIRS-2"):
        assert case_id not in answer


def test_each_applicant_sees_only_their_own_count(client, repo):
    seed(repo, "APP-1", cases=(("A-1", "DOCUMENT_COLLECTION"),))
    seed(repo, "APP-2", cases=(("B-1", "DOCUMENT_COLLECTION"),
                               ("B-2", "READY_FOR_CPA")))

    first = ask(client, "my cases", applicant_id="APP-1").json()["answer"]
    second = ask(client, "my cases", applicant_id="APP-2").json()["answer"]

    assert first.startswith("Across 1 case:")
    assert second.startswith("Across 2 cases:")


# ==========================================================================
# CASE-SCOPED: OWNERSHIP STILL GATES
# ==========================================================================


def test_a_case_scoped_question_still_works(client, repo):
    seed(repo, cases=THREE)

    response = ask(client, "what is the status of this case?",
                   case_id="CASE-1")

    assert response.status_code == 200, response.text
    assert response.json()["case_id"] == "CASE-1"


def test_a_case_belonging_to_another_applicant_is_refused(client, repo):
    """
    THE OWNERSHIP RULE IS UNCHANGED. Making `case_id` optional must not
    make it optional to own the case you name.
    """
    seed(repo, "APP-1", cases=(("MINE-1", "DOCUMENT_COLLECTION"),))
    seed(repo, "APP-2", cases=(("THEIRS-1", "DOCUMENT_COLLECTION"),))

    response = ask(client, "why is this case in review?",
                   applicant_id="APP-1", case_id="THEIRS-1")

    assert response.status_code == 403


def test_a_case_that_does_not_exist_is_refused(client, repo):
    seed(repo, cases=THREE)

    response = ask(client, "why is this case in review?",
                   case_id="NO-SUCH-CASE")

    assert response.status_code == 403


def test_ownership_is_checked_before_any_retrieval(client, repo):
    """
    The order that matters: refused BEFORE the store is read for an
    answer, not after.
    """
    from unittest.mock import patch

    seed(repo, "APP-1", cases=(("MINE-1", "DOCUMENT_COLLECTION"),))
    seed(repo, "APP-2", cases=(("THEIRS-1", "DOCUMENT_COLLECTION"),))

    with patch("app.agents.applicant.case_memory_facts.case_memory") as read:
        response = ask(client, "why is this case in review?",
                       applicant_id="APP-1", case_id="THEIRS-1")

    assert response.status_code == 403
    read.assert_not_called()


# ==========================================================================
# NOTHING ELSE MOVED
# ==========================================================================


def test_the_contract_no_longer_requires_a_case(client):
    from app.api.routes.copilot_api import CopilotQueryRequest

    assert not CopilotQueryRequest.model_fields["case_id"].is_required()
    assert CopilotQueryRequest.model_fields["message"].is_required()


def test_the_portfolio_intent_is_mapped_like_every_other(client):
    """The completeness guard: an unmapped intent falls to CLARIFICATION."""
    from app.agents.applicant.intents import Intent
    from app.agents.applicant.query_types import QueryType, type_for

    assert type_for(Intent.CASE_PORTFOLIO) is QueryType.CASE_FACT


def test_no_mcp_read_tool_is_unscoped():
    """
    The structural guarantee behind "no global retrieval": every read
    requires a case or an applicant to scope it.
    """
    from app.mcp.contracts import CONTRACTS

    for name, contract in CONTRACTS.items():
        if contract.writes:
            continue
        required = (contract.input_schema or {}).get("required") or []
        assert {"case_id", "applicant_id"} & set(required), name


@pytest.mark.parametrize("message,expected", [
    ("why is this case in review?", "CASE_HISTORY"),
    ("what documents are missing?", "DOCUMENTS_MISSING"),
    ("my application status", "APPLICATION_STATUS"),
    ("is this case ready to hand over?", "READINESS"),
])
def test_existing_intents_are_unchanged(message, expected):
    """
    A bare "my" in the portfolio patterns was too greedy and captured
    "the status of my application" -- a question about THE case in
    front of the officer. These hold that line.
    """
    from app.agents.applicant.intents import classify

    assert classify(message).intent.value == expected
