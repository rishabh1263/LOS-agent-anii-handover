"""
Answering the question that was asked, from the evidence that answers it.

THE ANSWER THIS EXISTS TO PREVENT. Asked "was the bank statement
successfully verified", the Copilot replied:

    "The address differs across the submitted documents, hence bank
     account details were not verified in this case."

The stored result for that document was PASS. Nothing was hallucinated:
retrieval returned what the case had, which for a case under review is
dominated by its problems, and the model was never shown the verdict.
It reasoned from the only evidence it had and reached a confident,
readable, wrong conclusion.

THREE THINGS FIX IT, AND ARE PINNED HERE:

  THE EVIDENCE IS NARROWED TO THE QUESTION. A question naming a
  document is answered from that document's evidence, not from the
  whole case.

  THE COMPUTED VERDICT IS SHOWN TO THE MODEL AND OUTRANKS IT. It goes
  into the prompt as ESTABLISHED, and a generated sentence that denies
  what it affirms is discarded in favour of the computed one.

  THE CONCEPTS STAY APART. Document verification, cross-document KYC,
  the case decision and stage guidance are four different facts. A
  document that passed is passed even while KYC is inconclusive --
  both are true, and collapsing them is what produced the wrong
  answer.

These use fake embeddings and no model: what is asserted is which
evidence is selected and which answer wins, all of which is decided
before a model is reached.
"""

from __future__ import annotations

import pytest

from app.knowledge import grounding
from app.knowledge.retrieval import Evidence, RetrievalResult

BANK_PASS = "Bank statement is VERIFIED."


def evidence(text, **provenance):
    return Evidence(text=text, score=0.9, provenance=provenance)


def context(case=(), process=()):
    return grounding.GroundedContext(
        case=RetrievalResult(evidence=tuple(case), sufficient=bool(case)),
        process=RetrievalResult(evidence=tuple(process),
                                sufficient=bool(process)),
    )


# ==========================================================================
# A. A VERDICT IS NEVER CONTRADICTED
# ==========================================================================


@pytest.mark.parametrize("generated", [
    "The address differs across the submitted documents, hence bank account "
    "details were not verified in this case.",
    "The bank statement failed verification.",
    "The bank statement could not be verified.",
    "Bank details were not verified.",
])
def test_a_denial_of_an_established_pass_is_refused(generated):
    """
    THE EXACT WRONG ANSWER, AND ITS NEIGHBOURS. The computed answer
    says the document verified; a generated sentence saying it did
    not is discarded rather than published.
    """
    assert grounding._denies(generated, BANK_PASS) is True


@pytest.mark.parametrize("generated", [
    "Yes. The bank statement was successfully verified.",
    "The bank statement was verified. Cross-document checks remain "
    "inconclusive because there is not enough comparable information.",
    "The bank statement is verified and the statement period was read.",
])
def test_an_answer_that_agrees_is_left_alone(generated):
    assert grounding._denies(generated, BANK_PASS) is False


def test_a_denial_is_allowed_when_the_record_does_not_affirm():
    """
    NOT A GENERAL FACT-CHECKER. Where the computed answer does not
    say the document passed, a negative sentence may be the honest
    one -- a statement under review genuinely was not verified.
    """
    assert grounding._denies(
        "The bank statement could not be verified automatically.",
        "Bank statement is REVIEW.") is False


@pytest.mark.parametrize("structured", ["", None, "No documents are on file."])
def test_nothing_is_refused_when_nothing_was_established(structured):
    assert grounding._denies("It was not verified.", structured or "") is False


# ==========================================================================
# B. THE EVIDENCE IS NARROWED TO THE QUESTION
# ==========================================================================


def test_a_document_question_sees_only_that_documents_evidence():
    from app.api.routes.copilot_api import _focused

    narrowed = _focused(context(case=[
        evidence("The bank statement verified.",
                 document_type="BANK_STATEMENT"),
        evidence("The address differs across documents.",
                 document_type="SALE_DEED"),
        evidence("A declared detail did not match.", document_type="PAN"),
    ]), "BANK_STATEMENT")

    texts = [item.text for item in narrowed.case.evidence]

    assert texts == ["The bank statement verified."]


def test_process_guidance_survives_the_narrowing():
    """A mixed question still needs it; it belongs to a stage, not a file."""
    from app.api.routes.copilot_api import _focused

    narrowed = _focused(
        context(case=[evidence("Bank statement verified.",
                               document_type="BANK_STATEMENT")],
                process=[evidence("RCU samples files.", stage="RCU")]),
        "BANK_STATEMENT")

    assert len(narrowed.process.evidence) == 1


def test_nothing_is_narrowed_away_when_nothing_matches():
    """
    A question naming a document the case does not hold would
    otherwise be answered from no evidence at all.
    """
    from app.api.routes.copilot_api import _focused

    original = context(case=[evidence("The address differs.",
                                      document_type="SALE_DEED")])

    narrowed = _focused(original, "PASSPORT")

    assert len(narrowed.case.evidence) == 1


# ==========================================================================
# C. THE SOURCES SUPPORT THE ANSWER
# ==========================================================================


def test_a_document_answer_does_not_cite_the_whole_case():
    from app.api.routes.copilot_api import _relevant

    kept = _relevant([
        {"type": "CASE_DOCUMENT", "document_type": "BANK_STATEMENT"},
        {"type": "CASE_FINDING", "document_type": "SALE_DEED",
         "reason_code": "ADDRESS_MISMATCH"},
        {"type": "CASE_FINDING", "document_type": "PAN",
         "reason_code": "PROFILE_MISMATCH"},
    ], "BANK_STATEMENT")

    assert [s["document_type"] for s in kept] == ["BANK_STATEMENT"]


def test_the_decision_and_the_guidance_are_kept():
    from app.api.routes.copilot_api import _relevant

    kept = _relevant([
        {"type": "CASE_DOCUMENT", "document_type": "BANK_STATEMENT"},
        {"type": "CASE_DECISION", "decision": "REVIEW"},
        {"type": "PROCESS_KNOWLEDGE", "stage": "RCU"},
        {"type": "CASE_FINDING", "document_type": "SALE_DEED"},
    ], "BANK_STATEMENT")

    assert {s["type"] for s in kept} == {
        "CASE_DOCUMENT", "CASE_DECISION", "PROCESS_KNOWLEDGE"}


def test_a_case_question_keeps_every_source():
    from app.api.routes.copilot_api import _relevant

    sources = [{"type": "CASE_FINDING", "document_type": "SALE_DEED"},
               {"type": "CASE_FINDING", "document_type": "PAN"}]

    assert _relevant(sources, None) == sources


# ==========================================================================
# D. THE FOUR CONCEPTS STAY APART
# ==========================================================================


def test_a_passing_document_and_inconclusive_kyc_can_both_be_true():
    """
    INSUFFICIENT_SOURCES is a statement about comparing documents to
    each other. It says nothing about whether any one of them
    verified, and must not be read as a failure of one.
    """
    answer = ("The bank statement was verified. Cross-document checks "
              "remain inconclusive because there is not enough comparable "
              "information from the other documents.")

    assert grounding._denies(answer, BANK_PASS) is False


def test_the_established_verdict_reaches_the_model():
    """
    It was absent from the payload, which is the whole reason the
    model had nothing to weigh the case findings against.
    """
    payload = grounding._payload(
        "was the bank statement verified?", {},
        context(case=[evidence("The address differs.",
                               document_type="SALE_DEED")]),
        established=BANK_PASS)

    assert payload["established"] == BANK_PASS


def test_the_prompt_says_the_verdict_is_authoritative():
    assert "ESTABLISHED" in grounding._SYSTEM
    assert "never contradict" in grounding._SYSTEM.lower()


# ==========================================================================
# E. A DOCUMENT TYPE IS A WORD, NOT AN ENUM
# ==========================================================================


@pytest.mark.parametrize("raw,expected", [
    ("The BANK_STATEMENT was verified.", "bank statement"),
    ("The SALE_DEED is under review.", "sale deed"),
    ("A DRIVING_LICENCE was uploaded.", "driving licence"),
    ("The VOTER_ID is on file.", "voter ID"),
])
def test_a_stored_type_is_rewritten_for_a_reader(raw, expected):
    said = grounding._readable(raw)

    assert expected in said
    assert "_" not in said


def test_pan_is_left_as_people_say_it():
    assert "PAN" in grounding._readable("The PAN is verified.")
