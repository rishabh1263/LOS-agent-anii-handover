"""
One Copilot, seven stages.

THE SHAPE THIS PROTECTS. `POST /api/v1/copilot/query` stays a single
endpoint backed by a single agent. Stage awareness is a property of the
answer, not a fork in the architecture -- seven endpoints, or seven
classifiers behind one endpoint, would be seven chatbots that disagree
with each other about the same case.

THE FAILURE IT PREVENTS. Six of the seven stages have no corpus. Without
a registry, a BOPS process question falls through to the only corpus
that exists -- the FOS handbook -- and a BOPS officer receives
authoritative-sounding FOS policy about a stage it does not describe.
CAPABILITY_UNAVAILABLE is the honest answer, and it is deliberately not
a 403, not missing case data and not CLARIFICATION.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.store import set_repository
from app.store.models import (
    Applicant,
    Application,
    ApplicationStatus,
    CaseDecision,
    CaseEvent,
    CaseFinding,
    FindingKind,
)
from app.store.sqlite_repo import SQLiteRepository

ENDPOINT = "/api/v1/copilot/query"


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "stage.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


@pytest.fixture(autouse=True)
def memory_on():
    from unittest.mock import patch

    with patch("app.agents.los.config.case_memory_enabled", return_value=True):
        yield


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update(
        {"Authorization": f"Bearer {make_token(scopes=['los.read'])}"})
    return c


def seed(repo, case_id="CASE-1", applicant_id="APP-1",
         status=ApplicationStatus.DOCUMENT_COLLECTION, stage=None):
    repo.save_applicant(Applicant(applicant_id=applicant_id))
    repo.save_application(Application(case_id=case_id,
                                      applicant_id=applicant_id,
                                      status=status))
    repo.save_finding(CaseFinding(
        finding_id=f"f-{case_id}", case_id=case_id, party_id=applicant_id,
        finding_kind=FindingKind.VERIFICATION, status="FAIL",
        reason_codes=["DOCUMENT_TYPE_MISMATCH"], source_id="pan.jpg",
        document_id=f"{case_id}:{applicant_id}:pan.jpg",
        content_hash=f"h-{case_id}"))
    repo.save_decision(CaseDecision(
        decision_id=f"d-{case_id}", case_id=case_id, decision="REVIEW",
        next_action="MANUAL_REVIEW", status="PARTIAL"))
    if stage:
        repo.record_event(CaseEvent(
            event_id=f"e-{case_id}", case_id=case_id,
            event_type="STAGE", stage=stage, sequence=1))


def ask(client, message="Why is this case in review?", **body):
    body.setdefault("case_id", "CASE-1")
    body.setdefault("applicant_id", "APP-1")
    return client.post(ENDPOINT, json={"message": message, **body})


# ==========================================================================
# F. THE STAGE IS ON THE ANSWER WHEN IT IS KNOWN
# ==========================================================================


def test_a_case_in_fos_says_so(client, repo):
    seed(repo)

    body = ask(client).json()

    assert body["stage"] == "FOS"
    assert body["stage_resolution"] == "APPLICATION_STATUS"


def test_a_recorded_stage_outranks_the_application_status(client, repo):
    seed(repo, stage="CREDIT")

    body = ask(client).json()

    assert body["stage"] == "CREDIT"
    assert body["stage_resolution"] == "CASE_TIMELINE"


def test_a_caller_cannot_override_the_cases_own_stage(client, repo):
    """
    THE BOUNDARY. A frontend that could set the stage could choose which
    stage's answers it receives.
    """
    seed(repo, stage="FOS")

    body = ask(client, stage="DISBURSEMENT").json()

    assert body["stage"] == "FOS"
    assert body["stage_resolution"] == "CASE_TIMELINE"


def silent_record():
    """
    An owned case whose record establishes no stage.

    The case must EXIST -- ownership is checked before any of this -- so
    what is simulated is a recorded case with no stage on its timeline
    and no status to derive one from, which is the real shape of a case
    created outside the LOS pipeline.
    """
    from unittest.mock import patch

    return patch("app.agents.los.stages._from_case", return_value=None)


def test_a_claimed_stage_is_marked_as_claimed(client, repo):
    """The record is silent, so the caller's word is used -- and labelled."""
    seed(repo)

    with silent_record():
        body = ask(client, stage="CPA").json()

    assert body["stage"] == "CPA"
    assert body["stage_resolution"] == "CALLER_SUPPLIED"


def test_an_unresolvable_stage_is_absent_rather_than_defaulted(client, repo):
    """
    Defaulting to FOS would answer every unresolvable case out of the
    FOS corpus.
    """
    seed(repo)

    with silent_record():
        body = ask(client).json()

    assert body.get("stage") is None
    assert body["stage_resolution"] == "UNRESOLVED"


def test_a_case_the_caller_does_not_own_is_still_refused_before_any_stage_work(
        client, repo):
    """Ownership comes first. Stage resolution never softens it."""
    seed(repo)

    response = ask(client, case_id="CASE-SOMEONE-ELSE")

    assert response.status_code == 403


# ==========================================================================
# G. AN UNBUILT STAGE SAYS SO
# ==========================================================================


#: A question the classifier routes to KNOWLEDGE_ONLY -- naming a
#: product makes it a policy question rather than a case one.
KNOWLEDGE_QUESTION = "What documents are required for a personal loan?"


@pytest.mark.parametrize("stage", ["CPA", "CREDIT", "RCU", "BOPS", "HOPS",
                                   "DISBURSEMENT"])
def test_a_process_question_on_a_guide_only_stage_is_answered(client, repo,
                                                              stage):
    """
    CHANGED WHEN THE GUIDES WERE INDEXED. These six used to be refused
    with CAPABILITY_UNAVAILABLE because no corpus existed for them.
    B4 indexed a demonstration stage guide for every stage, so the
    refusal would now be wrong: the Copilot CAN answer how the stage
    works. What it still cannot do is read a case at that stage, and
    the test below holds that line.
    """
    seed(repo, stage=stage)

    body = ask(client, KNOWLEDGE_QUESTION).json()

    assert body.get("status") != "CAPABILITY_UNAVAILABLE"
    assert body["stage"] == stage


@pytest.mark.parametrize("stage", ["CPA", "CREDIT", "RCU", "BOPS", "HOPS",
                                   "DISBURSEMENT"])
def test_a_guide_only_stage_still_has_no_case_capability(client, repo, stage):
    """
    A DOWNSTREAM request needs a capability, not a corpus. Six stages
    have a guide and nothing that can act on a case.
    """
    from app.agents.los import stage_registry
    from app.agents.los.stages import LosStage

    registered = stage_registry.capabilities_for(LosStage(stage))

    assert registered.answers_knowledge()
    assert not registered.answers_downstream()


def test_a_downstream_request_on_a_guide_only_stage_is_refused(client, repo):
    """
    THE REFUSAL STILL EXISTS, for the thing that still has no
    capability. A 403 would tell a reviewer to fix the caller's
    scopes; nothing is wrong with the caller -- the stage simply
    cannot act on a case.
    """
    seed(repo, stage="BOPS")

    response = ask(client, "what is my credit score?")

    assert response.status_code == 200
    assert response.json()["status"] == "CAPABILITY_UNAVAILABLE"


def test_the_refusal_is_not_an_unrecognised_question(client, repo):
    """
    The question was understood perfectly. Reporting CLARIFICATION would
    send the officer to rephrase something that was never the problem.
    """
    seed(repo, stage="BOPS")

    body = ask(client, KNOWLEDGE_QUESTION).json()

    assert body["intent"] != "UNKNOWN"
    assert body["category"] != "CLARIFICATION"


def test_a_bops_question_is_answered_from_the_bops_guide(client, repo):
    """
    CHANGED, AND THIS IS THE POINT OF THE PHASE. The old version
    asserted that a BOPS question returned nothing, because the FOS
    handbook was the only corpus and answering from it would have been
    wrong. There is a BOPS guide now, so the question is answered --
    and any process source cited must carry the BOPS stage, never
    another one.
    """
    seed(repo, stage="BOPS")

    body = ask(client, KNOWLEDGE_QUESTION).json()
    process = [s for s in (body.get("sources") or [])
               if s.get("source_type") == "PROCESS_KNOWLEDGE"]

    for item in process:
        assert item["stage"] == "BOPS"
        assert item.get("case_id") is None


def test_a_case_fact_question_is_answered_whatever_the_stage(client, repo):
    """
    CASE FACTS ARE NOT STAGE-GATED. They come from the case's own stored
    records, which exist whatever desk the case sits on. Refusing them
    would make the Copilot useless the moment a case left FOS.
    """
    seed(repo, stage="BOPS")

    body = ask(client).json()

    assert body.get("status") != "CAPABILITY_UNAVAILABLE"
    assert body["stage"] == "BOPS"
    assert "DOCUMENT_TYPE_MISMATCH" not in body["answer"]
    assert body["answer"]


# ==========================================================================
# ONE COPILOT, NOT SEVEN
# ==========================================================================


def test_there_is_exactly_one_universal_copilot_endpoint():
    """
    Seven stages did not become seven endpoints. `/api/v1/fos/copilot`
    is a different, pre-existing surface with its own stage boundary and
    is untouched -- it is not a second universal copilot.
    """
    import main

    paths = [p for p in main.app.openapi()["paths"] if "copilot" in p]

    assert "/api/v1/copilot/query" in paths
    assert sorted(paths) == ["/api/v1/copilot/query", "/api/v1/fos/copilot"]


def test_every_stage_reaches_the_same_endpoint(client, repo):
    """
    Seven stages, one door. The answer differs; the architecture does
    not fork.
    """
    seen = set()
    for stage in ("FOS", "CPA", "CREDIT", "RCU", "BOPS", "HOPS",
                  "DISBURSEMENT"):
        seed(repo, case_id=f"CASE-{stage}", applicant_id=f"APP-{stage}",
             stage=stage)
        response = ask(client, case_id=f"CASE-{stage}",
                       applicant_id=f"APP-{stage}")
        assert response.status_code == 200, stage
        seen.add(response.json()["stage"])

    assert seen == {"FOS", "CPA", "CREDIT", "RCU", "BOPS", "HOPS",
                    "DISBURSEMENT"}


def test_the_stage_never_comes_from_the_wording_of_the_question(client, repo):
    """A CPA-sounding question does not put a case in CPA."""
    seed(repo, stage="FOS")

    body = ask(client, "What does CPA need from this case for disbursement?").json()

    assert body["stage"] == "FOS"
