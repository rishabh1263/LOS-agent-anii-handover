"""
THE VERIFY CAPABILITY: "verify", "PAN verify karo", "sab documents verify kar
do", "iska score?" -- orchestration over the EXISTING verification pipeline.

    request(message)       what was asked: a SELECTION (plain "verify"), ONE
                           document, ALL documents, or a REPORT of one result
    run(...)               the documents in scope, each answered from:
                             RECORDED     a verdict already on the record --
                                          reused, never re-OCR'd
                             VERIFIED_NOW an uploaded, unverified document read
                                          and verified NOW by the existing job
                                          step (ocr_queue.claim/finish), fanned
                                          out IN PARALLEL with bounded
                                          concurrency inside a time budget
                             PROCESSING   still running when the budget ended:
                                          its durable job finishes it and
                                          GET_VERIFICATION_STATUS reports it
                             MISSING      nothing uploaded: an UPLOAD action with
                                          the accepted types from the checklist

THIS DOES NOT VERIFY ANYTHING ITSELF. The verdict, the reason codes and the
persistence are the document pipeline's (ocr_queue._read -> the verification
gate -> the store). A score is published only when a recorded VERIFICATION
finding for that document carries one; it is never computed here.

AUTHORIZED BEFORE IT RUNS. The caller of `run` has already proven ownership
and the access policy (agent.py). Starting a verification changes a record,
so it also needs the caller's write scope; without it the recorded results
are shown and nothing is started.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from app.agents.applicant.copilot.answering import structured

SELECTION, ONE, ALL, REPORT = "SELECTION", "ONE", "ALL", "REPORT"
WRITE_SCOPES = frozenset({"documents:write", "upload_document"})

_VERIFY = re.compile(r"\b(verify|verif(y|ied|ication)\w*|re-?verify|validate|validation|jaanch|jaach)\b", re.I)
_REQUEST = re.compile(
    r"\b(karo|kar\s*do|kardo|karna|karwa\w*|kariye|karke|please|pls|can\s+you|could\s+you|start|run|now|abhi|"
    r"chahiye|batao)\b|^\s*(please\s+|pls\s+)?(re-?)?verify\b", re.I)
#: A question about a state already reached ("is my PAN verified?", "PAN verify
#: hua?", "verification status") is a status read, not a request to verify.
_STATE_QUESTION = re.compile(
    r"\b(is|are|was|were|has|have)\b[^?]{0,40}\bverified\b|\bverified\s*(hai|h|hua|hui|kya|\?)|"
    r"\bverify\s+(hua|hui|ho\s+gaya|ho\s+gayi|hai\s+kya)\b|\bverification\s+(status|result)\b|"
    r"\bkya\b[^?]{0,30}\bverif", re.I)
_ALL = re.compile(r"\b(all|every|sab|sabhi|saare|sare|sabko|everything|documents|docs)\b", re.I)
_SCORE = re.compile(r"\b(score|confidence)\b", re.I)
#: "score of my documents" -- the documents as a set, not one type
_DOCUMENTS = re.compile(r"\b(documents?|docs?|kagaz\w*|dastavez\w*)\b", re.I)


@dataclass(frozen=True)
class Request:
    scope: str
    document_type: str | None = None


def request(message: str) -> Request | None:
    """What verification was asked for, or None when this is not one."""
    from app.agents.applicant.copilot.semantics import intents

    text = " ".join(str(message or "").split())
    doc = intents._document_type(text)
    if _SCORE.search(text) and doc and not re.search(r"\bkyc\b", text, re.I):
        return Request(REPORT, doc)                 # "PAN ka score kitna hai?"
    if _SCORE.search(text) and _DOCUMENTS.search(text) and not re.search(r"\b(kyc|credit|cibil|bureau)\b", text, re.I):
        return Request(REPORT)                      # "score of my documents?"
    if not _VERIFY.search(text):
        return None
    if _STATE_QUESTION.search(text) and not re.search(r"\b(karo|kar\s*do|kardo|please|pls)\b", text, re.I):
        return None
    if not _REQUEST.search(text) and len(text.split()) > 3:
        return None
    if doc:
        return Request(ONE, doc)
    if _ALL.search(text):
        return Request(ALL)
    return Request(SELECTION)


def concurrency() -> int:
    try:
        return max(1, int(os.getenv("COPILOT_VERIFY_CONCURRENCY", "3")))
    except ValueError:
        return 3


_POOL: "concurrent.futures.ThreadPoolExecutor | None" = None
_PROCESS_POOL: "concurrent.futures.ProcessPoolExecutor | None" = None
_PROCESS_KEY: tuple | None = None

#: Configuration a read depends on, snapshotted into a worker process when it
#: starts: a pool built under different settings is replaced, never reused.
_READ_ENV_PREFIXES = ("LOS_", "BANK_STATEMENT_", "DOCUMENT_", "APPLICANT_AGENT_", "OCR_", "KYC_")


def _pool():
    """The thread pool fallback: at most `concurrency()` reads at once."""
    global _POOL
    size = concurrency()
    if _POOL is None or _POOL._max_workers != size:
        _POOL = concurrent.futures.ThreadPoolExecutor(max_workers=size,
                                                      thread_name_prefix="copilot-verify")
    return _POOL


def _shareable(repository: Any) -> tuple[str, str | None] | None:
    """(store path, documents root) when BOTH stores can be reopened by path in
    another process -- SQLite (WAL, busy timeout) and the local document store."""
    try:
        from app.store.documents import LocalDocumentStore, get_document_store
        from app.store.sqlite_repo import SQLiteRepository

        store = get_document_store()
        if isinstance(repository, SQLiteRepository) and isinstance(store, LocalDocumentStore):
            return str(repository._path), str(store._root)
    except Exception:  # noqa: BLE001
        pass
    return None


def executor_mode() -> str:
    """
    COPILOT_VERIFY_EXECUTOR: thread (default) | process.

    THREADS BY DEFAULT. A thread read uses the serving process's OCR engines,
    which are warmed at startup. A worker process starts cold: it imports the
    service and builds its own engines inside the first read it is given (tens
    of seconds on a loaded machine), and creating it can fail outright where
    endpoint security hooks process creation. Process mode stays available for
    a deployment that has measured the parse holding the interpreter lock.
    """
    mode = str(os.getenv("COPILOT_VERIFY_EXECUTOR", "thread")).strip().lower()
    return mode if mode in ("process", "thread") else "thread"


def _discard_process_pool() -> None:
    """Drop a broken pool; the next read builds a fresh one."""
    global _PROCESS_POOL, _PROCESS_KEY
    broken, _PROCESS_POOL, _PROCESS_KEY = _PROCESS_POOL, None, None
    if broken is not None:
        try:
            broken.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001 - already broken
            pass


def _process_pool() -> "concurrent.futures.ProcessPoolExecutor":
    """
    THE READS RUN IN WORKER PROCESSES. A bank-statement parse is CPU-bound
    Python; run as threads it holds the interpreter lock and slows every other
    request the server is answering. Separate processes read in parallel and
    leave the serving process free. At most `concurrency()` at once.
    """
    global _PROCESS_POOL, _PROCESS_KEY
    key = (concurrency(), tuple(sorted((k, v) for k, v in os.environ.items()
                                       if k.startswith(_READ_ENV_PREFIXES))))
    if _PROCESS_POOL is None or _PROCESS_KEY != key:
        if _PROCESS_POOL is not None:
            _PROCESS_POOL.shutdown(wait=False, cancel_futures=False)
        _PROCESS_POOL = concurrent.futures.ProcessPoolExecutor(max_workers=concurrency())
        _PROCESS_KEY = key
    return _PROCESS_POOL


def _read_in_process(store_path: str, documents_root: str | None, document_id: str) -> str:
    """
    One claimed job, finished in a worker process through the SAME job step
    (ocr_queue.finish): the stores are reopened by path, the job read back by
    its document id. Returns the job's final status.
    """
    from app.store import ocr_queue, set_repository
    from app.store.documents import LocalDocumentStore, set_document_store
    from app.store.sqlite_repo import SQLiteRepository

    repository = SQLiteRepository(store_path)
    set_repository(repository)
    if documents_root:
        set_document_store(LocalDocumentStore(documents_root))
    job = repository.get_ocr_job(document_id)
    if job is None:
        return "FAILED"
    done = ocr_queue.finish(repository, job)
    return getattr(done.status, "value", str(done.status))


def budget_seconds() -> float:
    try:
        return max(0.5, float(os.getenv("COPILOT_VERIFY_BUDGET_S", "2.0")))
    except ValueError:
        return 2.0


def _in_scope(documents: list[dict[str, Any]], checklist: list[dict[str, Any]],
              req: Request, party: str | None) -> list[dict[str, Any]]:
    docs = [d for d in documents if isinstance(d, dict)]
    if party in ("PRIMARY_APPLICANT", "CO_APPLICANT"):
        docs = [d for d in docs if str(d.get("party_role") or "PRIMARY_APPLICANT") == party]
    if req.document_type:
        wanted = req.document_type.upper()
        slot_accepts = {str(a).upper() for r in checklist if str(r.get("slot") or "").upper() == wanted
                        for a in r.get("accepts") or []}
        docs = [d for d in docs if str(d.get("document_type") or "").upper() in ({wanted} | slot_accepts)]
    return docs


def _scores(repository: Any, case_id: str) -> dict[str, Any]:
    """document_id -> the RECORDED verification score (never computed)."""
    out: dict[str, Any] = {}
    try:
        # the CURRENT verification finding per document (latest written wins)
        for f in repository.get_current_findings(case_id, kind="VERIFICATION"):
            if getattr(f, "document_id", None):
                out[str(f.document_id)] = getattr(f, "score", None)
        out = {k: v for k, v in out.items() if v is not None}
    except Exception:  # noqa: BLE001 - no score is "not recorded", never a guess
        pass
    return out


def _public(document: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    if document is None:
        return fallback
    status = getattr(getattr(document, "status", None), "value", getattr(document, "status", None))
    return {**fallback, "status": status, "verification_status": getattr(document, "verification_status", None),
            "reason_codes": list(getattr(document, "reason_codes", []) or [])}


async def _verify_pending(repository: Any, pending: list[dict[str, Any]], *, case_id: str,
                          applicant_id: str) -> tuple[dict[str, str], dict[str, str], float]:
    """
    Read and verify every UPLOADED-but-unverified document IN PARALLEL through
    the existing job step. Returns (document_id -> source, document_id -> job_id,
    elapsed ms). A document still running when the budget ends stays
    PROCESSING: its durable job finishes it.
    """
    from app.store import ocr_queue

    shared = _shareable(repository) if executor_mode() == "process" else None
    pool = None if shared else _pool()
    process_pool = _process_pool() if shared else None
    loop = asyncio.get_running_loop()
    sources: dict[str, str] = {}
    jobs: dict[str, str] = {}
    started = time.perf_counter()

    async def one(document: dict[str, Any]) -> None:
        document_id = str(document.get("document_id"))
        job = ocr_queue.submit(repository, document_id=document_id, case_id=case_id,
                               applicant_id=applicant_id, party_id=document.get("party_id"),
                               document_type=document.get("document_type"))
        if job is None:
            sources[document_id] = "RECORDED"
            return
        jobs[document_id] = job.job_id
        claimed = ocr_queue.claim(repository, job)
        if claimed is None:
            sources[document_id] = "PROCESSING"
            return
        # THE READS RUN ON A POOL OF THEIR OWN, sized to the concurrency limit:
        # the limit holds for as long as the reads actually run (not only while
        # someone is waiting on them), and a read that outlives the budget
        # finishes there without holding the request's event loop.
        if process_pool is not None:
            future = loop.run_in_executor(process_pool, _read_in_process,
                                          shared[0], shared[1], document_id)
        else:
            future = loop.run_in_executor(pool, contextvars.copy_context().run,
                                          ocr_queue.finish, repository, claimed)
        try:
            done = await asyncio.wait_for(asyncio.shield(future), timeout=budget_seconds())
            # ONLY A READ THAT COMPLETED verified anything; a failed read
            # stays queued (retried) or FAILED, and is reported as it is
            status = done if isinstance(done, str) else \
                getattr(getattr(done, "status", None), "value", getattr(done, "status", None))
            if status in (None, "COMPLETED"):
                sources[document_id] = "VERIFIED_NOW"
            else:
                sources[document_id] = "FAILED" if status == "FAILED" else "PROCESSING"
        except asyncio.TimeoutError:
            sources[document_id] = "PROCESSING"
        except concurrent.futures.process.BrokenProcessPool:
            # A WORKER THAT COULD NOT START (a spawn failure under memory or
            # handle pressure) breaks the whole pool: it is rebuilt for the
            # next request, and THIS document is read on the thread path now
            # -- the same job step, never skipped, never read twice.
            _discard_process_pool()
            try:
                done = await asyncio.wait_for(loop.run_in_executor(
                    _pool(), contextvars.copy_context().run, ocr_queue.finish, repository, claimed),
                    timeout=budget_seconds())
                status = getattr(getattr(done, "status", None), "value", getattr(done, "status", None))
                sources[document_id] = "VERIFIED_NOW" if status in (None, "COMPLETED") else (
                    "FAILED" if status == "FAILED" else "PROCESSING")
            except Exception:  # noqa: BLE001 - its durable job finishes it
                sources[document_id] = "PROCESSING"
        except Exception as exc:  # noqa: BLE001 - one document never costs the others
            # its durable job finishes it; WHY it did not finish here is logged
            import logging

            logging.getLogger(__name__).warning("verify-now read of %s did not finish here: %s: %s",
                                                document_id, type(exc).__name__, exc)
            sources[document_id] = "PROCESSING"

    await asyncio.gather(*(one(d) for d in pending))
    # verdicts were just written: every later read in this request is live
    from app.store import request_cache

    request_cache.invalidate()
    return sources, jobs, round((time.perf_counter() - started) * 1000, 1)


def _line(entry: dict[str, Any], multi_party: bool) -> str:
    whose = ""
    if multi_party:
        whose = " (co-applicant)" if entry.get("party_role") == "CO_APPLICANT" else " (you)"
    label = f"{entry['label']}{whose}"
    verdict = entry["verdict"]
    said = {"PASS": "verified", "FAIL": "failed verification", "REVIEW": "needs a reviewer",
            "PENDING": "uploaded, not verified yet", "PROCESSING": "being verified now",
            "MISSING": "not uploaded yet"}.get(verdict, verdict.lower())
    parts = [f"{label} -- {said}"]
    if entry.get("score_recorded"):
        parts.append(f"score {entry['score']}"
                     + (f", confidence {entry['confidence']}" if entry.get("confidence") is not None else ""))
    if entry.get("reason") and verdict in ("FAIL", "REVIEW"):
        parts.append(entry["reason"].rstrip(".").lower())
    step = entry.get("next_action") or {}
    if step.get("label") and verdict in ("FAIL", "MISSING"):
        parts.append(f"next: {step['label'][0].lower()}{step['label'][1:]}")
    return "; ".join(parts)


_AGAIN = re.compile(r"\b(re-?verify|re-?check|again|dobara|phir\s+se|fir\s+se)\b", re.I)


async def run(req: Request, *, documents: list[dict[str, Any]], checklist: list[dict[str, Any]],
              party: str | None, repository: Any, case_id: str, applicant_id: str,
              can_write: bool, req_text: str | None = None) -> dict[str, Any]:
    """The verification answer: prose + the structured block + processing."""
    checklist = [r for r in checklist or [] if isinstance(r, dict)]
    targets = _in_scope(documents, checklist, req, party)
    # a VERIFIED copy of the same type for the same person settles that type:
    # an earlier failed / reviewed attempt is history, not a result to act on
    settled = {(str(d.get("document_type")), str(d.get("party_role") or "PRIMARY_APPLICANT"))
               for d in targets if str(d.get("verification_status") or "").upper() == "PASS"}
    # ONLY a wrong-type attempt at a slot (a licence uploaded as the PAN) is
    # superseded by a verified copy; several statements are several documents
    targets = [d for d in targets
               if not ("DOCUMENT_TYPE_MISMATCH" in (d.get("reason_codes") or [])
                       and (str(d.get("document_type")), str(d.get("party_role") or "PRIMARY_APPLICANT"))
                       in settled)]
    multi_party = len({str(d.get("party_role") or "PRIMARY_APPLICANT") for d in documents}) > 1 \
        and party not in ("PRIMARY_APPLICANT", "CO_APPLICANT")
    scores = _scores(repository, case_id)

    if req.scope == SELECTION:
        block = structured.verification_block(targets, checklist, scores=scores, include_missing=True)
        lines = [f"{i}. {_line(e, multi_party)}" for i, e in enumerate(block["documents"], 1)]
        message = ("Sure. Here's what can be verified on this application:\n" + "\n".join(lines)
                   if lines else "No documents have been uploaded on this application yet.")
        return {"answer": message, "verification": block, "response_type": "DOCUMENT_VERIFICATION_SELECTION",
                "actions": [a for e in block["documents"] for a in e["actions"]
                            if a and a.get("action") in ("UPLOAD_DOCUMENT",) or (a and a.get("enabled"))],
                "processing": None}

    if req.document_type and not targets:
        # nothing uploaded for it: say so, with the upload action
        rows = [r for r in checklist if str(r.get("slot") or "").upper() == req.document_type.upper()
                or req.document_type.upper() in [str(a).upper() for a in r.get("accepts") or []]]
        block = {"documents": [structured.missing_entry(rows[0])] if rows else [], "summary": {},
                 "needs_attention": []}
        name = structured._readable_type(req.document_type)
        return {"answer": f"No {name} has been uploaded yet, so there is nothing to verify. Upload it and "
                          f"it is verified as part of the upload.",
                "verification": block, "response_type": "DOCUMENT_VERIFICATION_RESULT",
                "actions": [e["next_action"] for e in block["documents"] if e.get("next_action")],
                "processing": None}

    # WAITING FOR VERIFICATION: no verdict yet, or a provisional one while its
    # durable job is still QUEUED (a document deferred to the background read)
    queued: set[str] = set()
    try:
        from app.store.ocr_queue import OcrJobStatus

        queued = {str(j.document_id) for j in repository.get_ocr_jobs(case_id)
                  if j.status is OcrJobStatus.QUEUED}
    except Exception:  # noqa: BLE001 - no queue: only unverified documents wait
        queued = set()
    pending = [d for d in targets
               if str(d.get("document_id")) in queued
               or (not d.get("verification_status")
                   and str(d.get("status") or "").upper() in ("UPLOADED", "PROCESSING"))]
    sources: dict[str, str] = {}
    jobs: dict[str, str] = {}
    elapsed = 0.0
    if pending and req.scope != REPORT and can_write:
        sources, jobs, elapsed = await _verify_pending(repository, pending, case_id=case_id,
                                                       applicant_id=applicant_id)
        refreshed = []
        for d in targets:
            if sources.get(str(d.get("document_id"))) == "VERIFIED_NOW":
                refreshed.append(_public(repository.get_document(str(d.get("document_id"))), d))
            elif sources.get(str(d.get("document_id"))) == "PROCESSING":
                refreshed.append({**d, "status": "PROCESSING", "verification_status": None})
            else:
                refreshed.append(d)
        targets = refreshed
        scores = _scores(repository, case_id)

    block = structured.verification_block(targets, checklist, scores=scores, sources=sources)
    counts = block["summary"]
    lines = [_line(e, multi_party) for e in block["documents"]]
    reused = sum(1 for e in block["documents"] if e["source"] == "RECORDED" and e["verdict"] != "PENDING")
    if req.scope == REPORT and not req.document_type:
        # EVERY uploaded document's recorded numbers, one line each
        lines = [_line(e, multi_party) for e in block["documents"]]
        head = ("Recorded verification scores:\n" + "\n".join(f"- {line}" for line in lines)
                if lines else "No documents have been uploaded on this application yet.")
        return {"answer": head, "verification": block, "response_type": "DOCUMENT_VERIFICATION_RESULT",
                "actions": [], "processing": None}
    if req.scope == REPORT:
        entry = block["documents"][0] if block["documents"] else None
        if entry and entry["score_recorded"]:
            head = f"The recorded verification score for the {entry['label']} is {entry['score']}"
            head += (f", with confidence {entry['confidence']}." if entry.get("confidence") is not None else ".")
        elif entry:
            head = (f"No verification score was recorded for the {entry['label']} -- its recorded result "
                    f"is: {_line(entry, multi_party).split(' -- ', 1)[1]}.")
        else:
            head = "That document hasn't been uploaded."
        return {"answer": head, "verification": block, "response_type": "DOCUMENT_VERIFICATION_RESULT",
                "actions": [], "processing": None}
    if pending and not can_write:
        head = ("Here are the recorded verification results. Starting a new verification needs "
                "upload permission, which this session doesn't have.")
    elif reused == len(block["documents"]):
        head = "These documents were verified when they were uploaded -- here are the results:"
    elif any(v == "PROCESSING" for v in sources.values()):
        # NEVER A FALSE "DONE": something is still running
        head = "I've started the verification -- here's where each document stands:"
    else:
        head = "Done -- here's where each document stands:"
    answer = head + "\n" + "\n".join(f"- {line}" for line in lines)
    # A RE-VERIFICATION of a type with no background reader cannot be run from
    # the chat: said plainly, with the upload route (verification_adapters.py)
    if req.scope == ONE and not pending and _AGAIN.search(req_text or ""):
        from app.agents.applicant.copilot.capabilities import verification_adapters

        adapter = verification_adapters.adapter_for(req.document_type)
        if adapter is not None and not adapter.background_reader:
            answer += (f"\nTo verify the {structured._readable_type(req.document_type)} again, upload it "
                       "again -- it is verified as part of the upload.")
    processing = {
        "parallel": True, "concurrency": concurrency(), "budget_s": budget_seconds(),
        "verified_now": sum(1 for s in sources.values() if s == "VERIFIED_NOW"),
        "reused_from_record": reused,
        "processing": [{"document_id": k, "job_id": jobs.get(k), "status": "PROCESSING"}
                       for k, s in sources.items() if s == "PROCESSING"],
        "elapsed_ms": elapsed, "summary": counts,
    }
    actions = [e["next_action"] for e in block["documents"]
               if e.get("next_action") and e["verdict"] in ("FAIL", "MISSING")]
    return {"answer": answer, "verification": block, "response_type": "DOCUMENT_VERIFICATION_RESULT",
            "actions": actions, "processing": processing}


__all__ = ["ALL", "ONE", "REPORT", "Request", "SELECTION", "WRITE_SCOPES", "budget_seconds",
           "concurrency", "request", "run"]
