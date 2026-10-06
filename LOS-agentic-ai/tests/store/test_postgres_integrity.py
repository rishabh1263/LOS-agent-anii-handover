"""
POSTGRES INTEGRITY under concurrency, on a real PostgreSQL database (2026-10-06).

Foreign keys refuse orphans; duplicate concurrent writes collapse to one row; a
burst of parallel writes leaves no orphan or partial row; and two concurrent stage
moves from the same version cannot both win (the unique (case, version) constraint
guarantees it; the per-case advisory lock makes the loser wait instead of failing).
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from app.store.models import (Applicant, Application, CaseEvent, CaseFinding, CaseStage, Document, DocumentStatus,
                              FindingKind, StageTransition)
from app.store.repository import RepositoryError
from app.store.testing import fresh_repository


@pytest.fixture
def repo():
    r = fresh_repository()
    r.initialise()
    r.save_applicant(Applicant(applicant_id="A1", full_name="Asha Rao"))
    r.save_application(Application(case_id="C1", applicant_id="A1", product="PERSONAL_LOAN"))
    return r


def _count(repo, sql):
    with psycopg.connect(repo._dsn) as c:
        return c.execute(sql).fetchone()[0]


def test_a_document_for_a_case_that_does_not_exist_is_refused(repo):
    with pytest.raises(RepositoryError):
        repo.save_document(Document(document_id="X:A1:pan.jpg", case_id="NO-SUCH-CASE", applicant_id="A1",
                                    document_type="PAN", status=DocumentStatus.VERIFIED, party_id="A1"))


def test_twenty_concurrent_identical_findings_are_one_row(repo):
    def write(_):
        repo.save_finding(CaseFinding(finding_id=uuid.uuid4().hex, case_id="C1", finding_kind=FindingKind.KYC,
                                      party_id="A1", status="PASS", payload={"fields": []}, source_type="KYC",
                                      content_hash="same-content"))
    with ThreadPoolExecutor(10) as pool:
        list(pool.map(write, range(20)))
    assert _count(repo, "SELECT count(*) FROM case_findings WHERE case_id = 'C1'") == 1


def test_a_parallel_write_burst_leaves_no_orphans(repo):
    def write(i):
        repo.save_document(Document(document_id=f"C1:A1:doc{i}.jpg", case_id="C1", applicant_id="A1",
                                    document_type="PAN", status=DocumentStatus.VERIFIED, party_id="A1",
                                    source_id=f"doc{i}.jpg"))
    with ThreadPoolExecutor(10) as pool:
        list(pool.map(write, range(40)))
    assert len(repo.list_documents("C1")) == 40
    assert _count(repo, "SELECT count(*) FROM documents d LEFT JOIN applications a ON a.case_id = d.case_id "
                        "WHERE a.case_id IS NULL") == 0
    assert _count(repo, "SELECT count(*) FROM case_findings f LEFT JOIN applications a ON a.case_id = f.case_id "
                        "WHERE a.case_id IS NULL") == 0


def _move(repo, to_stage, expected):
    tid = uuid.uuid4().hex
    state = CaseStage(case_id="C1", stage=to_stage, stage_status="IN_PROGRESS", version=expected + 1)
    transition = StageTransition(transition_id=tid, case_id="C1", version=expected + 1, kind="ADVANCE",
                                 to_stage=to_stage, to_status="IN_PROGRESS", from_stage="FOS")
    event = CaseEvent(event_id=uuid.uuid4().hex, case_id="C1", event_type="STAGE_ENTERED", stage=to_stage)
    return repo.apply_stage_transition(expected, state, transition, event)


def test_concurrent_stage_moves_from_the_same_version_have_exactly_one_winner(repo):
    with ThreadPoolExecutor(8) as pool:
        outcomes = list(pool.map(lambda i: _move(repo, f"STAGE{i}", 0), range(8)))
    assert outcomes.count(True) == 1, outcomes
    assert repo.get_case_stage("C1").version == 1
    assert len(repo.get_stage_transitions("C1")) == 1          # no second, lost transition recorded


def test_a_failed_write_inside_a_transition_rolls_back_everything(repo):
    first = uuid.uuid4().hex
    state = CaseStage(case_id="C1", stage="CPA", stage_status="IN_PROGRESS", version=1)
    ok = repo.apply_stage_transition(0, state, StageTransition(
        transition_id=first, case_id="C1", version=1, kind="ADVANCE", to_stage="CPA", to_status="IN_PROGRESS",
        from_stage="FOS"), CaseEvent(event_id=uuid.uuid4().hex, case_id="C1", event_type="STAGE_ENTERED"))
    assert ok is True
    # the second move reuses the first transition id -> a primary-key violation mid-transaction,
    # reported as "not applied" (False) after a rollback -- never a half-written move
    assert repo.apply_stage_transition(1, CaseStage(case_id="C1", stage="CREDIT", stage_status="IN_PROGRESS",
                                                 version=2),
                                    StageTransition(transition_id=first, case_id="C1", version=2, kind="ADVANCE",
                                                    to_stage="CREDIT", to_status="IN_PROGRESS", from_stage="CPA"),
                                    CaseEvent(event_id=uuid.uuid4().hex, case_id="C1",
                                              event_type="STAGE_ENTERED")) is False
    # everything or nothing: the stage row did not move either
    assert repo.get_case_stage("C1").stage == "CPA" and repo.get_case_stage("C1").version == 1
    assert len(repo.get_stage_transitions("C1")) == 1
