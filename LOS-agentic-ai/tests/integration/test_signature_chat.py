"""
A REAL SIGNATURE ON WHITE PAPER (user bug report, 2026-10-05).

A clean signature went to REVIEW as "part of the image is cut off" and "too
faint or too small": white paper counted as tone clipping. And the chat read
out seven reason codes, three meaning the same thing, then repeated them as
"However, your application is under review because image clipped, ...".

Still REVIEW without a specimen -- PASS requires a reference comparison by
policy (documents.yaml signature.reference_comparison_enabled) -- but for that
reason alone, said once, in words.
"""

from __future__ import annotations

import math

import pytest
from PIL import Image, ImageDraw

from tests.integration.test_fos_stage_boundary import open_case, upload
from tests.integration.test_reupload_supersedes import _BACKEND, _store, ask, client  # noqa: F401


def _signature(path, background=(255, 255, 255)):
    im = Image.new("RGB", (600, 220), background)
    d = ImageDraw.Draw(im)
    pts = [(40 + i * 2.6, 110 + 45 * math.sin(i / 9.0) + 18 * math.sin(i / 2.7)) for i in range(200)]
    d.line(pts, fill=(20, 30, 120), width=4)
    d.line([(80, 150), (520, 140)], fill=(20, 30, 120), width=3)
    im.save(path, quality=92)
    return path


def test_a_clean_signature_is_not_flagged_as_poor_quality(tmp_path):
    from app.agents.signature import analysis

    clean = Image.open(_signature(tmp_path / "s.jpg"))
    stats = analysis.measure(clean)
    assert stats["clipped_ratio"] > analysis.CLIPPING_CEILING          # mostly white paper...
    assert stats["black_clipped_ratio"] <= analysis.CLIPPING_CEILING    # ...which is not clipping
    dark = Image.new("L", (600, 220), 0)
    assert analysis.measure(dark)["black_clipped_ratio"] > analysis.CLIPPING_CEILING


def test_the_chat_says_why_once_in_words(client, tmp_path):
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id, [("sign.jpg", _signature(tmp_path / "sign.jpg"))],
                  ["SIGNATURE"]).json()
    card = body["verification"]["documents_processed"][0]
    assert "IMAGE_CLIPPED" not in card["reason_codes"]
    assert "SIGNATURE_LOW_QUALITY" not in card["reason_codes"]
    assert "SIGNATURE_REFERENCE_MISSING" in card["reason_codes"]      # the code stays for a frontend
    assert not _BACKEND.search(body["answer"]), body["answer"]
    assert "KYC was not run" not in body["answer"]
    assert body["answer"].count("reference signature") == 1

    answer = ask(client, applicant_id, case_id, "is my signature verified?")
    assert answer.count("reference signature") == 1, answer
    assert "REVIEW" not in answer
