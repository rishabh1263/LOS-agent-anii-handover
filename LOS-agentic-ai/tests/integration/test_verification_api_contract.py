"""
THE REAL VERIFICATION SERVICES, over HTTP, per document type -- not mocks.

For every configured type with a real sample in samples/:
  - the endpoint answers (200 / 422 for a FAIL -- never a 5xx)
  - the response parses and normalizes into the Copilot's common contract
    (verification_adapters.normalize): a decision from the service, reasons
    as codes, `authoritative: false`, no invented score
  - asserting the WRONG type fails (a PAN is not a Voter ID)
And the common upload gate in front of it: empty, unsupported, renamed
executable, script, signature mismatch, PDF launch action -- all refused
before any OCR runs.

The adapter inventory covers every type the verification agent is
configured for, and says honestly which ones can be re-read in the
background and which need a new upload.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant.copilot.capabilities import verification_adapters as adapters

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
DOCS = SAMPLES / "documents"

#: (document type, sample, endpoint) -- a real document of each type
REAL = [
    ("PAN", SAMPLES / "lPan.jpg", "/api/v1/verify"),
    ("DRIVING_LICENCE", DOCS / "driving_license.jpg", "/api/v1/verify"),
    ("VOTER_ID", DOCS / "voter_id2.jpg", "/api/v1/verify"),
    ("PASSPORT", SAMPLES / "passports" / "passport_samples0_1.jpg", "/api/v1/verify"),
    ("BANK_STATEMENT", DOCS / "demo_bank_statement.pdf", "/api/v1/financial/verify"),
    ("SALARY_SLIP", DOCS / "demo_salary_slip_rahul_sharma.pdf", "/api/v1/financial/verify"),
    ("ITR", DOCS / "ITR.pdf", "/api/v1/financial/verify"),
]
RESULTS: list[dict] = []


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers["Authorization"] = "Bearer " + make_token(scopes=["documents:write", "upload_document"])
    return c


def _post(client, path, name, content, expected=None):
    data = {"expected_type": expected} if expected and "financial" not in path else {}
    started = time.perf_counter()
    response = client.post(path, files={"file": (name, content, "application/octet-stream")}, data=data)
    return response, round((time.perf_counter() - started) * 1000, 1)


@pytest.mark.parametrize("document_type,sample,path", [r for r in REAL if r[1].exists()],
                         ids=[r[0] for r in REAL if r[1].exists()])
def test_each_type_verifies_through_its_real_service(client, document_type, sample, path):
    response, ms = _post(client, path, sample.name, sample.read_bytes(), expected=document_type)
    assert response.status_code in (200, 422), response.text        # 422 = a FAIL, never a 5xx
    body = response.json()
    normalized = adapters.normalize(body, source=path)
    assert normalized["decision"] in ("PASS", "REVIEW", "FAIL")
    assert normalized["authoritative"] is False and normalized["authenticity_checked"] is False
    assert normalized["score"] is None or 0 <= normalized["score"] <= 100
    assert all(isinstance(c, str) and c.isupper() for c in normalized["reason_codes"])
    if normalized["decision"] != "PASS":
        assert normalized["reason_codes"], "a non-PASS names its reason"
    RESULTS.append({"type": document_type, "endpoint": path, "http": response.status_code,
                    "decision": normalized["decision"], "reasons": normalized["reason_codes"], "ms": ms})


def test_asserting_the_wrong_type_fails(client):
    sample = SAMPLES / "lPan.jpg"
    if not sample.exists():
        pytest.skip("PAN sample not available")
    response, _ = _post(client, "/api/v1/verify", sample.name, sample.read_bytes(), expected="VOTER_ID")
    assert response.status_code == 422
    assert adapters.normalize(response.json(), source="verify")["decision"] == "FAIL"


@pytest.mark.parametrize("name,content,status", [
    ("empty.jpg", b"", 400),
    ("notes.txt", b"hello", 415),
    ("pan.pdf", b"MZ\x90\x00" + b"\x00" * 64, 415),                       # a renamed executable
    ("pan.jpg", b"<script>alert(1)</script>", 415),                       # a script
    ("pan.jpg", b"\x00\x01garbage-that-is-no-document-format" * 4, 415),  # no document format at all
    ("pan.pdf", b"%PDF-1.4\n1 0 obj << /Type /Action /S /Launch >> endobj", 415),
])
def test_the_upload_gate_refuses_before_any_ocr(client, monkeypatch, name, content, status):
    from app.agents.document_agent import ocr

    called = []
    monkeypatch.setattr(ocr, "run_ocr", lambda *a, **k: called.append(1))
    response, _ = _post(client, "/api/v1/verify", name, content)
    assert response.status_code == status, response.text
    assert called == []


def test_the_inventory_covers_every_configured_type_honestly():
    from app.agents.verification import configuration

    configured = {t for types in configuration()["classes"].values() for t in types}
    rows = {r["document_type"]: r for r in adapters.inventory()}
    assert configured <= set(rows)                                  # every verifier class
    assert rows["BANK_SIGNATURE"]["service"] == "specialist:signature_verification"
    assert "never called genuine" in rows["PAN_SIGNATURE"]["limitation"]
    assert all(r["authoritative"] is False for r in rows.values())
    assert rows["BANK_STATEMENT"]["background_reader"] is True
    assert rows["PAN"]["background_reader"] is False and "upload" in rows["PAN"]["limitation"]
    assert "EXTERNAL" in rows["AADHAAR"]["limitation"]


def test_normalize_never_invents_a_score_or_reason():
    out = adapters.normalize({"document_type": "PAN", "status": "PASS"}, source="x")
    assert out["score"] is None and out["reason_code"] is None and out["decision"] == "PASS"


def test_report(capsys):
    for row in RESULTS:
        print("VERIFY_API", row)


# ---- PASSPORT: structure, MRZ, expiry -- and never "genuine" ---------------------------
PASSPORTS = SAMPLES / "passports"


@pytest.mark.parametrize("name", ["passport_samples0_1.jpg", "passport_samples0_10.jpg",
                                  "passport_samples0_15.jpg"])
def test_an_expired_passport_fails_as_expired_on_both_paths(client, name):
    sample = PASSPORTS / name
    if not sample.exists():
        pytest.skip("passport sample not available")
    quick, _ = _post(client, "/api/v1/verify", name, sample.read_bytes())
    assert quick.status_code == 422
    assert "DOCUMENT_EXPIRED" in quick.json()["reason_codes"]
    full = client.post("/api/v1/document-agent", files={"file": (name, sample.read_bytes(), "image/jpeg")},
                       data={"operation": "VERIFY"}).json()["verification"]
    assert full["status"] == "FAIL" and "DOCUMENT_EXPIRED" in full["reason_codes"]
    assert adapters.normalize(quick.json(), source="verify")["authenticity_checked"] is False


def test_a_current_passport_passes_structurally_and_is_not_called_genuine(client):
    sample = PASSPORTS / "passport_samples0_11.jpg"
    if not sample.exists():
        pytest.skip("passport sample not available")
    body = _post(client, "/api/v1/verify", sample.name, sample.read_bytes())[0].json()
    assert body["document_type"] == "PASSPORT" and body["status"] == "PASS"
    assert body["authenticity_checked"] is False
    normalized = adapters.normalize(body, source="verify")
    assert normalized["authoritative"] is False and "genuine" not in str(normalized).lower()


def test_the_mrz_expiry_is_trusted_only_with_its_check_digit():
    from app.agents.verification import basic

    assert basic._mrz_expiry("J245240951N09110203F2008309").isoformat() == "2020-08-30"
    assert basic._mrz_expiry("J245240951N09110203F2008308") is None       # check digit wrong
    assert basic._mrz_expiry("NOTHING HERE") is None


def test_a_legible_mrz_classifies_a_passport_whose_caption_ocr_lost():
    from app.agents.verification import basic

    compact = "REPUBLICOFINDIA" + "J245240951N09110203F2008309"
    assert basic.classify(compact)[0] is basic.DocumentClass.PASSPORT
    assert basic.classify("REPUBLICOFINDIA")[0] is not basic.DocumentClass.PASSPORT


# ---- SIGNATURE: the real service, honest verdicts ---------------------------------------
def test_a_signature_without_a_reference_is_review_never_pass():
    from app.agents.signature import verify_signature

    sample = SAMPLES / "lPan.jpg"
    if not sample.exists():
        pytest.skip("PAN sample not available")
    outcome = verify_signature(str(sample), "PAN_SIGNATURE")
    assert outcome.decision is None or str(getattr(outcome.decision, "value", outcome.decision)) != "PASS"
    assert str(getattr(outcome.comparison, "value", outcome.comparison)) in ("NO_REFERENCE", "NOT_COMPARABLE")
    assert outcome.reference_available is False and outcome.comparison_score is None
