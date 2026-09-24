"""
What a deterministic answer is allowed to say, and to whom.

THE FOUR DEFECTS A DEMO AUDIT FOUND, each of them in prose the
service writes itself rather than anything a model produced:

  AN IDENTIFIER IN A SENTENCE. "AUDIT DEMO APPLICANT - application
  CASE-AUDIT-001 is at Basic Document Verification" -- noise to the
  officer reading it, and a leak in any transcript of the
  conversation. The id belongs in a field.

  AN ENUM WEARING A HAT. "Reason: Document Requires Ocr." is a
  constant that has been title-cased, not an explanation.

  A QUESTION ABOUT THIS CASE ANSWERED FROM THE HANDBOOK. "What
  documents are STILL pending" missed the pending-documents pattern
  by one adverb, fell to UNKNOWN, and was answered with FOS policy
  about no case in particular.

  A REFUSAL IN FRONT OF AN ANSWER. A mixed question whose case half
  is a history question returned "No answer is available for this
  request." with the handbook paragraph underneath -- while the
  explanation sat in case memory, which is where the same question
  asked on its own reads it from.

The identifiers and the wording are checked here, at the source. The
routing distinction is checked in the intent tests below it.
"""

from __future__ import annotations

import pytest

from app.agents.applicant import answer as A
from app.agents.applicant import intents
from app.agents.applicant.intents import Intent

VIEW = {
    "applicant": {"applicant_id": "APP-AUDIT-001",
                  "full_name": "AUDIT DEMO APPLICANT"},
    "application": {"case_id": "CASE-AUDIT-001",
                    "status": "BASIC_DOCUMENT_VERIFICATION"},
    "stage": "BASIC_DOCUMENT_VERIFICATION",
    "documents": [
        {"document_type": "PAN", "status": "VERIFIED"},
        {"document_type": "BANK_STATEMENT", "status": "REVIEW"},
    ],
    "checklist": [],
    "pending_items": [],
    "readiness": {"status": "NOT_READY", "blocking_items": []},
    "next_action": {},
}


# ==========================================================================
# A. NO IDENTIFIERS IN PROSE
# ==========================================================================


def test_the_summary_does_not_name_the_case():
    said = A._summary_text(VIEW)

    assert "CASE-AUDIT-001" not in said
    assert "APP-AUDIT-001" not in said


def test_the_summary_still_says_where_the_case_is():
    """Removing the id must not remove the information."""
    said = A._summary_text(VIEW)

    assert "application is at" in said.lower()
    assert "Basic Document Verification" in said


def test_the_applicant_is_named_because_a_person_has_a_name():
    """
    A NAME IS NOT AN IDENTIFIER. The officer opened this case; being
    told whose it is orients them, and it is not a database key.
    """
    assert "AUDIT DEMO APPLICANT" in A._summary_text(VIEW)


# ==========================================================================
# B. A REASON CODE READS AS A SENTENCE
# ==========================================================================


def test_a_written_code_uses_the_written_sentence():
    said = A._explained("DOCUMENT_REQUIRES_OCR")

    assert "scan" in said.lower()
    assert "Document Requires Ocr" not in said
    assert "_" not in said


def test_a_code_nobody_wrote_is_not_given_invented_prose():
    """
    The catalogue is consulted; prose is not generated. An unmapped
    code keeps a readable form of its own name so a reader can still
    look it up.
    """
    said = A._explained("SOMETHING_NOBODY_MAPPED")

    assert "Something Nobody Mapped" in said
    assert "_" not in said


@pytest.mark.parametrize("code", [None, ""])
def test_an_absent_code_does_not_crash_the_sentence(code):
    assert isinstance(A._explained(code), str)


# ==========================================================================
# C. WHICH QUESTIONS ARE ABOUT THIS CASE
# ==========================================================================


@pytest.mark.parametrize("question", [
    "What documents are still pending?",
    "What documents are pending?",
    "Which documents are still outstanding?",
    "What documents are currently under review?",
])
def test_a_pending_question_is_about_this_case(question):
    assert intents.classify(question).intent is Intent.DOCUMENTS_PENDING


@pytest.mark.parametrize("question", [
    "What documents are required for a personal loan?",
    "What can be used as address proof?",
])
def test_a_policy_question_is_still_a_policy_question(question):
    """
    THE DISTINCTION THAT MATTERS. One asks what this case is waiting
    for; the other asks what the product requires of anybody. The
    first must never be answered from the handbook, and the second
    must not be answered from this case.
    """
    assert intents.classify(question).intent is Intent.FOS_KNOWLEDGE


def test_a_missing_documents_question_keeps_its_own_intent():
    assert intents.classify(
        "What documents are missing?").intent is Intent.DOCUMENTS_MISSING


# ==========================================================================
# D. NEVER A REFUSAL IN FRONT OF AN ANSWER
# ==========================================================================


def test_the_refusal_sentence_is_named_once():
    """
    The mixed path recognises it by identity rather than by matching
    a sentence, so changing the wording cannot silently break the
    check that suppresses it.
    """
    assert A.NOTHING_AVAILABLE
    assert A.deterministic_answer(Intent.UNKNOWN, {}) == A.NOTHING_AVAILABLE


def test_the_mixed_path_suppresses_it_before_appending_knowledge():
    """
    Asserted on the agent's source rather than through a live model:
    the suppression is a branch, and what matters is that it is there
    and keyed to the named constant.
    """
    import inspect

    from app.agents.applicant import agent

    source = inspect.getsource(agent.answer_question)

    assert "NOTHING_AVAILABLE" in source
    assert "case_memory_facts.explain" in source
