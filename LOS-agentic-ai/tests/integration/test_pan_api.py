"""
PAN-ONLY PARTNER API -- POSITIVE AND NEGATIVE TESTS (2026-10-16).

    POST /api/v1/pan/verify-extract   one call: verify, and extract ONLY when verification passes

THE RULE EVERY TEST CHECKS: a response that is not VERIFIED carries NO fields -- a failed or doubtful verification
never leaks a PAN number, a name or a date of birth. Half / cut cards, other cards, blank or blurred photos and bad
files are all refused; genuine PANs (real samples and the synthetic set across qualities) are VERIFIED with fields.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
REAL_PANS = [ROOT / "samples" / "documents" / n for n in ("lPan.jpg", "rpan.jpg", "original.jpg", "f3.jpg")] + \
            [ROOT / "samples" / "real_batch" / "pan_bw1.jpg"]
DL = ROOT / "samples" / "real_test" / "dl1.jpg"
VOTER = ROOT / "samples" / "real_test" / "voter4.jpg"
PASSPORT = ROOT / "samples" / "passports" / "passport_samples0_10.jpg"
SYN = ROOT / "samples" / "synthetic" / "heldout"
EICAR = ("X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS-" + "TEST-FILE!$H+H*").encode()
LEAKS = ("Traceback", "File \"", "site-packages", "C:\\")
pytestmark = pytest.mark.skipif(not REAL_PANS[0].is_file(), reason="real samples not present")


@pytest.fixture
def partner(make_token):
    import main

    c = TestClient(main.app, raise_server_exceptions=False)
    c.headers.update({"Authorization": f"Bearer {make_token(subject='partner', scopes=['pan:verify'])}"})
    return c


def _post(client, name: str, data: bytes):
    return client.post("/api/v1/pan/verify-extract", files={"file": (name, data)})


def _jpeg(image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=92)
    return buffer.getvalue()


def _no_fields_unless_verified(body: dict) -> None:
    if body["status"] != "VERIFIED":
        assert body["fields"] is None, body
    assert not [x for x in LEAKS if x in json.dumps(body)]


def _synthetic_pans() -> list[tuple[str, dict]]:
    if not (SYN / "ground_truth.json").is_file():
        return []
    truth = json.loads((SYN / "ground_truth.json").read_text(encoding="utf-8"))
    return [(k, v) for k, v in truth.items() if not k.startswith("_") and v["document_type"] == "PAN"]


# ---- positive ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", REAL_PANS, ids=lambda p: p.name)
def test_a_real_pan_is_verified_with_all_four_fields(partner, path):
    body = _post(partner, path.name, path.read_bytes()).json()
    assert body["status"] == "VERIFIED", body["reason_codes"]
    assert body["document_type"] == "PAN"
    assert all(body["fields"][f] for f in ("pan_number", "name", "father_name", "date_of_birth"))
    assert "pan_card_complete" not in body["checks"] or body["checks"]["pan_card_complete"] == "PASS"


@pytest.mark.parametrize("quality", ["clean", "phone", "skew", "low_light", "jpeg", "small"])
def test_a_synthetic_pan_of_each_quality_returns_the_true_values_or_nothing(partner, quality):
    picks = [(k, v) for k, v in _synthetic_pans() if v["quality"] == quality]
    if not picks:
        pytest.skip("synthetic set not generated")
    for name, truth in picks[:2]:
        body = _post(partner, name, (SYN / name).read_bytes()).json()
        _no_fields_unless_verified(body)
        if body["status"] == "VERIFIED":
            assert body["fields"]["pan_number"] == truth["fields"]["pan_number"], name
            assert body["fields"]["date_of_birth"] == truth["fields"]["date_of_birth"], name


def test_a_pan_as_a_pdf_is_verified(partner):
    from PIL import Image

    buffer = io.BytesIO()
    Image.open(REAL_PANS[0]).convert("RGB").save(buffer, "PDF")
    body = _post(partner, "pan.pdf", buffer.getvalue()).json()
    assert body["status"] == "VERIFIED" and body["fields"]["pan_number"]


# ---- negative: a half / cut PAN is REJECTED and returns nothing --------------------------------------------------
CUTS = {"left_half": lambda w, h: (0, 0, w // 2, h), "right_half": lambda w, h: (w // 2, 0, w, h),
        "top_half": lambda w, h: (0, 0, w, h // 2), "bottom_half": lambda w, h: (0, h // 2, w, h),
        "top_60_percent": lambda w, h: (0, 0, w, int(h * 0.6))}


@pytest.mark.parametrize("path", REAL_PANS[:4], ids=lambda p: p.name)
@pytest.mark.parametrize("cut", list(CUTS))
def test_a_half_pan_is_rejected_and_no_field_is_returned(partner, path, cut):
    from PIL import Image

    image = Image.open(path).convert("RGB")
    body = _post(partner, "half.jpg", _jpeg(image.crop(CUTS[cut](*image.size)))).json()
    assert body["status"] == "REJECTED", (path.name, cut, body["status"], body["reason_codes"])
    assert body["fields"] is None


def test_the_left_half_says_why(partner):
    """The case that used to PASS: every PAN field sits on the left of the card."""
    from PIL import Image

    image = Image.open(REAL_PANS[4]).convert("RGB")
    body = _post(partner, "half.jpg", _jpeg(image.crop((0, 0, image.width // 2, image.height)))).json()
    assert body["status"] == "REJECTED" and body["fields"] is None
    assert "PAN_CARD_INCOMPLETE" in body["reason_codes"]


# ---- negative: not a PAN -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", [DL, VOTER, PASSPORT], ids=lambda p: p.name)
def test_another_card_is_rejected_with_no_fields(partner, path):
    body = _post(partner, path.name, path.read_bytes()).json()
    assert body["status"] == "REJECTED" and body["fields"] is None
    assert "DOCUMENT_TYPE_MISMATCH" in body["reason_codes"]


def test_an_aadhaar_style_card_returns_nothing(partner):
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1012, 638), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    draw.text((60, 40), "GOVERNMENT OF INDIA", font=ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 34), fill=0)
    draw.text((60, 120), "Unique Identification Authority of India",
              font=ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 26), fill=0)
    body = _post(partner, "card.jpg", _jpeg(image)).json()
    assert body["status"] != "VERIFIED" and body["fields"] is None


@pytest.mark.parametrize("tone", [255, 0, 128])
def test_a_blank_photo_returns_nothing(partner, tone):
    from PIL import Image

    body = _post(partner, "blank.jpg", _jpeg(Image.new("RGB", (1012, 638), (tone,) * 3))).json()
    assert body["status"] != "VERIFIED" and body["fields"] is None


def test_a_heavily_blurred_pan_returns_nothing(partner):
    from PIL import Image, ImageFilter

    body = _post(partner, "blur.jpg", _jpeg(Image.open(REAL_PANS[0]).filter(ImageFilter.GaussianBlur(8)))).json()
    assert body["status"] != "VERIFIED" and body["fields"] is None


def test_a_pan_with_its_number_covered_returns_nothing(partner):
    """The PAN number blacked out: the card cannot be verified, so nothing is returned."""
    from PIL import Image, ImageDraw

    image = Image.open(REAL_PANS[0]).convert("RGB")
    w, h = image.size
    ImageDraw.Draw(image).rectangle((0, int(h * 0.30), int(w * 0.75), int(h * 0.55)), fill=(255, 255, 255))
    body = _post(partner, "covered.jpg", _jpeg(image)).json()
    assert body["status"] != "VERIFIED" and body["fields"] is None


# ---- negative: bad files -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,data,code", [
    ("p.jpg", b"", 400),
    ("p.exe", b"MZ\x90\x00", 422),
    ("p.jpg", b"not an image at all", 422),
    ("p.jpg", EICAR, 422),
    ("p.jpg", b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4, 422),
    ("p.pdf", b"%PDF-1.4 garbage", 422),
])
def test_bad_uploads_are_clean_4xx_with_no_fields(partner, name, data, code):
    r = _post(partner, name, data)
    assert r.status_code == code, (r.status_code, r.text[:200])
    assert not [x for x in LEAKS if x in r.text] and "pan_number" not in r.text


def test_an_oversized_upload_is_413(partner):
    assert _post(partner, "p.jpg", b"\xff\xd8\xff" + b"\x00" * (25 * 2**20 + 10)).status_code == 413


# ---- access ----------------------------------------------------------------------------------------------------------
def test_the_partner_token_opens_no_other_api(partner):
    data = REAL_PANS[0].read_bytes()
    assert partner.post("/api/v1/document-agent", files={"file": ("p.jpg", data)},
                        data={"operation": "EXTRACT"}).status_code == 403
    assert partner.post("/api/v1/passbook/extract-passbook", files={"file": ("p.jpg", data)}).status_code == 403


def test_no_token_is_401(monkeypatch):
    import main

    monkeypatch.setenv("AUTH_ENABLED", "true")
    assert _post(TestClient(main.app), "p.jpg", REAL_PANS[0].read_bytes()).status_code == 401


def test_a_weak_token_is_403(make_token, monkeypatch):
    import main

    monkeypatch.setenv("AUTH_ENABLED", "true")
    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=['read_applicant'])}"})
    assert _post(c, "p.jpg", REAL_PANS[0].read_bytes()).status_code == 403


def test_a_genuine_pan_whose_date_of_birth_cannot_be_read_returns_nothing(partner):
    """A real card with its printed date of birth covered: a required field unread -> REJECTED, no fields."""
    from tests.integration.pan_fixtures import SOURCE, pan_with_unreadable_dob

    if not SOURCE.is_file():
        pytest.skip("real sample not present")
    path = pan_with_unreadable_dob()
    body = _post(partner, "pan.jpg", path.read_bytes()).json()
    assert body["status"] == "REJECTED" and body["fields"] is None, (body["status"], body["reason_codes"])


def test_a_native_e_pan_pdf_is_read_from_its_text_without_ocr(partner, monkeypatch):
    """A digital e-PAN carries its own text: no OCR runs, and it answers in milliseconds (made-up person/number)."""
    import time

    import pymupdf

    from app.agents.document_agent import ocr

    def _no_ocr(*_a, **_k):
        raise AssertionError("OCR ran on a PDF that carries its own text")

    monkeypatch.setattr(ocr.RapidOCREngine, "read_array", _no_ocr)
    doc = pymupdf.open()
    page = doc.new_page(width=420, height=265)
    for i, line in enumerate(["INCOME TAX DEPARTMENT        GOVT. OF INDIA", "Permanent Account Number Card",
                              "ABCPE1234F", "Name", "RAHUL KUMAR SHARMA", "Father's Name", "SURESH KUMAR SHARMA",
                              "Date of Birth", "14/07/1994", "Signature"]):
        page.insert_text((20, 25 + 23 * i), line, fontsize=11)
    started = time.perf_counter()
    body = _post(partner, "e-pan.pdf", doc.tobytes()).json()
    assert body["status"] == "VERIFIED", body["reason_codes"]
    assert body["fields"]["pan_number"] == "ABCPE1234F" and body["fields"]["date_of_birth"]
    assert time.perf_counter() - started < 1.0


def test_a_scanned_pan_pdf_reads_like_its_photo(partner):
    """A photo saved as a PDF: the picture inside is read directly (no re-render), same verdict and fields."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.open(REAL_PANS[4]).convert("RGB").save(buffer, "PDF")
    as_pdf = _post(partner, "scan.pdf", buffer.getvalue()).json()
    as_jpg = _post(partner, "scan.jpg", REAL_PANS[4].read_bytes()).json()
    assert as_pdf["status"] == as_jpg["status"] == "VERIFIED"
    assert as_pdf["fields"] == as_jpg["fields"]


def _pan_back() -> "Image.Image":
    """The standard back of a PAN card: lost-card notice and the issuing service's address (public wording)."""
    from PIL import Image, ImageDraw, ImageFont

    im = Image.new("RGB", (1012, 638), (225, 236, 245))
    draw = ImageDraw.Draw(im)
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 22)
    lines = ["If this card is lost / someone's lost card is found,", "please inform / return to :",
             "Income Tax PAN Services Unit, Protean eGov Technologies Limited",
             "(formerly NSDL e-Governance Infrastructure Limited)", "4th Floor, Sapphire Chambers,",
             "Baner Road, Baner,", "Pune - 411045", "Tel: 91-20-2721 8080, e-mail: tininfo@proteantech.in",
             "ito.systems1.1@incometax.gov.in, pangrievance@incometax.gov.in"]
    for i, line in enumerate(lines):
        draw.text((40, 220 + 34 * i), line, font=font, fill=(20, 20, 20))
    return im


def test_a_pan_photographed_front_and_back_together_is_verified(partner):
    """2026-10-16: a genuine PAN, front and back in one photo, read 456 characters > the 450 card limit and was
    REJECTED as a longer document. The back's own wording allows for it."""
    from PIL import Image

    front = Image.open(REAL_PANS[0]).convert("RGB")
    back = _pan_back().resize((front.width, int(front.width * 638 / 1012)))
    both = Image.new("RGB", (front.width, front.height + back.height + 20), (255, 255, 255))
    both.paste(front, (0, 0))
    both.paste(back, (0, front.height + 20))
    body = _post(partner, "front_back.jpg", _jpeg(both)).json()
    assert body["status"] == "VERIFIED", body["reason_codes"]
    assert all(body["fields"].values())


def test_a_long_document_quoting_pan_captions_is_still_not_a_card():
    """The allowance is for a card's back, not for any long text: without the back's wording the limit stands."""
    from app.agents.verification.basic import pan_identity

    text = ("INCOME TAX DEPARTMENT GOVT OF INDIA PERMANENT ACCOUNT NUMBER ABCPE1234F FATHER'S NAME DATE OF BIRTH "
            "01/01/1990 SIGNATURE " + "This statement of account lists the transactions for the period. " * 12)
    assert pan_identity(text)[1] == "PAN_DOCUMENT_STRUCTURE_INVALID"
