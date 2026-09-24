"""
A PAN-shaped number is not a PAN card.

THE LIVE FALSE POSITIVE. A handwritten page -- not a PAN card -- carrying the
words "Permanent Account Number" and the string ERIPG2112G came back:

    document_type PAN, status PASS, identifier ERIPG2112G,
    identifier_format_valid true, document_class_identified PAN (score 0.6)

The class rested on one caption, counted twice ("ACCOUNTNUMBER" sits inside
"PERMANENTACCOUNTNUMBER"), and the identifier matched the PAN format. Nothing
asked whether the page IS a PAN card.

THE GATE. PAN document identity is now its own check, separate from
`identifier_format_valid`, established from what a PAN card itself prints --
the Income Tax Department header plus at least two of: GOVT. OF INDIA, the
number caption, the signature caption, the field labels, a date of birth
(never a camera timestamp) -- on a card-sized page. Calibrated on every real
PAN in the sample corpus.

    nothing a card carries beyond the number (and at most its caption)
                                            -> FAIL  DOCUMENT_NOT_PAN
    some evidence, not enough               -> REVIEW PAN_DOCUMENT_IDENTITY_NOT_ESTABLISHED
    a card's captions in a letter-length page -> REVIEW PAN_DOCUMENT_STRUCTURE_INVALID

None of it is authenticity: a card that passes still says NOT_ESTABLISHED.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from app.agents.verification.basic import pan_identity

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "samples" / "documents"
BATCH = ROOT / "samples" / "real_batch"
GENUINE = [DOCS / "rpan.jpg", DOCS / "original.jpg", DOCS / "f3.jpg",
           DOCS / "64b7474a-b3ca-4079-97cc-9ea8cf0cca4a.jpg", BATCH / "pan_bw2.jpg"]
FONTS = Path("C:/Windows/Fonts")
FOS_SCOPES = ["read_applicant", "read_application", "read_documents",
              "read_verification", "read_pending_items", "read_next_action",
              "create_applicant", "create_application", "upload_document"]


# ==========================================================================
# THE RULE, ON TEXT
# ==========================================================================


@pytest.mark.parametrize("text,outcome,code", [
    ("ERIPG2112G", "NOT_PAN", "DOCUMENT_NOT_PAN"),
    ("Permanent Account Number ERIPG2112G", "NOT_PAN", "DOCUMENT_NOT_PAN"),
    ("My PAN is ERIPG2112G Rahul Sharma 12/05/1990", "NOT_ESTABLISHED",
     "PAN_DOCUMENT_IDENTITY_NOT_ESTABLISHED"),
    ("INCOME TAX DEPARTMENT ERIPG2112G", "NOT_ESTABLISHED",
     "PAN_DOCUMENT_IDENTITY_NOT_ESTABLISHED"),
    ("INCOME TAX DEPARTMENT GOVT OF INDIA with reference to your Permanent Account "
     "Number ERIPG2112G " + "we write about the assessment and request documents " * 10
     + "dated 12/05/2023 Signature", "NOT_ESTABLISHED", "PAN_DOCUMENT_STRUCTURE_INVALID"),
])
def test_what_is_not_a_card_is_never_established(text, outcome, code):
    assert pan_identity(text)[:2] == (outcome, code)


def test_a_camera_timestamp_is_not_a_date_of_birth():
    _, _, anchors, _ = pan_identity("INCOME TAX DEPARTMENT Permanent Account Number "
                                    "ERIPG2112G 20-08-2026 14:11")
    assert "date_of_birth" not in anchors


def test_a_card_read_with_ocr_damage_is_still_a_card():
    """The real photocopy that reads 'GOVT OF INDLA' and 'INOOME TAXDEPARTMENT'."""
    text = ("GOVT OF INDLA INOOME TAXDEPARTMENT Permanent Account Number Card "
            "BEKPN6257F HARNARAYAN FathereNem SHANKARSINGH 20-08-2026 14:11")
    assert pan_identity(text)[0] == "ESTABLISHED"


# ==========================================================================
# GENERATED NEGATIVES -- plainly not PAN cards
# ==========================================================================


def _font(name: str, size: int):
    try:
        return ImageFont.truetype(str(FONTS / name), size)
    except OSError:
        return ImageFont.load_default()


def handwritten(lines: list[str], font="segoepr.ttf", ruled=True) -> bytes:
    """Handwriting-font text on (optionally ruled) notebook paper."""
    image = Image.new("RGB", (1200, 900), (250, 250, 245))
    draw = ImageDraw.Draw(image)
    if ruled:
        for y in range(120, 900, 70):
            draw.line([(40, y), (1160, y)], fill=(170, 190, 230), width=2)
    for i, line in enumerate(lines):
        draw.text((90, 60 + i * 140), line, fill=(20, 30, 90), font=_font(font, 64))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def typed_letter() -> bytes:
    """A typed page that happens to quote a PAN-shaped number."""
    image = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(image)
    font = _font("arial.ttf", 30)
    body = ["ACME TRADING COMPANY", "Invoice No. 4471", "Date: 12/05/2023",
            "Bill to: Rahul Sharma", "Customer PAN: ERIPG2112G",
            "Description: office stationery supplies",
            "Amount payable: Rs 12,450", "Thank you for your business."]
    for i, line in enumerate(body):
        draw.text((100, 120 + i * 80), line, fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def text_pdf(lines: list[str]) -> bytes:
    """A minimal PDF with a real text layer -- the typed-document path."""
    stream = "BT /F1 14 Tf 72 760 Td 18 TL " + " ".join(
        f"({line}) Tj T*" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return out


TYPED_PDF_LINES = (
    ["Rental agreement between the landlord and the tenant.",
     "Tenant name: Rahul Sharma. Tenant PAN: ERIPG2112G.",
     "Permanent Account Number of the tenant is recorded above."]
    + ["The tenant agrees to pay the monthly rent on or before the fifth day."] * 6)


# ==========================================================================
# THROUGH THE REAL ROUTES
# ==========================================================================


@pytest.fixture
def api(make_token, tmp_path, monkeypatch):
    import main

    from app.store import set_repository
    from app.store.sqlite_repo import SQLiteRepository

    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    repository = SQLiteRepository(tmp_path / "identity.sqlite3")
    repository.initialise()
    set_repository(repository)
    client = TestClient(main.app)
    client.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES + ['los.read'])}"})
    yield client
    set_repository(None)


def verify(api, content: bytes, name: str, expected="PAN") -> dict:
    """POST /api/v1/verify -- the route that returned the false PASS."""
    response = api.post("/api/v1/verify", data={"expected_type": expected},
                        files={"file": (name, content, "application/octet-stream")})
    # The route answers a FAIL verdict with 422 and anything else with 200;
    # the verdict itself is in the body either way.
    body = response.json()
    assert response.status_code == (422 if body.get("status") == "FAIL" else 200), response.text
    return body


def fos_upload(api, content: bytes, name: str, declared="PAN") -> dict:
    opened = api.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "Identity Test", "mobile": "9876543210"},
        "application": {"product": "PERSONAL_LOAN"}}).json()
    response = api.post("/api/v1/fos/copilot", data={
        "applicant_id": opened["applicant_id"], "case_id": opened["case_id"],
        "action": "UPLOAD_DOCUMENT", "document_type": declared},
        files={"file": (name, content, "application/octet-stream")})
    assert response.status_code == 200, response.text
    return response.json()["verification"]


def checks(body) -> dict:
    return {c["name"]: c["passed"] for c in body.get("checks") or []}


@pytest.mark.ocr
@pytest.mark.parametrize("path", GENUINE, ids=lambda p: p.name[:20])
def test_a_genuine_pan_passes(api, path):
    if not path.exists():
        pytest.skip("sample not available")
    body = verify(api, path.read_bytes(), path.name)

    assert body["status"] == "PASS", body.get("reason_codes")
    assert checks(body).get("pan_document_identity") is True


@pytest.mark.ocr
@pytest.mark.parametrize("font", ["segoepr.ttf", "Inkfree.ttf", "segoesc.ttf"])
@pytest.mark.parametrize("lines", [
    ["ERIPG2112G"],
    ["Permanent Account Number", "ERIPG2112G"],
    ["PAN no.", "ERIPG2112G", "Rahul Sharma", "12/05/1990"],
], ids=["number", "caption+number", "note"])
def test_a_handwritten_pan_number_never_passes(api, font, lines):
    body = verify(api, handwritten(lines, font=font), "note.jpg")

    assert body["status"] != "PASS", body
    assert body["status"] in ("FAIL", "REVIEW")


@pytest.mark.ocr
def test_the_live_false_positive_now_fails(api):
    """Caption + number on paper: the identifier is valid, the identity is not."""
    body = verify(api, handwritten(["Permanent Account Number", "ERIPG2112G"]), "note.jpg")

    assert body["status"] == "FAIL"
    if body.get("identifier"):
        # Kept apart: the number's FORMAT may be valid while the document
        # is not a PAN card.
        assert checks(body).get("identifier_format_valid") is True
        assert checks(body).get("pan_document_identity") is False
        assert "DOCUMENT_NOT_PAN" in body["reason_codes"]


#: The live case, legibly: enough clear handwriting that every structural
#: check passes -- legible, class PAN (score 0.6), identifier valid, scan
#: quality fine. Only the identity gate stands between it and PASS.
LEGIBLE_NOTE = ["Name: Rahul Sharma", "Father: Ram Sharma", "DOB: 12/05/1990",
                "Permanent Account Number", "ERIPG2112G"]


@pytest.mark.ocr
@pytest.mark.parametrize("font", ["segoepr.ttf", "Inkfree.ttf", "comic.ttf"])
def test_the_legible_handwritten_live_case_never_passes(api, font):
    body = verify(api, handwritten(LEGIBLE_NOTE, font=font), "note.jpg")
    seen = checks(body)

    assert body["status"] != "PASS", body
    # Where OCR read it well enough to reproduce the live verdict's
    # evidence, the identity check -- not illegibility -- is what refused it.
    if seen.get("document_legible") and seen.get("identifier_format_valid"):
        assert seen.get("pan_document_identity") is False
        assert set(body["reason_codes"]) & {"DOCUMENT_NOT_PAN",
                                            "PAN_DOCUMENT_IDENTITY_NOT_ESTABLISHED"}


@pytest.mark.ocr
def test_a_typed_pan_number_in_an_arbitrary_document_never_passes(api):
    body = verify(api, typed_letter(), "invoice.png")

    assert body["status"] in ("FAIL", "REVIEW")


def test_a_typed_pdf_quoting_a_pan_never_passes(api):
    """The text-layer path: no OCR, the PDF's own text is read."""
    body = verify(api, text_pdf(TYPED_PDF_LINES), "agreement.pdf")

    assert body["status"] in ("FAIL", "REVIEW")


@pytest.mark.ocr
def test_a_wrong_document_carrying_a_pan_string_fails(api):
    """A driving licence declared as a PAN."""
    licence = DOCS / "driving_license.jpg"
    if not licence.exists():
        pytest.skip("sample not available")
    body = verify(api, licence.read_bytes(), "pan.jpg")

    assert body["status"] == "FAIL"


@pytest.mark.ocr
def test_a_malformed_pan_fails(api):
    """A card-shaped page whose number breaks the PAN structure (D is not a holder type)."""
    image = Image.new("RGB", (1000, 630), (245, 245, 240))
    draw = ImageDraw.Draw(image)
    font = _font("arial.ttf", 36)
    for y, text in ((30, "INCOME TAX DEPARTMENT   GOVT. OF INDIA"), (130, "RAHUL SHARMA"),
                    (200, "RAM SHARMA"), (270, "01/01/1990"),
                    (340, "Permanent Account Number"), (400, "ABCDE1234F"), (520, "Signature")):
        draw.text((40, y), text, fill=(20, 20, 20), font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    result = fos_upload(api, buffer.getvalue(), "pan.png")

    assert result["verification"] == "FAIL"
    assert result["extraction_released"] is False


@pytest.mark.ocr
def test_an_unreadable_pan_does_not_pass(api):
    source = DOCS / "rpan.jpg"
    if not source.exists():
        pytest.skip("sample not available")
    image = Image.open(source).convert("RGB")
    small = image.resize((max(1, image.width // 14), max(1, image.height // 14)))
    buffer = io.BytesIO()
    small.resize(image.size).save(buffer, format="JPEG", quality=12)

    body = verify(api, buffer.getvalue(), "pan.jpg")

    assert body["status"] in ("FAIL", "REVIEW")


@pytest.mark.ocr
def test_a_handwritten_pan_is_refused_through_the_fos_upload_too(api):
    result = fos_upload(api, handwritten(["Permanent Account Number", "ERIPG2112G"]), "note.jpg")

    assert result["verification"] != "PASS"
    assert result["extraction_released"] is False


@pytest.mark.ocr
def test_a_passing_card_still_claims_no_authenticity(api):
    source = DOCS / "rpan.jpg"
    if not source.exists():
        pytest.skip("sample not available")
    result = fos_upload(api, source.read_bytes(), "pan.jpg")

    assert result["verification"] == "PASS"
    assert result["authenticity"] == "NOT_ESTABLISHED"
    assert result["issuer_verified"] is False
