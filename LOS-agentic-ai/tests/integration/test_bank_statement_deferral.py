"""
A digital statement too long for the upload is QUEUED, not truncated.

THE LIVE DEFECT. samples/documents/Bank Statement.pdf -- 104 digital pages
of a fully ruled table, ~613 ruling objects a page against Canara's 15 --
costs ~231 ms a page in pdfplumber's table pass, ~42 s of work in all,
against a 25 s synchronous budget. The budget cut the parse part-way
through, and the truncated reading was scored: REVIEW /
VERIFICATION_TIMEOUT, after holding the caller for 25 s. And not even
deterministically: how many rows were read before the cut depended on
how busy the machine was (252 on one run, 1,105 on the next).

WHAT HOLDS NOW:
  the upload decides from the first pages' measured cost, stops early,
  and queues the statement -- REVIEW / DOCUMENT_QUEUED_FOR_PROCESSING,
  never VERIFICATION_TIMEOUT and never a truncated parse;
  ingest records a durable job for it;
  the worker reads it IN FULL and the same rules score it -- PASS;
  a statement that fits the budget is read inline exactly as before.

The budget is set explicitly in each test so the outcome does not
depend on the speed of the machine running it.
"""

from __future__ import annotations

import pathlib

import pytest

from app.store import ocr_queue
from app.store.documents import (
    LocalDocumentStore,
    get_document_store,
    set_document_store,
    storage_key,
)
from app.store.models import Applicant, Application, Document, DocumentStatus
from app.store.ocr_queue import OcrJobStatus
from app.store.sqlite_repo import SQLiteRepository

LONG = pathlib.Path("samples/documents/Bank Statement.pdf")
FITS = pathlib.Path("samples/documents/Canara Bank Statement.pdf")
QUEUED = "DOCUMENT_QUEUED_FOR_PROCESSING"

pytestmark = pytest.mark.skipif(not LONG.exists(), reason="fixture not available")

CASE, APPLICANT = "CASE-LONG", "APP-LONG"


def _verdict(result):
    from app.agents.verification import bank_statement_checks, scoring

    return scoring.assess("BANK_STATEMENT", bank_statement_checks.checks_for(result))


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "deferral.sqlite3")
    repository.initialise()
    repository.save_applicant(Applicant(applicant_id=APPLICANT))
    repository.save_application(Application(case_id=CASE, applicant_id=APPLICANT))
    yield repository


@pytest.fixture
def store(tmp_path):
    set_document_store(LocalDocumentStore(tmp_path / "documents"))
    yield get_document_store()
    set_document_store(None)


# ==========================================================================
# THE UPLOAD
# ==========================================================================


@pytest.mark.slow
def test_the_long_statement_is_queued_not_truncated(monkeypatch):
    """THE LIVE DEFECT: no truncated parse, no timeout verdict."""
    from app.agents.bank_statement.extract import extract_bank_statement

    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "8000")

    result = extract_bank_statement(str(LONG))

    assert result.status.value == "REQUIRES_OCR"
    assert result.source_kind.value == "DIGITAL"
    assert result.transaction_count == 0          # nothing half-read is scored
    assert any("Stopped after page" in w for w in result.warnings)

    verdict = _verdict(result)
    assert verdict.status == "REVIEW"
    assert QUEUED in verdict.reason_codes
    assert "VERIFICATION_TIMEOUT" not in verdict.reason_codes
    assert "DOCUMENT_REQUIRES_OCR" not in verdict.reason_codes   # it is not a scan


@pytest.mark.slow
def test_a_statement_that_fits_is_read_inline_as_before(monkeypatch):
    from app.agents.bank_statement.extract import extract_bank_statement

    if not FITS.exists():
        pytest.skip("fixture not available")
    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "25000")

    result = extract_bank_statement(str(FITS))

    assert result.status.value == "SUCCESS"
    assert result.balance_reconciles is True
    assert _verdict(result).status == "PASS"


# ==========================================================================
# THE QUEUE
# ==========================================================================


def test_ingest_records_a_job_for_a_queued_statement(repo, monkeypatch):
    """Without this, "queued" would be a promise nothing keeps."""
    from app.store import set_repository
    from app.store.ingest import persist_los_result

    set_repository(repo)
    try:
        persist_los_result({
            "applicant_id": APPLICANT, "case_id": CASE,
            "documents": [{"source_id": "statement.pdf", "type": "BANK_STATEMENT",
                           "party_id": APPLICANT, "verification": "REVIEW",
                           "reason_codes": [QUEUED]}],
        })
    finally:
        set_repository(None)

    jobs = ocr_queue.jobs_for_case(repo, CASE)
    assert len(jobs) == 1


@pytest.mark.slow
def test_the_worker_reads_it_in_full_and_it_passes(repo, store):
    """The exact failing document, through the real worker."""
    document_id = f"{CASE}:{APPLICANT}:statement.pdf"
    repo.save_document(Document(
        document_id=document_id, case_id=CASE, applicant_id=APPLICANT,
        party_id=APPLICANT, document_type="BANK_STATEMENT",
        status=DocumentStatus.REVIEW, verification_status="REVIEW",
        reason_codes=[QUEUED]))
    store.put(storage_key(document_id), LONG.read_bytes(),
              content_type="application/pdf")
    ocr_queue.submit(repo, document_id=document_id, case_id=CASE,
                     applicant_id=APPLICANT, party_id=APPLICANT,
                     document_type="BANK_STATEMENT")

    job = ocr_queue.run_once(repo)

    assert job.status is OcrJobStatus.COMPLETED
    document = repo.get_document(document_id)
    assert document.verification_status == "PASS"
    assert document.status is DocumentStatus.VERIFIED
    assert QUEUED not in (document.reason_codes or [])


# ==========================================================================
# CASE MEMORY FOLLOWS THE WORKER
# ==========================================================================

READABLE = pathlib.Path("samples/documents/demo_bank_statement.pdf")


def _queued_with_finding(repo, store, source_id="demo.pdf"):
    from app.store.models import CaseFinding, FindingKind

    document_id = f"{CASE}:{APPLICANT}:{source_id}"
    repo.save_document(Document(
        document_id=document_id, case_id=CASE, applicant_id=APPLICANT,
        party_id=APPLICANT, document_type="BANK_STATEMENT", source_id=source_id,
        status=DocumentStatus.REVIEW, verification_status="REVIEW",
        reason_codes=[QUEUED]))
    # What the upload recorded, exactly as ingest records it.
    repo.save_finding(CaseFinding(
        finding_id="upload", case_id=CASE, party_id=APPLICANT,
        finding_kind=FindingKind.VERIFICATION, status="REVIEW",
        reason_codes=[QUEUED], payload={"type": "BANK_STATEMENT"},
        source_type="DOCUMENT", source_id=source_id, document_id=document_id,
        content_hash="upload"))
    store.put(storage_key(document_id), READABLE.read_bytes(),
              content_type="application/pdf")
    ocr_queue.submit(repo, document_id=document_id, case_id=CASE,
                     applicant_id=APPLICANT, party_id=APPLICANT,
                     document_type="BANK_STATEMENT")
    return document_id


def test_the_workers_verdict_becomes_the_current_finding(repo, store, monkeypatch):
    """
    THE LIVE CONTRADICTION: "Bank Statement is VERIFIED. However ... a long
    statement is still being read in the background." The worker now
    records its verdict where the upload's was, and the latest one wins.
    """
    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    _queued_with_finding(repo, store)

    ocr_queue.run_once(repo)

    current = [f for f in repo.get_current_findings(CASE)
               if f.finding_kind.value == "VERIFICATION"]
    assert [(f.status, f.reason_codes) for f in current] == [("PASS", [])]
    history = [f.status for f in repo.get_case_findings(CASE)
               if f.finding_kind.value == "VERIFICATION"]
    assert history == ["REVIEW", "PASS"]          # the upload's verdict is kept


def test_nothing_is_recorded_when_case_memory_is_off(repo, store, monkeypatch):
    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "false")
    _queued_with_finding(repo, store)

    ocr_queue.run_once(repo)

    statuses = [f.status for f in repo.get_case_findings(CASE)
                if f.finding_kind.value == "VERIFICATION"]
    assert statuses == ["REVIEW"]
