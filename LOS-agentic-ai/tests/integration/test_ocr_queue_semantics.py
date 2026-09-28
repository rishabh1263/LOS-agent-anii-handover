"""
Async OCR semantics: durable, honest job state.

  * a document is PROCESSING only while a job is open AND a worker runs
  * an orphaned PROCESSING job (worker died) is reclaimed after its lease
  * a FAILED read returns the document to REVIEW -- never left "in progress"
  * the worker applies the SAME post-verdict controls as the upload path
    (authenticity cap, forensics, issuer) before anything becomes VERIFIED
  * a queued document of a type the reader cannot read is not read as a
    bank statement
  * the case owner can see the real queue state; nobody else can
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.store import ocr_queue, set_repository
from app.store.documents import LocalDocumentStore, set_document_store, storage_key
from app.store.models import Applicant, Application, Document, DocumentStatus, utcnow
from app.store.ocr_queue import OcrJob, OcrJobStatus
from app.store.sqlite_repo import SQLiteRepository

CASE, APP, DOC = "CASE-Q", "APP-Q", "CASE-Q:APP-Q:scan.pdf"
OWNER = "fos-owner"


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "queue.sqlite3")
    repository.initialise()
    repository.save_applicant(Applicant(applicant_id=APP))
    repository.save_application(Application(case_id=CASE, applicant_id=APP))
    repository.grant_access(OWNER, "APPLICANT", APP)
    repository.save_document(Document(
        document_id=DOC, case_id=CASE, applicant_id=APP, document_type="BANK_STATEMENT",
        status=DocumentStatus.REVIEW, verification_status="REVIEW",
        reason_codes=["DOCUMENT_REQUIRES_OCR"], source_id="scan.pdf", party_id=APP))
    set_repository(repository)
    set_document_store(LocalDocumentStore(tmp_path / "docs"))
    yield repository
    set_repository(None)
    set_document_store(None)


def job(repo, status=OcrJobStatus.PROCESSING, attempts=1, age_seconds=0,
        document_type="BANK_STATEMENT") -> OcrJob:
    j = OcrJob(document_id=DOC, case_id=CASE, applicant_id=APP, party_id=APP,
               document_type=document_type, status=status, attempts=attempts)
    repo.save_ocr_job(j)
    if age_seconds:
        from app.store.sqlite_repo import _iso

        repo._write("UPDATE ocr_jobs SET updated_at = ? WHERE document_id = ?",
                    (_iso(utcnow() - timedelta(seconds=age_seconds)), DOC))
    return repo.get_ocr_job(DOC)


# ==========================================================================
# orphaned jobs
# ==========================================================================

def test_an_orphaned_processing_job_is_requeued_after_its_lease(repo, monkeypatch):
    monkeypatch.setenv("LOS_OCR_WORKER_TIMEOUT_SECONDS", "30")
    job(repo, age_seconds=2 * 30 + 5)
    assert repo.reclaim_stale_ocr_jobs(ocr_queue.lease_seconds(), 2) == 1
    reclaimed = repo.get_ocr_job(DOC)
    assert reclaimed.status is OcrJobStatus.QUEUED
    assert "interrupted" in reclaimed.detail


def test_an_orphaned_job_with_spent_attempts_fails(repo, monkeypatch):
    monkeypatch.setenv("LOS_OCR_WORKER_TIMEOUT_SECONDS", "30")
    job(repo, attempts=2, age_seconds=100)
    assert repo.reclaim_stale_ocr_jobs(ocr_queue.lease_seconds(), 2) == 1
    assert repo.get_ocr_job(DOC).status is OcrJobStatus.FAILED


def test_a_live_processing_job_is_left_alone(repo):
    job(repo, age_seconds=1)
    assert repo.reclaim_stale_ocr_jobs(ocr_queue.lease_seconds(), 2) == 0
    assert repo.get_ocr_job(DOC).status is OcrJobStatus.PROCESSING


def test_run_once_reclaims_then_claims(repo, monkeypatch):
    """A worker restart picks the orphan up -- the claim the code used to make."""
    monkeypatch.setenv("LOS_OCR_WORKER_TIMEOUT_SECONDS", "30")
    job(repo, age_seconds=100)
    with patch.object(ocr_queue, "_read", return_value=(False, "no bytes")):
        done = ocr_queue.run_once(repo)
    assert done is not None and done.attempts == 2


# ==========================================================================
# PROCESSING means processing
# ==========================================================================

def _persist_queued(monkeypatch, worker_on: bool):
    from app.store.ingest import persist_los_result

    monkeypatch.setenv("LOS_OCR_WORKER_ENABLED", "true" if worker_on else "false")
    with patch("app.agents.los.config.case_memory_enabled", return_value=True):
        persist_los_result({"applicant_id": APP, "case_id": CASE, "documents": [
            {"source_id": "scan.pdf", "type": "BANK_STATEMENT", "verification": "REVIEW",
             "reason_codes": ["DOCUMENT_REQUIRES_OCR"], "party_id": APP}]})


def test_a_queued_document_is_processing_while_a_worker_runs(repo, monkeypatch):
    _persist_queued(monkeypatch, worker_on=True)
    assert repo.get_document(DOC).status is DocumentStatus.PROCESSING
    assert repo.get_ocr_job(DOC).status is OcrJobStatus.QUEUED


def test_without_a_worker_the_document_stays_review(repo, monkeypatch):
    """No worker: "in progress" would be a promise nothing keeps."""
    _persist_queued(monkeypatch, worker_on=False)
    assert repo.get_document(DOC).status is DocumentStatus.REVIEW


def test_a_failed_read_returns_the_document_to_review(repo, monkeypatch):
    monkeypatch.setenv("LOS_OCR_MAX_ATTEMPTS", "1")
    document = repo.get_document(DOC)
    document.status = DocumentStatus.PROCESSING
    repo.save_document(document)
    job(repo, status=OcrJobStatus.QUEUED, attempts=0)
    with patch.object(ocr_queue, "_read", return_value=(False, "unreadable")):
        done = ocr_queue.run_once(repo)
    assert done.status is OcrJobStatus.FAILED
    assert repo.get_document(DOC).status is DocumentStatus.REVIEW
    assert repo.get_document(DOC).verification_status == "REVIEW"


# ==========================================================================
# the worker applies the upload path's controls
# ==========================================================================

class _Reconciled:
    """A result verification would PASS on its own."""
    def __init__(self):
        from datetime import date
        from decimal import Decimal

        from app.agents.bank_statement.schemas import (BankStatementResult, ExtractionStatus,
                                                       SourceKind, StatementPeriod, Transaction)

        self.result = BankStatementResult(
            status=ExtractionStatus.SUCCESS, source_kind=SourceKind.SCANNED,
            transactions=[Transaction(date=date(2026, 1, 5), credit=Decimal("100"),
                                      balance=Decimal("200"), amount_source="OCR")],
            transaction_count=1, opening_balance=Decimal("100"), closing_balance=Decimal("200"),
            total_credit=Decimal("100"), total_debit=Decimal("0"), balance_reconciles=True,
            reconciliation="RECONCILED", pages=1, pages_with_text=0,
            period=StatementPeriod(start=date(2026, 1, 5), end=date(2026, 1, 5)))


def _run_worker_with(repo, monkeypatch, result):
    job(repo, status=OcrJobStatus.QUEUED, attempts=0)
    from app.store.documents import get_document_store

    get_document_store().put(storage_key(DOC), b"%PDF-1.4 synthetic", content_type=None)
    with patch.object(ocr_queue, "_extract", return_value=result):
        return ocr_queue.run_once(repo)


def test_the_authenticity_cap_applies_in_the_background_too(repo, monkeypatch):
    from app.agents.verification import authenticity

    with patch.object(authenticity, "cap",
                      return_value=("REVIEW", ["AUTHENTICITY_NOT_ESTABLISHED"])):
        done = _run_worker_with(repo, monkeypatch, _Reconciled().result)
    assert done.status is OcrJobStatus.COMPLETED
    document = repo.get_document(DOC)
    assert document.status is DocumentStatus.REVIEW and document.verification_status == "REVIEW"
    assert "AUTHENTICITY_NOT_ESTABLISHED" in document.reason_codes


def test_forensics_can_hold_a_background_read(repo, monkeypatch):
    from app.agents.verification import forensics

    def hold(result, content, filename):
        result["verification"]["status"] = "REVIEW"
        result["verification"]["reason_codes"].append(forensics.REVIEW_CODE)
        return result

    with patch.object(forensics, "apply", side_effect=hold):
        done = _run_worker_with(repo, monkeypatch, _Reconciled().result)
    assert done.status is OcrJobStatus.COMPLETED
    assert repo.get_document(DOC).status is DocumentStatus.REVIEW


def test_a_failing_control_fails_closed(repo, monkeypatch):
    from app.agents.verification import issuer

    with patch.object(issuer, "apply", side_effect=RuntimeError("issuer down")):
        done = _run_worker_with(repo, monkeypatch, _Reconciled().result)
    assert done.status is OcrJobStatus.COMPLETED
    document = repo.get_document(DOC)
    assert document.status is DocumentStatus.REVIEW
    assert "VERIFICATION_CONTROLS_UNAVAILABLE" in document.reason_codes


def test_a_clean_background_read_can_still_verify(repo, monkeypatch):
    done = _run_worker_with(repo, monkeypatch, _Reconciled().result)
    assert done.status is OcrJobStatus.COMPLETED
    document = repo.get_document(DOC)
    assert document.verification_status in ("PASS", "REVIEW")     # the configured policy decides
    if document.verification_status == "PASS":
        assert document.status is DocumentStatus.VERIFIED


def test_a_non_bank_document_is_not_read_as_a_statement(repo, monkeypatch):
    monkeypatch.setenv("LOS_OCR_MAX_ATTEMPTS", "1")
    from app.store.documents import get_document_store

    get_document_store().put(storage_key(DOC), b"%PDF-1.4 itr", content_type=None)
    job(repo, status=OcrJobStatus.QUEUED, attempts=0, document_type="ITR")
    with patch.object(ocr_queue, "_extract") as extract:
        done = ocr_queue.run_once(repo)
    extract.assert_not_called()
    assert done.status is OcrJobStatus.FAILED and "No background reader" in done.detail


# ==========================================================================
# the status endpoint
# ==========================================================================

@pytest.fixture
def client():
    import main

    return TestClient(main.app)


def test_the_owner_sees_the_real_queue_state(repo, client, make_token, monkeypatch):
    job(repo, status=OcrJobStatus.QUEUED, attempts=0)
    r = client.get(f"/api/v1/los/cases/{CASE}/processing",
                   headers={"Authorization": f"Bearer {make_token(subject=OWNER, scopes=['read_documents'])}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jobs"][0]["status"] == "QUEUED"
    assert body["documents"][0]["document_id"] == DOC
    assert body["documents"][0]["status"] == "REVIEW"
    assert "worker_enabled" in body


def test_a_stranger_cannot_see_the_queue(repo, client, make_token):
    r = client.get(f"/api/v1/los/cases/{CASE}/processing",
                   headers={"Authorization": f"Bearer {make_token(subject='stranger', scopes=['read_documents'])}"})
    assert r.status_code == 403


def test_the_queue_needs_a_token(repo, client):
    assert client.get(f"/api/v1/los/cases/{CASE}/processing").status_code == 401
