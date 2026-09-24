"""
The asynchronous OCR queue that the upload path kept promising.

WHAT WAS WRONG. A scanned statement with no text layer, or one too
large to read inside a request, came back REVIEW with
DOCUMENT_REQUIRES_OCR and a sentence telling the caller it had been
routed to "the asynchronous extraction queue". No queue existed.
Nothing was recorded, no worker ran, and the document stayed REVIEW
until somebody re-uploaded it. The message was the only part of the
feature that had been built.

WHAT THIS IS. A durable job per document, a worker that claims one at
a time, and status transitions a caller can see:

    QUEUED -> PROCESSING -> COMPLETED
                         -> FAILED

DURABLE, BECAUSE A ROW HERE IS A PROMISE. An in-memory list would lose
work on restart that a caller was told would be done, which is the
same lie in a slower form. The table is in the case store beside the
documents it concerns.

ONE AT A TIME, DELIBERATELY. OCR is CPU-bound and already has its own
bounded executor; a queue that ran jobs in parallel would compete with
the synchronous uploads a person is waiting on. Throughput here is not
the point -- finishing, eventually and visibly, is.

IT NEVER RUNS INSIDE THE REQUEST. `submit` records the job and
returns. The upload answers in milliseconds with REVIEW and an honest
reason, exactly as before; what changed is that the work now actually
happens afterwards.

NOTHING IS CLAIMED THAT DID NOT HAPPEN. A job that fails is FAILED
with the reason recorded, the document stays REVIEW, and the case says
so. A document is only moved off REVIEW when a real extraction and a
real verification said it could be.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

#: Whether the worker runs at all. OFF BY DEFAULT: a deployment that
#: has not decided to run background work should not discover it has.
ENV_WORKER = "LOS_OCR_WORKER_ENABLED"

#: How often an idle worker looks for new work.
ENV_POLL_SECONDS = "LOS_OCR_WORKER_POLL_SECONDS"

#: How many times one document is retried before it is left FAILED.
ENV_MAX_ATTEMPTS = "LOS_OCR_MAX_ATTEMPTS"

#: The page ceiling the WORKER reads a scan up to. Deliberately far
#: higher than the inline one: nobody is waiting on this.
ENV_WORKER_PAGES = "LOS_OCR_WORKER_MAX_PAGES"

#: How long the WORKER may spend reading one document.
#:
#: THE WHOLE POINT OF THE QUEUE WAS BEING DEFEATED BY ONE INHERITED
#: NUMBER. Extraction is bounded by `BANK_STATEMENT_TIME_BUDGET_MS`,
#: which is 25 seconds because an HTTP caller is waiting. The worker
#: raised the page ceiling and left the clock alone, so a four-page
#: scan that takes about thirty seconds to read was cut off at
#: twenty-five, retried, cut off again and recorded FAILED -- a
#: document the same extractor reads perfectly when given the time.
#:
#: BOUNDED, NOT UNLIMITED. A job that runs for ever holds the single
#: worker thread and nothing behind it ever runs.
ENV_WORKER_TIMEOUT = "LOS_OCR_WORKER_TIMEOUT_SECONDS"


class OcrJobStatus(str, Enum):
    """Where one piece of queued work has got to."""

    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


#: Statuses a caller may still expect movement from.
OPEN_STATUSES = (OcrJobStatus.QUEUED, OcrJobStatus.PROCESSING)


@dataclass
class OcrJob:
    """One document waiting to be read, or already read."""

    document_id: str
    case_id: str
    applicant_id: str
    party_id: str | None = None
    document_type: str | None = None
    status: OcrJobStatus = OcrJobStatus.QUEUED
    attempts: int = 0
    detail: str | None = None
    job_id: str = field(default_factory=lambda: f"ocr_{uuid.uuid4().hex}")
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))

    def public(self) -> dict[str, Any]:
        """
        What a caller is told. No paths, no stack traces, no internals.

        `detail` is a sentence written here, never an exception's text:
        a traceback in an API response tells an officer nothing and
        tells everybody else too much.
        """
        published: dict[str, Any] = {
            "document_id": self.document_id,
            "document_type": self.document_type,
            "status": self.status.value,
            "attempts": self.attempts,
            "updated_at": self.updated_at.astimezone(timezone.utc).isoformat(),
        }
        if self.detail:
            published["detail"] = self.detail
        return published


def worker_enabled() -> bool:
    return (os.getenv(ENV_WORKER) or "").strip().lower() in {
        "1", "true", "yes", "on"}


def poll_seconds() -> float:
    try:
        return max(0.5, float(os.getenv(ENV_POLL_SECONDS) or 5.0))
    except ValueError:
        return 5.0


def max_attempts() -> int:
    try:
        return max(1, int(os.getenv(ENV_MAX_ATTEMPTS) or 2))
    except ValueError:
        return 2


def worker_pages() -> int:
    try:
        return max(1, int(os.getenv(ENV_WORKER_PAGES) or 60))
    except ValueError:
        return 60


def worker_timeout_seconds() -> int:
    """
    The worker's own clock. Five minutes by default.

    Chosen from the measured workload: the four-page scan this was
    written for reads in about thirty seconds, and a fifty-page one
    is the reason the queue exists at all. Configurable because the
    right number depends on the hardware, and floored at the
    synchronous budget so this can never be the tighter of the two.
    """
    try:
        configured = int(os.getenv(ENV_WORKER_TIMEOUT) or 300)
    except ValueError:
        configured = 300
    return max(30, configured)


# ==========================================================================
# SUBMITTING WORK
# ==========================================================================


def submit(repository: Any, *, document_id: str, case_id: str,
           applicant_id: str, party_id: str | None = None,
           document_type: str | None = None) -> OcrJob | None:
    """
    Record that this document still needs reading. None if it already is.

    IDEMPOTENT PER DOCUMENT. Re-processing the same upload must not
    queue it twice, and a document that has already been read must not
    be queued again -- the unique index on `document_id` is what makes
    that true rather than hoped for.

    NEVER RAISES. The upload has already been answered; a queue that
    is unavailable costs the follow-up work, not the response.
    """
    try:
        existing = repository.get_ocr_job(document_id)
        if existing is not None:
            if existing.status in OPEN_STATUSES:
                return existing
            if existing.status is OcrJobStatus.COMPLETED:
                return None
            # A previous attempt failed. Queue it again so a re-upload
            # or a fixed engine gets another go, from attempt zero.
            existing.status = OcrJobStatus.QUEUED
            existing.attempts = 0
            existing.detail = None
            return repository.save_ocr_job(existing)

        return repository.save_ocr_job(OcrJob(
            document_id=document_id, case_id=case_id,
            applicant_id=applicant_id, party_id=party_id,
            document_type=document_type,
        ))
    except Exception as exc:
        logger.warning("Could not queue OCR for %s: %r", document_id, exc)
        return None


def jobs_for_case(repository: Any, case_id: str) -> list[dict[str, Any]]:
    """The queue, as a caller sees it. Empty when nothing is queued."""
    try:
        return [job.public() for job in repository.get_ocr_jobs(case_id)]
    except Exception:
        return []


# ==========================================================================
# DOING THE WORK
# ==========================================================================


def run_once(repository: Any) -> OcrJob | None:
    """
    Claim one job and finish it. None when there was nothing to do.

    Returns the job in its final state, so a test can drive the queue
    without running the worker thread -- the whole lifecycle is
    exercisable in-process.
    """
    job = repository.claim_ocr_job()
    if job is None:
        return None

    logger.info("OCR job %s: reading %s", job.job_id, job.document_id)
    try:
        read, detail = _read(repository, job)
    except Exception as exc:                          # pragma: no cover
        logger.warning("OCR job %s raised: %r", job.job_id, exc)
        read, detail = False, "The document could not be read."

    if read:
        job.status = OcrJobStatus.COMPLETED
        job.detail = detail
    elif job.attempts >= max_attempts():
        job.status = OcrJobStatus.FAILED
        job.detail = detail
    else:
        job.status = OcrJobStatus.QUEUED
        job.detail = detail

    return repository.save_ocr_job(job)


def _read(repository: Any, job: OcrJob) -> tuple[bool, str]:
    """
    Read one stored document properly, and record what came of it.

    THE BYTES COME FROM THE DOCUMENT STORE. Nothing re-reads the
    upload's temporary file: it is gone by the time this runs, which
    is the reason the store had to be wired into the upload path
    before any of this could work.

    THE PAGE CEILING IS RAISED HERE, AND ONLY HERE. Inline extraction
    refuses a long scan because somebody is waiting; this path exists
    precisely because nobody is.
    """
    from app.store.documents import get_document_store, storage_key

    content = get_document_store().get(storage_key(job.document_id))
    if not content:
        return False, ("The uploaded file is no longer available, so it "
                       "could not be read. Please upload it again.")

    # THE PAGE CEILING AND THE CLOCK, both raised for this one call.
    # Raising the pages alone was the bug: the extractor was allowed
    # to look at sixty pages and still cut off after the twenty-five
    # seconds an HTTP caller would have waited.
    #
    # FOR THIS THREAD ONLY. They used to be written into the process
    # environment for the length of the job, and every upload served
    # while a job ran inherited them -- a caller held for minutes on a
    # statement the upload path should have queued in seconds.
    from app.agents.bank_statement.extract import raised_limits

    with raised_limits({
        "BANK_STATEMENT_INLINE_OCR_PAGES": worker_pages(),
        "BANK_STATEMENT_TIME_BUDGET_MS": worker_timeout_seconds() * 1000,
    }):
        result = _extract(job, content)

    if result is None:
        return False, ("This document is a scan that could not be read "
                       "automatically. It needs manual review.")

    rows = int(getattr(result, "transaction_count", 0) or 0)

    # TRANSACTIONS, NOT A CLEAN BILL OF HEALTH. This asked for
    # SUCCESS, which a scan rarely gets: the statement that prompted
    # the queue reads 33 transactions and comes back PARTIAL because
    # its balance cannot be proven. Treating that as a failed read
    # threw away everything OCR had recovered and left the job
    # FAILED, which is what the queue was supposed to stop.
    #
    # The read succeeded when it produced transactions. Whether they
    # add up is verification's question, and it is asked below.
    if not rows:
        return False, ("This document is a scan that could not be read "
                       "automatically. It needs manual review.")

    _record(repository, job, result, rows)
    return True, f"Read {rows} transactions from the scanned statement."


def _extract(job: OcrJob, content: bytes):
    """Run the real extractor over the stored bytes."""
    import tempfile
    from pathlib import Path

    suffix = ".pdf"
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        handle.write(content)
        handle.close()
        from app.agents.bank_statement.extract import extract_bank_statement

        return extract_bank_statement(handle.name)
    except Exception as exc:
        logger.warning("OCR extraction failed for %s: %s",
                       job.document_id, type(exc).__name__)
        return None
    finally:
        try:
            Path(handle.name).unlink(missing_ok=True)
        except OSError:                               # pragma: no cover
            pass


def _verdict(result: Any) -> tuple[str, list[str]]:
    """
    What the EXISTING verification makes of what was read.

    NOT THE WORKER'S CALL. The queue reads a document; it does not
    get to decide whether the document is sound. The same checks and
    the same scoring the synchronous pipeline runs are run here, and
    whatever they say is what gets recorded -- PASS when the
    transactions prove the balance, REVIEW when they do not.

    THIS IS THE LINE THAT MATTERS. An earlier version wrote
    VERIFIED/PASS whenever the read produced rows, which would have
    marked a statement verified on the strength of having been
    readable. A document is only ever moved on evidence.
    """
    from app.agents.verification import bank_statement_checks, scoring

    try:
        assessment = scoring.assess(
            "BANK_STATEMENT", bank_statement_checks.checks_for(result))
    except Exception as exc:                          # pragma: no cover
        logger.warning("Could not verify the re-read document: %r", exc)
        return "REVIEW", []

    # `status` is the verdict; `public()` deliberately withholds it
    # because it publishes the two numbers and the reasons, not the
    # gate. The gate is exactly what has to be recorded here.
    return (str(assessment.status or "REVIEW"),
            list(assessment.reason_codes or []))


def _record_finding(repository: Any, job: OcrJob, document: Any,
                    status: str) -> None:
    """
    The new verdict, in case memory, in the slot the upload's verdict used.

    WHY. The upload recorded this document as REVIEW / "still being read",
    and the Copilot answers from the LATEST finding for each document. With
    only the document row updated, it said "the bank statement is VERIFIED"
    and, in the next sentence, "under review because a long statement is
    still being read" -- both true of different moments. Written in the
    same slot (kind, source type, party, source), this finding supersedes
    the upload's; the upload's stays in the case's history.

    ONLY THE VERIFICATION. The case DECISION is the pipeline's to make and
    is not re-taken here. Only when case memory is on, like every other
    finding, and never fatal.
    """
    try:
        from app.agents.los import config as los_config

        if not los_config.case_memory_enabled():
            return

        from app.store.ingest import _content_hash
        from app.store.models import CaseFinding, FindingKind

        payload = {"type": job.document_type or document.document_type,
                   "read_in_background": True}
        codes = list(document.reason_codes or [])
        repository.save_finding(CaseFinding(
            finding_id=uuid.uuid4().hex,
            case_id=job.case_id,
            party_id=job.party_id,
            finding_kind=FindingKind.VERIFICATION,
            status=status,
            reason_codes=codes,
            payload=payload,
            source_type="DOCUMENT",
            source_id=getattr(document, "source_id", None),
            document_id=job.document_id,
            content_hash=_content_hash(
                ["VERIFICATION", job.party_id, getattr(document, "source_id", None),
                 status, sorted(codes), payload]),
        ))
    except Exception as exc:
        logger.warning("Could not record the background verdict for %s: %r",
                       job.document_id, exc)


def _record(repository: Any, job: OcrJob, result: Any, rows: int) -> None:
    """
    Record what was read, and the verdict verification reached on it.

    ONLY ON A REAL READ. Reached when extraction produced
    transactions; a scan that could not be read leaves the document
    exactly where it was.
    """
    from app.store.models import CaseEvent, DocumentStatus

    status, codes = _verdict(result)

    document = repository.get_document(job.document_id)
    if document is not None:
        document.status = (DocumentStatus.VERIFIED if status == "PASS"
                           else DocumentStatus.REVIEW)
        document.verification_status = status
        # The document was read, so it is no longer waiting to be. Any
        # other reason verification raised takes its place.
        document.reason_codes = [
            code for code in codes
            if code not in ("DOCUMENT_REQUIRES_OCR",
                            "DOCUMENT_QUEUED_FOR_PROCESSING")
        ]
        repository.save_document(document)
        _record_finding(repository, job, document, status)

    repository.record_event(CaseEvent(
        event_id=f"evt_{uuid.uuid4().hex}",
        case_id=job.case_id,
        party_id=job.party_id,
        event_type="OCR_COMPLETED",
        # Scanned or merely long: either way it was read in full here.
        summary=(f"The {job.document_type or 'document'} was read in full "
                 f"in the background; {rows} transactions were extracted "
                 f"and verification recorded {status}."),
        ref_id=job.document_id,
    ))

    # The case changed, so what can be searched about it changed too.
    try:
        from app.knowledge import indexing
        from app.knowledge.vector_store import get_vector_store

        if indexing.demo_index_enabled():
            indexing.rebuild_case(repository, get_vector_store(), job.case_id)
    except Exception as exc:
        logger.warning("Could not re-index %s after OCR: %r", job.case_id, exc)


# ==========================================================================
# THE WORKER
# ==========================================================================


class _Worker:
    """A daemon thread that drains the queue, one job at a time."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="ocr-worker", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    def _loop(self) -> None:
        from app.store import get_repository

        logger.info("OCR worker started.")
        while not self._stop.is_set():
            try:
                done = run_once(get_repository())
            except Exception as exc:                  # pragma: no cover
                # A WORKER THAT DIES IS WORSE THAN A SLOW ONE. Anything
                # unhandled is logged and the loop continues; the job
                # it was holding stays PROCESSING and is picked up
                # again after a restart.
                logger.warning("OCR worker iteration failed: %r", exc)
                done = None

            if done is None:
                self._stop.wait(poll_seconds())

        logger.info("OCR worker stopped.")


_WORKER = _Worker()


def start_worker() -> bool:
    """Start the background worker. False when it is switched off."""
    if not worker_enabled():
        return False
    return _WORKER.start()


def stop_worker() -> None:
    _WORKER.stop()


def drain(repository: Any, limit: int = 20) -> int:
    """
    Run queued jobs until there are none. For tests and for a
    deployment that would rather sweep on a schedule than hold a
    thread open.
    """
    done = 0
    while done < limit and run_once(repository) is not None:
        done += 1
    return done


__all__ = [
    "ENV_MAX_ATTEMPTS", "ENV_POLL_SECONDS", "ENV_WORKER", "ENV_WORKER_PAGES",
    "OPEN_STATUSES", "OcrJob", "OcrJobStatus", "drain", "jobs_for_case",
    "run_once", "start_worker", "stop_worker", "submit", "worker_enabled",
    "worker_timeout_seconds",
]
