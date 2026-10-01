"""
The VERIFY capability (copilot/capabilities/verification.py): it understands
the request, reuses a recorded verdict instead of re-reading a document,
verifies only UPLOADED-but-unverified documents -- through the existing job
step, IN PARALLEL with bounded concurrency inside a time budget -- and never
invents a score. The job step itself is stubbed here: this is the
orchestration's contract, not OCR's.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from app.agents.applicant.copilot.capabilities import verification as cap

CHECKLIST = [
    {"slot": "PAN", "accepts": ["PAN"], "mandatory": True, "status": "UPLOADED"},
    {"slot": "ADDRESS_PROOF", "accepts": ["DRIVING_LICENCE", "PASSPORT", "VOTER_ID"], "mandatory": True,
     "status": "MISSING"},
]


def _doc(i: int, kind: str, *, verdict=None, status="UPLOADED", role="PRIMARY_APPLICANT", codes=()):
    return {"document_id": f"case:APP:{kind.lower()}{i}.jpg", "document_type": kind, "status": status,
            "verification_status": verdict, "reason_codes": list(codes), "party_role": role,
            "party_id": "APP"}


class Repo:
    """What the capability reads: findings (scores) and documents after a run."""

    def __init__(self, scores=None):
        self.scores = scores or {}
        self.verified: set[str] = set()

    def get_current_findings(self, case_id, kind=None):
        return [SimpleNamespace(finding_kind="VERIFICATION", document_id=d, score=s, confidence=None)
                for d, s in self.scores.items()]

    def get_ocr_jobs(self, case_id):
        return []

    def get_document(self, document_id):
        if document_id in self.verified:
            return SimpleNamespace(status="VERIFIED", verification_status="PASS", reason_codes=[])
        return None


@pytest.mark.parametrize("message,scope,doc", [
    ("verify", cap.SELECTION, None),
    ("verify karna hai", cap.SELECTION, None),
    ("ye wala doc verify karke batao", cap.SELECTION, None),
    ("sab documents verify kar do", cap.ALL, None),
    ("verify all my documents", cap.ALL, None),
    ("PAN verify karo", cap.ONE, "PAN"),
    ("please verify my passport", cap.ONE, "PASSPORT"),
    ("PAN ka score kitna hai?", cap.REPORT, "PAN"),
])
def test_a_verification_request_is_understood(message, scope, doc):
    req = cap.request(message)
    assert req is not None and req.scope == scope and req.document_type == doc


@pytest.mark.parametrize("message", [
    "is my PAN verified?", "PAN verified hai?", "PAN verify hua?", "what is my verification status?",
    "KYC score kya hai?", "what is my loan amount?",
])
def test_a_status_question_is_not_a_request_to_verify(message):
    assert cap.request(message) is None


def _patch_jobs(monkeypatch, *, seconds: float, repo: Repo):
    from app.store import ocr_queue

    state = {"running": 0, "peak": 0, "submitted": 0}
    lock = threading.Lock()

    def submit(repository, *, document_id, **_):
        state["submitted"] += 1
        return SimpleNamespace(job_id=f"job-{document_id}", document_id=document_id)

    def claim(repository, job):
        return job

    def finish(repository, job):
        with lock:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        time.sleep(seconds)
        with lock:
            state["running"] -= 1
        repo.verified.add(job.document_id)
        return job

    monkeypatch.setattr(ocr_queue, "submit", submit)
    monkeypatch.setattr(ocr_queue, "claim", claim)
    monkeypatch.setattr(ocr_queue, "finish", finish)
    return state


def _run(req, documents, repo, *, can_write=True, party=None):
    return asyncio.run(cap.run(req, documents=documents, checklist=CHECKLIST, party=party, repository=repo,
                               case_id="case", applicant_id="APP", can_write=can_write))


def test_independent_documents_are_verified_in_parallel(monkeypatch):
    monkeypatch.setenv("COPILOT_VERIFY_CONCURRENCY", "5")
    monkeypatch.setenv("COPILOT_VERIFY_BUDGET_S", "5")
    repo = Repo()
    state = _patch_jobs(monkeypatch, seconds=0.3, repo=repo)
    docs = [_doc(i, kind) for i, kind in enumerate(["PAN", "AADHAAR", "DRIVING_LICENCE", "PASSPORT", "VOTER_ID"])]
    started = time.perf_counter()
    out = _run(cap.Request(cap.ALL), docs, repo)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, elapsed                         # serial would be 1.5 s
    assert state["peak"] > 1
    assert out["processing"]["verified_now"] == 5
    assert {e["verdict"] for e in out["verification"]["documents"]} == {"PASS"}
    assert out["response_type"] == "DOCUMENT_VERIFICATION_RESULT"


def test_concurrency_is_bounded(monkeypatch):
    monkeypatch.setenv("COPILOT_VERIFY_CONCURRENCY", "2")
    monkeypatch.setenv("COPILOT_VERIFY_BUDGET_S", "5")
    repo = Repo()
    state = _patch_jobs(monkeypatch, seconds=0.15, repo=repo)
    _run(cap.Request(cap.ALL), [_doc(i, "PAN") for i in range(6)], repo)
    assert state["peak"] <= 2


def test_a_document_still_running_is_reported_processing_with_its_job(monkeypatch):
    monkeypatch.setenv("COPILOT_VERIFY_BUDGET_S", "0.5")
    repo = Repo()
    _patch_jobs(monkeypatch, seconds=1.2, repo=repo)
    out = _run(cap.Request(cap.ONE, "PAN"), [_doc(1, "PAN")], repo)
    running = out["processing"]["processing"]
    assert running and running[0]["job_id"] == "job-case:APP:pan1.jpg"
    assert out["verification"]["documents"][0]["verdict"] == "PROCESSING"


def test_a_recorded_verdict_is_reused_never_re_read(monkeypatch):
    repo = Repo()
    state = _patch_jobs(monkeypatch, seconds=0.0, repo=repo)
    docs = [_doc(1, "PAN", verdict="PASS", status="VERIFIED"),
            _doc(2, "PAN", verdict="FAIL", status="REJECTED", role="CO_APPLICANT",
                 codes=["DOCUMENT_TYPE_MISMATCH"])]
    out = _run(cap.Request(cap.ALL), docs, repo)
    assert state["submitted"] == 0
    entries = out["verification"]["documents"]
    assert [e["verdict"] for e in entries] == ["PASS", "FAIL"]
    failed = entries[1]
    assert failed["reason"] and failed["next_action"]["action"] == "UPLOAD_DOCUMENT"
    assert "co-applicant" in out["answer"]


def test_without_write_scope_nothing_is_started(monkeypatch):
    repo = Repo()
    state = _patch_jobs(monkeypatch, seconds=0.0, repo=repo)
    out = _run(cap.Request(cap.ALL), [_doc(1, "PAN")], repo, can_write=False)
    assert state["submitted"] == 0
    assert "permission" in out["answer"]


def test_a_score_is_published_only_when_recorded(monkeypatch):
    _patch_jobs(monkeypatch, seconds=0.0, repo=Repo())
    doc = _doc(1, "PAN", verdict="PASS", status="VERIFIED")
    none = _run(cap.Request(cap.REPORT, "PAN"), [doc], Repo())
    assert none["verification"]["documents"][0]["score"] is None
    assert "No verification score was recorded" in none["answer"]
    recorded = _run(cap.Request(cap.REPORT, "PAN"), [doc], Repo(scores={doc["document_id"]: 96}))
    assert recorded["verification"]["documents"][0]["score"] == 96
    assert "96" in recorded["answer"]


def test_plain_verify_is_a_selection_with_upload_actions_for_missing_slots(monkeypatch):
    _patch_jobs(monkeypatch, seconds=0.0, repo=Repo())
    out = _run(cap.Request(cap.SELECTION), [_doc(1, "PAN", verdict="PASS", status="VERIFIED")], Repo())
    assert out["response_type"] == "DOCUMENT_VERIFICATION_SELECTION"
    missing = [e for e in out["verification"]["documents"] if e["verdict"] == "MISSING"]
    assert missing and missing[0]["accepted_types"] == ["DRIVING_LICENCE", "PASSPORT", "VOTER_ID"]
    assert any(a.get("action") == "UPLOAD_DOCUMENT" for a in out["actions"])


def test_a_named_document_not_uploaded_offers_its_upload(monkeypatch):
    _patch_jobs(monkeypatch, seconds=0.0, repo=Repo())
    out = _run(cap.Request(cap.ONE, "PASSPORT"), [_doc(1, "PAN")], Repo())
    assert "No Passport has been uploaded" in out["answer"]
    assert out["actions"] and out["actions"][0]["action"] == "UPLOAD_DOCUMENT"


def test_a_broken_process_pool_is_discarded_and_rebuilt(monkeypatch):
    """A spawn failure breaks the whole ProcessPoolExecutor; the next read must
    not reuse it (verification._discard_process_pool)."""
    from app.agents.applicant.copilot.capabilities import verification as v

    class Broken:
        shut = False

        def shutdown(self, wait=False, cancel_futures=False):
            Broken.shut = True

    monkeypatch.setattr(v, "_PROCESS_POOL", Broken())
    monkeypatch.setattr(v, "_PROCESS_KEY", ("stale",))
    v._discard_process_pool()
    assert v._PROCESS_POOL is None and v._PROCESS_KEY is None and Broken.shut
