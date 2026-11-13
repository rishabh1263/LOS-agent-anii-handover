"""
ONE SIGNATURE API, THREE WAYS OF SIGNING -- POST /api/v1/hrms/signature/verify (2026-10-17). Always a normal FILE
(an image or a PDF; owner: no base64): UPLOAD (scan / photo), DRAW (the canvas sent as its PNG -- transparent, any ink
colour), CAPTURE (a camera photo). Positive and negative through the real app.

THE CONTRACT: a genuine signature VERIFIED in about a second (measured: tens of ms); a blank page, an ID card or a
printed page REJECTED in a blink (no OCR); a bad signature never VERIFIED; a clean response; a valid request still
works after bad ones.
"""

from __future__ import annotations

import base64
import io
import random

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from evals.signature import modes as M

ROUTE = "/api/v1/hrms/signature/verify"
LEAKS = ("Traceback", "File \"", "site-packages", "C:\\", "onnx", "cv2")
EICAR = ("X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS-" + "TEST-FILE!$H+H*").encode()
FAST_MS = 1000           # a signature, any mode
BLINK_MS = 500           # a blank page / another document


@pytest.fixture
def client(make_token):
    import main

    c = TestClient(main.app, raise_server_exceptions=False)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=['hrms:verify'])}"})
    return c


def _file(client, data: bytes, name: str, source: str | None = None, headers=None):
    form = {"source": source} if source else {}
    return client.post(ROUTE, files={"file": (name, io.BytesIO(data))}, data=form, headers=headers or {})


def _clean(r, *codes):
    assert r.status_code in codes, (r.status_code, r.text[:300])
    assert not [x for x in LEAKS if x in r.text], r.text[:300]


def _drawn(seed=1, ink=(0, 0, 0, 255), width=4, size=(700, 240)):
    return M._png(M.scribble(random.Random(seed), size, ink=ink, bg=(0, 0, 0, 0), width=width))


def _pdf_of(png: bytes) -> bytes:
    b = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(b, "PDF", resolution=150)
    return b.getvalue()


def _page():
    page = Image.new("RGB", (1000, 700), "white")
    d = ImageDraw.Draw(page)
    f = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 26)
    for r in range(14):
        d.text((40, 30 + r * 46), "This agreement is made between the parties on the date below.", font=f,
               fill=(0, 0, 0))
    return M._jpeg(page, 90)


# ---- UPLOAD ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_upload_a_written_signature_is_verified_fast(client, seed):
    rng = random.Random(seed)
    im = M.written(rng, "heldout", (1000, 360), ink=(25, 40, 140), bg=(250, 247, 238, 255))
    r = _file(client, M._jpeg(im, 90), "sig.jpg", "upload")
    _clean(r, 200)
    body = r.json()
    assert body["status"] == "VERIFIED", body["reasons"]
    assert body["source"] == "upload" and set(body["checks"]) == {"not_blank", "sharp", "whole", "handwritten"}
    assert body["time_ms"] < FAST_MS, body["time_ms"]


def test_upload_a_signature_as_a_pdf_is_verified(client):
    im = M.written(random.Random(9), "dev", (1000, 360))
    r = _file(client, _pdf_of(M._png(im)), "sig.pdf", "upload")
    _clean(r, 200)
    assert r.json()["status"] == "VERIFIED", r.json()["reasons"]


def test_upload_on_a_printed_signature_line_is_verified(client):
    im = M.written(random.Random(5), "dev", (1000, 360), ink=(20, 20, 30), bg=(255, 255, 255, 255)).convert("RGB")
    ImageDraw.Draw(im).line((0, 280, im.width, 280), fill=(90, 90, 90), width=2)
    assert _file(client, M._jpeg(im, 90), "sig.jpg").json()["status"] == "VERIFIED"


def test_upload_default_source_is_upload(client):
    im = M.written(random.Random(7), "dev", (900, 320))
    assert _file(client, M._png(im), "sig.png").json()["source"] == "upload"


@pytest.mark.parametrize("label,make,status", [
    ("blank paper", lambda: M._jpeg(Image.new("RGB", (1000, 360), (248, 246, 240)), 90), "REJECTED"),
    ("blurred", lambda: M._jpeg(M.written(random.Random(3), "dev", (1000, 360)).convert("RGB")
                                .filter(ImageFilter.GaussianBlur(4)), 88), "REJECTED"),
    ("cut in half", lambda: M._jpeg(M.written(random.Random(4), "dev", (1000, 360)).convert("RGB")
                                    .crop((0, 0, 500, 360)), 90), "REJECTED"),
])
def test_upload_a_bad_signature_is_rejected(client, label, make, status):
    r = _file(client, make(), "sig.jpg", "upload")
    _clean(r, 200)
    assert r.json()["status"] == status, (label, r.json()["reasons"])


def test_upload_a_typed_name_is_never_verified(client):
    im = Image.new("RGB", (900, 240), "white")
    ImageDraw.Draw(im).text((40, 60), "RAHUL SHARMA", font=ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 90),
                            fill=(0, 0, 0))
    assert _file(client, M._png(im), "sig.png").json()["status"] != "VERIFIED"


# ---- OTHER DOCUMENTS: rejected in a blink ---------------------------------------------------------------------------
@pytest.mark.parametrize("label,path", [("PAN card", "samples/documents/lPan.jpg"),
                                        ("PAN card 2", "samples/documents/rpan.jpg"),
                                        ("driving licence", "samples/documents/driving_license.jpg")])
def test_an_id_card_is_rejected_in_a_blink(client, label, path):
    from pathlib import Path

    if not Path(path).is_file():
        pytest.skip("sample not present")
    r = _file(client, Path(path).read_bytes(), "x.jpg", "upload")
    _clean(r, 200)
    body = r.json()
    assert body["status"] == "REJECTED", (label, body["reasons"])
    assert body["time_ms"] < BLINK_MS, (label, body["time_ms"])


def test_a_printed_page_is_rejected_in_a_blink_as_not_a_signature(client):
    r = _file(client, _page(), "page.jpg", "upload")
    _clean(r, 200)
    body = r.json()
    assert body["status"] == "REJECTED" and body["reasons"][0]["code"] == "NOT_A_SIGNATURE", body["reasons"]
    assert body["time_ms"] < BLINK_MS, body["time_ms"]


def test_a_printed_page_as_a_pdf_is_rejected(client):
    r = _file(client, _pdf_of(_page()), "page.pdf", "upload")
    _clean(r, 200)
    assert r.json()["status"] == "REJECTED"


def test_a_blank_page_is_rejected_in_a_blink(client):
    r = _file(client, M._jpeg(Image.new("RGB", (2000, 800), (248, 246, 240)), 90), "blank.jpg")
    _clean(r, 200)
    assert r.json()["status"] == "REJECTED" and r.json()["time_ms"] < BLINK_MS


# ---- DRAW (the canvas sent as its PNG file) -------------------------------------------------------------------------
@pytest.mark.parametrize("ink", [(0, 0, 0, 255), (20, 40, 160, 255), (255, 255, 255, 255)])
def test_draw_a_transparent_canvas_in_any_ink_colour_is_verified(client, ink):
    r = _file(client, _drawn(ink=ink), "sig.png", "draw")
    _clean(r, 200)
    assert r.json()["status"] == "VERIFIED", (ink, r.json()["reasons"])
    assert r.json()["time_ms"] < FAST_MS


@pytest.mark.parametrize("width", [2, 3, 6])
def test_draw_thin_or_thick_strokes_are_verified(client, width):
    assert _file(client, _drawn(seed=width, width=width), "sig.png", "draw").json()["status"] == "VERIFIED"


@pytest.mark.parametrize("label,make", [
    ("empty transparent canvas", lambda: M._png(Image.new("RGBA", (600, 200), (0, 0, 0, 0)))),
    ("empty white canvas", lambda: M._png(Image.new("RGB", (600, 200), "white"))),
    ("a dot", lambda: _dot()),
    ("one straight line", lambda: _line()),
    ("stroke runs off the canvas", lambda: M._png(M.scribble(random.Random(2), (600, 200), ink=(0, 0, 0, 255),
                                                             width=4).crop((0, 0, 380, 200)))),
])
def test_draw_an_empty_or_bad_canvas_is_rejected(client, label, make):
    r = _file(client, make(), "sig.png", "draw")
    _clean(r, 200)
    assert r.json()["status"] == "REJECTED", (label, r.json()["reasons"])


def _dot():
    im = Image.new("RGBA", (600, 200), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((300, 100, 306, 106), fill=(0, 0, 0, 255))
    return M._png(im)


def _line():
    im = Image.new("RGBA", (600, 200), (0, 0, 0, 0))
    ImageDraw.Draw(im).line((60, 120, 540, 121), fill=(0, 0, 0, 255), width=4)
    return M._png(im)


# ---- CAPTURE (a camera photo file) ----------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_capture_a_photo_with_shadow_and_tilt_is_verified_fast(client, seed):
    rng = random.Random(seed)
    im = M.written(rng, "heldout", (1200, 500), ink=(20, 20, 30), bg=(250, 248, 240, 255), margin=0.2)
    r = _file(client, M.camera(im.convert("RGB"), rng), "cap.jpg", "capture")
    _clean(r, 200)
    assert r.json()["status"] == "VERIFIED", r.json()["reasons"]
    assert r.json()["time_ms"] < FAST_MS


def test_capture_of_blank_paper_with_shadow_is_rejected(client):
    data = M.camera(Image.new("RGB", (1200, 500), (245, 243, 236)), random.Random(4))
    assert _file(client, data, "cap.jpg", "capture").json()["status"] == "REJECTED"


def test_capture_with_motion_blur_is_never_verified(client):
    rng = random.Random(5)
    im = M.written(rng, "dev", (1200, 500), ink=(20, 20, 30), bg=(250, 248, 240, 255), margin=0.2).convert("RGB")
    assert _file(client, M.camera(im.filter(ImageFilter.BoxBlur(5)), rng), "cap.jpg", "capture") \
        .json()["status"] != "VERIFIED"


def test_capture_cut_off_by_the_frame_is_never_verified(client):
    rng = random.Random(6)
    im = M.written(rng, "dev", (1200, 500), ink=(20, 20, 30), bg=(250, 248, 240, 255), margin=0.2).convert("RGB")
    data = M.camera(im.crop((im.width // 2 - 80, 0, im.width, im.height)), rng)
    assert _file(client, data, "cap.jpg", "capture").json()["status"] != "VERIFIED"


# ---- INPUT VALIDATION / SECURITY ------------------------------------------------------------------------------------
def test_base64_text_is_not_accepted(client):
    """Owner 2026-10-17: a normal file only -- an image sent as base64 text is refused (no file part)."""
    text = "data:image/png;base64," + base64.b64encode(_drawn()).decode()
    r = client.post(ROUTE, data={"image_base64": text, "source": "draw"})
    _clean(r, 400, 422)
    r = client.post(ROUTE, json={"image_base64": text, "source": "draw"})
    _clean(r, 400, 415, 422)


def test_no_file_is_422(client):
    _clean(client.post(ROUTE, data={"source": "draw"}), 400, 422)


def test_an_unknown_source_is_422(client):
    r = _file(client, _drawn(), "sig.png", "fax")
    _clean(r, 422)
    assert r.json()["detail"]["code"] == "INVALID_SOURCE"


@pytest.mark.parametrize("name,data,codes", [
    ("sig.png", b"", (400,)),
    ("sig.exe", b"MZ\x90\x00", (415,)),
    ("sig.txt", b"just text", (415,)),
    ("sig.jpg", EICAR, (422,)),
    ("sig.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, (422,)),
    ("sig.jpg", b"not an image", (422,)),
    ("sig.pdf", b"%PDF-1.4 garbage", (422,)),
])
def test_an_invalid_upload_is_refused_cleanly(client, name, data, codes):
    r = _file(client, data, name)
    _clean(r, *codes)
    assert "VERIFIED" not in r.text


def test_an_oversized_file_is_413(client):
    _clean(_file(client, b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 2**20 + 10), "big.png"), 413)


def test_no_token_is_401_and_a_weak_token_is_403(make_token, monkeypatch):
    import main

    monkeypatch.setenv("AUTH_ENABLED", "true")
    bare = TestClient(main.app)
    assert _file(bare, _drawn(), "sig.png", "draw").status_code == 401
    weak = {"Authorization": f"Bearer {make_token(scopes=['read_applicant'])}"}
    assert _file(bare, _drawn(), "sig.png", "draw", headers=weak).status_code == 403


def test_a_valid_request_works_after_bad_ones(client):
    _file(client, EICAR, "sig.jpg")
    _file(client, b"", "sig.png")
    assert _file(client, _drawn(seed=12), "sig.png", "draw").json()["status"] == "VERIFIED"
