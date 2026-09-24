"""
PAN verification: consistency between the card's own fields.

WHAT IS AND IS NOT CLAIMED. There is no issuer lookup in this service, so
nothing here can show a PAN is genuine, and every verdict keeps saying
`authenticity: NOT_ESTABLISHED`. What CAN be checked offline is whether the
card contradicts itself:

    PAN_HOLDER_TYPE_INCONSISTENT  a non-individual code on a card that
                                  prints a father's name
    PAN_NAME_FATHER_IDENTICAL     holder and father read as one name --
                                  a misread, the class of fault behind a
                                  real card published under the wrong name
    PAN_SERIAL_UNISSUED           a 0000 serial, never issued

Each is REVIEW, not FAIL: a contradiction is evidence something is wrong,
not proof of what. And none of them may fire on a genuine card -- the
control below runs them over what the real pipeline read from every PAN
in the sample corpus.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from app.agents.verification import rules

GOOD = {"pan_number": "NUHPS4875K", "name": "RISHABH AJIT SINGH",
        "father_name": "AJIT SINGH", "date_of_birth": "2002-06-12"}


def verdict(**overrides):
    fields = {**GOOD, **overrides}
    status, codes, detail = rules.apply(document_class="PAN", status="PASS",
                                        fields=fields)
    advisories = [a["code"] for a in detail.get("advisories") or []]
    return status, codes, advisories, detail


# ==========================================================================
# POSITIVE
# ==========================================================================


def test_a_consistent_individual_card_passes_and_claims_nothing_more():
    status, codes, advisories, detail = verdict()

    assert status == "PASS"
    assert codes == [] and advisories == []
    assert detail["authenticity"] == "NOT_ESTABLISHED"


#: What the pipeline itself read from each PAN in the sample corpus. Real
#: extraction output, not invented values.
REAL_READS = {
    "lPan.jpg": ("EVPPG6189E", "LAXMISANTOSHGUPTA", "SANTOSHRAMASHAREGUPTA", "2004-12-20"),
    "rpan.jpg": ("NUHPS4875K", "RISHABHAJITSINGH", "AJIT SINGH", "2002-06-12"),
    "f3.jpg": ("ELWPM8089J", "RAHUL MISHRA", "SATENDRA MISHRA", "1997-01-30"),
    "original.jpg": ("CNEPB1150M", "BIMAL BHATTARAI", "PADAM RAJ BHATTARAI", "1997-06-26"),
    "lakshmi.jpg": ("CEIPL7871N", "LAKSHMI", "GUTTI", "1988-01-01"),
    "64b7474a (older layout)": ("BYPPD8795R", "TINKU DAS", "JUGENDRO DAS", "1985-02-03"),
    "pan_bw2.jpg": ("AECPV7900A", "NAMO", "VASUNDHRA", "1984-07-01"),
}
NEW_CODES = {"PAN_HOLDER_TYPE_INCONSISTENT", "PAN_NAME_FATHER_IDENTICAL",
             "PAN_SERIAL_UNISSUED"}


@pytest.mark.parametrize("sample", sorted(REAL_READS))
def test_no_new_rule_fires_on_a_genuine_card(sample):
    number, name, father, dob = REAL_READS[sample]

    status, codes, _, _ = verdict(pan_number=number, name=name,
                                  father_name=father, date_of_birth=dob)

    assert not set(codes) & NEW_CODES, (sample, codes)


# ==========================================================================
# CONSISTENCY
# ==========================================================================


@pytest.mark.parametrize("holder_type", ["C", "F", "H", "T", "A", "B", "G", "L", "J", "K", "E"])
def test_a_non_individual_number_with_a_fathers_name_is_reviewed(holder_type):
    number = f"ABC{holder_type}S1234K"

    status, codes, _, _ = verdict(pan_number=number)

    assert status == "REVIEW"
    assert "PAN_HOLDER_TYPE_INCONSISTENT" in codes


def test_a_non_individual_card_without_a_fathers_name_is_not_questioned():
    """A company's card has no father's name; nothing contradicts."""
    status, codes, _, _ = verdict(pan_number="ABCCS1234K", father_name=None)

    assert "PAN_HOLDER_TYPE_INCONSISTENT" not in codes


@pytest.mark.parametrize("father", ["RISHABH AJIT SINGH", "Rishabh  Ajit-Singh",
                                    "RISHABHAJITSINGH"])
def test_holder_and_father_read_as_one_name_is_reviewed(father):
    status, codes, _, _ = verdict(father_name=father)

    assert status == "REVIEW"
    assert "PAN_NAME_FATHER_IDENTICAL" in codes


def test_a_shared_surname_is_not_the_same_name():
    status, codes, _, _ = verdict(name="AJIT SINGH", father_name="RAM SINGH",
                                  pan_number="NUHPS4875K")

    assert "PAN_NAME_FATHER_IDENTICAL" not in codes


# ==========================================================================
# MALFORMED AND SYNTHETIC
# ==========================================================================


def test_an_unissued_serial_is_reviewed():
    status, codes, _, _ = verdict(pan_number="NUHPS0000K", name="DEEPAK SHARMA",
                                  father_name="RAM SHARMA")

    assert status == "REVIEW"
    assert "PAN_SERIAL_UNISSUED" in codes


def test_the_familiar_specimen_already_fails_on_structure():
    """ABCDE1234F: "D" is not a holder type, so it is not a PAN at all."""
    status, codes, _, _ = verdict(pan_number="ABCDE1234F")

    assert status == "FAIL"
    assert "PAN_STRUCTURE_INVALID" in codes


@pytest.mark.parametrize("number", ["NUHXS4875K", "NUHP54875K", "NUHPS487K"])
def test_a_malformed_number_still_fails(number):
    status, codes, _, _ = verdict(pan_number=number)

    assert status == "FAIL"
    assert "PAN_STRUCTURE_INVALID" in codes


def test_the_existing_initial_advisory_is_unchanged():
    status, codes, advisories, _ = verdict(name="INHU AS", father_name="X Y",
                                           pan_number="BYPPD8795R")

    assert status == "PASS"                              # advisory, not a verdict
    assert "PAN_NAME_INITIAL_MISMATCH" in advisories


def test_each_new_rule_can_be_switched_off(monkeypatch):
    original = rules._rule_on
    monkeypatch.setattr(rules, "_rule_on", lambda cls, name, default=True: (
        False if name in {"holder_type_consistency", "name_father_distinct",
                          "serial_issued"} else original(cls, name, default)))

    # Would trip all three: non-individual code, same names, 0000 serial.
    status, codes, _, _ = verdict(pan_number="ABCCS0000K",
                                  father_name="RISHABH AJIT SINGH")

    assert not set(codes) & NEW_CODES


def test_every_new_code_is_explained():
    from app.agents.verification import reasons

    for code in NEW_CODES:
        assert reasons.known(code), code


# ==========================================================================
# THROUGH THE REAL FOS UPLOAD
# ==========================================================================

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
STATEMENT = SAMPLES / "documents" / "demo_bank_statement.pdf"
PAN_GENUINE = SAMPLES / "real_batch" / "pan_bw2.jpg"
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
    repository = SQLiteRepository(tmp_path / "pan.sqlite3")
    repository.initialise()
    set_repository(repository)
    client = TestClient(main.app)
    client.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES)}"})
    opened = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "Pan Test", "mobile": "9876543210"},
        "application": {"product": "PERSONAL_LOAN"}}).json()

    def upload(content: bytes, name: str, declared: str = "PAN") -> dict:
        response = client.post("/api/v1/fos/copilot", data={
            "applicant_id": opened["applicant_id"], "case_id": opened["case_id"],
            "action": "UPLOAD_DOCUMENT", "document_type": declared},
            files={"file": (name, content, "application/octet-stream")})
        assert response.status_code == 200, response.text
        return response.json()["verification"]

    yield upload
    set_repository(None)


def _synthetic_card(number: str) -> bytes:
    """A generated card in the PAN layout. Plainly synthetic; never a real one."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1000, 630), (245, 245, 240))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 38)
    except OSError:
        font = ImageFont.load_default()
    for y, text in ((40, "INCOME TAX DEPARTMENT    GOVT. OF INDIA"),
                    (150, "DEEPAK SHARMA"), (230, "RAM SHARMA"),
                    (310, "01/01/1990"), (380, "Permanent Account Number"),
                    (440, number), (540, "Signature")):
        draw.text((40, y), text, fill=(20, 20, 20), font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.ocr
def test_a_genuine_pan_still_passes_through_the_upload(fos):
    if not PAN_GENUINE.exists():
        pytest.skip("sample not available")
    result = fos(PAN_GENUINE.read_bytes(), "pan.jpg")

    assert result["verification"] == "PASS"
    assert not set(result["reason_codes"]) & NEW_CODES


@pytest.mark.ocr
def test_a_bank_statement_declared_as_a_pan_fails(fos):
    if not STATEMENT.exists():
        pytest.skip("sample not available")
    result = fos(STATEMENT.read_bytes(), "pan.pdf")

    assert result["verification"] == "FAIL"
    assert "DOCUMENT_TYPE_MISMATCH" in result["reason_codes"]
    assert result["extraction_released"] is False


@pytest.mark.ocr
def test_a_synthetic_card_with_a_specimen_number_never_passes(fos):
    result = fos(_synthetic_card("ABCDE1234F"), "pan.png")

    assert result["verification"] != "PASS"
    assert result["extraction_released"] is False


@pytest.mark.ocr
def test_an_unreadable_pan_never_passes(fos):
    if not PAN_GENUINE.exists():
        pytest.skip("sample not available")
    from PIL import Image

    image = Image.open(PAN_GENUINE).convert("RGB")
    small = image.resize((image.width // 14, image.height // 14))
    buffer = io.BytesIO()
    small.resize(image.size).save(buffer, format="JPEG", quality=12)

    result = fos(buffer.getvalue(), "pan.jpg")

    assert result["verification"] != "PASS"
    assert result["extraction_released"] is False
