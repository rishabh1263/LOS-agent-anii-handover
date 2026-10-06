"""
USER REPORTS (2026-10-06), through the real HTTP API:

  "real sign reject hora hai"  -- a signature uploaded WITHOUT a declared type was
                                 classified UNKNOWN (no text to read) and REJECTED.
                                 It now reaches the signature specialist: REVIEW with
                                 one clear reason when no reference is on file.
  "pan se kya details mila voh ans nhi dera"  -- natural ways to ask for a document's
                                 extracted details were not understood.
"""

from __future__ import annotations

import math
import random

import pytest
from PIL import Image, ImageDraw, ImageFilter

from app.agents.applicant.copilot.semantics.intents import classify
from tests.integration.test_fos_stage_boundary import open_case, upload
from tests.integration.test_reupload_supersedes import RISHABH_PAN, _store, ask, client  # noqa: F401


def photo_signature(path, caption=True):
    """A phone photo of a real signature: off-white paper, noise, slight blur, optional printed caption."""
    rnd = random.Random(7)
    im = Image.new("RGB", (1200, 700), (236, 233, 226))
    px = im.load()
    for _ in range(60000):
        x, y = rnd.randrange(1200), rnd.randrange(700)
        g = rnd.randrange(215, 245)
        px[x, y] = (g, g - 2, g - 6)
    d = ImageDraw.Draw(im)
    pts = [(200 + i * 4.2, 330 + 70 * math.sin(i / 11.0) + 25 * math.sin(i / 3.1)) for i in range(190)]
    d.line(pts, fill=(25, 35, 110), width=6)
    d.line([(260, 420), (980, 405)], fill=(25, 35, 110), width=4)
    if caption:
        d.line([(150, 520), (1050, 520)], fill=(60, 60, 60), width=2)
        d.text((480, 535), "Signature of Applicant", fill=(40, 40, 40))
    im.filter(ImageFilter.GaussianBlur(0.8)).save(path, quality=88)
    return path


@pytest.mark.parametrize("caption", [False, True])
def test_an_undeclared_signature_photo_is_reviewed_never_rejected(client, tmp_path, caption):
    a, c = open_case(client)
    path = photo_signature(tmp_path / "sign.jpg", caption)
    r = client.post("/api/v1/fos/copilot", data={"applicant_id": a, "case_id": c, "action": "UPLOAD_DOCUMENT"},
                    files=[("files", ("sign.jpg", path.read_bytes(), "image/jpeg"))])
    assert r.status_code == 200, r.text
    body = r.json()
    card = body["verification"]["documents_processed"][0]
    assert "SIGNATURE_REFERENCE_MISSING" in card["reason_codes"] and "DOC_CLASS_UNRECOGNISED" not in card["reason_codes"]
    assert body["documents"][0]["status"] == "REVIEW"
    assert "reference signature" in body["answer"] and "did not pass verification" not in body["answer"]


def test_a_blank_image_is_not_taken_for_a_signature(client, tmp_path):
    a, c = open_case(client)
    blank = tmp_path / "blank.jpg"
    Image.new("RGB", (900, 600), (240, 240, 240)).save(blank)
    body = client.post("/api/v1/fos/copilot", data={"applicant_id": a, "case_id": c, "action": "UPLOAD_DOCUMENT"},
                       files=[("files", ("blank.jpg", blank.read_bytes(), "image/jpeg"))]).json()
    card = body["verification"]["documents_processed"][0]
    assert "SIGNATURE_REFERENCE_MISSING" not in card["reason_codes"]          # not routed to signatures


@pytest.mark.parametrize("question", ["PAN se kya details mila?", "PAN mein kya details hai?", "PAN me kya likha hai?",
                                      "PAN ki jaankari", "what did you read from my PAN?", "PAN extract kya hua?"])
def test_details_questions_are_understood(question):
    c = classify(question)
    assert c.intent.value == "DOCUMENT_DETAILS" and c.document_type == "PAN", (question, c)


@pytest.mark.parametrize("question", ["is my PAN verified?", "PAN ka kya hua?", "why was my PAN rejected?"])
def test_status_questions_about_pan_stay_status(question):
    assert classify(question).intent.value == "DOCUMENT_VERIFICATION"


def test_the_details_answer_shows_the_extracted_fields(client):
    a, c = open_case(client)
    upload(client, a, c, [("pan.jpg", RISHABH_PAN)], ["PAN"])
    answer = ask(client, a, c, "PAN se kya details mila?")
    assert "XXXXXX" in answer and "RISHABH" in answer.upper(), answer      # masked number, the name as read
