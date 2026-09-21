"""
KYC, scoped to one person at a time.

THE DEFECT THIS CLOSES. KYC asks whether several documents describe ONE
person. Run over a whole two-party case it compared the primary
applicant's PAN against the co-applicant's and reported NAME_MISMATCH,
DOB_MISMATCH, PAN_MISMATCH and FATHER_NAME_MISMATCH — two different
people correctly disagreeing, read as a KYC failure, and a clean joint
application sent to a human for it. A joint application is BY DEFINITION
two people; treating that as evidence of fraud is backwards.

WHAT CHANGED, AND WHAT DID NOT. Only the SCOPE of the input. The name
matcher, the date and PAN normalisation, the address comparison, the
confidence model, the field weights, the thresholds and the reason codes
are all exactly whatever KYC already did. A single-applicant case makes
the same call over the same documents it always did.

THE MANDATORY GUARD is `test_two_different_people_do_not_mismatch_each_other`
and the four field-specific tests beside it.
"""

from __future__ import annotations

import pytest

from app.agents.los.flow import (
    _case_kyc, _did_not_run, _kyc_for_party, _released_for_matching,
)

# Two different people. Every field differs, which is exactly what a
# joint application looks like.
PERSON_A = {"name": "RISHABH AJIT SINGH", "date_of_birth": "2002-06-12",
            "pan_number": "NUHPS4875K", "father_name": "AJIT SINGH"}
PERSON_B = {"name": "LAXMI SANTOSH GUPTA", "date_of_birth": "2004-12-20",
            "pan_number": "EVPPG6189E", "father_name": "SANTOSH RAMASHARE GUPTA"}

IDENTITY_MISMATCHES = {"NAME_MISMATCH", "DOB_MISMATCH", "PAN_MISMATCH",
                       "FATHER_NAME_MISMATCH"}


def document(person, *, source_id="pan.jpg", party_id="APP-1",
             document_type="PAN", verification="PASS", **overrides):
    """One document as the flow carries it internally."""
    fields = {**person, **overrides}
    return {
        "source_id": source_id, "status": verification, "party_id": party_id,
        "party_role": ("CO_APPLICANT" if party_id.startswith("COAPP")
                       else "PRIMARY_APPLICANT"),
        "document": {"type": document_type},
        "verification": {"status": verification, "reason_codes": []},
        "extraction": {"fields": fields},
    }


def kyc_for(documents, party_id="APP-1"):
    return _kyc_for_party(documents, party_id=party_id, request_id="test")


def codes(payload) -> set[str]:
    return set(payload.get("reason_codes") or [])


def both_parties(primary_docs, co_docs):
    """Each party scored over their own documents only."""
    return (kyc_for(primary_docs, "APP-1"), kyc_for(co_docs, "COAPP-9"))


# ==========================================================================
# 11. THE MANDATORY REGRESSION GUARD
# ==========================================================================


def test_two_different_people_do_not_mismatch_each_other():
    """
    THE WHOLE POINT OF THIS PHASE.

    Person A is the applicant, Person B the co-applicant. Every identity
    field differs. Neither party's KYC may report a mismatch, because
    neither party's own documents disagree with each other — they were
    simply never compared with the other person's.
    """
    primary, co = both_parties(
        [document(PERSON_A, source_id="pan.jpg"),
         document(PERSON_A, source_id="dl.jpg",
                  document_type="DRIVING_LICENCE")],
        [document(PERSON_B, source_id="pan.jpg", party_id="COAPP-9"),
         document(PERSON_B, source_id="dl.jpg", party_id="COAPP-9",
                  document_type="DRIVING_LICENCE")],
    )

    assert codes(primary) & IDENTITY_MISMATCHES == set()
    assert codes(co) & IDENTITY_MISMATCHES == set()
    assert primary["status"] == "PASS"
    assert co["status"] == "PASS"


@pytest.mark.parametrize("field, code", [
    ("name", "NAME_MISMATCH"),
    ("date_of_birth", "DOB_MISMATCH"),
    ("pan_number", "PAN_MISMATCH"),
    ("father_name", "FATHER_NAME_MISMATCH"),
])
def test_one_differing_field_across_parties_is_not_a_mismatch(field, code):
    """
    B, C, D, E: each identity field in turn. The two parties differ on
    it — as two people do — and neither party's KYC reports it.
    """
    person_b = {**PERSON_A, field: PERSON_B[field]}

    primary, co = both_parties(
        [document(PERSON_A, source_id="pan.jpg"),
         document(PERSON_A, source_id="dl.jpg",
                  document_type="DRIVING_LICENCE")],
        [document(person_b, source_id="pan.jpg", party_id="COAPP-9"),
         document(person_b, source_id="dl.jpg", party_id="COAPP-9",
                  document_type="DRIVING_LICENCE")],
    )

    assert code not in codes(primary)
    assert code not in codes(co)


# ==========================================================================
# F/G. WITHIN ONE PARTY, THE CHECK STILL WORKS
# ==========================================================================


def test_one_partys_agreeing_documents_still_pass():
    result = kyc_for([
        document(PERSON_A, source_id="pan.jpg"),
        document(PERSON_A, source_id="dl.jpg",
                 document_type="DRIVING_LICENCE"),
    ])

    assert result["status"] == "PASS"
    assert codes(result) & IDENTITY_MISMATCHES == set()


@pytest.mark.parametrize("field, code", [
    ("name", "NAME_MISMATCH"),
    ("date_of_birth", "DOB_MISMATCH"),
    ("pan_number", "PAN_MISMATCH"),
])
def test_one_partys_disagreeing_documents_are_still_caught(field, code):
    """
    SCOPING MUST NOT BLUNT THE CHECK. A real within-party disagreement —
    one person's two documents saying different things — is exactly what
    KYC is for, and it still fires.
    """
    result = kyc_for([
        document(PERSON_A, source_id="pan.jpg"),
        document({**PERSON_A, field: PERSON_B[field]}, source_id="dl.jpg",
                 document_type="DRIVING_LICENCE"),
    ])

    assert code in codes(result)
    assert result["status"] != "PASS"


def test_the_matching_algorithm_is_untouched():
    """
    Same thresholds, same normalisation. An abbreviated name on one
    document still matches the full name on the other, exactly as it did
    before scoping.
    """
    result = kyc_for([
        document(PERSON_A, source_id="pan.jpg"),
        document({**PERSON_A, "name": "SINGH RISHABH AJIT"},
                 source_id="dl.jpg", document_type="DRIVING_LICENCE"),
    ])

    assert "NAME_MISMATCH" not in codes(result)


# ==========================================================================
# H/I. NO CONTAMINATION IN EITHER DIRECTION
# ==========================================================================


def test_the_co_applicants_documents_never_reach_the_primarys_kyc():
    """
    The caller filters before calling, but the scoping must be visible
    in the result: the primary's evidence cites only the primary's
    documents.
    """
    primary = kyc_for([
        document(PERSON_A, source_id="a.jpg"),
        document(PERSON_A, source_id="b.jpg",
                 document_type="DRIVING_LICENCE"),
    ])

    cited = {source_id
             for field in primary["fields"]
             for source in (field.get("sources") or [])
             for source_id in [source.get("source_id")]}

    assert cited <= {"a.jpg", "b.jpg"}


def test_the_primarys_documents_never_reach_the_co_applicants_kyc():
    co = kyc_for([
        document(PERSON_B, source_id="c.jpg", party_id="COAPP-9"),
        document(PERSON_B, source_id="d.jpg", party_id="COAPP-9",
                 document_type="DRIVING_LICENCE"),
    ], party_id="COAPP-9")

    cited = {source_id
             for field in co["fields"]
             for source in (field.get("sources") or [])
             for source_id in [source.get("source_id")]}

    assert cited <= {"c.jpg", "d.jpg"}


def test_each_result_records_whose_it_is():
    primary, co = both_parties(
        [document(PERSON_A), document(PERSON_A, source_id="dl.jpg",
                                      document_type="DRIVING_LICENCE")],
        [document(PERSON_B, party_id="COAPP-9"),
         document(PERSON_B, party_id="COAPP-9", source_id="dl.jpg",
                  document_type="DRIVING_LICENCE")],
    )

    assert primary["party_id"] == "APP-1"
    assert co["party_id"] == "COAPP-9"


# ==========================================================================
# J. THE VERIFICATION RELEASE GATE
#
# This was BROKEN, not merely at risk. `to_kyc_source` checks only that
# `extraction.fields` is populated, and the internal envelope carries
# extraction whatever the verdict -- so a REVIEW, a FAIL and a REJECTED
# all fed cross-document KYC with fields the caller was never shown,
# while the comment in the flow claimed the opposite.
# ==========================================================================


@pytest.mark.parametrize("verdict", ["REVIEW", "FAIL", "REJECTED", "SKIPPED"])
def test_an_unreleased_document_cannot_feed_kyc(verdict):
    result = kyc_for([
        document(PERSON_A, source_id="pan.jpg", verification=verdict),
        document(PERSON_A, source_id="dl.jpg", verification=verdict,
                 document_type="DRIVING_LICENCE"),
    ])

    assert _did_not_run(result)
    assert result["status"] == "SKIPPED"


def test_a_passing_document_does_feed_kyc():
    result = kyc_for([
        document(PERSON_A, source_id="pan.jpg"),
        document(PERSON_A, source_id="dl.jpg",
                 document_type="DRIVING_LICENCE"),
    ])

    assert not _did_not_run(result)
    assert result["status"] == "PASS"


def test_a_withheld_document_cannot_create_a_mismatch():
    """
    THE DAMAGING SHAPE. A REVIEW document carrying a misread name sat
    beside a clean PAN and produced NAME_MISMATCH on the application —
    from a field the caller was never allowed to see and could not
    dispute.
    """
    result = kyc_for([
        document(PERSON_A, source_id="pan.jpg"),
        document({**PERSON_A, "name": "GARBLED OCR NAME"},
                 source_id="dl.jpg", document_type="DRIVING_LICENCE",
                 verification="REVIEW"),
    ])

    assert "NAME_MISMATCH" not in codes(result)


def test_switching_extraction_off_stops_kyc_too():
    from unittest.mock import patch

    with patch("app.agents.los.config.extraction_enabled", return_value=False):
        result = kyc_for([
            document(PERSON_A, source_id="pan.jpg"),
            document(PERSON_A, source_id="dl.jpg",
                     document_type="DRIVING_LICENCE"),
        ])

    assert _did_not_run(result)


def test_kyc_uses_the_same_gate_as_profile_matching():
    """Not a second copy. The gate was lost once already by being written twice."""
    import inspect

    from app.agents.los import flow

    assert "_released_for_matching" in inspect.getsource(flow._kyc_for_party)


def test_the_gate_leaves_a_released_document_intact():
    """The gate filters; it must not alter what it lets through."""
    passing = document(PERSON_A)

    released = _released_for_matching([passing])

    assert released[0]["extraction"]["fields"] == PERSON_A


# ==========================================================================
# 6. THE CASE-LEVEL ROLL-UP
# ==========================================================================


def payload(status, party_id, *, ran=True, score=80, confidence=70,
            reason_codes=None, fields=None):
    return {"party_id": party_id, "ran": ran, "status": status,
            "reason_codes": list(reason_codes or []),
            "overall_score": score, "overall_confidence": confidence,
            "fields": fields or [{"field": "NAME", "status": status}],
            "checks": []}


def test_one_party_is_returned_untouched():
    """
    A single-applicant case must be byte-for-byte what it was. Not
    "equivalent" — the same object.
    """
    only = payload("PASS", "APP-1")

    result, _rank = _case_kyc([only])

    assert result is only


def test_the_case_takes_the_worst_partys_verdict():
    result, _rank = _case_kyc([payload("PASS", "APP-1"),
                               payload("REVIEW", "COAPP-9")])

    assert result["status"] == "REVIEW"


def test_the_case_score_is_the_worst_not_the_average():
    """
    An average lets a well-documented applicant hide a poorly
    documented co-applicant.
    """
    result, _rank = _case_kyc([payload("PASS", "APP-1", score=100,
                                       confidence=95),
                               payload("REVIEW", "COAPP-9", score=40,
                                       confidence=50)])

    assert result["overall_score"] == 40
    assert result["overall_confidence"] == 50


def test_the_case_collects_both_parties_reason_codes():
    result, _rank = _case_kyc([
        payload("REVIEW", "APP-1", reason_codes=["NAME_MISMATCH"]),
        payload("REVIEW", "COAPP-9", reason_codes=["DOB_MISMATCH"]),
    ])

    assert result["reason_codes"] == ["NAME_MISMATCH", "DOB_MISMATCH"]


def test_a_code_reported_by_both_parties_appears_once():
    result, _rank = _case_kyc([
        payload("REVIEW", "APP-1", reason_codes=["NAME_MISMATCH"]),
        payload("REVIEW", "COAPP-9", reason_codes=["NAME_MISMATCH"]),
    ])

    assert result["reason_codes"] == ["NAME_MISMATCH"]


def test_case_level_rows_say_whose_they_are():
    """
    Two parties produce two NAME rows. Without an owner a reviewer
    cannot tell which person the row is about.
    """
    result, _rank = _case_kyc([payload("PASS", "APP-1"),
                               payload("PASS", "COAPP-9")])

    assert {row["party_id"] for row in result["fields"]} == {"APP-1",
                                                             "COAPP-9"}


def test_a_party_that_could_not_run_contributes_nothing():
    """
    "The co-applicant has not sent anything yet" must not read as "the
    co-applicant failed".
    """
    result, _rank = _case_kyc([
        payload("PASS", "APP-1", score=100),
        payload("SKIPPED", "COAPP-9", ran=False, score=0,
                reason_codes=["INSUFFICIENT_SOURCES"]),
    ])

    assert result["status"] == "PASS"
    assert result["overall_score"] == 100
    assert "INSUFFICIENT_SOURCES" not in result["reason_codes"]


def test_no_party_running_reproduces_the_old_empty_shape():
    result, rank = _case_kyc([
        payload("SKIPPED", "APP-1", ran=False),
        payload("SKIPPED", "COAPP-9", ran=False),
    ])

    assert result["status"] == "SKIPPED"
    assert result["reason_codes"] == ["INSUFFICIENT_SOURCES"]
    assert rank == 0
    # AND IT MUST SAY SO. Without this flag the caller is told SKIPPED
    # and left to infer whether the gate held or KYC simply agreed --
    # the KYC_NOT_RUN error that distinguishes them keys on it.
    assert _did_not_run(result)


def test_a_lone_document_is_a_verdict_not_an_absence():
    """
    KYC reports INSUFFICIENT_SOURCES itself when it runs over one
    document and has nothing to compare it WITH. Reading that as "did
    not run" silently dropped a real REVIEW out of the case roll-up.
    """
    real_verdict = payload("REVIEW", "APP-1", ran=True,
                           reason_codes=["INSUFFICIENT_SOURCES"])

    assert not _did_not_run(real_verdict)

    result, rank = _case_kyc([real_verdict, payload("PASS", "COAPP-9")])

    assert result["status"] == "REVIEW"
    assert rank == 1


def test_the_case_roll_up_compares_nothing():
    """
    NOT A CROSS-PARTY COMPARISON, AND IT MUST NEVER BECOME ONE. The
    aggregate is built only from verdicts each party already reached.
    """
    import ast
    import inspect
    import textwrap

    from app.agents.los import flow

    # IDENTIFIERS, not text. Grepping the source matched the word
    # "compares" in the function's own docstring, which proves nothing.
    tree = ast.parse(textwrap.dedent(inspect.getsource(flow._case_kyc)))
    called = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    }

    assert called.isdisjoint({"match_names", "normalize_pan", "run_kyc",
                              "normalize_date", "compare", "to_kyc_source"})
