"""
HRMS APIs -- END TO END, POSITIVE AND NEGATIVE (2026-10-16).

    POST /api/v1/hrms/signature/verify    the signature judged on its own (never against another signature)
    POST /api/v1/hrms/passbook/verify     the passbook APIs, under /hrms
    POST /api/v1/hrms/passbook/extract

Real OCR, real auth. Positive signatures are pen strokes drawn on paper (and the real signature samples when they are
on this machine); handwriting FONTS are deliberately not used as positives -- they are typeset text, which is exactly
what the typed-name check exists to refuse.
"""

from __future__ import annotations

import io
import json
import math
import random
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from tests.integration.test_passbook_api import every_passbook_field  # noqa: F401  (autouse: all five fields)

ROOT = Path(__file__).resolve().parents[2]
REAL_SIGNATURES = [Path("C:/Users/ET0002183/Downloads") / n for n in ("SIGN.png", "SIGN1.png",
                                                                        "test_signature_google.jpg")]
PASSBOOKS = ROOT / "samples" / "synthetic_passbook" / "heldout"
EICAR = ("X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS-" + "TEST-FILE!$H+H*").encode()
LEAKS = ("Traceback", "File \"", "site-packages", "C:\\")


@pytest.fixture
def hr(make_token):
    import main

    c = TestClient(main.app, raise_server_exceptions=False)
    c.headers.update({"Authorization": f"Bearer {make_token(subject='hrms', scopes=['hrms:verify'])}"})
    return c


def _sign(client, data: bytes, name: str = "signature.png"):
    r = client.post("/api/v1/hrms/signature/verify", files={"file": (name, data)})
    assert not [x for x in LEAKS if x in r.text], r.text[:300]
    return r


def _png(image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "PNG")
    return buffer.getvalue()


def _jpeg(image, quality: int = 90) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


def pen_signature(seed: int, size=(640, 240)) -> Image.Image:
    """A handwritten-looking signature: one or two connected pen paths of loops and a flourish, varying pressure,
    on slightly toned paper with sensor noise. Not text: OCR reads nothing in it, as with a real signature."""
    rng = random.Random(seed)
    w, h = size
    im = Image.new("L", size, rng.randint(236, 250))
    draw = ImageDraw.Draw(im)
    x, y = rng.randint(70, 110), h // 2 + rng.randint(-15, 15)
    points = []
    for k in range(rng.randint(70, 110)):
        x += rng.uniform(3.5, 6.0)
        y = h // 2 + 45 * math.sin(k * rng.uniform(0.35, 0.55)) * math.cos(k * 0.11) + rng.uniform(-4, 4)
        points.append((x, y))
        if x > w - 120:
            break
    ink = rng.randint(10, 60)
    draw.line(points, fill=ink, width=rng.randint(3, 5), joint="curve")
    draw.line([(points[0][0] - 10, h // 2 + 55), (points[-1][0] + 25, h // 2 + 40)], fill=ink, width=3)
    arr = np.asarray(im, dtype="float32") + np.random.default_rng(seed).normal(0, 3, (h, w))
    return Image.fromarray(np.clip(arr, 0, 255).astype("uint8")).convert("RGB")


# ---- signature: positive -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", range(6))
def test_a_handwritten_signature_is_verified(hr, seed):
    r = _sign(hr, _png(pen_signature(seed)))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "VERIFIED", body["reasons"]
    assert body["checks"] == {"not_blank": "PASS", "sharp": "PASS", "whole": "PASS", "handwritten": "PASS"}


@pytest.mark.parametrize("path", REAL_SIGNATURES, ids=lambda p: p.name)
def test_real_signature_samples_are_verified(hr, path):
    if not path.is_file():
        pytest.skip("real signature sample not on this machine")
    assert _sign(hr, path.read_bytes(), path.name).json()["status"] == "VERIFIED"


def test_a_signature_inside_a_form_box_is_verified(hr):
    im = pen_signature(3)
    ImageDraw.Draw(im).rectangle((1, 1, im.width - 2, im.height - 2), outline=(90, 90, 90), width=2)
    assert _sign(hr, _png(im)).json()["status"] == "VERIFIED"


@pytest.mark.parametrize("quality", [60, 85])
def test_a_jpeg_photo_of_a_signature_is_verified(hr, quality):
    assert _sign(hr, _jpeg(pen_signature(1), quality), "s.jpg").json()["status"] == "VERIFIED"


def test_a_signature_sent_as_a_pdf_is_verified(hr):
    buffer = io.BytesIO()
    pen_signature(2).save(buffer, "PDF")
    assert _sign(hr, buffer.getvalue(), "s.pdf").json()["status"] == "VERIFIED"


# ---- signature: negative -------------------------------------------------------------------------------------------
def _typed(text="Rahul Sharma") -> Image.Image:
    im = Image.new("RGB", (640, 240), (250, 250, 250))
    ImageDraw.Draw(im).text((60, 80), text, font=ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 64), fill=0)
    return im


NEGATIVES = {
    "blank white": (lambda: Image.new("RGB", (640, 240), (250, 250, 250)), "REJECTED", "BLANK"),
    "blank grey": (lambda: Image.new("RGB", (640, 240), (180, 180, 180)), "REJECTED", "BLANK"),
    "blurred": (lambda: pen_signature(0).filter(ImageFilter.GaussianBlur(6)), "REJECTED", "BLURRED"),
    "cut in half": (lambda: pen_signature(0).crop((0, 0, 360, 240)), "REJECTED", "CUT_OFF"),
    "bottom cut off": (lambda: pen_signature(0).crop((0, 0, 640, 125)), "REJECTED", "CUT_OFF"),
    "typed name": (_typed, "REJECTED", "TYPED_TEXT"),
    "a straight line": (lambda: _drawn(lambda d: d.line([(80, 120), (560, 122)], fill=0, width=5)), "REJECTED", "LINE"),
    "a dot": (lambda: _drawn(lambda d: d.ellipse((310, 110, 322, 122), fill=0)), "REJECTED", "DOT"),
}


def _drawn(fn) -> Image.Image:
    im = Image.new("RGB", (640, 240), (248, 248, 248))
    fn(ImageDraw.Draw(im))
    return im


@pytest.mark.parametrize("case", list(NEGATIVES))
def test_a_bad_signature_is_rejected_with_its_reason(hr, case):
    build, status, reason = NEGATIVES[case]
    body = _sign(hr, _png(build())).json()
    assert body["status"] == status, (case, body["status"], body["reasons"])
    assert reason in [x["code"] for x in body["reasons"]], (case, body["reasons"])
    assert any(v == "FAIL" for v in body["checks"].values())


def test_a_slightly_soft_signature_is_never_verified_silently(hr):
    body = _sign(hr, _png(pen_signature(4).filter(ImageFilter.GaussianBlur(1.8)))).json()
    assert body["status"] in ("MANUAL_REVIEW", "REJECTED")


def test_a_pan_card_is_not_a_signature(hr):
    pan = ROOT / "samples" / "documents" / "lPan.jpg"
    if not pan.is_file():
        pytest.skip("sample not present")
    assert _sign(hr, pan.read_bytes(), "pan.jpg").json()["status"] != "VERIFIED"


@pytest.mark.parametrize("name,data,code", [
    ("s.png", b"", 400),
    ("s.exe", b"MZ\x90\x00", 415),
    ("s.docx", b"PK\x03\x04", 415),
    ("s.png", EICAR, 422),
    ("s.png", b"just text", 422),
    ("s.jpg", b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4, 422),
    ("s.pdf", b"%PDF-1.4 junk", 422),
])
def test_bad_files_are_clean_4xx(hr, name, data, code):
    r = _sign(hr, data, name)
    assert r.status_code == code, (r.status_code, r.text[:200])


def test_an_oversized_signature_is_413(hr):
    assert _sign(hr, b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 2**20 + 10)).status_code == 413


def test_an_engine_failure_is_a_clean_500_and_the_next_request_works(hr, monkeypatch):
    from app.agents.signature import presence

    original = presence.check
    monkeypatch.setattr(presence, "check", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("engine down")))
    r = _sign(hr, _png(pen_signature(0)))
    assert r.status_code == 500 and r.json()["detail"]["status"] == "ERROR" and "engine down" not in r.text
    monkeypatch.setattr(presence, "check", original)
    assert _sign(hr, _png(pen_signature(0))).json()["status"] == "VERIFIED"


# ---- passbook under /hrms ------------------------------------------------------------------------------------------
def test_the_passbook_apis_under_hrms_match_the_originals(hr):
    truth = json.loads((PASSBOOKS / "ground_truth.json").read_text(encoding="utf-8"))
    name = next(k for k, v in truth.items() if not k.startswith("_") and v["scenario"] == "GENUINE"
                and v["quality"] == "clean")
    data = (PASSBOOKS / name).read_bytes()
    v = hr.post("/api/v1/hrms/passbook/verify", files={"file": (name, data)}).json()
    e = hr.post("/api/v1/hrms/passbook/extract", files={"file": (name, data)}).json()
    assert v["status"] == "VERIFIED" and e["reused_result"] is True
    assert e["fields"]["account_number"]["value"] == truth[name]["fields"]["account_number"]


def test_a_conflicting_passbook_is_rejected_under_hrms(hr):
    truth = json.loads((PASSBOOKS / "ground_truth.json").read_text(encoding="utf-8"))
    name = next(k for k, v in truth.items() if not k.startswith("_") and v["scenario"] == "BANK_CONFLICT"
                and v["quality"] == "clean")
    assert hr.post("/api/v1/hrms/passbook/verify", files={"file": (name, (PASSBOOKS / name).read_bytes())}
                   ).json()["status"] == "REJECTED"


# ---- access --------------------------------------------------------------------------------------------------------
def test_no_token_is_401_and_a_weak_token_is_403(make_token, monkeypatch):
    import main

    monkeypatch.setenv("AUTH_ENABLED", "true")
    bare = TestClient(main.app)
    data = _png(pen_signature(0))
    assert bare.post("/api/v1/hrms/signature/verify", files={"file": ("s.png", data)}).status_code == 401
    weak = {"Authorization": f"Bearer {make_token(scopes=['read_applicant'])}"}
    assert bare.post("/api/v1/hrms/signature/verify", files={"file": ("s.png", data)}, headers=weak).status_code == 403
    pan_only = {"Authorization": f"Bearer {make_token(scopes=['pan:verify'])}"}
    assert bare.post("/api/v1/hrms/signature/verify", files={"file": ("s.png", data)},
                     headers=pan_only).status_code == 403
