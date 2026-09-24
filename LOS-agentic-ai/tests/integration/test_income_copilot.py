"""
Income questions, and the case-specific mismatch question, end to end.

TWO ROUTING DEFECTS AND ONE VOCABULARY RULE.

  "WHAT EXACTLY IS THE MISMATCH IN MY DOCUMENTS?" went to the knowledge
  base and came back with a definition of the word -- to an officer
  looking at a case whose PAN and bank statement name two different
  people, with both names already recorded.

  "IS MY SALARY VERIFIED?" matched the generic <thing> + verified
  pattern and was answered from document verification, which says the
  salary slip is a readable, coherent document. That is not what was
  asked, and it is not what "verified" means about a salary.

  AND NO ANSWER INVENTS A SALARY. A slip STATES one; a statement
  EVIDENCES credits. Where only the statement exists the answer says
  recurring credits, whatever the figure looks like.
"""

from __future__ import annotations

import pytest

from app.agents.applicant.intents import Intent, classify

# ==========================================================================
# A / B. THE CASE'S MISMATCH, AND THE WORD'S DEFINITION
# ==========================================================================


@pytest.mark.parametrize("question", [
    "What exactly is the mismatch in my documents?",
    "What is wrong with my documents?",
    "Which details don't match?",
    "Which names don't match?",
    "Why is there a mismatch in my case?",
    "What issue was found in my documents?",
    "Why are my documents causing a review?",
    "What mismatch was found?",
])
def test_a_case_specific_mismatch_question_reads_the_case(question):
    assert classify(question).intent is Intent.CASE_HISTORY


@pytest.mark.parametrize("question", [
    "What does document mismatch mean?",
    "What is a document mismatch?",
    "How does document mismatch work?",
])
def test_a_generic_mismatch_question_reads_the_handbook(question):
    assert classify(question).intent is Intent.FOS_KNOWLEDGE


def test_widening_the_case_route_did_not_narrow_the_policy_route():
    """The boundary works both ways, or it is not a boundary."""
    for question in ("What documents are required for a personal loan?",
                     "What can be used as address proof?"):
        assert classify(question).intent is not Intent.CASE_HISTORY


# ==========================================================================
# L. THE EIGHT INCOME QUESTIONS
# ==========================================================================


@pytest.mark.parametrize("question", [
    "What salary is shown on my salary slip?",
    "What salary credits are visible in my bank statement?",
    "Does my bank statement support my salary?",
    "Does my salary slip match my bank statement?",
    "Is there any income mismatch?",
    "Why is my income under review?",
    "How much monthly income is evidenced?",
    "Is my salary verified?",
])
def test_an_income_question_is_answered_from_income_evidence(question):
    assert classify(question).intent is Intent.INCOME_EVIDENCE


def test_an_ordinary_verification_question_is_still_document_verification():
    """
    The income patterns run before the verification ones and had to stay
    narrow enough not to swallow them.
    """
    assert classify("Is the PAN verified?").intent is Intent.DOCUMENT_VERIFICATION
    assert classify(
        "Are my documents verified?").intent is Intent.DOCUMENT_VERIFICATION


def test_income_is_a_case_fact_and_needs_the_verification_scope():
    from app.agents.applicant.permissions import _READ_REQUIREMENT
    from app.agents.applicant.query_types import QueryType, type_for
    from app.agents.applicant.routing import QueryCategory, category_for

    assert type_for(Intent.INCOME_EVIDENCE) is QueryType.CASE_FACT
    assert category_for(Intent.INCOME_EVIDENCE) is QueryCategory.CASE_ONLY
    assert _READ_REQUIREMENT[Intent.INCOME_EVIDENCE] == "verification"


# ==========================================================================
# THE ANSWERS, FROM RECORDED EVIDENCE ONLY
# ==========================================================================

SLIP = {"figure": "NET_PAY", "amount": "50000.00", "pay_period": "MAY 2026"}
BANK = {"type": "RECURRING_CREDIT", "estimated_monthly_amount": "48500.00",
        "months_observed": 6.0, "confidence": 0.8, "recurring_credit_count": 6}


def memory(**income) -> dict:
    return {"findings": [{"finding_kind": "FINANCIAL", "status": income.get(
        "status"), "income": income}], "decisions": [], "timeline": []}


def test_both_sources_are_reported_as_what_each_one_is():
    from app.agents.applicant import income_facts

    said, sources = income_facts.answer(memory(
        status="PASS", reason_codes=["INCOME_CONSISTENT"],
        salary_slip=SLIP, bank_statement=BANK))

    assert "salary slip states a net salary of ₹50,000" in said
    assert "recurring credits of approximately ₹48,500" in said
    assert "within the configured comparison tolerance" in said
    assert sources


def test_a_difference_asks_for_review_and_accuses_nobody():
    from app.agents.applicant import income_facts

    said, _ = income_facts.answer(memory(
        status="REVIEW", reason_codes=["INCOME_AMOUNT_MISMATCH"],
        salary_slip=SLIP,
        bank_statement={**BANK, "estimated_monthly_amount": "32000.00"}))

    assert "₹50,000" in said and "₹32,000" in said
    assert "requires review" in said
    for word in ("fraud", "fake", "forged", "reject"):
        assert word not in said.lower()


def test_bank_evidence_alone_is_never_called_salary():
    """
    THE SENTENCE THIS FILE EXISTS FOR. Six identical monthly credits are
    still only credits, and "verified salary" is a claim the document
    cannot support.
    """
    from app.agents.applicant import income_facts

    said, _ = income_facts.answer(memory(
        status="SKIPPED", reason_codes=["SALARY_SLIP_MISSING"],
        bank_statement=BANK))

    assert "recurring credits of approximately ₹48,500" in said
    assert "No salary slip has been uploaded" in said
    assert "verified salary" not in said.lower()
    assert "your salary is" not in said.lower()


def test_a_labelled_credit_may_be_called_salary_because_the_bank_said_so():
    from app.agents.applicant import income_facts

    said, _ = income_facts.answer(memory(
        status="SKIPPED", reason_codes=["SALARY_SLIP_MISSING"],
        bank_statement={**BANK, "type": "SALARY_CREDIT"}))

    assert "recurring salary credits" in said


def test_a_slip_alone_states_rather_than_evidences():
    from app.agents.applicant import income_facts

    said, _ = income_facts.answer(memory(
        status="SKIPPED", reason_codes=["BANK_INCOME_EVIDENCE_MISSING"],
        salary_slip=SLIP))

    assert "The salary slip states a net salary of ₹50,000 for MAY 2026" in said
    assert "no recurring credit evidence" in said


def test_nothing_recorded_invents_no_figure():
    from app.agents.applicant import income_facts

    said, sources = income_facts.answer(
        {"findings": [], "decisions": [], "timeline": []})

    assert "No income evidence has been recorded" in said
    assert "₹" not in said
    assert sources == []


def test_a_skipped_income_check_does_not_explain_a_review():
    """
    An income comparison that never ran is not a reason the case stands
    anywhere, and neither is one that passed.
    """
    from app.agents.applicant import case_memory_facts

    for status, codes in (("SKIPPED", ["SALARY_SLIP_MISSING"]),
                          ("PASS", ["INCOME_CONSISTENT"])):
        said, _ = case_memory_facts.explain({
            "findings": [{"finding_kind": "FINANCIAL", "status": status,
                          "reason_codes": codes}],
            "decisions": [{"decision": "REVIEW", "status": "PARTIAL",
                           "reason_codes": []}],
            "timeline": [],
        })

        assert "No individual findings were recorded" in said
