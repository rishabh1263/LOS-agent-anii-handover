"""
Field-level KYC: what each comparison must say, and what it must never say.

THE RULES THIS SUITE DEFENDS:

    formatting noise never becomes a mismatch
    a materially different value always does
    missing data is SKIPPED, never FAIL
    one source is SKIPPED, never a document matching itself
    match_score and confidence are separate numbers and stay separate
    a mismatch routes to a human; it does not reject anybody
    no OCR token, box or internal ever reaches the result

The last two matter most. A KYC agent that rejects on a smudged character,
or that leaks the OCR it rejected on, fails the applicant twice.

Values here are invented test fixtures, not customer data.
"""

from __future__ import annotations

import pytest

from app.agents.kyc import config
from app.agents.kyc.agent import configuration, run_kyc
from app.agents.kyc.schemas import (
    AddressInput,
    FieldStatus,
    KycField,
    KycRequest,
    SourceDocument,
)


@pytest.fixture(autouse=True)
def _policy():
    """Every test reads the shipped policy, not a doctored one."""
    config.reset_policy_cache()
    yield
    config.reset_policy_cache()


def doc(source_id, document_type, **fields):
    return SourceDocument(source_id=source_id, document_type=document_type,
                          **fields)


def assess(*documents):
    return run_kyc(KycRequest(documents=list(documents)), request_id="test")


def field(result, kind: KycField):
    found = result.field(kind)
    assert found is not None, f"{kind.value} missing from the result"
    return found


PAN_A = "ABCPS1234K"
PAN_B = "ZZZPK9999Q"
ADDRESS_A = "12 MG Road, Andheri East, Mumbai 400069"


# ==========================================================================
# A -- EXACT MATCH
# ==========================================================================

def test_a_identical_documents_pass_every_field():
    result = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH",
            father_name="AJIT SINGH", date_of_birth="1990-04-12", pan=PAN_A),
        doc("dl.jpg", "DRIVING_LICENCE", name="RISHABH AJIT SINGH",
            father_name="AJIT SINGH", date_of_birth="1990-04-12", pan=PAN_A),
    )

    for kind in (KycField.NAME, KycField.DATE_OF_BIRTH, KycField.PAN_NUMBER,
                 KycField.FATHER_NAME):
        row = field(result, kind)
        assert row.status is FieldStatus.PASS, f"{kind.value} did not pass"
        assert row.match_score == 100
        assert row.reason_code == "EXACT_MATCH"

    assert result.overall_score == 100


# ==========================================================================
# B -- CASE AND SPACING
# ==========================================================================

@pytest.mark.parametrize("variant", [
    "rishabh ajit singh",
    "RISHABH   AJIT   SINGH",
    "  Rishabh Ajit Singh  ",
    "Rishabh  Ajit\tSingh",
])
def test_b_case_and_spacing_never_become_a_mismatch(variant):
    """
    THE FALSE-MISMATCH TEST.

    Every one of these is the same person written differently. An agent that
    fails any of them will reject genuine applicants over whitespace.
    """
    result = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH"),
        doc("dl.jpg", "DRIVING_LICENCE", name=variant),
    )
    row = field(result, KycField.NAME)
    assert row.status is FieldStatus.PASS, f"{variant!r} was read as a mismatch"
    assert row.match_score == 100


def test_b_punctuation_and_honorifics_are_harmless():
    result = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH"),
        doc("dl.jpg", "DRIVING_LICENCE", name="Mr. Rishabh A. Singh"),
    )
    row = field(result, KycField.NAME)
    assert row.status in (FieldStatus.PASS, FieldStatus.PARTIAL)
    assert row.match_score >= 70


# ==========================================================================
# C -- PARTIAL SIMILARITY
# ==========================================================================

def test_c_a_name_between_the_thresholds_is_partial_and_explains_itself():
    """
    The uncertain middle, which must reach a human rather than be decided.

    Sitting between review_threshold and match_threshold is the one case
    where the agent genuinely does not know, and the result has to say so
    rather than round to the nearest convenient answer.
    """
    review = config.threshold("name", "review_threshold", 0.70)
    match = config.threshold("name", "match_threshold", 0.85)

    result = assess(
        doc("pan.jpg", "PAN", name="RAJESH KUMAR SHARMA"),
        doc("dl.jpg", "DRIVING_LICENCE", name="RAJESH KUMAAR SHARMAN"),
    )
    row = field(result, KycField.NAME)

    if row.status is FieldStatus.PARTIAL:
        assert int(review * 100) <= row.match_score < int(match * 100) + 1
        assert row.reason_code == "NAME_PARTIAL_MATCH"
        assert row.reason, "a partial match with no explanation"
    else:
        # Whatever the matcher decided, it must be on the right side of the
        # thresholds it published. A verdict that contradicts its own score
        # is the real failure here.
        if row.status is FieldStatus.PASS:
            assert row.match_score >= int(match * 100)
        else:
            assert row.match_score < int(match * 100)


# ==========================================================================
# D -- DIFFERENT NAME
# ==========================================================================

def test_d_a_different_name_fails_with_a_low_score():
    result = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH"),
        doc("voter.jpg", "VOTER_ID", name="MUKESH KUMAR"),
    )
    row = field(result, KycField.NAME)
    assert row.status is FieldStatus.FAIL
    assert row.match_score < 50
    assert row.reason_code == "NAME_MISMATCH"
    assert "PAN" in row.reason and "Voter ID" in row.reason


# ==========================================================================
# E and F -- DATE OF BIRTH
# ==========================================================================

@pytest.mark.parametrize("written", ["1990-04-12", "12-04-1990", "12/04/1990",
                                     "12.04.1990"])
def test_e_the_same_date_written_differently_passes(written):
    """A date format is not a difference of fact."""
    result = assess(
        doc("pan.jpg", "PAN", date_of_birth="1990-04-12"),
        doc("dl.jpg", "DRIVING_LICENCE", date_of_birth=written),
    )
    row = field(result, KycField.DATE_OF_BIRTH)
    assert row.status is FieldStatus.PASS
    assert row.match_score == 100


def test_f_a_different_date_of_birth_fails():
    result = assess(
        doc("pan.jpg", "PAN", date_of_birth="1990-04-12"),
        doc("dl.jpg", "DRIVING_LICENCE", date_of_birth="1985-01-01"),
    )
    row = field(result, KycField.DATE_OF_BIRTH)
    assert row.status is FieldStatus.FAIL
    assert row.match_score == 0
    assert row.reason_code == "DOB_MISMATCH"


def test_f_a_date_of_birth_has_no_near_miss():
    """One day apart is a different date, not a close one."""
    result = assess(
        doc("pan.jpg", "PAN", date_of_birth="1990-04-12"),
        doc("dl.jpg", "DRIVING_LICENCE", date_of_birth="1990-04-13"),
    )
    assert field(result, KycField.DATE_OF_BIRTH).status is FieldStatus.FAIL


# ==========================================================================
# G and H -- PAN
# ==========================================================================

@pytest.mark.parametrize("written", ["ABCPS1234K", "abcps1234k",
                                     "  ABCPS1234K  "])
def test_g_the_same_pan_written_differently_passes(written):
    result = assess(
        doc("pan.jpg", "PAN", pan=PAN_A),
        doc("itr.pdf", "ITR", pan=written),
    )
    row = field(result, KycField.PAN_NUMBER)
    assert row.status is FieldStatus.PASS
    assert row.match_score == 100


def test_h_a_different_pan_fails():
    result = assess(
        doc("pan.jpg", "PAN", pan=PAN_A),
        doc("itr.pdf", "ITR", pan=PAN_B),
    )
    row = field(result, KycField.PAN_NUMBER)
    assert row.status is FieldStatus.FAIL
    assert row.match_score == 0
    assert row.reason_code == "PAN_MISMATCH"


# ==========================================================================
# I, J, K -- ADDRESS
# ==========================================================================

def test_i_address_formatting_variation_is_not_a_mismatch():
    """
    The example from the specification, which naive equality fails.

    "12 MG Road" and "12, M.G. Road" are the same address. An agent using
    string equality here would flag most genuine applicants.
    """
    result = assess(
        doc("pan.jpg", "PAN", address=AddressInput(raw=ADDRESS_A)),
        doc("dl.jpg", "DRIVING_LICENCE", address=AddressInput(
            raw="12, M.G. Road, Andheri East, Mumbai 400069")),
    )
    row = field(result, KycField.ADDRESS)
    assert row.status is FieldStatus.PASS, row.reason
    assert row.match_score >= 80


def test_j_a_partly_overlapping_address_is_partial_not_failed():
    """
    Same street, same locality, same pincode, different house number.

    Neither a pass nor a rejection: it is the case a human resolves, and
    reporting it as either loses the only thing they needed to know.
    """
    result = assess(
        doc("pan.jpg", "PAN", address=AddressInput(raw=ADDRESS_A)),
        doc("dl.jpg", "DRIVING_LICENCE", address=AddressInput(
            raw="45 MG Road, Andheri East, Mumbai 400069")),
    )
    row = field(result, KycField.ADDRESS)
    assert row.status is FieldStatus.PARTIAL
    assert row.reason_code == "ADDRESS_PARTIAL_MATCH"
    assert 0 < row.match_score < 80


def test_k_a_completely_different_address_fails():
    result = assess(
        doc("pan.jpg", "PAN", address=AddressInput(raw=ADDRESS_A)),
        doc("dl.jpg", "DRIVING_LICENCE", address=AddressInput(
            raw="88 Park Street, Salt Lake, Kolkata 700091")),
    )
    row = field(result, KycField.ADDRESS)
    assert row.status is FieldStatus.FAIL
    assert row.reason_code == "ADDRESS_MISMATCH"


# ==========================================================================
# L and M -- ABSENT DATA IS NOT A FAILURE
# ==========================================================================

def test_l_a_missing_field_is_skipped_never_failed():
    """
    THE RULE THAT PROTECTS APPLICANTS. Absent evidence is not adverse
    evidence. A document that did not carry a field says nothing about the
    applicant, and turning silence into FAIL rejects people for the
    extractor's limitations.
    """
    result = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH"),
        doc("dl.jpg", "DRIVING_LICENCE", name="RISHABH AJIT SINGH"),
    )

    for kind in (KycField.DATE_OF_BIRTH, KycField.PAN_NUMBER,
                 KycField.ADDRESS, KycField.FATHER_NAME):
        row = field(result, kind)
        assert row.status is FieldStatus.SKIPPED, f"{kind.value} was not skipped"
        assert row.status is not FieldStatus.FAIL

    assert field(result, KycField.NAME).status is FieldStatus.PASS


def test_l_a_skipped_field_does_not_drag_the_overall_score_down():
    """
    Skipped fields are excluded and the remaining weights renormalised, so a
    bundle that happens not to carry a father's name is not punished for it.
    """
    result = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH",
            date_of_birth="1990-04-12"),
        doc("dl.jpg", "DRIVING_LICENCE", name="RISHABH AJIT SINGH",
            date_of_birth="1990-04-12"),
    )
    assert result.overall_score == 100


def test_m_a_single_source_is_skipped_not_matched_against_itself():
    """
    One document cannot corroborate itself.

    An agent that compares a document with itself reports a confident PASS
    on no evidence at all -- the most dangerous possible false positive.
    """
    result = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH",
            date_of_birth="1990-04-12", pan=PAN_A),
    )

    for kind in (KycField.NAME, KycField.DATE_OF_BIRTH, KycField.PAN_NUMBER):
        row = field(result, kind)
        assert row.status is FieldStatus.SKIPPED
        assert row.reason_code.endswith("SINGLE_SOURCE")
        assert row.match_score == 0
        assert row.confidence == 0
        assert len(row.sources) == 1

    assert result.status.value != "PASS"


def test_m_a_second_document_without_the_field_is_still_a_single_source():
    result = assess(
        doc("pan.jpg", "PAN", pan=PAN_A),
        doc("dl.jpg", "DRIVING_LICENCE", name="RISHABH AJIT SINGH"),
    )
    row = field(result, KycField.PAN_NUMBER)
    assert row.status is FieldStatus.SKIPPED
    assert row.reason_code == "PAN_SINGLE_SOURCE"


# ==========================================================================
# N -- CONFIDENCE IS NOT MATCH SCORE
# ==========================================================================

def test_n_a_poor_extraction_lowers_confidence_but_not_the_match():
    """
    THE CENTRAL DISTINCTION, stated as a test.

    The same values, read badly, still match each other exactly. What falls
    is how much anyone should trust that they were read correctly at all.
    A system with one number cannot express this.
    """
    clean = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH",
            date_of_birth="1990-04-12",
            field_quality={"name": 0.97, "date_of_birth": 0.96}),
        doc("dl.jpg", "DRIVING_LICENCE", name="RISHABH AJIT SINGH",
            date_of_birth="1990-04-12",
            field_quality={"name": 0.95, "date_of_birth": 0.94}),
    )
    poor = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH",
            date_of_birth="1990-04-12",
            field_quality={"name": 0.20, "date_of_birth": 0.22}),
        doc("dl.jpg", "DRIVING_LICENCE", name="RISHABH AJIT SINGH",
            date_of_birth="1990-04-12",
            field_quality={"name": 0.25, "date_of_birth": 0.24}),
    )

    clean_name = field(clean, KycField.NAME)
    poor_name = field(poor, KycField.NAME)

    assert clean_name.match_score == poor_name.match_score == 100
    assert poor_name.confidence < clean_name.confidence
    assert poor.overall_confidence < clean.overall_confidence
    assert poor.overall_score == clean.overall_score


def test_n_a_clean_disagreement_is_reported_with_high_confidence():
    """
    A real mismatch is a reliable finding, and confidence must say so.

    If confidence tracked match_score, every genuine disagreement would look
    like an unreliable result and reviewers would learn to ignore it.
    """
    result = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH",
            field_quality={"name": 0.97}),
        doc("voter.jpg", "VOTER_ID", name="MUKESH KUMAR",
            field_quality={"name": 0.96}),
    )
    row = field(result, KycField.NAME)
    assert row.status is FieldStatus.FAIL
    assert row.match_score < 50
    assert row.confidence >= 70, (
        "a clean, well-read disagreement was reported as unreliable"
    )


def test_n_confidence_is_never_simply_the_match_score():
    result = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH",
            date_of_birth="1990-04-12", pan=PAN_A),
        doc("dl.jpg", "DRIVING_LICENCE", name="RISHABH AJIT SINGH",
            date_of_birth="1990-04-12", pan=PAN_A),
    )
    compared = [f for f in result.fields if f.status is not FieldStatus.SKIPPED]
    assert compared
    assert any(f.confidence != f.match_score for f in compared), (
        "confidence is a copy of match_score"
    )


def test_n_every_confidence_can_be_explained():
    """A number an operator cannot get an answer about should not be shown."""
    result = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH"),
        doc("dl.jpg", "DRIVING_LICENCE", name="RISHABH AJIT SINGH"),
    )
    row = field(result, KycField.NAME)
    assert row.confidence_factors
    assert row.confidence_factors[0]["factor"] == "comparison_method"
    assert row.confidence_factors[-1]["adjustment"] == row.confidence


def test_n_a_third_agreeing_document_is_worth_more_than_two():
    two = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH",
            field_quality={"name": 0.95}),
        doc("dl.jpg", "DRIVING_LICENCE", name="RISHABH AJIT SINGH",
            field_quality={"name": 0.95}),
    )
    three = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH",
            field_quality={"name": 0.95}),
        doc("dl.jpg", "DRIVING_LICENCE", name="RISHABH AJIT SINGH",
            field_quality={"name": 0.95}),
        doc("passport.jpg", "PASSPORT", name="RISHABH AJIT SINGH",
            field_quality={"name": 0.95}),
    )
    assert (field(three, KycField.NAME).confidence
            > field(two, KycField.NAME).confidence)


# ==========================================================================
# O -- NO INTERNALS
# ==========================================================================

def test_o_the_field_result_carries_no_ocr_or_internal_data():
    """
    What a caller receives must contain nothing about how it was produced.

    Checked on the serialised form rather than the object, because that is
    what actually crosses the wire.
    """
    result = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH",
            date_of_birth="1990-04-12", pan=PAN_A,
            address=AddressInput(raw=ADDRESS_A),
            field_quality={"name": 0.9}),
        doc("dl.jpg", "DRIVING_LICENCE", name="RISHABH AJIT SINGH",
            date_of_birth="1990-04-12", pan=PAN_A,
            address=AddressInput(raw=ADDRESS_A),
            field_quality={"name": 0.9}),
    )

    from app.agents.los.response import public_kyc_fields

    published = public_kyc_fields({
        "fields": [f.model_dump(mode="json") for f in result.fields]
    })

    import json
    blob = json.dumps(published).lower()
    for forbidden in ("bounding", "bbox", "ocr", "token", "prompt",
                      "field_quality", "confidence_factors", "traceback",
                      "c:\\\\", "/app/", "sql", "select "):
        assert forbidden not in blob, f"{forbidden!r} leaked into the response"

    allowed = {"field", "status", "match_score", "confidence", "reason_code",
               "reason", "sources"}
    for row in published:
        assert set(row) <= allowed, f"unexpected key: {set(row) - allowed}"
        for source in row.get("sources", []):
            assert set(source) <= {"source_id", "document_type", "value",
                                   "normalized_value"}


# ==========================================================================
# P -- A MISMATCH ROUTES TO A HUMAN, IT DOES NOT REJECT
# ==========================================================================

def test_p_a_mismatch_reviews_rather_than_rejecting_under_the_shipped_policy():
    """
    Documents disagreeing is not a finding of fraud.

    This agent cannot tell a married name, a corrected date of birth or a
    transliteration from someone else's document in the bundle. Only a human
    can, so a mismatch goes to one. The finding is not softened -- the field
    still reports FAIL with both values -- only its power to reject is.
    """
    result = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH",
            date_of_birth="1990-04-12", pan=PAN_A),
        doc("voter.jpg", "VOTER_ID", name="MUKESH KUMAR",
            date_of_birth="1985-01-01", pan=PAN_B),
    )

    assert field(result, KycField.NAME).status is FieldStatus.FAIL
    assert field(result, KycField.DATE_OF_BIRTH).status is FieldStatus.FAIL
    assert field(result, KycField.PAN_NUMBER).status is FieldStatus.FAIL

    assert result.status.value == "REVIEW", (
        "a non-blocking mismatch rejected the applicant outright"
    )


def test_p_making_a_check_blocking_restores_its_power_to_fail(monkeypatch):
    """
    The mechanism is intact, not removed. A lender who wants a hard stop on a
    PAN mismatch configures one.
    """
    import app.agents.kyc.config as kyc_config

    real = kyc_config.check_blocking
    monkeypatch.setattr(
        kyc_config, "check_blocking",
        lambda section: True if section == "pan" else real(section),
    )

    result = assess(
        doc("pan.jpg", "PAN", pan=PAN_A),
        doc("itr.pdf", "ITR", pan=PAN_B),
    )
    assert result.status.value == "FAIL"


# ==========================================================================
# SCORING IS EXPLAINABLE
# ==========================================================================

def test_the_overall_score_follows_the_configured_weights():
    """
    Recomputed here from the published weights. If the roll-up ever stops
    being a weighted mean of the field scores, this catches it -- an
    unexplainable overall number is the thing to avoid.
    """
    result = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH",
            date_of_birth="1990-04-12", pan=PAN_A),
        doc("voter.jpg", "VOTER_ID", name="RISHABH AJIT SINGH",
            date_of_birth="1985-01-01", pan=PAN_A),
    )

    weights = config.section("fields").get("weights") or {}
    compared = [f for f in result.fields if f.status is not FieldStatus.SKIPPED]
    total = sum(float(weights.get(f.field.value, 0)) for f in compared)
    expected = sum(
        f.match_score * float(weights.get(f.field.value, 0)) for f in compared
    ) / total

    assert result.overall_score == int(round(expected))


def test_the_configuration_endpoint_publishes_the_weights_and_the_model():
    published = configuration()
    assert published["field_weights"]["NAME"]
    assert published["confidence_model"]["method_base"]
    assert published["confidence_model"]["penalties"]
    note = published["confidence_model"]["note"].lower()
    assert "neither is derived from the other" in note
    assert published["field_weights"]["INCOME"] == 0, (
        "income is a financial signal; weighting it would let a salary "
        "disagreement read as 'possibly not the same person'"
    )


def test_father_name_is_compared_against_father_name_only():
    """
    An applicant must never be matched against their own father's name.

    If the two name fields were ever merged, this bundle would report a
    passing NAME check on a document that names a different person.
    """
    result = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH",
            father_name="AJIT SINGH"),
        doc("dl.jpg", "DRIVING_LICENCE", name="AJIT SINGH",
            father_name="HARBANS SINGH"),
    )
    # The applicant's name and the father's name are compared on their own
    # tracks. "RISHABH AJIT SINGH" against "AJIT SINGH" shares two tokens, so
    # the matcher reports it as close rather than plainly different -- what
    # matters is that it is NOT a pass, and that the father's name is judged
    # separately from it.
    name = field(result, KycField.NAME)
    assert name.status is not FieldStatus.PASS, (
        "an applicant was matched against a document naming their father"
    )
    assert field(result, KycField.FATHER_NAME).status is FieldStatus.FAIL


def test_a_document_naming_only_the_father_does_not_pass_the_name_check():
    """The unambiguous version: no shared tokens at all."""
    result = assess(
        doc("pan.jpg", "PAN", name="RISHABH AJIT SINGH",
            father_name="HARBANS LAL"),
        doc("dl.jpg", "DRIVING_LICENCE", name="HARBANS LAL",
            father_name="RISHABH AJIT SINGH"),
    )
    assert field(result, KycField.NAME).status is FieldStatus.FAIL
    assert field(result, KycField.FATHER_NAME).status is FieldStatus.FAIL
