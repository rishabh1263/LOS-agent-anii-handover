"""
The queue the upload path used to promise and never had.

WHAT WAS THERE BEFORE. A scanned statement came back REVIEW with
DOCUMENT_REQUIRES_OCR and a sentence saying it had been "routed to the
asynchronous extraction queue". No queue existed: nothing was
recorded, no worker ran, and the document stayed REVIEW until somebody
uploaded it again. The message was the only part of the feature that
had been built, and it read like a promise.

WHAT THESE PIN:

  THE WORK IS RECORDED, DURABLY. A row per document, in the case store,
  so a restart does not lose work a caller was told would happen.

  A DOCUMENT IS ONLY MOVED ON A REAL READ. Success means extraction
  actually succeeded and produced transactions. Anything else leaves
  the document exactly where it was, with the reason recorded --
  never a status invented to make the queue look finished.

  IT IS IDEMPOTENT. Re-processing the same upload does not stack jobs,
  and a document already read is not queued again.

FAST ON PURPOSE. The success path uses the generated text-layer
fixture, which needs no OCR; the failure paths use bytes that are not
a document at all. Real OCR over a real scan takes about fifteen
seconds and is exercised by hand, not by every test run.
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
from app.store.models import (
    Applicant,
    Application,
    Document,
    DocumentStatus,
)
from app.store.ocr_queue import OcrJobStatus
from app.store.sqlite_repo import SQLiteRepository

CASE = "CASE-1"
APPLICANT = "APP-1"
READABLE = pathlib.Path("samples/documents/demo_bank_statement.pdf")


@pytest.fixture(autouse=True)
def no_ambient_flags(monkeypatch):
    for name in (ocr_queue.ENV_WORKER, ocr_queue.ENV_MAX_ATTEMPTS,
                 ocr_queue.ENV_WORKER_PAGES, ocr_queue.ENV_POLL_SECONDS):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "queue.sqlite3")
    repository.initialise()
    repository.save_applicant(Applicant(applicant_id=APPLICANT))
    repository.save_application(
        Application(case_id=CASE, applicant_id=APPLICANT))
    yield repository


@pytest.fixture
def store(tmp_path):
    set_document_store(LocalDocumentStore(tmp_path / "documents"))
    yield get_document_store()
    set_document_store(None)


def unread_document(repo, source_id="statement.pdf"):
    """A document the pipeline could not read, as ingestion leaves one."""
    document_id = f"{CASE}:{APPLICANT}:{source_id}"
    repo.save_document(Document(
        document_id=document_id, case_id=CASE, applicant_id=APPLICANT,
        party_id=APPLICANT, document_type="BANK_STATEMENT",
        status=DocumentStatus.REVIEW, verification_status="REVIEW",
        reason_codes=["DOCUMENT_REQUIRES_OCR"],
    ))
    return document_id


def queue(repo, document_id):
    return ocr_queue.submit(
        repo, document_id=document_id, case_id=CASE,
        applicant_id=APPLICANT, party_id=APPLICANT,
        document_type="BANK_STATEMENT")


# ==========================================================================
# A. THE WORK IS RECORDED
# ==========================================================================


def test_an_unread_document_is_queued(repo):
    job = queue(repo, unread_document(repo))

    assert job.status is OcrJobStatus.QUEUED
    assert job.attempts == 0


def test_the_job_survives_a_new_repository_object(repo, tmp_path):
    """
    DURABLE, BECAUSE A ROW IS A PROMISE. An in-memory queue would drop
    on restart the work a caller was told would be done.
    """
    document_id = unread_document(repo)
    queue(repo, document_id)

    reopened = SQLiteRepository(tmp_path / "queue.sqlite3")

    assert reopened.get_ocr_job(document_id) is not None


def test_queueing_the_same_document_twice_makes_one_job(repo):
    document_id = unread_document(repo)
    first = queue(repo, document_id)
    second = queue(repo, document_id)

    assert second.job_id == first.job_id
    assert len(repo.get_ocr_jobs(CASE)) == 1


def test_a_completed_document_is_not_queued_again(repo):
    document_id = unread_document(repo)
    job = queue(repo, document_id)
    job.status = OcrJobStatus.COMPLETED
    repo.save_ocr_job(job)

    assert queue(repo, document_id) is None


def test_claiming_marks_the_job_and_counts_the_attempt(repo):
    queue(repo, unread_document(repo))

    claimed = repo.claim_ocr_job()

    assert claimed.status is OcrJobStatus.PROCESSING
    assert claimed.attempts == 1
    assert repo.claim_ocr_job() is None, "claimed twice"


def test_the_queue_is_visible_without_internals(repo):
    queue(repo, unread_document(repo))

    published = ocr_queue.jobs_for_case(repo, CASE)[0]

    assert published["status"] == "QUEUED"
    assert published["document_type"] == "BANK_STATEMENT"
    for leak in ("path", "detail_raw", "traceback", "job_id"):
        assert leak not in published


# ==========================================================================
# B. A REAL READ, OR NOTHING
# ==========================================================================


@pytest.mark.skipif(not READABLE.exists(), reason="fixture not generated")
def test_a_readable_document_completes_and_moves_the_document(repo, store):
    document_id = unread_document(repo, "demo_bank_statement.pdf")
    store.put(storage_key(document_id), READABLE.read_bytes(),
              content_type="application/pdf")
    queue(repo, document_id)

    done = ocr_queue.run_once(repo)

    assert done.status is OcrJobStatus.COMPLETED
    document = repo.get_document(document_id)
    assert document.status is DocumentStatus.VERIFIED
    assert document.verification_status == "PASS"
    assert "DOCUMENT_REQUIRES_OCR" not in (document.reason_codes or [])


@pytest.mark.skipif(not READABLE.exists(), reason="fixture not generated")
def test_the_timeline_records_that_it_was_read_in_the_background(repo, store):
    document_id = unread_document(repo, "demo_bank_statement.pdf")
    store.put(storage_key(document_id), READABLE.read_bytes(),
              content_type="application/pdf")
    queue(repo, document_id)

    ocr_queue.run_once(repo)

    events = [e.event_type for e in repo.get_case_timeline(CASE)]
    assert "OCR_COMPLETED" in events


def test_a_document_that_cannot_be_read_leaves_the_document_alone(repo, store):
    """
    NEVER A STATUS INVENTED TO LOOK FINISHED. The document stays
    exactly as verification left it.
    """
    document_id = unread_document(repo)
    store.put(storage_key(document_id), b"this is not a pdf",
              content_type="application/pdf")
    queue(repo, document_id)

    ocr_queue.run_once(repo)

    document = repo.get_document(document_id)
    assert document.status is DocumentStatus.REVIEW
    assert "DOCUMENT_REQUIRES_OCR" in (document.reason_codes or [])
    assert [e.event_type for e in repo.get_case_timeline(CASE)] == []


def test_missing_bytes_are_reported_as_such(repo, store):
    """
    The job points at a document nobody kept. Said plainly, with what
    to do about it -- not as a failure of the document.
    """
    document_id = unread_document(repo)
    queue(repo, document_id)

    done = ocr_queue.run_once(repo)

    assert "upload it again" in (done.detail or "").lower()


def test_it_retries_before_giving_up(repo, store, monkeypatch):
    monkeypatch.setenv(ocr_queue.ENV_MAX_ATTEMPTS, "2")
    document_id = unread_document(repo)
    store.put(storage_key(document_id), b"not a pdf", content_type="application/pdf")
    queue(repo, document_id)

    first = ocr_queue.run_once(repo)
    assert first.status is OcrJobStatus.QUEUED, "gave up on the first attempt"

    second = ocr_queue.run_once(repo)
    assert second.status is OcrJobStatus.FAILED
    assert second.attempts == 2


def test_a_failed_job_carries_a_sentence_not_an_exception(repo, store):
    document_id = unread_document(repo)
    store.put(storage_key(document_id), b"not a pdf", content_type="application/pdf")
    queue(repo, document_id)

    ocr_queue.drain(repo)
    job = repo.get_ocr_job(document_id)

    assert job.status is OcrJobStatus.FAILED
    assert job.detail
    for leak in ("Traceback", "Error(", ".py", "line "):
        assert leak not in job.detail


def test_draining_an_empty_queue_does_nothing(repo):
    assert ocr_queue.drain(repo) == 0
    assert ocr_queue.run_once(repo) is None


# ==========================================================================
# C. THE WORKER IS OFF UNLESS ASKED FOR
# ==========================================================================


def test_the_worker_is_disabled_by_default():
    assert ocr_queue.worker_enabled() is False
    assert ocr_queue.start_worker() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_the_worker_flag_reads_the_usual_spellings(value, monkeypatch):
    monkeypatch.setenv(ocr_queue.ENV_WORKER, value)

    assert ocr_queue.worker_enabled() is True


def test_the_worker_reads_further_than_the_upload_path(monkeypatch):
    """
    Inline extraction refuses a long scan because somebody is waiting.
    The worker exists precisely because nobody is, so its ceiling is
    higher -- otherwise queueing would change nothing.
    """
    from app.agents.bank_statement.extract import inline_ocr_pages

    monkeypatch.delenv("BANK_STATEMENT_INLINE_OCR_PAGES", raising=False)

    assert ocr_queue.worker_pages() > inline_ocr_pages()


def test_the_page_ceiling_is_restored_after_a_job(repo, store, monkeypatch):
    """
    The worker raises the ceiling through the environment, which is
    process-wide. Left raised, the next synchronous upload would
    inherit it and block a caller for minutes.
    """
    monkeypatch.setenv("BANK_STATEMENT_INLINE_OCR_PAGES", "4")
    document_id = unread_document(repo)
    store.put(storage_key(document_id), b"not a pdf", content_type="application/pdf")
    queue(repo, document_id)

    ocr_queue.run_once(repo)

    import os

    assert os.environ["BANK_STATEMENT_INLINE_OCR_PAGES"] == "4"


# ==========================================================================
# E. THE WORKER'S OWN CLOCK, AND WHOSE CALL THE VERDICT IS
# ==========================================================================


def test_the_worker_does_not_inherit_the_request_budget():
    """
    THE DEFECT THE DEMO AUDIT FOUND. Extraction is bounded by
    `BANK_STATEMENT_TIME_BUDGET_MS`, which is 25 seconds because an
    HTTP caller is waiting. The worker raised the page ceiling and
    left the clock alone, so a four-page scan that reads in about
    thirty seconds was cut off at twenty-five, retried, cut off
    again, and recorded FAILED -- a document the same extractor
    reads perfectly when given the time.
    """
    from app.agents.bank_statement.extract import time_budget_ms

    assert ocr_queue.worker_timeout_seconds() * 1000 > time_budget_ms()


def test_the_worker_clock_is_configurable_and_bounded(monkeypatch):
    monkeypatch.setenv(ocr_queue.ENV_WORKER_TIMEOUT, "120")
    assert ocr_queue.worker_timeout_seconds() == 120

    # Never below the synchronous budget, and never nonsense.
    monkeypatch.setenv(ocr_queue.ENV_WORKER_TIMEOUT, "1")
    assert ocr_queue.worker_timeout_seconds() >= 30

    monkeypatch.setenv(ocr_queue.ENV_WORKER_TIMEOUT, "not a number")
    assert ocr_queue.worker_timeout_seconds() == 300


def test_the_budget_is_restored_after_a_job(repo, store, monkeypatch):
    """
    Both the pages and the clock are raised through the environment,
    which is process-wide. Left raised, the next synchronous upload
    would inherit them and hold a caller for minutes.
    """
    import os

    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "25000")
    document_id = unread_document(repo)
    store.put(storage_key(document_id), b"not a pdf",
              content_type="application/pdf")
    queue(repo, document_id)

    ocr_queue.run_once(repo)

    assert os.environ["BANK_STATEMENT_TIME_BUDGET_MS"] == "25000"


def test_a_read_that_does_not_reconcile_is_still_a_completed_read(repo, store,
                                                                  monkeypatch):
    """
    A PARTIAL READ IS A READ. The worker demanded extraction status
    SUCCESS, which a scan rarely gets: the statement that prompted
    this queue reads 33 transactions and comes back PARTIAL because
    its balance cannot be proven. Treating that as a failed read
    threw away everything OCR had recovered.
    """
    from app.agents.bank_statement.schemas import ExtractionStatus

    document_id = unread_document(repo)
    store.put(storage_key(document_id), b"pretend pdf",
              content_type="application/pdf")
    queue(repo, document_id)

    class Partial:
        status = ExtractionStatus.PARTIAL
        transaction_count = 33

    monkeypatch.setattr(ocr_queue, "_extract", lambda *_a: Partial())
    monkeypatch.setattr(ocr_queue, "_verdict",
                        lambda _r: ("REVIEW",
                                    ["BANK_STATEMENT_RECONCILIATION_INCONCLUSIVE"]))

    done = ocr_queue.run_once(repo)

    assert done.status is OcrJobStatus.COMPLETED
    assert "33 transactions" in (done.detail or "")


def test_the_document_takes_the_verdict_verification_gives_it(repo, store,
                                                              monkeypatch):
    """
    NOT THE WORKER'S CALL. An earlier version wrote VERIFIED/PASS
    whenever a read produced rows, which marks a statement verified
    on the strength of having been readable. The document is only
    ever moved on evidence.
    """
    from app.agents.bank_statement.schemas import ExtractionStatus

    document_id = unread_document(repo)
    store.put(storage_key(document_id), b"pretend pdf",
              content_type="application/pdf")
    queue(repo, document_id)

    class Partial:
        status = ExtractionStatus.PARTIAL
        transaction_count = 33

    monkeypatch.setattr(ocr_queue, "_extract", lambda *_a: Partial())
    monkeypatch.setattr(ocr_queue, "_verdict",
                        lambda _r: ("REVIEW", ["BANK_STATEMENT_RECONCILIATION_INCONCLUSIVE"]))

    ocr_queue.run_once(repo)
    document = repo.get_document(document_id)

    assert document.verification_status == "REVIEW"
    assert document.status is DocumentStatus.REVIEW
    assert "DOCUMENT_REQUIRES_OCR" not in (document.reason_codes or [])
    assert "BANK_STATEMENT_RECONCILIATION_INCONCLUSIVE" in (
        document.reason_codes or [])
