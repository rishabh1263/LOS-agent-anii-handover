"""Runtime timing logs: one line per document request (success, refusal, crash), with stage timings, nothing personal."""

from __future__ import annotations

import logging

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.observability import doc_timing


@pytest.fixture
def tiny():
    router = APIRouter(prefix="/pan", route_class=doc_timing.timed_route("PAN"))

    @router.post("/ok")
    async def ok():
        doc_timing.note_stages({"ocr_ms": 12.5, "flag": True})
        return {"request_id": "pan_1", "status": "VERIFIED", "timings_ms": {"decode_ms": 1.25}}

    @router.post("/refused")
    async def refused():
        raise HTTPException(422, detail={"code": "PDF_ACTIVE_CONTENT", "request_id": "pan_2", "message": "x"})

    @router.post("/crash")
    async def crash():
        raise RuntimeError("boom")

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def _lines(caplog):
    return [r for r in caplog.records if r.name == "doc_timing"]


def test_a_success_logs_status_request_id_and_every_stage(tiny, caplog):
    caplog.set_level(logging.INFO, logger="doc_timing")
    assert tiny.post("/pan/ok", files={"file": ("RAHUL SHARMA pan.jpg", b"x")}).status_code == 200
    [rec] = _lines(caplog)
    msg = rec.getMessage()
    assert rec.levelno == logging.INFO
    for part in ("doc=PAN", "route=/pan/ok", "http=200", "status=VERIFIED", "request_id=pan_1", "total_ms=",
                 "decode_ms:1.2", "ocr_ms:12.5"):
        assert part in msg, part
    assert "flag" not in msg and "RAHUL" not in msg            # a non-number is dropped; no file name is logged


def test_a_refusal_and_a_crash_are_logged_too(tiny, caplog):
    caplog.set_level(logging.INFO, logger="doc_timing")
    assert tiny.post("/pan/refused").status_code == 422
    assert tiny.post("/pan/crash").status_code == 500
    refused, crash = (r.getMessage() for r in _lines(caplog))
    assert "http=422" in refused and "code=PDF_ACTIVE_CONTENT" in refused and "request_id=pan_2" in refused
    assert "http=500" in crash and "code=UNHANDLED_ERROR" in crash


def test_a_slow_request_is_a_warning_and_the_log_can_be_switched_off(tiny, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="doc_timing")
    monkeypatch.setenv("DOC_TIMING_SLOW_MS", "0")
    tiny.post("/pan/ok")
    assert _lines(caplog)[-1].levelno == logging.WARNING
    monkeypatch.setenv("DOC_TIMING_LOG", "false")
    caplog.clear()
    tiny.post("/pan/ok")
    assert _lines(caplog) == []


@pytest.mark.parametrize("route, doc", [
    ("/api/v1/pan/verify-extract", "PAN"),
    ("/api/v1/marksheet/verify-marksheet", "MARKSHEET"),
    ("/api/v1/passbook/verify-extract", "PASSBOOK"),
    ("/api/v1/hrms/signature/verify", "SIGNATURE"),
    ("/api/v1/hrms/resume/verify-extract", "RESUME"),
])
def test_every_document_route_logs_its_timing(route, doc, make_token, caplog):
    from main import app

    caplog.set_level(logging.INFO, logger="doc_timing")
    client = TestClient(app)
    token = make_token(scopes=["pan:verify", "hrms:verify", "documents:write", "upload_document"])
    r = client.post(route, files={"file": ("empty.pdf", b"", "application/pdf")},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (400, 415, 422)                     # an empty upload is refused, and still timed
    lines = [x.getMessage() for x in _lines(caplog)]
    assert any(f"doc={doc}" in line and f"route={route}" in line and f"http={r.status_code}" in line
               for line in lines), lines
