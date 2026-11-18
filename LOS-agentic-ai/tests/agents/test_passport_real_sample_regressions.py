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
# (sample, the verdict its printed / MRZ dates establish)
ROTATED = [("passport_samples0_12.jpg", "REVIEW"),   # 90 degrees, valid until 2027; MRZ unreadable -> expiry unconfirmed
           # upside down. Until 2026-10-15 nothing on it was read, so REVIEW. It is now read, and the same three dates
           # come back in all four orientations: born 20/10/1991, issued 31/08/2010, EXPIRES 30/08/2020 (issue + 10
           # years - 1 day). The specimen genuinely expired: FAIL with DOCUMENT_EXPIRED is the correct verdict.
           ("passport_samples1_10.jpg", "FAIL")]


@pytest.mark.skipif(not (SAMPLES / ROTATED[0][0]).exists(), reason="real passport samples not present")
@pytest.mark.parametrize("name,expected", ROTATED)
def test_a_rotated_passport_photo_is_a_passport_and_never_a_type_mismatch(name, expected):
    from app.agents.document_agent import workflow

    out = asyncio.run(workflow.process_document(
        file_bytes=(SAMPLES / name).read_bytes(), filename=name, operation="EXTRACT",
        requested_class="PASSPORT", request_id="t"))
    verification = out.get("verification") or {}
    reasons = set(verification.get("reason_codes") or [])
    assert (out.get("document") or {}).get("type") == "PASSPORT"
    assert not {"DOC_CLASS_UNRECOGNISED", "DOCUMENT_TYPE_MISMATCH"} & reasons     # never the wrong document
    assert verification.get("status") == expected, (verification.get("status"), reasons)
    if expected == "FAIL":
        assert "DOCUMENT_EXPIRED" in reasons                                       # failed for its date, nothing else
    else:
        assert verification.get("status") != "PASS"                              # expiry unconfirmed: never a PASS
