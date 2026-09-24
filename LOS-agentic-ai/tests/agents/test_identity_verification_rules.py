"""
Driving licence, voter ID and passport verification rules.

ONE CONTRACT, NOT ONE ALGORITHM. Each class reports (verdict, code, detail)
findings the same way; what each checks is its own.

COVERAGE, STATED EXACTLY:

    DRIVING LICENCE  rule-level AND real fixtures (six real licences)
    VOTER ID         real fixtures; no new rule (by decision -- see below)
    PASSPORT         RULE-LEVEL ONLY. There is no passport image anywhere in
                     the sample corpus, so no passport test here exercises
                     OCR, MRZ reading or extraction on a real document.
                     The MRZ check-digit tests use the ICAO 9303 specimen
                     values, which are published test data, not a passport.

Nothing here establishes authenticity; every identity verdict keeps saying
`authenticity: NOT_ESTABLISHED`.
"""

from __future__ import annotations

import io
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.agents.verification import rules

TODAY = date.today()


def apply(document_class, **fields):
    status, codes, detail = rules.apply(document_class=document_class,
                                        status="PASS", fields=fields)
    return status, codes, detail


# ==========================================================================
# PASSPORT -- RULE-LEVEL ONLY (no passport fixture exists)
# ==========================================================================

PASSPORT = {"passport_number": "Z1234567", "name": "R SHARMA",
            "date_of_birth": "1990-01-01", "issuing_country": "IND",
            "mrz_verified": True}


def test_passport_an_expired_passport_fails():
    """THE GAP: the expiry rule read `valid_till` and never saw a passport's."""
    expired = (TODAY - timedelta(days=1)).isoformat()

    status, codes, _ = apply("PASSPORT", **PASSPORT, date_of_expiry=expired)

    assert status == "FAIL"
    assert "DOCUMENT_EXPIRED" in codes


def test_passport_a_valid_passport_is_not_expired():
    valid = (TODAY + timedelta(days=365)).isoformat()

    status, codes, detail = apply("PASSPORT", **PASSPORT, date_of_expiry=valid)

    assert status == "PASS" and codes == []
    assert detail["authenticity"] == "NOT_ESTABLISHED"


@pytest.mark.parametrize("number", ["12345678", "ZZ123456", "Z123456", "Z12345678"])
def test_passport_an_indian_number_in_the_wrong_format_is_reviewed(number):
    status, codes, _ = apply("PASSPORT", **{**PASSPORT, "passport_number": number})

    assert status == "REVIEW"
    assert "PASSPORT_NUMBER_FORMAT_INVALID" in codes


def test_passport_a_foreign_number_is_not_judged_by_indias_format():
    status, codes, _ = apply("PASSPORT", **{**PASSPORT, "issuing_country": "GBR",
                                            "passport_number": "123456789"})

    assert "PASSPORT_NUMBER_FORMAT_INVALID" not in codes


def test_passport_mrz_check_digits_are_unchanged():
    """ICAO 9303 specimen values; preserved behaviour, not new behaviour."""
    from app.agents.document_agent.fields import mrz

    assert mrz.check_digit("L898902C3") == 6
    assert mrz.check_digit("740812") == 2
    assert mrz.check_digit("120415") == 9
    assert mrz.verify("L898902C3", "6") is True
    assert mrz.verify("L898902C3", "7") is False


def test_passport_a_failed_mrz_still_blocks_a_pass():
    """`mrz_verified` stays a REQUIRED field that must be true."""
    from app.agents.document_agent.pipeline import PASSPORT_SPEC
    from app.agents.document_agent import validate as V

    validator, required = PASSPORT_SPEC["mrz_verified"]
    assert required is True
    assert validator is V.validate_bool_true


# ==========================================================================
# DRIVING LICENCE
# ==========================================================================

#: What the real pipeline read from each licence in the sample corpus.
REAL_LICENCES = {
    "dl1.jpg": dict(dl_number="KA0520150009483", name="NARAYANAPPA",
                    date_of_birth="1989-01-06", date_of_issue="2015-04-25",
                    valid_till="2035-04-21"),
    "dl2.jpg (legacy number)": dict(dl_number="39712/NLG/1997", name="VENKATAIAH V",
                                    guardian_name="BUCHAIAH", date_of_birth="1977-04-05",
                                    date_of_issue="2023-05-02", valid_till="2028-05-01"),
    "dl4.jpg": dict(dl_number="KA4220200004221", name="MOHAMMED RAFEEQ",
                    date_of_birth="1972-06-20", date_of_issue="2020-03-23"),
    "driving_license.jpg": dict(dl_number="MH0320220045390", name="RISHABH AJIT SINGH",
                                guardian_name="AJIT SINGH", date_of_birth="2002-06-12",
                                date_of_issue="2022-10-04", valid_till="2042-06-11"),
    "sidkamble.jpg": dict(dl_number="MH0320120037600", name="SIDDHANT KAMBLE",
                          guardian_name="MANOJKAMBLE", date_of_birth="1994-01-03",
                          date_of_issue="2012-10-11", valid_till="2032-10-10"),
}
DL_CODES = {"DL_STATE_CODE_UNKNOWN", "DL_ISSUED_BEFORE_ELIGIBLE_AGE",
            "DL_DATES_INCONSISTENT", "DL_NAME_GUARDIAN_IDENTICAL"}
GOOD_DL = REAL_LICENCES["driving_license.jpg"]


@pytest.mark.parametrize("sample", sorted(REAL_LICENCES))
def test_dl_no_new_rule_fires_on_a_genuine_licence(sample):
    status, codes, _ = apply("DRIVING_LICENCE", **REAL_LICENCES[sample])

    assert not set(codes) & DL_CODES, (sample, codes)


def test_dl_an_unknown_state_code_is_reviewed():
    status, codes, _ = apply("DRIVING_LICENCE", **{**GOOD_DL, "dl_number": "ZZ0320220045390"})

    assert status == "REVIEW"
    assert "DL_STATE_CODE_UNKNOWN" in codes


def test_dl_issued_before_sixteen_is_reviewed():
    status, codes, _ = apply("DRIVING_LICENCE", **{**GOOD_DL, "date_of_issue": "2010-01-01"})

    assert status == "REVIEW"
    assert "DL_ISSUED_BEFORE_ELIGIBLE_AGE" in codes


def test_dl_validity_ending_before_issue_is_reviewed():
    status, codes, _ = apply("DRIVING_LICENCE", **{**GOOD_DL, "date_of_issue": "2043-01-01"})

    assert "DL_DATES_INCONSISTENT" in codes


def test_dl_holder_and_guardian_read_as_one_name_is_reviewed():
    status, codes, _ = apply("DRIVING_LICENCE", **{**GOOD_DL, "guardian_name": "Rishabh Ajit Singh"})

    assert status == "REVIEW"
    assert "DL_NAME_GUARDIAN_IDENTICAL" in codes


def test_dl_expiry_behaviour_is_unchanged():
    expired = (TODAY - timedelta(days=1)).isoformat()

    status, codes, _ = apply("DRIVING_LICENCE", **{**GOOD_DL, "valid_till": expired})

    assert status == "FAIL"
    assert "DOCUMENT_EXPIRED" in codes


def test_dl_rules_are_configurable(monkeypatch):
    original = rules._rule_on
    monkeypatch.setattr(rules, "_rule_on", lambda cls, name, default=True: (
        False if cls == "DRIVING_LICENCE" and name in {
            "state_code", "issue_age", "validity_order", "name_guardian_distinct"}
        else original(cls, name, default)))

    status, codes, _ = apply("DRIVING_LICENCE", **{
        **GOOD_DL, "dl_number": "ZZ0320220045390", "date_of_issue": "2010-01-01",
        "guardian_name": "RISHABH AJIT SINGH"})

    assert not set(codes) & DL_CODES


def test_every_new_code_is_explained():
    from app.agents.verification import reasons

    for code in DL_CODES | {"PASSPORT_NUMBER_FORMAT_INVALID"}:
        assert reasons.known(code), code


# ==========================================================================
# VOTER ID -- NO NEW RULE, BY DECISION
# ==========================================================================


def test_voter_no_name_equality_rule_was_introduced():
    """
    Decided against: a voter card's holder and relative are not judged
    against each other. Recorded here so adding one is a visible decision.
    """
    status, codes, _ = apply("VOTER_ID", epic_number="ZAX0399947",
                             name="SUNITA", relation_name="SUNITA")

    assert not [c for c in codes if c.startswith("VOTER_NAME")]


# ==========================================================================
# REAL DOCUMENTS THROUGH THE FOS UPLOAD
# ==========================================================================

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
DL_REAL = SAMPLES / "real_batch" / "dl1.jpg"
VOTER_REAL = SAMPLES / "real_batch" / "voter_id2.jpg"
FOS_SCOPES = ["read_applicant", "read_application", "read_documents",
              "read_verification", "read_pending_items", "read_next_action",
              "create_applicant", "create_application", "upload_document"]


@pytest.fixture
def fos(tmp_path, make_token, monkeypatch):
    import main
    from fastapi.testclient import TestClient

    from app.store import set_repository
    from app.store.sqlite_repo import SQLiteRepository

    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    repository = SQLiteRepository(tmp_path / "identity.sqlite3")
    repository.initialise()
    set_repository(repository)
    client = TestClient(main.app)
    client.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES)}"})
    opened = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "Identity Test", "mobile": "9876543210"},
        "application": {"product": "PERSONAL_LOAN"}}).json()

    def upload(content: bytes, name: str, declared: str) -> dict:
        response = client.post("/api/v1/fos/copilot", data={
            "applicant_id": opened["applicant_id"], "case_id": opened["case_id"],
            "action": "UPLOAD_DOCUMENT", "document_type": declared},
            files={"file": (name, content, "application/octet-stream")})
        assert response.status_code == 200, response.text
        return response.json()["verification"]

    yield upload
    set_repository(None)


def _illegible(path: Path) -> bytes:
    from PIL import Image

    image = Image.open(path).convert("RGB")
    small = image.resize((max(1, image.width // 14), max(1, image.height // 14)))
    buffer = io.BytesIO()
    small.resize(image.size).save(buffer, format="JPEG", quality=12)
    return buffer.getvalue()


def _need(path):
    if not path.exists():
        pytest.skip(f"{path.name} not available")


@pytest.mark.ocr
def test_real_licence_passes_without_new_findings(fos):
    _need(DL_REAL)
    result = fos(DL_REAL.read_bytes(), "dl.jpg", "DRIVING_LICENCE")

    assert result["verification"] == "PASS"
    assert not set(result["reason_codes"]) & DL_CODES


@pytest.mark.ocr
def test_real_voter_card_is_unchanged(fos):
    _need(VOTER_REAL)
    result = fos(VOTER_REAL.read_bytes(), "voter.jpg", "VOTER_ID")

    assert result["verification"] == "PASS"


@pytest.mark.ocr
@pytest.mark.parametrize("path,declared", [
    (DL_REAL, "PASSPORT"), (DL_REAL, "VOTER_ID"), (VOTER_REAL, "DRIVING_LICENCE")])
def test_a_wrong_document_type_fails(fos, path, declared):
    _need(path)
    result = fos(path.read_bytes(), path.name, declared)

    assert result["verification"] == "FAIL"
    assert "DOCUMENT_TYPE_MISMATCH" in result["reason_codes"]
    assert result["extraction_released"] is False


@pytest.mark.ocr
@pytest.mark.parametrize("path,declared", [(DL_REAL, "DRIVING_LICENCE"),
                                           (VOTER_REAL, "VOTER_ID")])
def test_an_unreadable_card_never_passes(fos, path, declared):
    _need(path)
    result = fos(_illegible(path), path.name, declared)

    assert result["verification"] != "PASS"
    assert result["extraction_released"] is False
