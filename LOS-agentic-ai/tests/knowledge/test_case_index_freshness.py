"""
Indexed case text is never older than the case: evidence stamped with a stage the
case has left is dropped before it can support an answer about the current state.
"""

from __future__ import annotations

import pytest

from app.knowledge import grounding
from app.knowledge.retrieval import Evidence, RetrievalResult
from app.knowledge.vector_store import Scope
from app.store import set_repository
from app.store.models import Applicant, Application, ApplicationStatus
from app.store.sqlite_repo import SQLiteRepository

APP, CASE = "APP-FRESH00001", "CASE-FRESH-0001"


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "fresh.sqlite3")
    repository.initialise()
    set_repository(repository)
    repository.save_applicant(Applicant(applicant_id=APP, full_name="Fresh Person"))
    repository.save_application(Application(case_id=CASE, applicant_id=APP, product="PERSONAL_LOAN",
                                            status=ApplicationStatus.BASIC_DOCUMENT_VERIFICATION))
    yield repository
    set_repository(None)


def _found(*stages):
    return RetrievalResult(evidence=tuple(Evidence(text=f"text at {s}", score=0.9, stage=s) for s in stages),
                           sufficient=True)


def test_text_from_a_stage_the_case_has_left_is_dropped(repo):
    from app.agents.los import stage_lifecycle

    stage_lifecycle.transition(CASE, "CPA", reason="test", actor="svc", source="WORKFLOW")
    kept = grounding._current_only(_found("FOS", "CPA"), Scope(app_id=APP, case_id=CASE))
    assert [e.stage for e in kept.evidence] == ["CPA"] and kept.sufficient


def test_only_stale_text_is_not_sufficient(repo):
    from app.agents.los import stage_lifecycle

    stage_lifecycle.transition(CASE, "CPA", reason="test", actor="svc", source="WORKFLOW")
    kept = grounding._current_only(_found("FOS"), Scope(app_id=APP, case_id=CASE))
    assert kept.evidence == () and kept.sufficient is False


def test_current_text_is_untouched(repo):
    found = _found("FOS")
    assert grounding._current_only(found, Scope(app_id=APP, case_id=CASE)) is found
