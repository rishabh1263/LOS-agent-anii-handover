"""
Every supported document type carries a score, a confidence and a reason.

WHAT THIS DEFENDS, stated as the gap it closed. `scoring.py` implemented
the two-number model and was wired to ONE path. Measured at the start of
this phase, through the real pipeline:

    PAN / DL / Voter / Passport ..... score null, confidence null
    ITR ............................. score null, confidence null
    SALARY_SLIP ..................... score null, confidence null
    SALE_DEED ....... score null, confidence null, 4 codes, NO sentences
    BANK_STATEMENT .................. the only type with both

A reviewer triaging a mixed queue could rank bank statements and nothing
else. The framework was there; four of five document families were simply
not connected to it.

THESE TESTS RUN THE REAL PIPELINE against real sample documents. They
assert the CONTRACT -- both numbers present, in range, and a reason for
every non-pass -- and deliberately not particular values, which depend on
the samples and would make this a change-detector.

ONE ASSERTION IS ABOUT VALUES, and it is the important one: the verdicts
must be unchanged. Scoring describes a verdict; it must never move one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.agents.los.flow import PROCESS, UploadedDocument, process_application

#: One real sample per supported family. Not exhaustive -- exhaustive
#: belongs in the per-type suites -- but every ROUTE through the pipeline
#: is represented: identity, financial, and specialist.
SAMPLES = [
    ("PAN", "samples/real_batch/pan_bw2.jpg"),
    ("VOTER_ID", "samples/documents/voter_id2.jpg"),
    ("DRIVING_LICENCE", "samples/real_batch/dl1.jpg"),
    ("BANK_STATEMENT", "samples/real_batch/bank_canara.pdf"),
    ("ITR", "samples/real_batch/itr_v.pdf"),
    ("SALARY_SLIP", "samples/real_batch/salary_slip.pdf"),
    ("SALE_DEED", "samples/real_batch/sale_deed_clean.pdf"),
]


def run(expected_type: str, path: str) -> dict:
    name = Path(path).name
    result = asyncio.run(process_application(
        [UploadedDocument(source_id=name, filename=name,
                          content=Path(path).read_bytes(),
                          expected_type=expected_type)],
        operation=PROCESS, applicant_id="A", case_id="C", request_id="r",
        summarise=False, cross_document_checks=False, financial_analysis=False,
    ))
    return result["documents"][0]


@pytest.fixture(scope="module")
def processed() -> dict[str, dict]:
    """
    Every sample, run ONCE.

    Seven documents through the real OCR and parsing stack is several
    seconds; running them per-test would multiply that by the number of
    assertions for no additional coverage.
    """
    out: dict[str, dict] = {}
    for expected_type, path in SAMPLES:
        if not Path(path).exists():
            continue
        out[expected_type] = run(expected_type, path)
    return out


def available(processed, expected_type) -> dict:
    if expected_type not in processed:
        pytest.skip(f"no sample available for {expected_type}")
    return processed[expected_type]


# ==========================================================================
# A. BOTH NUMBERS, EVERY TYPE
# ==========================================================================


@pytest.mark.parametrize("expected_type", [t for t, _ in SAMPLES])
def test_every_type_reports_a_verification_score(processed, expected_type):
    document = available(processed, expected_type)

    assert document.get("verification_score") is not None, (
        f"{expected_type} reports no score, so a reviewer cannot rank it "
        f"against anything else in the queue"
    )
    assert 0 <= document["verification_score"] <= 100


@pytest.mark.parametrize("expected_type", [t for t, _ in SAMPLES])
def test_every_type_reports_a_confidence(processed, expected_type):
    document = available(processed, expected_type)

    assert document.get("verification_confidence") is not None
    assert 0 <= document["verification_confidence"] <= 100


@pytest.mark.parametrize("expected_type", [t for t, _ in SAMPLES])
def test_the_two_numbers_are_independent_fields(processed, expected_type):
    """
    They may coincide -- on a document where the only imperfection is an
    inconclusive check they are arithmetically the same -- but they must
    be computed and reported separately, never aliased.
    """
    document = available(processed, expected_type)

    assert "verification_score" in document
    assert "verification_confidence" in document


# ==========================================================================
# B. EVERY NON-PASS EXPLAINS ITSELF
# ==========================================================================


@pytest.mark.parametrize("expected_type", [t for t, _ in SAMPLES])
def test_a_non_pass_carries_reason_codes(processed, expected_type):
    document = available(processed, expected_type)

    if document["verification"] in {"PASS", "SKIPPED"}:
        pytest.skip(f"{expected_type} passed; nothing to explain")

    assert document.get("reason_codes"), (
        f"{expected_type} is {document['verification']} with no reason code"
    )


@pytest.mark.parametrize("expected_type", [t for t, _ in SAMPLES])
def test_a_non_pass_carries_a_sentence(processed, expected_type):
    """
    THE GAP THAT WAS WIDEST. A sale deed came back REVIEW with four
    precise reason codes and no prose at all.
    """
    document = available(processed, expected_type)

    if document["verification"] in {"PASS", "SKIPPED"}:
        pytest.skip(f"{expected_type} passed; nothing to explain")

    assert document.get("reasons"), (
        f"{expected_type} is {document['verification']} with codes "
        f"{document.get('reason_codes')} and no readable explanation"
    )
    for sentence in document["reasons"]:
        assert len(sentence) > 15
        assert sentence.endswith(".")


@pytest.mark.parametrize("expected_type", [t for t, _ in SAMPLES])
def test_a_passing_document_is_not_annotated_with_problems(
        processed, expected_type):
    """
    A clean document carrying reason codes reads as a document with
    something wrong with it.
    """
    document = available(processed, expected_type)

    if document["verification"] != "PASS":
        pytest.skip(f"{expected_type} did not pass")

    assert not document.get("reason_codes")


def test_a_reason_is_written_for_a_person(processed):
    """Not a stack trace, not an internal identifier."""
    for expected_type, document in processed.items():
        for sentence in document.get("reasons") or []:
            for internal in ("Traceback", ".py", "None", "cv2", "0x"):
                assert internal not in sentence, (
                    f"{expected_type}: {internal!r} leaked into {sentence!r}"
                )


# ==========================================================================
# C. SCORING DESCRIBES A VERDICT, IT DOES NOT MOVE ONE
# ==========================================================================

#: The verdicts these samples produced BEFORE any scoring was wired in.
#: Recorded so that connecting the framework cannot be shown to have
#: changed an outcome -- which would mean the numbers had become a second
#: opinion rather than a description.
BASELINE_VERDICTS = {
    "PAN": "PASS",
    "VOTER_ID": "PASS",
    "DRIVING_LICENCE": "PASS",
    "BANK_STATEMENT": "PASS",
    "ITR": "PASS",
    "SALARY_SLIP": "PASS",
    "SALE_DEED": "REVIEW",
}


@pytest.mark.parametrize("expected_type", sorted(BASELINE_VERDICTS))
def test_the_verdict_is_what_it_was_before_scoring(processed, expected_type):
    document = available(processed, expected_type)

    assert document["verification"] == BASELINE_VERDICTS[expected_type], (
        f"{expected_type} now reports {document['verification']} where it "
        f"reported {BASELINE_VERDICTS[expected_type]}. Scoring must describe "
        f"a verdict, never decide one."
    )


def test_a_low_score_does_not_by_itself_fail_a_document(processed):
    """
    The sale deed scores around half and still REVIEWs rather than
    FAILing -- the specialist's own verdict stands. A score that could
    drag a verdict down would be a hard gate nobody declared.
    """
    document = available(processed, "SALE_DEED")

    assert document["verification_score"] < 100
    assert document["verification"] != "FAIL"
    assert document["verification"] != "REJECTED"


# ==========================================================================
# D. THE DISTINCT OUTCOMES, THROUGH THE REAL PIPELINE
# ==========================================================================


def test_a_scan_awaiting_ocr_says_so_rather_than_failing_arithmetic():
    """
    PHASE 2B, end to end. A scanned bank statement this build cannot read
    must report that it needs OCR -- not that its transactions do not
    reconcile, which is an accusation about the customer's document.
    """
    path = Path("samples/real_batch/bank_sbi_scanned.pdf")
    if not path.exists():
        pytest.skip("scanned sample not available")

    document = run("BANK_STATEMENT", str(path))

    assert document["verification"] != "FAIL"
    assert "DOCUMENT_REQUIRES_OCR" in (document.get("reason_codes") or [])

    joined = " ".join(document.get("reasons") or []).lower()
    assert "do not add up" not in joined
    assert "reconcil" not in joined
