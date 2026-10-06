"""
INDIVIDUALLY UPLOADED SIGNATURES over the real HTTP route (POST /api/v1/los/process),
with and without a known-good specimen (`reference_signature`).

EVERY IMAGE IS SYNTHETIC (tests/agents/test_signature_standalone.py explains why):
these prove the contract -- no reference is never a PASS, no comparison publishes
no score, a specimen is screened and used only for that party's signatures --
not that the comparator tells genuine signatures from forgeries.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
from test_signature_standalone import synthetic_signature  # noqa: E402

from app.store import set_repository  # noqa: E402
from app.store.testing import fresh_repository  # noqa: E402

PAN = Path(__file__).resolve().parents[2] / "samples" / "lPan.jpg"


@pytest.fixture
def client(tmp_path, make_token):
    import main

    repository = fresh_repository(tmp_path / "sig.sqlite3")
    repository.initialise()
    set_repository(repository)
    c = TestClient(main.app)
    c.headers["Authorization"] = "Bearer " + make_token(subject="sig-officer", scopes=["documents:write"])
    yield c
    set_repository(None)


def _process(client, tmp_path, *, reference=None, extra=None, case="CASE-SIGHTTP01"):
    signature = synthetic_signature(tmp_path / "sig.png").read_bytes()
    files = [("files", ("sig.png", signature, "image/png"))] + list(extra or [])
    types = ["SIGNATURE"] + ["PAN"] * len(extra or [])
    if reference is not None:
        files.append(("reference_signature", ("specimen.png", reference, "image/png")))
    return client.post("/api/v1/los/process", data={"applicant_id": "APP-SIGHTTP001", "case_id": case,
                                                    "expected_types": types}, files=files)


def _signature_doc(body):
    return next(d for d in body["documents"] if d.get("source_id") == "sig.png")


def test_no_reference_is_review_never_pass_and_publishes_no_score(client, tmp_path):
    response = _process(client, tmp_path)
    assert response.status_code == 200, response.text
    doc = _signature_doc(response.json())
    assert doc["status"] == "REVIEW"
    assert doc["specialist"]["signature"]["comparison"] in ("NO_REFERENCE", "NOT_COMPARABLE")
    assert doc["specialist"]["signature"]["match_score"] is None
    assert doc.get("verification_score") is None and doc.get("verification_confidence") is None


def test_a_matching_specimen_is_compared(client, tmp_path):
    specimen = synthetic_signature(tmp_path / "ref.png").read_bytes()          # the same drawn curve
    doc = _signature_doc(_process(client, tmp_path, reference=specimen).json())
    assert doc["specialist"]["signature"]["comparison"] == "MATCH"
    assert doc["specialist"]["signature"]["match_score"] is not None
    assert doc["status"] == "PASS"


def test_a_different_specimen_is_not_a_pass(client, tmp_path):
    other = synthetic_signature(tmp_path / "other.png", phase=2.5).read_bytes()
    doc = _signature_doc(_process(client, tmp_path, reference=other).json())
    assert doc["specialist"]["signature"]["comparison"] in ("MISMATCH", "INCONCLUSIVE")
    assert doc["status"] != "PASS"


def test_a_specimen_that_is_not_an_image_is_refused_before_processing(client, tmp_path):
    response = _process(client, tmp_path, reference=b"MZ\x90\x00" + b"\x00" * 400)
    assert response.status_code == 400
    assert "reference signature" in response.json()["detail"]["message"]


def test_the_specimen_is_used_only_for_signature_uploads(client, tmp_path):
    specimen = synthetic_signature(tmp_path / "ref.png").read_bytes()
    body = _process(client, tmp_path, reference=specimen,
                    extra=[("files", ("pan.jpg", PAN.read_bytes(), "image/jpeg"))]).json()
    pan = next(d for d in body["documents"] if d.get("source_id") == "pan.jpg")
    assert "signature" not in (pan.get("specialist") or {})


def test_a_compared_signature_publishes_the_comparison_score_only(client, tmp_path):
    other = synthetic_signature(tmp_path / "other.png", phase=2.5).read_bytes()
    doc = _signature_doc(_process(client, tmp_path, reference=other).json())
    measured = doc["specialist"]["signature"]["match_score"]
    assert doc.get("verification_score") == round(measured * 100)          # not the generic 92
    assert doc.get("verification_confidence") is None                       # nothing invented
