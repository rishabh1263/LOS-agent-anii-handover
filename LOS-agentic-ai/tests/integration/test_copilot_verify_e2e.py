"""
VERIFY, END TO END, over the real HTTP API with a caller that is genuinely
allowed to upload (the FOS scopes, nothing widened for the test):

    upload (several real statements, one request)
      -> the LOS pipeline persists each document and its bytes
      -> a statement the inline budget cannot finish is deferred: REVIEW /
         DOCUMENT_QUEUED_FOR_PROCESSING, with a durable QUEUED job
      -> "sab documents verify kar do" (the Copilot)
      -> the queued jobs are claimed and read by the EXISTING worker step,
         IN PARALLEL, within COPILOT_VERIFY_CONCURRENCY
      -> the verifier's verdict AND score are persisted
      -> REST (GET_VERIFICATION_STATUS) and the Copilot both report the
         persisted score; asking again reads nothing twice.

The inline budget is set tiny so the deferral does not depend on the speed
of the machine; the worker reads with its own (generous) budget.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import config as agent_config
from app.store import set_repository
from app.store.documents import LocalDocumentStore, set_document_store
from app.store.testing import fresh_repository

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "documents"
STATEMENT = SAMPLES / "demo_bank_statement.pdf"
QUEUED = "DOCUMENT_QUEUED_FOR_PROCESSING"

FOS_SCOPES = [
    "read_applicant", "read_application", "read_documents", "read_verification",
    "read_pending_items", "read_next_action", "create_applicant",
    "update_applicant", "create_application", "upload_document",
]

pytestmark = pytest.mark.skipif(not STATEMENT.exists(), reason="sample statement not available")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("LOS_OCR_WORKER_ENABLED", "false")      # the Copilot drives the jobs here
    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "1")   # defer every statement on upload
    monkeypatch.setenv("LOS_OCR_WORKER_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("COPILOT_VERIFY_BUDGET_S", "120")
    agent_config.reload()
    from app.agents.los import config as los_config

    los_config.reload()
    repository = fresh_repository(tmp_path / "verify_e2e.sqlite3")
    repository.initialise()
    set_repository(repository)
    set_document_store(LocalDocumentStore(tmp_path / "documents"))
    yield repository
    set_repository(None)
    set_document_store(None)
    agent_config.reload()
    los_config.reload()


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES)}"})
    return c


def _open_case(client) -> tuple[str, str]:
    response = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "Test Applicant", "mobile": "9876543210",
                      "date_of_birth": "1990-04-12", "address": "Mumbai, Maharashtra"},
        "application": {"product": "PERSONAL_LOAN", "loan_amount": 500000},
    })
    assert response.status_code == 201, response.text
    body = response.json()
    return body["applicant_id"], body["case_id"]


#: Three REAL statements (different banks, real length), so a full read takes
#: long enough for parallel work to be observable.
REAL_STATEMENTS = [SAMPLES / "Canara Bank Statement.pdf", SAMPLES / "SBI Bank Statement.pdf",
                   SAMPLES / "KOTAK BANK STATEMENT.pdf"]


def _upload_many(client, applicant_id, case_id, count: int, *, real: bool = False):
    if real:
        files = [("files", (p.name.replace(" ", "_"), p.read_bytes(), "application/pdf"))
                 for p in REAL_STATEMENTS[:count]]
    else:
        content = STATEMENT.read_bytes()
        files = [("files", (f"statement_{i}.pdf", content, "application/pdf")) for i in range(count)]
    data = {"applicant_id": applicant_id, "case_id": case_id, "action": "UPLOAD_DOCUMENT",
            "document_types": ",".join(["BANK_STATEMENT"] * count)}
    response = client.post("/api/v1/fos/copilot", data=data, files=files)
    assert response.status_code == 200, response.text
    return response.json()


def _ask(client, applicant_id, case_id, message=None, action="CUSTOM_QUERY"):
    body = {"applicant_id": applicant_id, "case_id": case_id, "action": action}
    if message:
        body["message"] = message
    response = client.post("/api/v1/fos/copilot", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _instrument_reads(monkeypatch):
    """Count EVERY read of a document and the peak number running at once."""
    from app.store import ocr_queue

    real = ocr_queue._read
    state = {"reads": [], "running": 0, "peak": 0}
    lock = threading.Lock()

    def counted(repository, job):
        with lock:
            state["reads"].append(job.document_id)
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        try:
            return real(repository, job)
        finally:
            with lock:
                state["running"] -= 1

    monkeypatch.setattr(ocr_queue, "_read", counted)
    return state


def test_verify_now_runs_the_queued_documents_in_parallel_and_persists_scores(
        client, _isolated, monkeypatch):
    monkeypatch.setenv("COPILOT_VERIFY_CONCURRENCY", "2")
    monkeypatch.setenv("COPILOT_VERIFY_EXECUTOR", "thread")     # in-process: the reads can be counted here
    applicant_id, case_id = _open_case(client)

    if not all(p.exists() for p in REAL_STATEMENTS):
        pytest.skip("real statements not available")
    uploaded = _upload_many(client, applicant_id, case_id, 3, real=True)
    jobs = {j["document_id"]: j for j in (uploaded.get("processing_queue") or [])}
    before = _ask(client, applicant_id, case_id, action="GET_VERIFICATION_STATUS")
    from app.store.ocr_queue import OcrJobStatus

    queued_ids = {j.document_id for j in _isolated.get_ocr_jobs(case_id) if j.status is OcrJobStatus.QUEUED}
    deferred = [d for d in before["verification"]["documents"] if d["document_id"] in queued_ids]
    assert len(deferred) >= 2, (before["verification"], queued_ids)     # deferred to the durable queue
    assert all(d["verdict"] == "REVIEW" for d in deferred)          # provisional, awaiting the full read

    # the upload was forced to defer; the worker then reads with a real budget
    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "25000")
    reads = _instrument_reads(monkeypatch)
    started = time.perf_counter()
    verified = _ask(client, applicant_id, case_id, "sab documents verify kar do")
    elapsed = time.perf_counter() - started

    assert verified["response_type"] == "DOCUMENT_VERIFICATION_RESULT"
    processing = verified["processing"]
    assert processing["parallel"] is True and processing["concurrency"] == 2
    assert processing["verified_now"] == len(deferred), processing
    assert sorted(reads["reads"]) == sorted({d["document_id"] for d in deferred})   # each read once
    assert 1 < reads["peak"] <= 2, reads                                              # parallel, bounded
    cards = verified["verification"]["documents"]
    assert {c["source"] for c in cards if c["document_id"] in queued_ids} == {"VERIFIED_NOW"}
    assert all(c["verdict"] in ("PASS", "REVIEW", "FAIL") for c in cards)
    assert all(QUEUED not in c["reason_codes"] for c in cards if c["document_id"] in queued_ids)
    scored = [c for c in cards if c["score"] is not None]
    assert scored, cards                       # the verifier scored them; the score was kept

    # THE PERSISTED SCORE: the verification finding, REST and the Copilot agree
    findings = {f.document_id: f for f in _isolated.get_current_findings(case_id, kind="VERIFICATION")
                if str(getattr(f.finding_kind, "value", f.finding_kind)) == "VERIFICATION"
                and f.score is not None}
    rest = _ask(client, applicant_id, case_id, action="GET_VERIFICATION_STATUS")
    for card in rest["verification"]["documents"]:
        assert card["score"] == findings[card["document_id"]].score
        assert card["confidence"] == findings[card["document_id"]].confidence

    # ASKING AGAIN READS NOTHING AGAIN: the recorded results are reused
    again = _ask(client, applicant_id, case_id, "sab documents verify kar do")
    assert len(reads["reads"]) == len(deferred)
    assert {c["source"] for c in again["verification"]["documents"]} == {"RECORDED"}
    assert [c["score"] for c in again["verification"]["documents"]] == \
        [card["score"] for card in rest["verification"]["documents"]]
    assert jobs or processing                  # job ids travelled with the upload / the run
    print(f"\nVERIFY_E2E parallel verification of 3 documents: {elapsed * 1000:.0f} ms")


def test_a_caller_without_upload_permission_starts_nothing(client, make_token, monkeypatch):
    if not REAL_STATEMENTS[0].exists():
        pytest.skip("real statement not available")
    applicant_id, case_id = _open_case(client)
    _upload_many(client, applicant_id, case_id, 1, real=True)          # deferred: waits in the queue
    reads = _instrument_reads(monkeypatch)
    import main

    reader = TestClient(main.app)
    # the SAME subject, read scopes only: allowed to see the case, not to change it
    reader.headers.update({"Authorization": "Bearer " + make_token(
        scopes=[s for s in FOS_SCOPES if s.startswith("read_")])})
    body = reader.post("/api/v1/fos/copilot", json={
        "applicant_id": applicant_id, "case_id": case_id, "action": "CUSTOM_QUERY",
        "message": "sab documents verify kar do"}).json()
    assert reads["reads"] == []
    assert "permission" in body["answer"]


def test_a_single_document_verified_now(client, _isolated, monkeypatch):
    if not REAL_STATEMENTS[0].exists():
        pytest.skip("real statement not available")
    applicant_id, case_id = _open_case(client)
    _upload_many(client, applicant_id, case_id, 1, real=True)
    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "25000")
    monkeypatch.setenv("COPILOT_VERIFY_EXECUTOR", "thread")
    reads = _instrument_reads(monkeypatch)
    started = time.perf_counter()
    body = _ask(client, applicant_id, case_id, "bank statement verify karo")
    print("SINGLE", body["answer"][:200])
    elapsed = time.perf_counter() - started
    card = body["verification"]["documents"][0]
    assert card["source"] == "VERIFIED_NOW" and len(reads["reads"]) == 1
    assert card["verdict"] in ("PASS", "REVIEW", "FAIL")
    print(f"\nVERIFY_E2E single document: {elapsed * 1000:.0f} ms")


def test_a_slow_verification_answers_fast_and_finishes_in_the_background(client, _isolated, monkeypatch):
    """The default budget: the person gets an answer at once, PROCESSING with
    job ids; the reads carry on and the next ask reports their results."""
    if not all(p.exists() for p in REAL_STATEMENTS):
        pytest.skip("real statements not available")
    monkeypatch.setenv("COPILOT_VERIFY_BUDGET_S", "2")
    monkeypatch.setenv("COPILOT_VERIFY_CONCURRENCY", "3")
    applicant_id, case_id = _open_case(client)
    _upload_many(client, applicant_id, case_id, 3, real=True)
    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "25000")

    started = time.perf_counter()
    first = _ask(client, applicant_id, case_id, "sab documents verify kar do")
    elapsed = time.perf_counter() - started
    assert elapsed < 8, elapsed                                   # budget + request overhead
    running = first["processing"]["processing"]
    assert running and all(r["job_id"] for r in running), first["processing"]
    assert any(c["verdict"] == "PROCESSING" for c in first["verification"]["documents"])

    from app.store.ocr_queue import OcrJobStatus

    deadline = time.time() + 150
    while time.time() < deadline:
        if all(j.status is OcrJobStatus.COMPLETED for j in _isolated.get_ocr_jobs(case_id)
               if j.document_id in {r["document_id"] for r in running}):
            break
        time.sleep(1)
    later = _ask(client, applicant_id, case_id, "sab documents verify kar do")
    assert all(c["verdict"] != "PROCESSING" for c in later["verification"]["documents"])
    print(f"\nVERIFY_E2E budgeted answer: {elapsed * 1000:.0f} ms, background completion within the deadline")


def test_process_mode_reads_each_document_once_and_keeps_the_server_responsive(client, _isolated, monkeypatch):
    """The default executor: the reads run in worker processes (the same job
    step), each document is read exactly once, and the serving process stays
    free -- a light question asked during the heavy verification answers fast."""
    if not all(p.exists() for p in REAL_STATEMENTS):
        pytest.skip("real statements not available")
    import threading as _threading

    from app.store.ocr_queue import OcrJobStatus

    monkeypatch.setenv("COPILOT_VERIFY_EXECUTOR", "process")
    monkeypatch.setenv("COPILOT_VERIFY_CONCURRENCY", "3")
    # generous: three real statements read at once must finish inside the
    # request even when the machine is shared with other test processes
    monkeypatch.setenv("COPILOT_VERIFY_BUDGET_S", "600")
    applicant_id, case_id = _open_case(client)
    _upload_many(client, applicant_id, case_id, 3, real=True)
    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "25000")
    queued = {j.document_id for j in _isolated.get_ocr_jobs(case_id) if j.status is OcrJobStatus.QUEUED}

    result = {}
    worker = _threading.Thread(target=lambda: result.update(
        body=_ask(client, applicant_id, case_id, "sab documents verify kar do")))
    worker.start()
    time.sleep(1.0)                                            # the reads are running now
    started = time.perf_counter()
    light = _ask(client, applicant_id, case_id, "what is my loan amount?")
    light_ms = (time.perf_counter() - started) * 1000
    worker.join(timeout=300)

    assert "5,00,000" in light["answer"] or "500000" in light["answer"], light["answer"]
    assert light_ms < 3000, light_ms                           # the server was not starved
    processing = result["body"]["processing"]
    # THE CONTRACT: each document is verified now, or reported PROCESSING with its
    # job (a read that could not finish here -- e.g. a store lock under load --
    # stays with its durable job). Nothing is reported done that is not done.
    still = {p["document_id"] for p in processing["processing"]}
    assert processing["verified_now"] + len(still) == len(queued), processing
    assert all(p["job_id"] for p in processing["processing"])
    jobs = {j.document_id: j for j in _isolated.get_ocr_jobs(case_id)}
    done_now = queued - still
    assert all(jobs[d].status is OcrJobStatus.COMPLETED and jobs[d].attempts == 1 for d in done_now)
    # the durable job finishes whatever is left -- and nothing is read twice
    from app.store import ocr_queue

    ocr_queue.drain(_isolated)
    jobs = {j.document_id: j for j in _isolated.get_ocr_jobs(case_id)}
    assert all(jobs[d].status is OcrJobStatus.COMPLETED for d in queued), {d: jobs[d].status for d in queued}
    print(f"\nVERIFY_E2E process mode: light request during verification {light_ms:.0f} ms")


# ---- PENDING WORK: "jo pending hai kar do" (capabilities/work.py) -----------------------
def _ask_in(client, applicant_id, case_id, message, context=None):
    body = {"applicant_id": applicant_id, "case_id": case_id, "action": "CUSTOM_QUERY", "message": message}
    if context:
        body["context"] = context
    response = client.post("/api/v1/fos/copilot", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_do_everything_runs_what_it_can_and_reads_the_result_back(client, _isolated, monkeypatch):
    from app.store.ocr_queue import OcrJobStatus

    monkeypatch.setenv("COPILOT_VERIFY_EXECUTOR", "thread")
    reads = _instrument_reads(monkeypatch)
    applicant_id, case_id = _open_case(client)
    _upload_many(client, applicant_id, case_id, 2)
    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "25000")

    body = _ask(client, applicant_id, case_id, "check kar aur jo pending hai kar de")
    assert body["response_type"] == "ACTION_RESULT" and body["scope"] == "CASE"
    work = body["pending_work"]
    assert work["mode"] == "DO" and work["assistant_can_run"] is True
    assert {e["result"] for e in work["executed"]} == {"VERIFIED_NOW"} and len(work["executed"]) == 2
    # said in the question's language (Hinglish here), from the same executed results
    assert body["language_contract"]["reply_language"] == "hi-Latn"
    assert "verification ho gaya" in body["answer"]
    # THE REPORTED STATE IS THE RECORDED STATE: every executed item now has a verdict
    ran = {e["document_id"] for e in work["executed"]}
    assert all(i["state"] != "PENDING" for i in work["items"] if i["document_id"] in ran)
    assert all(j.status is OcrJobStatus.COMPLETED for j in _isolated.get_ocr_jobs(case_id))
    # what the person still has to do is an UPLOAD action, never a fake button
    assert all(a["action"] in ("UPLOAD_DOCUMENT", "VERIFY_DOCUMENT") for a in body["actions"])
    assert work["summary"]["user_action_required"] >= 1        # the other FOS slots are still missing

    # ASKED AGAIN: nothing is re-read, nothing re-run, nothing claimed
    before = len(reads["reads"])
    again = _ask(client, applicant_id, case_id, "jo pending hai kar do")
    assert again["pending_work"]["executed"] == [] and len(reads["reads"]) == before == 2
    assert "verification ho gaya" not in again["answer"]              # nothing claimed a second time


def test_what_can_you_do_offers_the_verification_and_yes_runs_it(client, _isolated, monkeypatch):
    monkeypatch.setenv("COPILOT_VERIFY_EXECUTOR", "thread")
    applicant_id, case_id = _open_case(client)
    _upload_many(client, applicant_id, case_id, 1)
    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "25000")

    offer = _ask_in(client, applicant_id, case_id, "abhi kya kar sakte ho?")
    assert offer["response_type"] == "PENDING_WORK"
    assert "I can verify" in offer["answer"] and "Shall I start" in offer["answer"]
    assert any(a["action"] == "VERIFY_DOCUMENT" for a in offer["actions"])
    yes = _ask_in(client, applicant_id, case_id, "haan", offer.get("context"))
    assert yes["processing"]["verified_now"] == 1, yes["answer"]


def test_without_upload_permission_nothing_is_run_and_it_says_why(client, _isolated, monkeypatch, make_token):
    applicant_id, case_id = _open_case(client)
    _upload_many(client, applicant_id, case_id, 1)
    reads = _instrument_reads(monkeypatch)
    client.headers["Authorization"] = "Bearer " + make_token(
        scopes=[s for s in FOS_SCOPES if s != "upload_document"])
    body = _ask(client, applicant_id, case_id, "jo pending hai kar do")
    assert body["pending_work"]["assistant_can_run"] is False
    assert body["pending_work"]["executed"] == [] and reads["reads"] == []
    assert "permission" in body["answer"]                                # said, in any language
