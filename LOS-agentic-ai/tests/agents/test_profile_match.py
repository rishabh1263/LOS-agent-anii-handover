"""
A party's declared profile, against that party's own documents.

THE FAILURE THIS LAYER MUST NOT HAVE. One case, two people, and both
upload `pan.jpg`. If the matcher is handed the case's documents rather
than the PARTY's, the primary applicant's declared PAN gets checked
against the co-applicant's card — and reports a mismatch on a perfectly
good application, or worse, a match against the wrong person's document.
Most of this file is that one boundary, asserted from both directions.

THE OTHER HALF IS RESTRAINT. A profile field nobody supplied, and a
document field the gate did not release, are both SKIPPED. Neither is a
mismatch. `fields_compared` is published beside the score precisely so
that "we could not check the address" can never be read as "the address
is wrong".

NOTHING HERE DECIDES ANYTHING. Profile matching is evidence. The tests at
the end assert it cannot reach a document's verdict, its reason codes or
its score.
"""

from __future__ import annotations

import pytest

from app.agents.kyc.schemas import FieldStatus, KycField
from app.agents.los import parties, profile_match
from app.agents.los.profile_match import Profile

PAN_NUMBER = "AECPV7900A"
OTHER_PAN = "BXZPK4411Q"


def document(
    *, party_id, source_id="pan.jpg", document_type="PAN",
    fields=None, party_role="PRIMARY_APPLICANT",
):
    """One processed document result, in the public (flat) shape."""
    entry = {
        "source_id": source_id,
        "type": document_type,
        "party_id": party_id,
        "party_role": party_role,
        "verification": "PASS",
    }
    if fields is not None:
        entry["extraction"] = fields
    return entry


def match(profile, documents, party_id="APP-1", role="PRIMARY_APPLICANT"):
    # `parties.owned_by` is THE ownership filter -- the same one the
    # response sections and KYC use. Profile matching had its own until
    # Phase 7, and the two disagreed about unstamped documents.
    return profile_match.match_party(
        party_id=party_id, party_role=role, profile=profile,
        documents=parties.owned_by(
            documents, party_id, is_primary=role == "PRIMARY_APPLICANT"),
    )


def field(result, name: KycField):
    return next(c for c in result.comparisons if c.field is name)


# ==========================================================================
# A. PARTY ISOLATION -- the four cases the brief names
# ==========================================================================


def test_primary_profile_matches_primary_pan():
    result = match(
        Profile(pan_number=PAN_NUMBER),
        [document(party_id="APP-1", fields={"pan_number": PAN_NUMBER})],
    )

    assert field(result, KycField.PAN_NUMBER).status is FieldStatus.PASS
    assert result.fields_compared == 1


def test_primary_profile_cannot_consume_a_co_applicant_pan():
    """
    THE ONE THAT MATTERS MOST. The co-applicant's card carries the PAN
    the primary applicant declared. Without isolation this reports a
    match — a match against a document belonging to somebody else.
    """
    result = match(
        Profile(pan_number=PAN_NUMBER),
        [document(party_id="COAPP-9", party_role="CO_APPLICANT",
                  fields={"pan_number": PAN_NUMBER})],
        party_id="APP-1",
    )

    comparison = field(result, KycField.PAN_NUMBER)
    assert comparison.status is FieldStatus.SKIPPED
    assert comparison.reason_code == profile_match.NO_DOCUMENT_VALUE
    assert result.fields_compared == 0


def test_co_applicant_profile_matches_co_applicant_pan():
    result = match(
        Profile(pan_number=OTHER_PAN),
        [document(party_id="COAPP-9", party_role="CO_APPLICANT",
                  fields={"pan_number": OTHER_PAN})],
        party_id="COAPP-9", role="CO_APPLICANT",
    )

    assert field(result, KycField.PAN_NUMBER).status is FieldStatus.PASS


def test_co_applicant_profile_cannot_consume_the_primarys_pan():
    result = match(
        Profile(pan_number=PAN_NUMBER),
        [document(party_id="APP-1", fields={"pan_number": PAN_NUMBER})],
        party_id="COAPP-9", role="CO_APPLICANT",
    )

    assert field(result, KycField.PAN_NUMBER).status is FieldStatus.SKIPPED
    assert result.fields_compared == 0


def test_the_same_filename_from_both_parties_stays_isolated():
    """
    Both uploaded `pan.jpg`. Each party sees only their own.
    """
    documents = [
        document(party_id="APP-1", fields={"pan_number": PAN_NUMBER}),
        document(party_id="COAPP-9", party_role="CO_APPLICANT",
                 fields={"pan_number": OTHER_PAN}),
    ]

    primary = match(Profile(pan_number=PAN_NUMBER), documents,
                    party_id="APP-1")
    co = match(Profile(pan_number=OTHER_PAN), documents,
               party_id="COAPP-9", role="CO_APPLICANT")

    assert field(primary, KycField.PAN_NUMBER).status is FieldStatus.PASS
    assert field(co, KycField.PAN_NUMBER).status is FieldStatus.PASS


def test_each_party_sees_only_its_own_documents():
    documents = [
        document(party_id="APP-1", fields={"pan_number": PAN_NUMBER}),
        document(party_id="COAPP-9", party_role="CO_APPLICANT",
                 fields={"pan_number": OTHER_PAN}),
    ]

    assert len(parties.owned_by(documents, "APP-1", is_primary=True)) == 1
    assert len(parties.owned_by(documents, "COAPP-9", is_primary=False)) == 1


def test_an_unstamped_document_goes_to_the_primary_and_only_them():
    """
    CHANGED IN PHASE 7, DELIBERATELY. Profile matching used to give an
    unstamped document to nobody while KYC and the response sections
    gave it to the primary applicant -- so on a case carrying pre-party
    rows, KYC compared a document profile matching reported it could
    not see. One filter now answers for all three.

    The adoption is still offered to the primary ONLY, which is the
    property that matters: it can never hand one person's document to
    another.
    """
    unstamped = {"source_id": "pan.jpg", "type": "PAN", "verification": "PASS",
                 "extraction": {"pan_number": PAN_NUMBER}}

    assert parties.owned_by([unstamped], "APP-1", is_primary=True) == [unstamped]
    assert parties.owned_by([unstamped], "COAPP-9", is_primary=False) == []


def test_asking_for_no_party_returns_nothing():
    documents = [document(party_id="APP-1", fields={"pan_number": PAN_NUMBER})]

    assert parties.owned_by(documents, "", is_primary=True) == []


# ==========================================================================
# B. MATCH AND MISMATCH
# ==========================================================================


def test_a_different_pan_is_a_mismatch():
    result = match(
        Profile(pan_number=PAN_NUMBER),
        [document(party_id="APP-1", fields={"pan_number": OTHER_PAN})],
    )
    comparison = field(result, KycField.PAN_NUMBER)

    assert comparison.status is FieldStatus.FAIL
    assert comparison.match_score == 0
    assert comparison.reason_code == profile_match.MISMATCH


def test_pan_is_never_fuzzy_matched():
    """
    Two PANs differing by one character are two taxpayers. A similarity
    score here would eventually approve one.
    """
    nearly = PAN_NUMBER[:-1] + ("B" if PAN_NUMBER[-1] != "B" else "C")
    result = match(
        Profile(pan_number=PAN_NUMBER),
        [document(party_id="APP-1", fields={"pan_number": nearly})],
    )

    assert field(result, KycField.PAN_NUMBER).status is FieldStatus.FAIL
    assert field(result, KycField.PAN_NUMBER).match_score == 0


def test_pan_normalisation_is_applied_before_comparison():
    result = match(
        Profile(pan_number=f"  {PAN_NUMBER.lower()}  "),
        [document(party_id="APP-1", fields={"pan_number": PAN_NUMBER})],
    )

    assert field(result, KycField.PAN_NUMBER).status is FieldStatus.PASS


def test_an_identical_name_passes():
    result = match(
        Profile(name="RAJESH SHARMA"),
        [document(party_id="APP-1", fields={"name": "RAJESH SHARMA"})],
    )

    assert field(result, KycField.NAME).status is FieldStatus.PASS
    assert field(result, KycField.NAME).match_score == 100


def test_a_completely_different_name_fails():
    result = match(
        Profile(name="RAJESH SHARMA"),
        [document(party_id="APP-1", fields={"name": "PRIYA IYER"})],
    )

    assert field(result, KycField.NAME).status is FieldStatus.FAIL


def test_name_matching_reuses_the_existing_matcher():
    """
    Token reordering is already handled by app/services/name_match. A
    second normaliser here is how two parts of one service end up
    disagreeing about the same two names.
    """
    result = match(
        Profile(name="SHARMA RAJESH"),
        [document(party_id="APP-1", fields={"name": "RAJESH SHARMA"})],
    )

    assert field(result, KycField.NAME).status is not FieldStatus.FAIL


def test_a_matching_date_of_birth_passes():
    result = match(
        Profile(date_of_birth="1990-04-12"),
        [document(party_id="APP-1", fields={"date_of_birth": "12/04/1990"})],
    )

    assert field(result, KycField.DATE_OF_BIRTH).status is FieldStatus.PASS


def test_a_different_date_of_birth_fails_outright():
    """A near-miss date is a different person, not a partial match."""
    result = match(
        Profile(date_of_birth="1990-04-12"),
        [document(party_id="APP-1", fields={"date_of_birth": "1991-04-12"})],
    )
    comparison = field(result, KycField.DATE_OF_BIRTH)

    assert comparison.status is FieldStatus.FAIL
    assert comparison.match_score == 0


def test_the_fathers_name_uses_the_name_matcher():
    result = match(
        Profile(father_name="MOHAN SHARMA"),
        [document(party_id="APP-1", fields={"father_name": "MOHAN SHARMA"})],
    )

    assert field(result, KycField.FATHER_NAME).status is FieldStatus.PASS


def test_a_fathers_name_is_read_from_a_guardian_field_too():
    """A licence calls it `guardian_name`; it is the same person."""
    result = match(
        Profile(father_name="MOHAN SHARMA"),
        [document(party_id="APP-1", document_type="DRIVING_LICENCE",
                  fields={"guardian_name": "MOHAN SHARMA"})],
    )

    assert field(result, KycField.FATHER_NAME).status is FieldStatus.PASS


# ==========================================================================
# B2. ADDRESS -- COMPARED COMPONENT BY COMPONENT, NOT AS TWO STRINGS
#
# This section exists because it was missing. Every other field had a
# real comparison asserted; the address only ever appeared in tests of
# the SKIPPED path. `kyc.address.compare` takes parsed addresses, the
# comparator was handing it raw strings, and it raised on every single
# call -- which the defensive handler turned into SKIPPED. The address
# was silently never checked and the response said only "could not be
# compared".
# ==========================================================================

PUNE = "12 MG Road, Shivaji Nagar, Pune, Maharashtra 411005"
BENGALURU = "88 Brigade Road, Ashok Nagar, Bengaluru, Karnataka 560025"


def test_an_identical_address_passes():
    result = match(
        Profile(address=PUNE),
        [document(party_id="APP-1", fields={"address": PUNE})],
    )
    comparison = field(result, KycField.ADDRESS)

    assert comparison.status is FieldStatus.PASS
    assert comparison.match_score == 100


def test_the_same_address_written_differently_still_passes():
    """
    THE CASE COMPARING STRINGS GETS WRONG. Documents abbreviate, reorder
    and punctuate differently; this is one address, not two.
    """
    result = match(
        Profile(address="12 M.G. Road, Shivajinagar, PUNE, MH - 411005"),
        [document(party_id="APP-1", fields={"address": PUNE})],
    )

    assert field(result, KycField.ADDRESS).status is FieldStatus.PASS


def test_a_different_address_fails():
    result = match(
        Profile(address=PUNE),
        [document(party_id="APP-1", fields={"address": BENGALURU})],
    )

    assert field(result, KycField.ADDRESS).status is FieldStatus.FAIL


def test_a_separately_extracted_pincode_is_used():
    """
    The strongest single component is often extracted into its own
    field. Comparing only the printed line throws it away.
    """
    result = match(
        Profile(address=PUNE),
        [document(party_id="APP-1", fields={
            "address": "12 MG Road, Shivaji Nagar, Pune, Maharashtra",
            "pin_code": "411005",
        })],
    )

    assert field(result, KycField.ADDRESS).status is FieldStatus.PASS


def test_the_address_comparison_is_actually_reached():
    """
    A guard against the original defect returning: the comparator raised
    on every call and the failure was swallowed into SKIPPED, so the
    absence of a crash is not evidence that anything was compared.
    """
    result = match(
        Profile(address=PUNE),
        [document(party_id="APP-1", fields={"address": PUNE})],
    )

    assert result.fields_compared == 1
    assert field(result, KycField.ADDRESS).reason_code == profile_match.MATCH


def test_a_comparator_failure_is_reported_rather_than_scored():
    """
    If a comparator does raise, the field is SKIPPED -- never counted as
    a mismatch, and never counted as compared.
    """
    from unittest.mock import patch

    # The DICT entry, not the module attribute: `_COMPARATORS` captured
    # the function object at import, so rebinding the name reaches
    # nothing and the test would pass while proving nothing.
    def explode(*_args):
        raise RuntimeError("boom")

    with patch.dict(profile_match._COMPARATORS,
                    {KycField.PAN_NUMBER: explode}):
        result = match(
            Profile(pan_number=PAN_NUMBER),
            [document(party_id="APP-1", fields={"pan_number": PAN_NUMBER})],
        )

    assert field(result, KycField.PAN_NUMBER).status is FieldStatus.SKIPPED
    assert result.fields_compared == 0


# ==========================================================================
# C. ABSENCE IS NEVER A MISMATCH
# ==========================================================================


def test_a_profile_field_that_was_not_supplied_is_skipped():
    result = match(
        Profile(pan_number=PAN_NUMBER),
        [document(party_id="APP-1", fields={"pan_number": PAN_NUMBER,
                                            "name": "RAJESH SHARMA"})],
    )
    comparison = field(result, KycField.NAME)

    assert comparison.status is FieldStatus.SKIPPED
    assert comparison.reason_code == profile_match.NO_PROFILE_VALUE


def test_a_document_field_that_was_not_released_is_skipped():
    result = match(
        Profile(name="RAJESH SHARMA", pan_number=PAN_NUMBER),
        [document(party_id="APP-1", fields={"pan_number": PAN_NUMBER})],
    )
    comparison = field(result, KycField.NAME)

    assert comparison.status is FieldStatus.SKIPPED
    assert comparison.reason_code == profile_match.NO_DOCUMENT_VALUE


def test_a_skipped_field_is_not_counted_as_compared():
    result = match(
        Profile(name="RAJESH SHARMA", pan_number=PAN_NUMBER),
        [document(party_id="APP-1", fields={"pan_number": PAN_NUMBER})],
    )

    assert result.fields_expected == 2
    assert result.fields_compared == 1


def test_a_missing_field_does_not_drag_the_score_down():
    """
    THE MOST DAMAGING THING THIS LAYER COULD GET WRONG. Averaging an
    uncomparable field in as zero turns "we could not check the address"
    into "the address is wrong".
    """
    everything = match(
        Profile(pan_number=PAN_NUMBER),
        [document(party_id="APP-1", fields={"pan_number": PAN_NUMBER})],
    )
    with_a_gap = match(
        Profile(pan_number=PAN_NUMBER, address="12 Main Street, Pune 411001"),
        [document(party_id="APP-1", fields={"pan_number": PAN_NUMBER})],
    )

    assert everything.score == with_a_gap.score == 100


def test_coverage_is_reported_so_a_gap_is_visible():
    result = match(
        Profile(name="RAJESH SHARMA", pan_number=PAN_NUMBER,
                date_of_birth="1990-04-12"),
        [document(party_id="APP-1", fields={"pan_number": PAN_NUMBER})],
    )

    assert result.fields_expected == 3
    assert result.fields_compared == 1
    assert result.fields_extracted >= 1


def test_a_party_with_no_documents_compares_nothing():
    result = match(Profile(pan_number=PAN_NUMBER), [])

    assert result.fields_compared == 0
    assert all(c.status is FieldStatus.SKIPPED for c in result.comparisons)


# ==========================================================================
# D. THE VERIFICATION GATE IS NOT RE-IMPLEMENTED, IT IS OBEYED
# ==========================================================================


def test_a_failed_document_releases_nothing_to_match_against():
    """
    A FAIL arrives with no `extraction` at all -- the gate withheld it.
    Reading the key is reading the gate's decision.
    """
    failed = document(party_id="APP-1", fields=None)
    failed["verification"] = "FAIL"

    result = match(Profile(pan_number=PAN_NUMBER), [failed])

    assert result.fields_compared == 0


def test_a_review_whose_fields_were_withdrawn_releases_nothing():
    review = document(party_id="APP-1", fields=None)
    review["verification"] = "REVIEW"

    result = match(Profile(pan_number=PAN_NUMBER), [review])

    assert field(result, KycField.PAN_NUMBER).status is FieldStatus.SKIPPED


def test_an_empty_extraction_releases_nothing():
    """Extraction disabled produces an empty dict, not a missing key."""
    result = match(
        Profile(pan_number=PAN_NUMBER),
        [document(party_id="APP-1", fields={})],
    )

    assert result.fields_compared == 0


def test_a_blank_field_value_is_not_evidence():
    result = match(
        Profile(pan_number=PAN_NUMBER),
        [document(party_id="APP-1", fields={"pan_number": "   "})],
    )

    assert field(result, KycField.PAN_NUMBER).status is FieldStatus.SKIPPED


def test_both_extraction_shapes_are_read():
    """
    The flow carries `extraction = {"fields": {...}}` internally and the
    public response flattens it. Handling only one silently found
    nothing and reported SKIPPED on a document that extracted perfectly.
    """
    nested = document(party_id="APP-1",
                      fields={"fields": {"pan_number": PAN_NUMBER}})

    result = match(Profile(pan_number=PAN_NUMBER), [nested])

    assert field(result, KycField.PAN_NUMBER).status is FieldStatus.PASS


# ==========================================================================
# D2. THE GATE IS OBEYED ON THE INTERNAL ENVELOPE TOO
#
# The tests above use the PUBLIC document shape, where the gate has
# already stripped what it withheld. Matching actually runs on the
# INTERNAL envelope, which carries `extraction` on every document
# whatever the verdict -- so obeying the gate there is a separate
# question, and it was originally got wrong: a REVIEW matched at 100.
# ==========================================================================


def internal(status="PASS", fields=None, quality=None):
    """A document as the flow carries it, before the response boundary."""
    return {
        "source_id": "pan.jpg", "status": status, "party_id": "APP-1",
        "document": {"type": "PAN"},
        "extraction": {"fields": fields if fields is not None
                       else {"pan_number": PAN_NUMBER},
                       "field_quality": quality or {}},
        "verification": {"status": status, "reason_codes": []},
    }


@pytest.mark.parametrize("status", ["REVIEW", "FAIL", "REJECTED", "SKIPPED"])
def test_only_a_pass_releases_fields_for_matching(status):
    """
    THE ONE THAT WAS WRONG. A REVIEW whose fields the caller never saw
    was still matched against, and reported PASS at score 100 on
    evidence the gate had withheld.
    """
    from app.agents.los.flow import _released_for_matching

    assert _released_for_matching([internal(status)]) == []


def test_a_pass_does_release_its_fields():
    from app.agents.los.flow import _released_for_matching

    assert len(_released_for_matching([internal("PASS")])) == 1


def test_switching_extraction_off_stops_matching_too():
    """
    An operator who withholds extracted fields has withheld them from
    every consumer, not just from the response body.
    """
    from unittest.mock import patch

    from app.agents.los.flow import _released_for_matching

    with patch("app.agents.los.config.extraction_enabled", return_value=False):
        assert _released_for_matching([internal("PASS")]) == []


def test_the_gate_is_the_same_one_the_response_uses():
    """
    Not a second copy. The gate was lost once already by being written
    twice, and the second copy did not have it.
    """
    import inspect

    from app.agents.los import flow

    assert "released_extraction" in inspect.getsource(
        flow._released_for_matching)


def test_a_withheld_document_reports_skipped_rather_than_vanishing():
    from app.agents.los.flow import _released_for_matching

    result = profile_match.match_party(
        party_id="APP-1", party_role="PRIMARY_APPLICANT",
        profile=Profile(pan_number=PAN_NUMBER),
        documents=_released_for_matching([internal("REVIEW")]),
    )

    assert field(result, KycField.PAN_NUMBER).status is FieldStatus.SKIPPED
    assert result.fields_compared == 0


def test_field_quality_survives_the_gate():
    """It feeds the confidence figure, and is never published."""
    from app.agents.los.flow import _released_for_matching

    released = _released_for_matching(
        [internal("PASS", quality={"pan_number": 0.4})])
    evidence = profile_match.released_fields(released)["pan_number"][0]

    assert evidence.quality == 0.4


def test_a_poor_reading_lowers_confidence_without_lowering_the_score():
    """
    Score and confidence answer different questions. A perfect match read
    off a poor photograph is a perfect match believed less.
    """
    from app.agents.los.flow import _released_for_matching

    def confidence_at(quality):
        result = profile_match.match_party(
            party_id="APP-1", party_role="PRIMARY_APPLICANT",
            profile=Profile(pan_number=PAN_NUMBER),
            documents=_released_for_matching(
                [internal("PASS", quality={"pan_number": quality})]),
        )
        return field(result, KycField.PAN_NUMBER)

    good, poor = confidence_at(0.99), confidence_at(0.30)

    assert good.match_score == poor.match_score == 100
    assert poor.confidence < good.confidence


# ==========================================================================
# E. PROFILE SOURCE PRIORITY
# ==========================================================================


class StoredApplicant:
    def __init__(self, full_name=None, date_of_birth=None, address=None):
        self.full_name = full_name
        self.date_of_birth = date_of_birth
        self.address = address


def test_the_request_wins_over_the_store():
    """
    The caller is describing the person in front of them now; the store
    is describing whoever was captured earlier.
    """
    merged = profile_match.merged(
        Profile(name="NEW NAME"),
        profile_match.stored_profile(StoredApplicant(full_name="OLD NAME")),
    )

    assert merged.name == "NEW NAME"


def test_the_store_fills_a_gap_the_request_left():
    merged = profile_match.merged(
        Profile(pan_number=PAN_NUMBER),
        profile_match.stored_profile(StoredApplicant(full_name="STORED NAME")),
    )

    assert merged.name == "STORED NAME"
    assert merged.pan_number == PAN_NUMBER


def test_a_field_in_neither_stays_absent():
    """Nothing is invented. An absent field is SKIPPED, not guessed."""
    merged = profile_match.merged(Profile(), profile_match.stored_profile(None))

    assert merged.father_name is None
    assert merged.is_empty()


def test_the_store_has_no_pan_or_fathers_name_today():
    """
    Recorded so that adding those columns later is a change here and
    nowhere else. Until then the two are matched only when the request
    supplies them.
    """
    stored = profile_match.stored_profile(
        StoredApplicant(full_name="X", date_of_birth="1990-01-01",
                        address="Pune"))

    assert stored.pan_number is None
    assert stored.father_name is None


def test_a_blank_request_value_does_not_shadow_the_store():
    merged = profile_match.merged(
        Profile(name="   "),
        profile_match.stored_profile(StoredApplicant(full_name="STORED")),
    )

    assert merged.name == "STORED"


# ==========================================================================
# F. THE PUBLISHED RESULT
# ==========================================================================


def test_the_published_row_carries_no_internals():
    result = match(
        Profile(pan_number=PAN_NUMBER),
        [document(party_id="APP-1", fields={"pan_number": PAN_NUMBER})],
    )
    row = field(result, KycField.PAN_NUMBER).public()

    assert "method" not in row
    assert "quality" not in row
    assert set(row) <= {"field", "status", "match_score", "confidence",
                        "reason_code", "reason", "source"}


def test_the_published_source_names_the_document():
    result = match(
        Profile(pan_number=PAN_NUMBER),
        [document(party_id="APP-1", fields={"pan_number": PAN_NUMBER})],
    )
    row = field(result, KycField.PAN_NUMBER).public()

    assert row["source"]["source_id"] == "pan.jpg"
    assert row["source"]["document_type"] == "PAN"


def test_the_party_result_names_its_party():
    published = match(
        Profile(pan_number=PAN_NUMBER),
        [document(party_id="APP-1", fields={"pan_number": PAN_NUMBER})],
    ).public()

    assert published["party_id"] == "APP-1"
    assert published["party_role"] == "PRIMARY_APPLICANT"
    assert set(published) >= {"score", "confidence", "fields_expected",
                              "fields_extracted", "fields_compared", "fields"}


def test_score_and_confidence_stay_in_range():
    for profile in (Profile(pan_number=PAN_NUMBER),
                    Profile(pan_number=OTHER_PAN),
                    Profile()):
        result = match(
            profile,
            [document(party_id="APP-1", fields={"pan_number": PAN_NUMBER})],
        )
        assert 0 <= result.score <= 100
        assert 0 <= result.confidence <= 100


def test_every_non_pass_comparison_carries_a_reason():
    result = match(
        Profile(name="RAJESH SHARMA", pan_number=OTHER_PAN),
        [document(party_id="APP-1", fields={"pan_number": PAN_NUMBER})],
    )

    for comparison in result.comparisons:
        if comparison.status is not FieldStatus.PASS:
            assert comparison.reason_code
            assert len(comparison.reason) > 15


def test_confidence_is_not_copied_from_the_score():
    """
    They answer different questions and the confidence model is the
    existing KYC one, not a second implementation.
    """
    result = match(
        Profile(pan_number=PAN_NUMBER),
        [document(party_id="APP-1", fields={"pan_number": PAN_NUMBER})],
    )
    comparison = field(result, KycField.PAN_NUMBER)

    assert comparison.match_score == 100
    assert comparison.confidence != comparison.match_score


# ==========================================================================
# G. IT DECIDES NOTHING
# ==========================================================================


def test_matching_does_not_mutate_the_documents_it_reads():
    """
    Profile matching is evidence. A verdict, a reason code or a score it
    could reach would make it a second gate.
    """
    import copy

    documents = [document(party_id="APP-1",
                          fields={"pan_number": PAN_NUMBER})]
    before = copy.deepcopy(documents)

    match(Profile(pan_number=OTHER_PAN), documents)

    assert documents == before


def test_a_total_mismatch_produces_no_verdict_field():
    result = match(
        Profile(pan_number=OTHER_PAN, name="WRONG PERSON"),
        [document(party_id="APP-1",
                  fields={"pan_number": PAN_NUMBER, "name": "RAJESH SHARMA"})],
    ).public()

    assert "verification" not in result
    assert "status" not in result
    assert "reason_codes" not in result
