"""
THE FOS CHAT UPLOAD RUNS VERIFY -> OCR EXTRACTION -> KYC (2026-10-04, default on).

  - a document that PASSES verification releases its OCR fields, and KYC compares them
  - a document that does NOT pass goes no further: no fields, no KYC, and the
    response says so per step (EXTRACT / KYC SKIPPED: VERIFICATION_NOT_PASSED)
  - identifiers in the released fields are masked
  - income comparison and eligibility do NOT run at FOS
  - FOS_KYC_ON_UPLOAD=false restores verification-only (test_fos_stage_boundary.py)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.test_fos_stage_boundary import FOS_SCOPES, open_case, upload  # noqa: F401
from app.agents.applicant import config as agent_config
from app.store import set_repository
from app.store.testing import fresh_repository

PAN = Path("samples/lPan.jpg")
DL = Path("samples/documents/driving_license.jpg")
VOTER = Path("samples/documents/voter_id2.jpg")
pytestmark = pytest.mark.skipif(not (PAN.exists() and DL.exists()), reason="real samples not present")


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    monkeypatch.delenv("FOS_KYC_ON_UPLOAD", raising=False)
    agent_config.reload()
    repository = fresh_repository(tmp_path / "fos_kyc.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)
    agent_config.reload()


@pytest.fixture
def client(make_token):
    from fastapi.testclient import TestClient

    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES)}"})
    return c


def _cards(body):
    return {c["source_id"]: c for c in (body.get("verification") or {}).get("documents_processed") or []}


def test_passed_documents_are_extracted_and_kyc_compares_them(client):
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id, [("pan.jpg", PAN), ("dl.jpg", DL)],
                  ["PAN", "DRIVING_LICENCE"]).json()
    cards = _cards(body)
    for name in ("pan.jpg", "dl.jpg"):
        steps = {s["step"]: s for s in cards[name]["pipeline"]}
        assert steps["VERIFY"]["status"] == "PASS" and steps["EXTRACT"]["status"] == "DONE"
        assert steps["KYC"]["status"] in ("PASS", "REVIEW", "FAIL", "PARTIAL")
        assert cards[name]["extracted_fields"]
    # the PAN and the licence are two different people: KYC must say so
    assert body["kyc"]["status"] == "REVIEW" and "NAME_MISMATCH" in body["kyc"]["reason_codes"]
    assert "KYC needs review" in body["answer"]
    # identifiers masked in the released fields
    assert "X" in cards["pan.jpg"]["extracted_fields"]["pan_number"]
    assert "X" in cards["dl.jpg"]["extracted_fields"]["dl_number"]


def test_a_document_that_does_not_pass_goes_no_further(client):
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id, [("voter.jpg", VOTER)], ["SIGNATURE"]).json()
    card = _cards(body)["voter.jpg"]
    steps = {s["step"]: s for s in card["pipeline"]}
    assert steps["VERIFY"]["status"] != "PASS"
    assert steps["EXTRACT"] == {"step": "EXTRACT", "status": "SKIPPED", "reason": "VERIFICATION_NOT_PASSED"}
    assert steps["KYC"]["status"] == "SKIPPED"
    assert card["extracted_fields"] is None and body["kyc"] is None
    # the answer says it is held and reads nothing out; "KYC was not run" is not
    # said for a signature, which is never a KYC input (2026-10-05)
    assert "needs a review" in body["answer"] or "did not pass" in body["answer"]
    assert "Details were read" not in body["answer"]


def test_income_and_eligibility_do_not_run_at_fos(client, monkeypatch):
    from app.agents.los import flow

    def forbidden(*a, **k):
        raise AssertionError("eligibility / income ran at FOS")

    monkeypatch.setattr(flow, "_eligibility_for", forbidden)
    monkeypatch.setattr(flow, "_income_consistency_for", forbidden)
    applicant_id, case_id = open_case(client)
    response = upload(client, applicant_id, case_id, [("pan.jpg", PAN), ("dl.jpg", DL)], ["PAN", "DRIVING_LICENCE"])
    assert response.status_code == 200 and response.json()["kyc"] is not None


def test_kyc_compares_across_uploads_not_only_within_one(client, monkeypatch):
    """A licence uploaded on its own is checked against the PAN uploaded before it."""
    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")       # as production: verdicts are recorded
    applicant_id, case_id = open_case(client)
    first = upload(client, applicant_id, case_id, [("pan.jpg", PAN)], ["PAN"]).json()
    assert first["kyc"]["reason_codes"] == ["INSUFFICIENT_SOURCES"]      # nothing to compare yet
    assert "once another verified" in first["answer"]
    second = upload(client, applicant_id, case_id, [("dl.jpg", DL)], ["DRIVING_LICENCE"]).json()
    assert second["kyc"]["status"] == "REVIEW"
    assert "NAME_MISMATCH" in second["kyc"]["reason_codes"]               # the earlier PAN took part


def test_an_oversized_or_crowded_upload_is_refused_whole(client, monkeypatch):
    """The FOS chat upload read every part whole with no cap: a 30 MB file was accepted
    (HTTP E2E, 2026-10-05). The same limits as POST /los/process now apply, before any OCR."""
    from app.agents.los import flow

    def never(*a, **k):
        raise AssertionError("an oversized upload reached processing")

    monkeypatch.setattr(flow, "process_application", never)
    applicant_id, case_id = open_case(client)
    big = b"\xff\xd8" + b"0" * (25 * 1024 * 1024 + 10)
    r = client.post("/api/v1/fos/copilot", data={"applicant_id": applicant_id, "case_id": case_id,
                                                 "action": "UPLOAD_DOCUMENT", "document_types": "PAN"},
                    files=[("files", ("big.jpg", big, "image/jpeg"))])
    assert r.status_code == 413 and r.json()["detail"]["error"] == "FILE_TOO_LARGE"
    many = [("files", (f"p{i}.jpg", PAN.read_bytes(), "image/jpeg")) for i in range(11)]
    r = client.post("/api/v1/fos/copilot", data={"applicant_id": applicant_id, "case_id": case_id,
                                                 "action": "UPLOAD_DOCUMENT"}, files=many)
    assert r.status_code == 413 and r.json()["detail"]["error"] == "TOO_MANY_FILES"
