"""
The background worker's raised limits never reach an upload.

THE LIVE DEFECT. The worker raised its clock (300 s) and page ceiling by
writing them into `os.environ` for the length of a job. The environment is
process-wide: a 104-page statement uploaded while the worker was reading an
old scan inherited the 300 s clock, was read inline for 160 s, and held
its caller for 163 s -- the upload path should have queued it at 25 s.

These hold the worker mid-job in one thread and read the limits from
another, which is exactly the overlap that happened live.
"""

from __future__ import annotations

import os
import threading

from app.agents.bank_statement import extract
from app.store import ocr_queue


def test_raised_limits_apply_to_the_raising_thread():
    with extract.raised_limits({"BANK_STATEMENT_TIME_BUDGET_MS": 300000}):
        assert extract.time_budget_ms() == 300000


def test_raised_limits_never_reach_another_thread(monkeypatch):
    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "25000")
    monkeypatch.setenv("BANK_STATEMENT_INLINE_OCR_PAGES", "4")
    inside, release = threading.Event(), threading.Event()
    seen = {}

    def worker():
        with extract.raised_limits({"BANK_STATEMENT_TIME_BUDGET_MS": 300000,
                                    "BANK_STATEMENT_INLINE_OCR_PAGES": 60}):
            seen["worker"] = (extract.time_budget_ms(), extract.inline_ocr_pages())
            inside.set()
            release.wait(5)

    thread = threading.Thread(target=worker)
    thread.start()
    assert inside.wait(5)
    try:
        # THE UPLOAD, while the worker is mid-job.
        assert extract.time_budget_ms() == 25000
        assert extract.inline_ocr_pages() == 4
        assert os.environ["BANK_STATEMENT_TIME_BUDGET_MS"] == "25000"
    finally:
        release.set()
        thread.join(5)

    assert seen["worker"] == (300000, 60)


def test_raised_limits_are_released_after_the_block(monkeypatch):
    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "25000")
    with extract.raised_limits({"BANK_STATEMENT_TIME_BUDGET_MS": 300000}):
        pass
    assert extract.time_budget_ms() == 25000


def test_a_worker_job_never_writes_the_process_environment(monkeypatch):
    """The job reads with the worker's limits and leaves os.environ alone."""
    import types

    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "25000")
    seen = {}

    def fake_extract(job, content):
        seen["env"] = os.environ.get("BANK_STATEMENT_TIME_BUDGET_MS")
        seen["budget"] = extract.time_budget_ms()
        return None

    monkeypatch.setattr(ocr_queue, "_extract", fake_extract)

    class _Store:
        def get(self, key):
            return b"%PDF-"

    monkeypatch.setattr("app.store.documents.get_document_store", lambda: _Store())

    ocr_queue._read(None, types.SimpleNamespace(document_id="D"))

    assert seen["env"] == "25000"                                  # untouched
    assert seen["budget"] == ocr_queue.worker_timeout_seconds() * 1000
