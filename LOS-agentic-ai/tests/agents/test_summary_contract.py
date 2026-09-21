"""
The summary is a contract, not a nicety.

WHAT WENT WRONG. The deterministic sentence names which person needs
attention and why. The model's sentence is allowed to replace it when it
passes validation — and validation only checked that the model had not
INVENTED anything. So on a real joint application the public summary
became:

    "Loan officer review shows all documents except Kyc status as
     successful, with multiple Kyc reason codes noted."

True, harmless, and useless. A reviewer cannot tell from it which of two
people needs looking at.

THE FIX IS A FLOOR, NOT A BAN. The model still writes the summary
wherever it can do the job. On a two-party case "the job" now includes
naming both parties and stating an outcome for each — which is exactly
what the deterministic sentence always did, so the bar is "at least as
informative as the fallback", not an arbitrary hurdle.

A single-applicant case is untouched: one person, no ambiguity about
whose result it is, and the existing sentence never named them either.
"""

from __future__ import annotations

import pytest

from app.agents.los.summary import (
    build_summary,
    deterministic_summary,
    validate_llm_summary,
)


def envelope(party_kyc=None, *, status="PARTIAL", documents=5, kyc=None):
    body = {
        "documents": [{"status": "SUCCESS"}] * documents,
        "status": status,
        "kyc": kyc or {"status": "REVIEW", "reason_codes": ["NAME_MISMATCH"]},
    }
    if party_kyc:
        body["applicant_id"] = "APP-001"
        body["co_applicant_id"] = "COAPP-001"
        body["party_kyc"] = party_kyc
    return body


def party(status, *codes):
    return {"status": status, "reason_codes": list(codes)}


REVIEW_AND_PASS = {
    "APP-001": party("REVIEW", "DOB_MISMATCH", "FATHER_NAME_MISMATCH",
                     "NAME_MISMATCH"),
    "COAPP-001": party("PASS"),
}


# ==========================================================================
# 1-4. THE DETERMINISTIC SENTENCE
# ==========================================================================


def test_a_single_applicant_summary_is_unchanged():
    """1. The existing behaviour, preserved exactly."""
    sentence = deterministic_summary(envelope(
        status="SUCCESS", documents=1, kyc={"status": "PASS"}))

    assert sentence == ("1 document(s) processed (1 success). "
                        "Cross-document KYC checks passed. Overall SUCCESS.")


def test_a_review_and_a_pass_name_both_parties():
    """2. The case that started this."""
    sentence = deterministic_summary(envelope(REVIEW_AND_PASS))

    assert sentence == (
        "5 document(s) processed (5 success). "
        "Primary applicant KYC requires review: DOB, father name and name "
        "mismatch. Co-applicant KYC passed. Overall PARTIAL.")


def test_two_clean_parties_both_say_so():
    """3."""
    sentence = deterministic_summary(envelope(
        {"APP-001": party("PASS"), "COAPP-001": party("PASS")},
        status="SUCCESS", kyc={"status": "PASS"}))

    assert sentence == ("5 document(s) processed (5 success). "
                        "Primary applicant KYC passed. Co-applicant KYC "
                        "passed. Overall SUCCESS.")


def test_two_parties_under_review_are_reported_separately():
    """4. Each party's own reasons, not one merged list."""
    sentence = deterministic_summary(envelope({
        "APP-001": party("REVIEW", "NAME_MISMATCH"),
        "COAPP-001": party("REVIEW", "DOB_MISMATCH", "INSUFFICIENT_SOURCES"),
    }))

    assert "Primary applicant KYC requires review: name mismatch" in sentence
    assert ("Co-applicant KYC requires review: DOB mismatch and insufficient "
            "sources") in sentence


def test_a_party_whose_kyc_did_not_run_says_so():
    """
    Not "passed". A co-applicant who has uploaded nothing has not
    passed anything.
    """
    sentence = deterministic_summary(envelope({
        "APP-001": party("PASS"),
        "COAPP-001": {"status": "SKIPPED", "reason_codes":
                      ["INSUFFICIENT_SOURCES"]},
    }))

    assert "Co-applicant KYC was not run" in sentence


# ==========================================================================
# 9/10. WHAT THE SENTENCE MUST CARRY
# ==========================================================================


def test_the_affected_party_is_always_identifiable():
    """9."""
    sentence = deterministic_summary(envelope(REVIEW_AND_PASS))

    assert "Primary applicant" in sentence
    assert "Co-applicant" in sentence


@pytest.mark.parametrize("code, word", [
    ("DOB_MISMATCH", "DOB"),
    ("FATHER_NAME_MISMATCH", "father name"),
    ("NAME_MISMATCH", "name"),
    ("ADDRESS_MISMATCH", "address"),
])
def test_each_kyc_reason_category_reaches_the_sentence(code, word):
    """10."""
    sentence = deterministic_summary(envelope({
        "APP-001": party("REVIEW", code), "COAPP-001": party("PASS")}))

    assert word in sentence


def test_mismatches_are_listed_once_not_repeated():
    """
    "DOB mismatch, father name mismatch and name mismatch" says the
    word three times to no purpose.
    """
    sentence = deterministic_summary(envelope(REVIEW_AND_PASS))

    assert sentence.count("mismatch") == 1


def test_a_non_mismatch_reason_keeps_its_own_wording():
    sentence = deterministic_summary(envelope({
        "APP-001": party("REVIEW", "INSUFFICIENT_SOURCES"),
        "COAPP-001": party("PASS")}))

    assert "insufficient sources" in sentence
    assert "insufficient sources mismatch" not in sentence


def test_the_summary_carries_no_document_detail():
    sentence = deterministic_summary(envelope(REVIEW_AND_PASS))

    assert ".jpg" not in sentence
    assert ".pdf" not in sentence


# ==========================================================================
# 8. A VAGUE GENERATED SENTENCE CANNOT WIN
# ==========================================================================


TWO_PARTY = {
    "documents": [{"status": "SUCCESS"}] * 5, "status": "PARTIAL",
    "kyc": {"status": "REVIEW", "reason_codes": ["NAME_MISMATCH"]},
    "applicant_id": "APP-001", "co_applicant_id": "COAPP-001",
    "party_kyc": REVIEW_AND_PASS,
}


def test_the_vague_sentence_that_caused_this_is_refused():
    """8. Verbatim, from a real run."""
    accepted, reason = validate_llm_summary(
        "Loan officer review shows all documents except Kyc status as "
        "successful, with multiple Kyc reason codes noted.", TWO_PARTY)

    assert accepted is False
    assert "did not identify" in reason


def test_a_sentence_naming_neither_party_is_refused():
    accepted, _ = validate_llm_summary(
        "All five documents were processed and the case is PARTIAL pending "
        "review.", TWO_PARTY)

    assert accepted is False


def test_a_sentence_naming_only_one_party_is_refused():
    accepted, reason = validate_llm_summary(
        "The primary applicant KYC requires review.", TWO_PARTY)

    assert accepted is False
    assert "co-applicant" in reason


def test_naming_both_parties_without_outcomes_is_refused():
    """Naming somebody and saying nothing about them is the same omission."""
    accepted, reason = validate_llm_summary(
        "Primary applicant and co-applicant were both assessed today.",
        TWO_PARTY)

    assert accepted is False


def test_a_sentence_that_does_the_job_is_still_accepted():
    """
    A FLOOR, NOT A BAN. The model is not shut out -- it has to be at
    least as informative as the sentence it would replace.
    """
    accepted, cleaned = validate_llm_summary(
        "Primary applicant KYC requires review for a name mismatch; "
        "co-applicant KYC passed.", TWO_PARTY)

    assert accepted is True
    assert cleaned.startswith("Primary applicant")


def test_a_single_applicant_summary_is_not_held_to_the_party_rule():
    """
    One person, no ambiguity about whose result it is. Requiring a
    name here would reject every summary that works today.
    """
    accepted, _ = validate_llm_summary(
        "All documents verified and KYC checks passed.",
        {"documents": [{"status": "SUCCESS"}], "status": "SUCCESS",
         "kyc": {"status": "PASS"}})

    assert accepted is True


def test_the_existing_invention_checks_still_apply():
    """The new rule is added to the old ones, not instead of them."""
    accepted, reason = validate_llm_summary(
        "Primary applicant KYC passed and co-applicant KYC passed, 97 "
        "documents reviewed.", TWO_PARTY)

    assert accepted is False
    assert "97" in reason


# ==========================================================================
# 5-7/11. THE MODEL BEING THERE, OR NOT
# ==========================================================================


def test_the_deterministic_sentence_is_used_when_the_model_is_off():
    """6."""
    summary, source = build_summary(TWO_PARTY, use_llm=False)

    assert source == "deterministic"
    assert "Primary applicant KYC requires review" in summary


def test_a_model_failure_falls_back_rather_than_erroring():
    """7. A timeout is a summary problem, never a response problem."""
    from unittest.mock import patch

    with patch("app.agents.los.summary._generate",
               side_effect=TimeoutError("model timed out")):
        summary, source = build_summary(TWO_PARTY, use_llm=True)

    assert source == "deterministic"
    assert "Co-applicant KYC passed" in summary


def test_a_vague_model_answer_falls_back_to_the_contract_sentence():
    """5 and 8 together: the model answered, and was not good enough."""
    from unittest.mock import patch

    with patch("app.agents.los.summary._generate",
               return_value="Loan officer review shows all documents except "
                            "Kyc status as successful."):
        summary, source = build_summary(TWO_PARTY, use_llm=True)

    assert source == "deterministic"
    assert "Primary applicant" in summary
    assert "Co-applicant" in summary


def test_a_good_model_answer_is_still_used_and_labelled():
    """
    11. `summary_source` stays honest: it says who wrote the sentence,
    and a client showing it to a human needs to know.
    """
    from unittest.mock import patch

    with patch("app.agents.los.summary._generate",
               return_value="Primary applicant KYC requires review for a "
                            "name mismatch; co-applicant KYC passed."):
        summary, source = build_summary(TWO_PARTY, use_llm=True)

    assert source == "llm"
    assert summary.startswith("Primary applicant")


def test_the_single_applicant_model_path_is_untouched():
    """The model still writes single-applicant summaries as before."""
    from unittest.mock import patch

    single = {"documents": [{"status": "SUCCESS"}], "status": "SUCCESS",
              "kyc": {"status": "PASS"}}

    with patch("app.agents.los.summary._generate",
               return_value="All documents verified and KYC checks passed."):
        summary, source = build_summary(single, use_llm=True)

    assert source == "llm"
    assert summary == "All documents verified and KYC checks passed."


# ==========================================================================
# 12. IT COSTS NOTHING
# ==========================================================================


def test_the_sentence_stays_short():
    """A summary is one line, whichever path produced it."""
    sentence = deterministic_summary(envelope(REVIEW_AND_PASS))

    assert len(sentence) < 300


def test_four_reasons_do_not_run_away():
    sentence = deterministic_summary(envelope({
        "APP-001": party("REVIEW", "NAME_MISMATCH", "DOB_MISMATCH",
                         "FATHER_NAME_MISMATCH", "ADDRESS_MISMATCH",
                         "PAN_MISMATCH", "INSUFFICIENT_SOURCES"),
        "COAPP-001": party("PASS")}))

    assert len(sentence) < 300
    # Capped, so one very unhappy party cannot crowd out the other.
    assert "Co-applicant KYC passed" in sentence
