"""
Every reason code says something a person can act on.

THE RULE: a REVIEW, FAIL or REJECTED carries both a code a queue can route
on and a sentence an officer can read. Before this, whether the sentence
existed depended entirely on which path produced the verdict -- bank
statements had prose, sale deeds had four codes and no prose at all, and
identity documents had prose only when the image was poor. A caller could
not rely on the field, so a UI could not render it.

WHY THE COMPLETENESS TEST ENUMERATES REAL EMITTERS. A hand-written list of
"codes we should cover" drifts the moment somebody adds one. These tests
import the enums and modules that actually produce codes and check the
catalogue against them, so a new code with no explanation fails here
rather than reaching a customer as a bare identifier.

PHASE 2B IS THE OTHER HALF. Seven outcomes must never be conflated, and
one of them caused real harm: a parser that ran out of time was reported
as BANK_STATEMENT_RECONCILIATION_INCONCLUSIVE, so "we were too slow" read
as "this customer's statement may not add up". The tests at the bottom
keep those seven apart.
"""

from __future__ import annotations

import importlib

import pytest

from app.agents.verification import reasons


def module_codes(module_name: str) -> set[str]:
    """Reason-code constants a module exports."""
    module = importlib.import_module(module_name)
    return {
        value for name, value in vars(module).items()
        if name.isupper() and isinstance(value, str)
        and value.isupper() and "_" in value and not name.startswith("_")
    }


def enum_codes(module_name: str, enum_name: str = "ReasonCode") -> set[str]:
    module = importlib.import_module(module_name)
    return {member.value for member in getattr(module, enum_name)}


#: Every producer of reason codes in the service, named rather than
#: grepped. Adding a producer means adding it here, which is the point:
#: the list is what makes the completeness test meaningful.
EMITTERS: dict[str, set[str]] = {
    "image quality": module_codes("app.agents.document_agent.quality"),
    "bank statement": module_codes(
        "app.agents.verification.bank_statement_checks"),
    "financial": module_codes("app.agents.verification.financial_checks"),
    "sale deed": enum_codes("app.agents.sale_deed.service"),
    "signature": enum_codes("app.agents.signature.schemas"),
    "business evidence": enum_codes("app.agents.business_evidence.schemas"),
}


def all_emitted() -> set[str]:
    from app.agents.verification import identity_checks

    codes: set[str] = set(identity_checks._CHECK_REASONS.values())
    for group in EMITTERS.values():
        codes |= group
    return codes


# ==========================================================================
# A. THE CATALOGUE IS COMPLETE
# ==========================================================================


@pytest.mark.parametrize("producer", sorted(EMITTERS))
def test_every_code_a_producer_emits_has_an_explanation(producer):
    """
    THE ONE THAT CATCHES THE NEXT GAP. A code with no entry still renders
    -- `derive` builds a sentence from the name -- but that sentence is
    shallow: "Party missing." where the document needed "The parties to
    this deed could not be read from it." The fallback is a safety net,
    not a plan, and this is what stops anything shipping on it.
    """
    missing = sorted(c for c in EMITTERS[producer] if not reasons.known(c))

    assert not missing, (
        f"{producer} emits {missing} with no entry in "
        f"app/agents/verification/reasons.py"
    )


def test_the_identity_checks_codes_are_covered():
    from app.agents.verification import identity_checks

    missing = sorted(c for c in identity_checks._CHECK_REASONS.values()
                     if not reasons.known(c))
    assert not missing


def test_every_catalogue_sentence_reads_like_a_sentence():
    for code, sentence in reasons.CATALOGUE.items():
        assert sentence.endswith("."), f"{code}: no full stop"
        assert sentence[0].isupper(), f"{code}: does not start capitalised"
        assert len(sentence) > 15, f"{code}: too terse to be useful"


def test_no_catalogue_sentence_leaks_an_internal():
    """
    These go in front of a customer-facing operator. A stack frame, a
    module path or a variable name in one is a leak.
    """
    for code, sentence in reasons.CATALOGUE.items():
        for internal in ("Traceback", ".py", "None", "cv2", "numpy",
                         "self.", "__", "0x"):
            assert internal not in sentence, f"{code} leaks {internal!r}"


def test_no_catalogue_sentence_asserts_something_unproven():
    """
    This service cannot establish that a document is forged, fake or
    fraudulent -- it has no issuer API. A sentence that says so would be
    an accusation the evidence does not support.
    """
    for code, sentence in reasons.CATALOGUE.items():
        lowered = sentence.lower()
        for overreach in ("forged", "fake", "fraudulent", "counterfeit",
                          "criminal", "lying"):
            assert overreach not in lowered, f"{code} overreaches: {sentence}"


# ==========================================================================
# B. THE FALLBACK
# ==========================================================================


def test_an_unknown_code_still_produces_a_sentence():
    assert reasons.explain("SOMETHING_NOBODY_CATALOGUED") == (
        "Something nobody catalogued."
    )


def test_the_fallback_never_returns_an_empty_string():
    for value in ("", None, "___", "X"):
        assert reasons.explain(value).strip()


def test_a_known_code_prefers_the_written_sentence():
    assert reasons.explain("PARTY_MISSING") != reasons.derive("PARTY_MISSING")
    assert reasons.known("PARTY_MISSING")


# ==========================================================================
# C. THE VERIFIER'S OWN WORDS WIN
# ==========================================================================


def test_a_supplied_sentence_is_never_overwritten():
    """
    A verifier that wrote prose saw the document; the catalogue did not.
    A generic line must never displace a specific one.
    """
    written = ["The closing balance on page 4 does not follow from page 3."]

    assert reasons.explain_all(
        ["BANK_STATEMENT_RECONCILIATION_FAILED"], existing=written,
    ) == written


def test_gaps_are_filled_when_nothing_was_written():
    filled = reasons.explain_all(["PARTY_MISSING", "CONSIDERATION_MISSING"])

    assert len(filled) == 2
    assert all(s.endswith(".") for s in filled)


def test_repeated_sentences_are_collapsed():
    """
    Two codes legitimately share a sentence. A response repeating it reads
    like two problems.
    """
    filled = reasons.explain_all(
        ["REQUIRED_FIELD_MISSING", "REQUIRED_FIELD_NOT_FOUND"])

    assert len(filled) == 1


def test_blank_supplied_sentences_do_not_count_as_supplied():
    filled = reasons.explain_all(["PARTY_MISSING"], existing=["", "   "])
    assert filled and filled[0].strip()


# ==========================================================================
# D. PHASE 2B -- THE SEVEN OUTCOMES STAY APART
# ==========================================================================


def test_all_seven_distinct_outcomes_are_catalogued():
    for code in reasons.DISTINCT_OUTCOMES:
        assert reasons.known(code), f"{code} has no written explanation"


def test_the_seven_outcomes_have_seven_different_sentences():
    """
    THE ONE THAT MATTERS MOST IN THIS FILE.

    "The parser ran out of time" and "this statement may not reconcile"
    are different findings with different operator responses -- one
    raises a budget or queues the document, the other sends a possible
    discrepancy to a human. They were reported identically once. A future
    edit that collapses any two of these fails here.
    """
    sentences = [reasons.explain(code)
                 for code in reasons.DISTINCT_OUTCOMES]

    assert len(set(sentences)) == len(reasons.DISTINCT_OUTCOMES), (
        "two of the distinct outcomes now say the same thing: "
        f"{sorted(sentences)}"
    )


def test_a_timeout_does_not_read_as_an_arithmetic_failure():
    timeout = reasons.explain("VERIFICATION_TIMEOUT").lower()

    for blame in ("do not add up", "does not add up", "not reconcile",
                  "inconsistent", "discrepan"):
        assert blame not in timeout, (
            f"a timeout is described as an arithmetic problem: {timeout}"
        )


def test_a_timeout_says_it_is_our_limitation():
    """
    An officer reading it has to know the document is probably fine, or
    they will go back to the customer for a replacement that changes
    nothing.
    """
    timeout = reasons.explain("VERIFICATION_TIMEOUT").lower()
    assert "limit of the service" in timeout or "not a finding" in timeout


def test_requires_ocr_is_not_the_same_as_unreadable():
    """
    A scan awaiting OCR is routable work. A corrupt file is not. Telling
    an officer to re-upload when the document only needed queueing wastes
    a customer visit.
    """
    assert (reasons.explain("DOCUMENT_REQUIRES_OCR")
            != reasons.explain("DOCUMENT_UNREADABLE"))


def test_requires_language_ocr_is_distinct_from_requires_ocr():
    """
    One needs a clearer scan; the other needs a script this build cannot
    read at all. Neither is a finding against the document.
    """
    language = reasons.explain("DOCUMENT_REQUIRES_LANGUAGE_OCR")

    assert language != reasons.explain("DOCUMENT_REQUIRES_OCR")
    assert "script" in language.lower()


def test_inconclusive_is_not_stated_as_failure():
    """
    The distinction the bank-statement path was fixed for: nobody did the
    arithmetic, so the statement did not fail it.
    """
    inconclusive = reasons.explain(
        "BANK_STATEMENT_RECONCILIATION_INCONCLUSIVE").lower()
    failed = reasons.explain("BANK_STATEMENT_RECONCILIATION_FAILED").lower()

    assert inconclusive != failed
    assert "could not be established" in inconclusive
    assert "do not add up" in failed


def test_an_empty_parse_is_not_a_timeout():
    assert (reasons.explain("BANK_STATEMENT_NO_TRANSACTIONS")
            != reasons.explain("VERIFICATION_TIMEOUT"))


def test_an_invalid_file_is_not_an_inconclusive_check():
    assert (reasons.explain("INVALID_DOCUMENT")
            != reasons.explain("VERIFICATION_INCONCLUSIVE"))
