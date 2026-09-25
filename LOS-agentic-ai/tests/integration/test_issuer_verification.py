"""
Issuer verification: the additional authenticity layer.

    DOCUMENT VALIDITY  -- the existing verifiers; unchanged, authoritative
    AUTHENTICITY       -- a trusted provider's answer; issuer.py
    FRAUD SIGNALS      -- forensics.py; flags only, never authenticity

What these hold: all 23 taxonomy rows are covered; only a traceable
provider confirmation sets issuer_verified; a mismatch fails; an outage,
timeout or error is never a PASS; REQUIRE_EXTERNAL holds what cannot be
confirmed; nothing visual -- a PAN regex, an MRZ checksum, a clean PDF, a
clean forensic result -- can manufacture a confirmation; and every attempt
is audited with names, never values.
"""

from __future__ import annotations

import io
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.verification import forensics, issuer, taxonomy
from app.agents.verification.issuer import (
    IssuerStatus,
    IssuerVerificationProvider,
    IssuerVerificationRequest,
    IssuerVerificationResult,
)

ROOT = Path(__file__).resolve().parents[2]
PAN = ROOT / "samples" / "documents" / "rpan.jpg"
STATEMENT = ROOT / "samples" / "documents" / "demo_bank_statement.pdf"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test: stub providers, default policy, its own audit file."""
    monkeypatch.setenv("ISSUER_VERIFICATION_AUDIT_PATH", str(tmp_path / "issuer.jsonl"))
    monkeypatch.delenv("VERIFICATION_AUTHENTICITY_POLICY", raising=False)
    monkeypatch.delenv("REQUIRE_EXTERNAL_ISSUER_VERIFICATION", raising=False)
    issuer.reset_providers()
    yield
    issuer.reset_providers()


def audit_entries(tmp_path) -> list[dict]:
    path = tmp_path / "issuer.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# ---- test providers --------------------------------------------------------

class Confirms(IssuerVerificationProvider):
    name = "test-confirms"

    def verify(self, request):
        return IssuerVerificationResult(
            status=IssuerStatus.ISSUER_CONFIRMED, reference_id="REF-123",
            verified_at=datetime.now(timezone.utc).isoformat(),
            verified_fields=["pan_number", "name", "date_of_birth"])


class Contradicts(IssuerVerificationProvider):
    name = "test-contradicts"

    def verify(self, request):
        return IssuerVerificationResult(
            status=IssuerStatus.ISSUER_MISMATCH, reference_id="REF-456",
            verified_at=datetime.now(timezone.utc).isoformat(),
            mismatched_fields=["name"])


class Slow(IssuerVerificationProvider):
    name = "test-slow"

    def verify(self, request):
        time.sleep(2)
        return Confirms().verify(request)


class Broken(IssuerVerificationProvider):
    name = "test-broken"

    def verify(self, request):
        raise ConnectionError("provider down")


class Untraceable(IssuerVerificationProvider):
    """Claims a confirmation with no reference -- not evidence."""
    name = "test-untraceable"

    def verify(self, request):
        return IssuerVerificationResult(status=IssuerStatus.ISSUER_CONFIRMED)


class LeaksValues(IssuerVerificationProvider):
    """Returns an Aadhaar NUMBER where field names belong."""
    name = "test-leaks"

    def verify(self, request):
        return IssuerVerificationResult(
            status=IssuerStatus.ISSUER_CONFIRMED, reference_id="REF-789",
            verified_at=datetime.now(timezone.utc).isoformat(),
            verified_fields=["name", "1234 5678 9012", "123456789012"])


def processed(document_type="PAN", status="PASS", codes=None, fields=None, **extra):
    """A processed document, shaped as the Document Agent returns one."""
    return {
        "document": {"type": document_type},
        "source_id": "doc.jpg", "party_id": "APP-1", "status": "SUCCESS",
        "verification": {"status": status, "reason_codes": list(codes or []),
                         "authenticity": "NOT_ESTABLISHED", "issuer_verified": False},
        "extraction": {"fields": fields or {"pan_number": "NUHPS4875K", "name": "R A SINGH"}},
        **extra,
    }


# ==========================================================================
# 1. THE TAXONOMY -- all 23 rows
# ==========================================================================


EXPECTED_ROWS = [
    ("Age Proof", "Govt. Issued Document"), ("Age Proof", "PAN"), ("Age Proof", "Passport"),
    ("Age Proof", "Aadhar"), ("Age Proof", "Driving License"), ("Age Proof", "Mark Sheet"),
    ("Age Proof", "Voter ID"),
    ("Signature Verification", "Bank Sign Verification"), ("Signature Verification", "PAN"),
    ("Signature Verification", "Driving License"), ("Signature Verification", "Passport"),
    ("Identity Proof", "Govt. Issued Document"), ("Identity Proof", "PAN"),
    ("Identity Proof", "ID Proof"), ("Identity Proof", "Address Proof"),
    ("Identity Proof", "Aadhar"), ("Identity Proof", "Driving License"),
    ("Income Proof", "ITR Return Document"), ("Income Proof", "Bank statement"),
    ("Income Proof", "Salary Slip"),
    ("Property Ownership Proof", "Sale Deed"),
    ("Business Photographs", "Business Proof1"), ("Business Photographs", "Business Proof2"),
]


def test_the_taxonomy_has_all_23_rows_and_no_others():
    assert len(EXPECTED_ROWS) == 23
    assert len(taxonomy.TAXONOMY) == 23
    assert [e.number for e in taxonomy.TAXONOMY] == list(range(1, 24))
    assert [(e.section, e.expected_document) for e in taxonomy.TAXONOMY] == EXPECTED_ROWS


@pytest.mark.parametrize("section,expected", EXPECTED_ROWS)
def test_every_row_resolves_and_has_a_provider_that_never_fakes(section, expected):
    entry = taxonomy.resolve(section, expected)
    assert entry is not None

    provider = issuer.provider_for(entry.document_type)
    answer = issuer.verify(IssuerVerificationRequest(document_type=entry.document_type))

    # Registry coverage: every row's type is configured.
    assert issuer._entry(entry.document_type), entry.document_type
    # A stub today: never a confirmation.
    assert provider.name == "none"
    assert answer.status is IssuerStatus.NOT_ESTABLISHED
    assert answer.issuer_verified is False


def test_generic_categories_stay_categories_with_no_invented_provider():
    assert taxonomy.GENERIC_CATEGORIES == {"GOVT_ISSUED_DOCUMENT", "ID_PROOF", "ADDRESS_PROOF"}
    for category in taxonomy.GENERIC_CATEGORIES:
        assert issuer.mode_for(category) is issuer.ProviderMode.NONE
        answer = issuer.verify(IssuerVerificationRequest(document_type=category))
        assert answer.reason_codes == ["ISSUER_VERIFICATION_NOT_AVAILABLE"]


@pytest.mark.parametrize("document_type", ["MARK_SHEET", "BUSINESS_PROOF_1", "BUSINESS_PROOF_2"])
def test_types_with_no_trusted_source_have_none(document_type):
    assert issuer.mode_for(document_type) is issuer.ProviderMode.NONE


def test_a_salary_slip_is_corroborated_never_issuer_authenticated():
    assert issuer.mode_for("SALARY_SLIP") is issuer.ProviderMode.CORROBORATION
    answer = issuer.verify(IssuerVerificationRequest(document_type="SALARY_SLIP"))
    assert answer.mode == "corroboration"
    assert answer.status is IssuerStatus.NOT_ESTABLISHED


@pytest.mark.parametrize("document_type", ["PAN", "AADHAAR", "DRIVING_LICENCE", "PASSPORT",
                                           "VOTER_ID", "BANK_STATEMENT", "ITR", "SALE_DEED",
                                           "BANK_SIGNATURE"])
def test_types_that_could_have_an_issuer_say_not_configured(document_type):
    answer = issuer.verify(IssuerVerificationRequest(document_type=document_type))
    assert answer.reason_codes == ["ISSUER_PROVIDER_NOT_CONFIGURED"]
    assert issuer.route_for(document_type)


# ==========================================================================
# 2. THE PROVIDER CONTRACT
# ==========================================================================


def test_a_traceable_confirmation_is_confirmed():
    issuer.set_provider("PAN", Confirms())
    answer = issuer.verify(IssuerVerificationRequest(document_type="PAN"))

    assert answer.status is IssuerStatus.ISSUER_CONFIRMED and answer.issuer_verified
    assert answer.reference_id == "REF-123" and answer.verified_at


def test_a_mismatch_is_a_mismatch_with_its_code():
    issuer.set_provider("PAN", Contradicts())
    answer = issuer.verify(IssuerVerificationRequest(document_type="PAN"))

    assert answer.status is IssuerStatus.ISSUER_MISMATCH
    assert answer.mismatched_fields == ["name"]
    assert answer.reason_codes[0] == "ISSUER_MISMATCH"


def test_a_timeout_is_never_a_pass(monkeypatch):
    monkeypatch.setenv("ISSUER_VERIFICATION_TIMEOUT_SECONDS", "0.2")
    issuer.set_provider("PAN", Slow())

    answer = issuer.verify(IssuerVerificationRequest(document_type="PAN"))

    assert answer.status is IssuerStatus.NOT_ESTABLISHED
    assert answer.reason_codes == ["ISSUER_PROVIDER_TIMEOUT"]


def test_an_exception_is_never_a_pass():
    issuer.set_provider("PAN", Broken())
    answer = issuer.verify(IssuerVerificationRequest(document_type="PAN"))

    assert answer.status is IssuerStatus.NOT_ESTABLISHED
    assert answer.reason_codes == ["ISSUER_PROVIDER_ERROR"]


def test_an_untraceable_confirmation_is_not_evidence():
    issuer.set_provider("PAN", Untraceable())
    answer = issuer.verify(IssuerVerificationRequest(document_type="PAN"))

    assert answer.status is IssuerStatus.NOT_ESTABLISHED
    assert answer.reason_codes == ["ISSUER_CONFIRMATION_INCOMPLETE"]


def test_a_configured_but_unregistered_provider_is_not_established(monkeypatch):
    monkeypatch.setitem(issuer._config()["documents"], "PAN",
                        {"provider": "vendor-x", "mode": "issuer"})
    answer = issuer.verify(IssuerVerificationRequest(document_type="PAN"))

    assert answer.reason_codes == ["ISSUER_PROVIDER_NOT_REGISTERED"]
    assert answer.status is IssuerStatus.NOT_ESTABLISHED


def test_a_registered_provider_is_selected_by_configuration(monkeypatch):
    provider = Confirms()
    provider.name = "vendor-y"
    issuer.register_provider(provider)
    monkeypatch.setitem(issuer._config()["documents"], "PAN",
                        {"provider": "vendor-y", "mode": "issuer"})

    assert issuer.provider_for("PAN") is provider
    assert issuer.verify(IssuerVerificationRequest(document_type="PAN")).issuer_verified


def test_values_never_reach_the_result_only_names():
    issuer.set_provider("AADHAAR", LeaksValues())
    answer = issuer.verify(IssuerVerificationRequest(
        document_type="AADHAAR", fields={"aadhaar_number": "123456789012"}))

    assert answer.verified_fields == ["name"]
    assert not re.search(r"\d{12}", json.dumps(answer.public()))
    assert issuer.mask_aadhaar("1234 5678 9012") == "XXXX-XXXX-9012"


# ==========================================================================
# 3. THE POLICY, APPLIED TO A PROCESSED DOCUMENT
# ==========================================================================


def test_policy_off_a_valid_document_is_unchanged():
    result = issuer.apply(processed(), case_id="C", applicant_id="APP-1")

    assert result["verification"]["status"] == "PASS"
    assert result["verification"]["reason_codes"] == []
    assert result["verification"]["issuer_verification"]["status"] == "NOT_ESTABLISHED"
    assert result["verification"]["issuer_verified"] is False


def test_policy_on_not_established_is_review(monkeypatch):
    monkeypatch.setenv("REQUIRE_EXTERNAL_ISSUER_VERIFICATION", "true")
    result = issuer.apply(processed(document_type="BANK_SIGNATURE"), case_id="C")

    assert result["verification"]["status"] == "REVIEW"
    assert "AUTHENTICITY_NOT_ESTABLISHED" in result["verification"]["reason_codes"]


def test_policy_on_a_confirmation_lifts_only_the_authenticity_hold(monkeypatch):
    monkeypatch.setenv("REQUIRE_EXTERNAL_ISSUER_VERIFICATION", "true")
    issuer.set_provider("PAN", Confirms())
    held = processed(status="REVIEW", codes=["AUTHENTICITY_NOT_ESTABLISHED"], status_doc="REVIEW")
    held["status"] = "REVIEW"

    result = issuer.apply(held, case_id="C")

    assert result["verification"]["status"] == "PASS"
    assert result["verification"]["reason_codes"] == []
    assert result["verification"]["issuer_verified"] is True
    assert result["status"] == "SUCCESS"


def test_a_confirmation_never_overrides_any_other_problem(monkeypatch):
    monkeypatch.setenv("REQUIRE_EXTERNAL_ISSUER_VERIFICATION", "true")
    issuer.set_provider("PAN", Confirms())
    held = processed(status="REVIEW", codes=["REQUIRED_FIELD_MISSING", "AUTHENTICITY_NOT_ESTABLISHED"])

    result = issuer.apply(held, case_id="C")

    assert result["verification"]["status"] == "REVIEW"


@pytest.mark.parametrize("required", ["true", "false"])
def test_a_mismatch_fails_whatever_the_policy(monkeypatch, required):
    monkeypatch.setenv("REQUIRE_EXTERNAL_ISSUER_VERIFICATION", required)
    issuer.set_provider("PAN", Contradicts())

    result = issuer.apply(processed(), case_id="C")

    assert result["verification"]["status"] == "FAIL"
    assert "ISSUER_MISMATCH" in result["verification"]["reason_codes"]
    assert result["verification"]["issuer_verified"] is False
    assert result["status"] == "REJECTED"


def test_a_failed_document_is_not_sent_to_the_issuer():
    calls = []

    class Counting(Confirms):
        def verify(self, request):
            calls.append(1)
            return super().verify(request)

    issuer.set_provider("PAN", Counting())
    result = issuer.apply(processed(status="FAIL", codes=["DOCUMENT_TYPE_MISMATCH"]), case_id="C")

    assert calls == []
    assert result["verification"]["status"] == "FAIL"
    assert result["verification"]["issuer_verification"]["reason_codes"] == [
        "ISSUER_VERIFICATION_NOT_ATTEMPTED"]


def test_a_specialist_verdict_is_read_the_way_the_response_reads_it(monkeypatch):
    monkeypatch.setenv("REQUIRE_EXTERNAL_ISSUER_VERIFICATION", "true")
    signature = {"document": {"type": "BANK_SIGNATURE"}, "source_id": "sig.jpg",
                 "verification": {"decision": "PASS"}, "specialist": {"decision": "PASS"}}

    result = issuer.apply(signature, case_id="C")

    assert result["verification"]["status"] == "REVIEW"


@pytest.mark.parametrize("policy_on", [False, True])
def test_a_provider_outage_is_never_a_pass_and_never_a_fail(monkeypatch, policy_on):
    if policy_on:
        monkeypatch.setenv("REQUIRE_EXTERNAL_ISSUER_VERIFICATION", "true")
    issuer.set_provider("PAN", Broken())

    result = issuer.apply(processed(), case_id="C")

    assert result["verification"]["issuer_verified"] is False
    assert result["verification"]["status"] == ("REVIEW" if policy_on else "PASS")


# ==========================================================================
# 4. NOTHING VISUAL MANUFACTURES A CONFIRMATION
# ==========================================================================


def test_a_valid_pan_regex_cannot_confirm():
    result = issuer.apply(processed(fields={"pan_number": "NUHPS4875K"}), case_id="C")
    assert result["verification"]["issuer_verified"] is False


def test_an_mrz_checksum_cannot_confirm():
    passport = processed(document_type="PASSPORT",
                         fields={"passport_number": "Z1234567", "mrz_verified": True})
    result = issuer.apply(passport, case_id="C")
    assert result["verification"]["issuer_verification"]["status"] == "NOT_ESTABLISHED"


def _pdf(producer: str) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Producer": producer})
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_a_clean_forensic_result_cannot_confirm():
    result = forensics.apply(processed(), _pdf("Oracle Analytics Publisher"), "s.pdf")
    result = issuer.apply(result, case_id="C")

    assert result["verification"]["fraud_signals"] == []
    assert result["verification"]["issuer_verified"] is False


def test_an_image_editor_is_a_high_signal_and_only_a_signal():
    result = forensics.apply(processed(document_type="BANK_STATEMENT"),
                             _pdf("Adobe Photoshop 24.0"), "s.pdf")

    signal = result["verification"]["fraud_signals"][0]
    assert (signal["code"], signal["severity"]) == ("PDF_PRODUCED_BY_IMAGE_EDITOR", "HIGH")
    assert result["verification"]["status"] == "PASS"          # off by default


def test_a_high_signal_holds_for_review_only_when_configured(monkeypatch):
    monkeypatch.setitem(issuer._config(), "forensics", {"review_on_high": True})
    result = forensics.apply(processed(document_type="BANK_STATEMENT"),
                             _pdf("GIMP 2.10"), "s.pdf")

    assert result["verification"]["status"] == "REVIEW"
    assert "FORENSIC_SIGNAL_REVIEW" in result["verification"]["reason_codes"]
    assert result["verification"]["issuer_verified"] is False


def test_a_common_pdf_tool_is_only_a_low_signal():
    signals = forensics.signals_for(_pdf("iLovePDF"), "s.pdf")
    assert [s["severity"] for s in signals if s["code"] == "PDF_EDITED_WITH_PDF_TOOL"] == ["LOW"]


# ==========================================================================
# 5. AUDIT
# ==========================================================================


def test_every_evaluation_is_audited_with_names_never_values(tmp_path):
    issuer.set_provider("AADHAAR", LeaksValues())
    issuer.apply(processed(document_type="AADHAAR",
                           fields={"aadhaar_number": "123456789012", "name": "R SINGH"}),
                 case_id="CASE-A", applicant_id="APP-1", request_id="req-1")

    entry = audit_entries(tmp_path)[-1]
    for key in ("case_id", "applicant_id", "document_id", "document_type", "provider",
                "provider_status", "reference_id", "consent_id", "verified_at",
                "verified_fields", "mismatched_fields", "reason_codes", "request_id",
                "source", "latency_ms"):
        assert key in entry, key
    assert entry["case_id"] == "CASE-A" and entry["document_id"] == "CASE-A:APP-1:doc.jpg"
    raw = (tmp_path / "issuer.jsonl").read_text()
    assert not re.search(r"\d{12}", raw)
    assert "R SINGH" not in raw


# ==========================================================================
# 6. THROUGH THE REAL LOS ROUTE
# ==========================================================================


@pytest.fixture
def client(make_token, tmp_path):
    import main

    from app.store import set_repository
    from app.store.sqlite_repo import SQLiteRepository

    repository = SQLiteRepository(tmp_path / "issuer.sqlite3")
    repository.initialise()
    set_repository(repository)
    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=['los.read', 'los.write'])}"})
    yield c
    set_repository(None)


def process(client, path, declared):
    response = client.post("/api/v1/los/process", data={
        "operation": "PROCESS", "applicant_id": "APP-I", "case_id": f"CASE-{declared}",
        "expected_types": declared},
        files=[("files", (path.name, path.read_bytes(), "application/octet-stream"))])
    assert response.status_code == 200, response.text
    return response.json()["documents"][0]


@pytest.mark.parametrize("path,declared", [(PAN, "PAN"), (STATEMENT, "BANK_STATEMENT")])
def test_backward_compatible_by_default(client, path, declared):
    """Valid documents stay valid; the new block says NOT_ESTABLISHED."""
    document = process(client, path, declared)

    assert document["verification"] == "PASS"
    assert document["authenticity"] == "NOT_ESTABLISHED"
    assert document["issuer_verified"] is False
    assert document["issuer_verification"]["status"] == "NOT_ESTABLISHED"
    assert document.get("extraction")


def test_bank_reconciliation_is_unchanged(client):
    document = process(client, STATEMENT, "BANK_STATEMENT")

    assert document["verification"] == "PASS"
    assert document["verification_scope"] == "DOCUMENT_STRUCTURE_AND_BALANCE_RECONCILIATION"


def test_a_confirmation_satisfies_the_required_policy_end_to_end(client, monkeypatch):
    monkeypatch.setenv("REQUIRE_EXTERNAL_ISSUER_VERIFICATION", "true")
    issuer.set_provider("PAN", Confirms())

    document = process(client, PAN, "PAN")

    assert document["verification"] == "PASS"
    assert document["authenticity"] == "ISSUER_CONFIRMED"
    assert document["issuer_verified"] is True
    assert document["issuer_verification"]["reference_id"] == "REF-123"
    assert document.get("extraction"), "a confirmed document releases its fields"


def test_without_a_confirmation_the_required_policy_holds_end_to_end(client, monkeypatch):
    monkeypatch.setenv("REQUIRE_EXTERNAL_ISSUER_VERIFICATION", "true")

    document = process(client, PAN, "PAN")

    assert document["verification"] == "REVIEW"
    assert "AUTHENTICITY_NOT_ESTABLISHED" in document["reason_codes"]
    assert not document.get("extraction")


def test_a_mismatch_fails_end_to_end_and_withholds_the_fields(client):
    issuer.set_provider("PAN", Contradicts())

    document = process(client, PAN, "PAN")

    assert document["verification"] == "FAIL"
    assert "ISSUER_MISMATCH" in document["reason_codes"]
    assert document["issuer_verification"]["mismatched_fields"] == ["name"]
    assert not document.get("extraction")
