"""NATURAL STYLE (2026-10-15): a full rewrite is shown only when it says exactly the engine's facts."""
from __future__ import annotations

import pytest

from app.agents.applicant.copilot.answering import polish

PENDING = "Still pending:\n- Address Proof\n- Signature\n**Next step:** Clear these to move the case to CPA."
MIXED = "PAN (Permanent Account Number) is verified. Salary Slip is verified. Address Proof has not been uploaded."


@pytest.mark.parametrize("original, rewrite", [
    (PENDING, "Address Proof and Signature are still pending, so clear those to move the case to CPA."),
    (MIXED, "PAN and Salary Slip are verified, but Address Proof has not been uploaded yet."),
    (MIXED, "PAN and Salary Slip are done; Address Proof is still to upload."),
])
def test_a_faithful_rewrite_is_used(original, rewrite):
    assert polish.natural_accepted(original, rewrite)


@pytest.mark.parametrize("original, rewrite", [
    (PENDING, "PAN and Signature are still pending; clear them to move the case to CPA."),           # a document added
    (PENDING, "Signature is pending. Clear it to move the case to CPA."),                            # one dropped
    (PENDING, "Address Proof is verified but Signature is still pending; clear it to move to CPA."),  # wrong status
    (PENDING, "Both Address Proof and Signature are pending - clear them for CPA."),                 # a count word
    (MIXED, "PAN is verified while Salary Slip and Address Proof are pending."),                     # wrong status
])
def test_a_rewrite_that_changes_a_fact_is_refused(original, rewrite):
    assert not polish.natural_accepted(original, rewrite)


def test_the_facts_do_not_repeat_the_opener():
    from app.agents.applicant.copilot.answering.polish import without_echo

    facts = "Not ready for CPA yet. Still to do:\n- Address Proof\n- Signature"
    assert without_echo("Not ready for CPA yet; address and signature proofs missing.", facts) \
        == "Still to do:\n- Address Proof\n- Signature"
    # a first sentence that adds anything (here: "for CPA") stays
    assert without_echo("Not ready yet.", "- Not ready for CPA yet.") == "- Not ready for CPA yet."


def test_under_the_line_only_what_it_does_not_say():
    from app.agents.applicant.copilot.answering.polish import brief

    line = "Address Proof and Signature are still pending, so it is not ready for CPA yet."
    facts = ("- Application name matches the verified **PAN**: \"A\" = \"A\".\nStill pending:\n- Address Proof\n"
             "- Signature\n**Next step:** Clear these to move the case to CPA.")
    assert brief(line, facts) == "**Next step:** Clear these to move the case to CPA."
    # at most two points, then the Next line; a summary keeps the whole card
    many = "- Stage: FOS\n- Done: PAN\n- Loan: Personal Loan\n- Next: Upload it."
    assert brief("Almost there.", many) == "- Stage: FOS\n- Done: PAN\n- Next: Upload it."
    assert brief("Almost there.", many, "FULL_SUMMARY") == many


def test_a_document_named_twice_is_refused_and_first_follows_a_next_step():
    # 2026-10-24 live: "Address Proof, Signature, and Signature are still pending" was shown
    facts = ("- Address Proof and Signature are still pending, so it is not ready for CPA yet.\n"
             "**Next step:** Upload the Address Proof.")
    assert not polish.natural_accepted(facts, "Address Proof, Signature, and Signature are still pending, so it is "
                                              "not ready for CPA yet. Upload the Address Proof.")
    # "first" is the order the Next step already gives
    assert polish.natural_accepted(facts, "No, it can't go to CPA yet. Address Proof and Signature are still pending. "
                                          "Upload the Address Proof first.")
    # ...and still a count word with no Next step behind it
    assert not polish.natural_accepted("Pending: Address Proof, Signature", "Upload the Address Proof first, then "
                                                                            "the Signature.")
