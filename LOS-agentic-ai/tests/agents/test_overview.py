"""
The application, arranged for the person reading it.

WHAT `overall` IS. A regrouping of values already published: the status
the roll-up produced, the decision the decision rules made, and each
party's own recorded reason codes -- now tagged with WHOSE they are.
Before it, answering "what happened, and whose problem is it" meant
joining `status`, `decision`, `next_action`, `cross_document` and two
party sections by hand.

WHAT IT IS NOT. It decides nothing. Every test below that could be
satisfied by recomputing a verdict instead asserts that the published
value is the one that was passed in, because the moment this layer can
disagree with the decision rules it has become a second opinion.

THE ONE FIELD THAT IS ALWAYS EMPTY. `missing` -- this endpoint has no
checklist. Required-document policy lives in the applicant agent and is
never computed here, so filling `missing` with plausible gaps would send
a reviewer chasing documents nobody asked for.
"""

from __future__ import annotations

import pytest

from app.agents.los import overview


def section(status="SUCCESS", verification=None, kyc=None):
    built = {"status": status}
    if verification is not None:
        built["verification"] = verification
    if kyc is not None:
        built["kyc"] = kyc
    return built


def build(sections, status="PARTIAL", decision="REVIEW",
          next_action="MANUAL_REVIEW", documents=None, ms=1234.5):
    return overview.build(
        status=status, decision=decision, next_action=next_action,
        sections=sections, documents=documents or [{}, {}], processing_ms=ms)


# ==========================================================================
# IT REPORTS, IT DOES NOT DECIDE
# ==========================================================================


def test_the_verdicts_are_the_ones_it_was_given():
    built = build({"primary_applicant": section()},
                  status="SUCCESS", decision="PASS", next_action="CONTINUE")

    assert built["status"] == "SUCCESS"
    assert built["decision"] == "PASS"
    assert built["next_action"] == "CONTINUE"


def test_it_does_not_recompute_a_status_from_the_party_sections():
    """
    A party in REVIEW beside an application the rules called SUCCESS is
    not this layer's disagreement to have. It publishes what it was
    handed.
    """
    built = build(
        {"primary_applicant": section(status="REVIEW")}, status="SUCCESS")

    assert built["status"] == "SUCCESS"


def test_nothing_is_invented_for_missing_items():
    assert build({"primary_applicant": section()})["missing"] == []


def test_the_processing_counts_come_from_the_documents():
    built = build({"primary_applicant": section()},
                  documents=[{}, {}, {}], ms=42.0)

    assert built["processing_summary"] == {
        "documents_received": 3, "documents_processed": 3,
        "processing_ms": 42.0}


# ==========================================================================
# EVERY ISSUE KNOWS WHOSE IT IS
# ==========================================================================


def test_an_issue_carries_the_party_it_belongs_to():
    built = build({
        "primary_applicant": section(),
        "co_applicant": section(status="REVIEW", kyc={
            "status": "REVIEW",
            "issues": [{"code": "NAME_MISMATCH", "message": "..."}]}),
    })

    assert built["issues"] == [
        {"party": "CO_APPLICANT", "code": "NAME_MISMATCH", "message": "..."}]


def test_a_primary_issue_is_not_attributed_to_the_co_applicant():
    built = build({
        "primary_applicant": section(status="REVIEW", kyc={
            "status": "REVIEW", "issues": [{"code": "DOB_MISMATCH"}]}),
        "co_applicant": section(),
    })

    assert [i["party"] for i in built["issues"]] == ["PRIMARY_APPLICANT"]


def test_verification_and_kyc_issues_both_surface():
    built = build({"primary_applicant": section(
        status="REVIEW",
        verification={"status": "FAIL",
                      "issues": [{"code": "DOCUMENT_TYPE_MISMATCH"}]},
        kyc={"status": "REVIEW", "issues": [{"code": "NAME_MISMATCH"}]})})

    assert {i["code"] for i in built["issues"]} == {
        "DOCUMENT_TYPE_MISMATCH", "NAME_MISMATCH"}


def test_a_clean_application_reports_no_issues():
    built = build({"primary_applicant": section(
        verification={"status": "PASS", "issues": []},
        kyc={"status": "PASS", "issues": []})}, status="SUCCESS")

    assert built["issues"] == []


# ==========================================================================
# THE SUMMARY EXPLAINS, AND DOES NOT BLAME THE ABSENT
# ==========================================================================


def test_an_absent_party_is_not_described_as_a_failure():
    """
    THE DISTINCTION THIS EXISTS FOR. Nobody sent anything for this
    party. That is not a failure, a rejection or a review.
    """
    built = build({
        "primary_applicant": section(),
        "co_applicant": section(status="NOT_PROVIDED"),
    }, status="SUCCESS")

    assert built["summary"] == (
        "Primary applicant verification passed. "
        "Co-applicant documents were not provided.")
    for word in ("fail", "reject", "review", "missing"):
        assert word not in built["summary"].lower()


def test_the_summary_names_the_party_and_the_reason():
    built = build({
        "primary_applicant": section(),
        "co_applicant": section(status="REVIEW", kyc={
            "status": "REVIEW", "issues": [{"code": "NAME_MISMATCH"}]}),
    })

    assert built["summary"] == (
        "Primary applicant verification passed. Co-applicant verification "
        "requires review due to NAME_MISMATCH.")


def test_several_reasons_are_listed_once_each():
    built = build({"primary_applicant": section(
        status="REVIEW",
        verification={"status": "REVIEW", "issues": [{"code": "A"}]},
        kyc={"status": "REVIEW",
             "issues": [{"code": "B"}, {"code": "A"}]})})

    assert built["summary"] == (
        "Primary applicant verification requires review due to B and A.")


def test_a_single_applicant_summary_mentions_nobody_else():
    built = build({"primary_applicant": section()}, status="SUCCESS")

    assert built["summary"] == "Primary applicant verification passed."
    assert "co-applicant" not in built["summary"].lower()


def test_an_empty_application_says_so_rather_than_nothing():
    assert build({})["summary"] == "Nothing was submitted."


# ==========================================================================
# THE PARTY-LEVEL OBJECTS
# ==========================================================================


def test_a_party_who_sent_nothing_has_no_verification_object():
    """
    Null, not `{"status": "SKIPPED"}`. A check that never ran has
    established nothing, and SKIPPED would claim it looked.
    """
    assert overview.verification_of([]) is None


def test_verification_reports_the_worst_of_a_partys_documents():
    published = overview.verification_of([
        {"source_id": "a.jpg", "verification": "PASS"},
        {"source_id": "b.jpg", "verification": "REVIEW",
         "reason_codes": ["LOW_QUALITY"]},
    ])

    assert published["status"] == "REVIEW"
    assert published["issues"] == [
        {"code": "LOW_QUALITY", "source_id": "b.jpg"}]


def test_a_passing_document_contributes_no_issue():
    published = overview.verification_of([
        {"source_id": "a.jpg", "verification": "PASS",
         "reason_codes": ["SOMETHING_INFORMATIONAL"]},
    ])

    assert published["status"] == "PASS"
    assert published["issues"] == []


def test_no_kyc_object_when_the_party_has_no_kyc():
    assert overview.kyc_of(None) is None
    assert overview.kyc_of({}) is None


def test_kyc_keeps_its_existing_score_untouched():
    """
    The score and its basis are whatever KYC produced. This layer
    never renames the basis and never invents an aggregate.
    """
    published = overview.kyc_of({
        "status": "REVIEW",
        "score": {"value": 38, "type": "NAME_MATCH",
                  "interpretation": "Low similarity", "confidence": 86},
        "reason_codes": ["NAME_MISMATCH"],
    })

    assert published["score"]["type"] == "NAME_MATCH"
    assert published["score"]["value"] == 38
    assert "accuracy" not in str(published).lower()


def test_a_kyc_with_nothing_comparable_publishes_no_score():
    published = overview.kyc_of({"status": "SKIPPED",
                                 "reason_codes": ["INSUFFICIENT_SOURCES"]})

    assert "score" not in published
    assert published["status"] == "SKIPPED"


def test_a_kyc_reason_code_becomes_a_readable_issue():
    published = overview.kyc_of({"status": "REVIEW",
                                 "reason_codes": ["NAME_MISMATCH"]})

    assert published["issues"][0]["code"] == "NAME_MISMATCH"
    assert "do not sufficiently match" in published["issues"][0]["message"]


def test_an_unmapped_reason_code_gets_no_invented_sentence():
    published = overview.kyc_of({"status": "REVIEW",
                                 "reason_codes": ["SOMETHING_NOBODY_MAPPED"]})

    assert published["issues"] == [{"code": "SOMETHING_NOBODY_MAPPED"}]


# ==========================================================================
# THE FOUR STATES STAY APART
# ==========================================================================


@pytest.mark.parametrize("state,expected", [
    ("NOT_PROVIDED", "Co-applicant documents were not provided."),
    ("SUCCESS", "Co-applicant verification passed."),
    ("REVIEW", "Co-applicant verification requires review."),
    ("FAILED", "Co-applicant verification failed."),
])
def test_absent_passed_review_and_failed_read_differently(state, expected):
    """
    NOT_PROVIDED, SKIPPED, REVIEW and FAIL are four different states
    and collapsing any two of them loses the reason a reviewer needs.
    """
    built = build({"co_applicant": section(status=state)})

    assert built["summary"] == expected
