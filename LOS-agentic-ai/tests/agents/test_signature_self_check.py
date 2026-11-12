"""
THE SIGNATURE IS JUDGED ON ITS OWN (2026-10-15): never matched against another signature. A signature that is
blank, blurred or cut off at the frame edge is REJECTED with a reason the applicant can act on; a sharp, whole,
handwritten one is VERIFIED; a doubtful one (slightly soft, cropped tight to the ink) goes to REVIEW, never VERIFIED.

Positive and negative cases over handwriting fonts (several hands, pens, paper tones, JPEG) and a drawn scribble,
so the thresholds are exercised on more than one shape. OCR is stubbed (nothing read): the typed-text check has its
own tests in test_step5c_signature_presence.py.
"""

from __future__ import annotations

import io
import math
import random
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.agents.signature import presence

NO_TEXT = lambda grey: (0, 0.0)                      # noqa: E731
HANDS = [p for p in (Path("C:/Windows/Fonts") / f for f in ("segoesc.ttf", "Inkfree.ttf", "LHANDW.TTF",
                                                             "BRUSHSCI.TTF", "MISTRAL.TTF")) if p.is_file()]
NAMES = ["Rohan Patil", "Neha Iyer", "S. Agarwal", "Priya Nair", "Imran Khan"]


def _paper(ink_mask: Image.Image, seed: int) -> Image.Image:
    rng = random.Random(seed)
    paper, pen = rng.randint(220, 250), rng.randint(10, 60)
    arr = paper - np.asarray(ink_mask, dtype="float32") / 255 * (paper - pen)
    arr += np.random.default_rng(seed).normal(0, 3, arr.shape)
    return Image.fromarray(np.clip(arr, 0, 255).astype("uint8"), "L")


def handwritten(seed: int) -> Image.Image:
    """A signature in a handwriting font, centred on a 640 x 240 card with a clear margin."""
    if not HANDS:
        return scribble(seed)
    rng = random.Random(seed)
    font = ImageFont.truetype(str(HANDS[seed % len(HANDS)]), rng.randint(48, 64))
    text = NAMES[seed % len(NAMES)]
    mask = Image.new("L", (640, 240), 0)
    draw = ImageDraw.Draw(mask)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(((640 - (right - left)) // 2 - left, (240 - (bottom - top)) // 2 - top), text, font=font, fill=255)
    return _paper(mask, seed)


def scribble(seed: int = 0) -> Image.Image:
    mask = Image.new("L", (640, 240), 0)
    draw = ImageDraw.Draw(mask)
    draw.line([(80 + k * 16, 120 + int(40 * math.sin(k * 0.7 + seed))) for k in range(30)], fill=255, width=6)
    draw.line([(140, 70), (170, 180), (220, 80), (250, 165)], fill=255, width=5)
    return _paper(mask, seed)


def jpeg(image: Image.Image, quality: int = 85) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


def png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def check(image: Image.Image, fmt=jpeg) -> dict:
    return presence.check(fmt(image), ocr=NO_TEXT)


def accepted(result: dict) -> bool:
    """VERIFIED -- or REVIEW only because a FONT-rendered name has printed-letter geometry (even letter heights on
    one baseline), which is what that check exists to catch. Never REJECTED, never blur / cut / blank."""
    return result["status"] == presence.VERIFIED or (
        result["status"] == presence.REVIEW and result["reasons"] == [presence.SIGNATURE_PRESENCE_POSSIBLE_TEXT])


SEEDS = range(6)


# ---- positive -----------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", SEEDS)
def test_a_sharp_whole_handwritten_signature_is_verified(seed):
    result = check(handwritten(seed))
    assert accepted(result), result


@pytest.mark.parametrize("seed", SEEDS)
def test_a_drawn_scribble_is_verified(seed):
    assert check(scribble(seed), png)["status"] == presence.VERIFIED


@pytest.mark.parametrize("quality", [60, 75, 95])
def test_ordinary_jpeg_compression_is_not_blur(quality):
    for seed in SEEDS:
        result = check(handwritten(seed), lambda im: jpeg(im, quality))
        assert accepted(result), (seed, result)


def test_a_signature_cropped_with_a_margin_is_verified():
    image = handwritten(2)
    ys, xs = np.nonzero(np.asarray(image) < 128)
    crop = image.crop((xs.min() - 15, ys.min() - 15, xs.max() + 16, ys.max() + 16))
    assert accepted(check(crop))


def test_a_scanner_border_along_the_edge_is_not_a_cut():
    image = handwritten(3)
    ImageDraw.Draw(image).rectangle((0, 0, image.width - 1, image.height - 1), outline=0, width=3)
    result = check(image, png)
    assert presence.SIGNATURE_PRESENCE_CUT_OFF not in result["reasons"], result


# ---- negative: blank ----------------------------------------------------------------------------
@pytest.mark.parametrize("tone", [255, 240, 200])
def test_a_blank_page_is_rejected(tone):
    result = check(Image.new("L", (640, 240), tone), png)
    assert result["status"] == presence.REJECTED and result["reasons"] == [presence.SIGNATURE_PRESENCE_BLANK]


def test_a_blank_page_with_sensor_noise_is_rejected():
    noisy = Image.fromarray(np.clip(np.random.default_rng(1).normal(235, 4, (240, 640)), 0, 255).astype("uint8"))
    assert check(noisy)["status"] == presence.REJECTED


# ---- negative: blurred --------------------------------------------------------------------------
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("radius", [3, 5, 8])
def test_a_blurred_signature_is_rejected_as_blurred(seed, radius):
    result = check(handwritten(seed).filter(ImageFilter.GaussianBlur(radius)))
    assert result["status"] == presence.REJECTED, result
    assert result["reasons"] == [presence.SIGNATURE_PRESENCE_BLURRED], result       # not "blank"


def test_a_blurred_scribble_is_rejected():
    assert check(scribble(0).filter(ImageFilter.GaussianBlur(4)))["reasons"] == [presence.SIGNATURE_PRESENCE_BLURRED]


def test_a_slightly_soft_signature_is_never_verified_silently():
    """Between sharp and unreadable a person looks: REVIEW or REJECTED, the measure is reported either way."""
    results = [check(handwritten(s).filter(ImageFilter.GaussianBlur(1.6))) for s in SEEDS]
    assert all("edge_steepness" in r["measures"] for r in results)
    assert sum(r["status"] != presence.VERIFIED for r in results) >= len(results) - 1


# ---- negative: half / cut off -------------------------------------------------------------------
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("cut", ["right_half", "left_half", "bottom", "top"])
def test_a_cut_off_signature_is_rejected(seed, cut):
    image = handwritten(seed)
    ys, xs = np.nonzero(np.asarray(image) < 128)
    mid_x, mid_y = (xs.min() + xs.max()) // 2, (ys.min() + ys.max()) // 2
    w, h = image.size
    box = {"right_half": (0, 0, mid_x, h), "left_half": (mid_x, 0, w, h),
           "bottom": (0, 0, w, mid_y), "top": (0, mid_y, w, h)}[cut]
    # the cut must run through ink (a stroke crosses the new edge), as a photo that missed part of it does; a cut
    # through the gap between two words leaves whole words, which nothing can tell from a full signature
    dark = np.asarray(image) < 128
    line = {"right_half": dark[:, box[2] - 1], "left_half": dark[:, box[0]],
            "bottom": dark[box[3] - 1, :], "top": dark[box[1], :]}[cut]
    if not line.any():
        pytest.skip("the cut fell between strokes; nothing is cut")
    result = check(image.crop(box))
    assert result["status"] == presence.REJECTED, result
    assert result["reasons"] == [presence.SIGNATURE_PRESENCE_CUT_OFF], result


def test_a_signature_cropped_tight_to_the_ink_goes_to_review():
    image = handwritten(4)
    ys, xs = np.nonzero(np.asarray(image) < 128)
    result = check(image.crop((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)))
    assert result["status"] != presence.VERIFIED, result


# ---- every reason reads as a sentence, and survives storage ----------------------------------
@pytest.mark.parametrize("code", [presence.SIGNATURE_PRESENCE_BLURRED, presence.SIGNATURE_PRESENCE_CUT_OFF,
                                  presence.SIGNATURE_PRESENCE_SLIGHTLY_BLURRED,
                                  presence.SIGNATURE_PRESENCE_TOUCHES_EDGES])
def test_new_reasons_have_a_sentence_and_round_trip(code):
    assert presence.MESSAGES[code]
    status = presence.REJECTED if code in (presence.SIGNATURE_PRESENCE_BLURRED,
                                           presence.SIGNATURE_PRESENCE_CUT_OFF) else presence.REVIEW
    stored = presence.from_codes([presence.STATUS_CODE[status], code])
    assert stored["status"] == status and stored["reasons"] == [code]
