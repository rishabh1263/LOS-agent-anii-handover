"""
PASSPORT REGRESSION FROM THE REAL-SAMPLE SWEEP (2026-10-03).

A passport photographed sideways or upside down was IDENTIFIED by the recogniser
and then re-classified UNKNOWN by the workflow's final classification, which did
not see the raw text carrying the MRZ line -- and FAILed as the wrong document
(15 of 30 real passport photos, including a valid one). It must never FAIL as a
type mismatch; with the MRZ unreadable in that orientation it goes to REVIEW
(expiry not established), never to PASS.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

SAMPLES = Path("samples/passports")
ROTATED = ["passport_samples0_12.jpg",   # 90 degrees, valid until 2027
           "passport_samples1_10.jpg"]   # upside down


@pytest.mark.skipif(not (SAMPLES / ROTATED[0]).exists(), reason="real passport samples not present")
@pytest.mark.parametrize("name", ROTATED)
def test_a_rotated_passport_photo_is_a_passport_and_never_a_type_mismatch(name):
    from app.agents.document_agent import workflow

    out = asyncio.run(workflow.process_document(
        file_bytes=(SAMPLES / name).read_bytes(), filename=name, operation="EXTRACT",
        requested_class="PASSPORT", request_id="t"))
    verification = out.get("verification") or {}
    assert (out.get("document") or {}).get("type") == "PASSPORT"
    assert verification.get("status") != "FAIL"
    assert not {"DOC_CLASS_UNRECOGNISED", "DOCUMENT_TYPE_MISMATCH"} & set(verification.get("reason_codes") or [])
    # the MRZ is not readable in this orientation: expiry unconfirmed -> REVIEW, never PASS
    assert verification.get("status") == "REVIEW"
