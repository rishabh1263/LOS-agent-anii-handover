"""
Eligibility questions, answered from what the pipeline recorded.

THE LINE THIS FILE HOLDS. A FOIR is the figure in this system most
likely to be repeated back confidently and wrongly: it is a percentage,
it sounds like arithmetic anyone could redo, and redoing it from a chat
turn's context would produce a second answer for one applicant -- with
the one a reviewer saw depending on which response they happened to
open.

So the Copilot reads the recorded verdict and nothing else. Where no
verdict was recorded it says so, rather than assessing one to fill the
silence.
"""

from __future__ import annotations

import pytest

from app.agents.applicant import eligibility_facts
from app.agents.applicant.intents import Intent, classify

RECORDED = {
    "status": "PASS",
    "reason_codes": ["ELIGIBILITY_WITHIN_POLICY"],
    "foir": {"status": "PASS", "value_pct": 23.93, "limit_pct": 50.0},
    "ltv": {"status": "NOT_APPLICABLE"},
    "inputs": {
        "monthly_income": 50000.0, "income_source": "SALARY_SLIP_NET",
        "monthly_obligations": 2000.0, "obligations_source": "DECLARED",
        "loan_amount": 300000.0, "tenure_months": 36,
        "interest_rate_pct": 12.0, "proposed_emi": 9964.29,
    },
    "policy": {"id": "ELIGIBILITY_PERSONAL_LOAN", "version": "9.9",
               "status": "CONFIRMED", "source": "config"},
}


def memory(**overrides) -> dict:
    recorded = {**RECORDED, **overrides}
    return {"findings": [{"finding_kind": "FINANCIAL",
                          "status": recorded["status"],
                          "eligibility": recorded}],
            "decisions": [], "timeline": []}


# ==========================================================================
# ROUTING
# ==========================================================================


@pytest.mark.parametrize("question", [
    "What is my eligibility status?",
    "What is my FOIR?",
    "Why am I not eligible?",
    "Why is my eligibility under review?",
    "What income was used for eligibility?",
    "What obligations were considered?",
    "Was my eligibility checked?",
    "Is my application eligible based on the available information?",
])
def test_an_eligibility_question_reads_the_recorded_verdict(question):
    assert classify(question).intent is Intent.ELIGIBILITY


def test_the_neighbouring_intents_did_not_move():
    """
    The eligibility patterns run before income and before verification,
    and had to stay narrow enough not to swallow either.
    """
    assert classify("Is my salary verified?").intent is Intent.INCOME_EVIDENCE
    assert classify("Is the PAN verified?").intent is Intent.DOCUMENT_VERIFICATION
    assert classify("Why is this case in review?").intent is Intent.CASE_HISTORY


def test_eligibility_is_a_case_fact_needing_the_verification_scope():
    from app.agents.applicant.permissions import _READ_REQUIREMENT
    from app.agents.applicant.query_types import QueryType, type_for
    from app.agents.applicant.routing import QueryCategory, category_for

    assert type_for(Intent.ELIGIBILITY) is QueryType.CASE_FACT
    assert category_for(Intent.ELIGIBILITY) is QueryCategory.CASE_ONLY
    assert _READ_REQUIREMENT[Intent.ELIGIBILITY] == "verification"


# ==========================================================================
# THE ANSWERS
# ==========================================================================


def test_the_recorded_figures_are_reported_as_recorded():
    said, sources = eligibility_facts.answer(memory())

    assert said.startswith("Eligibility: PASS")
    assert "FOIR 23.93% (limit 50.0%) -- PASS" in said
    assert "₹50,000" in said
    assert "EMI ₹9,964" in said
    assert sources


def test_the_answer_is_short_and_structured():
    """One line each: verdict, ratios, basis, policy. Nothing more."""
    said, _ = eligibility_facts.answer(memory())

    assert said.count(". ") <= 5
    assert len(said) < 400


def test_the_income_provenance_is_never_dropped():
    """
    "The income used was 50,000" is half a fact. Whether it is an
    employer's stated salary or credits nobody labelled is the half a
    reviewer acts on.
    """
    said, _ = eligibility_facts.answer(memory())
    assert "net salary (salary slip)" in said

    said, _ = eligibility_facts.answer(memory(inputs={
        **RECORDED["inputs"], "income_source": "BANK_RECURRING_CREDIT"}))
    assert "recurring bank credits" in said
    # Credits nobody labelled are never called a salary.
    assert "salary" not in said.split("Based on")[1]


def test_declared_obligations_are_said_to_be_declared():
    said, _ = eligibility_facts.answer(memory())

    assert "₹2,000 obligations (declared)" in said


def test_a_review_says_which_recorded_reason_caused_it():
    said, _ = eligibility_facts.answer(memory(
        status="REVIEW",
        reason_codes=["FOIR_ABOVE_THRESHOLD"],
        foir={"status": "REVIEW", "value_pct": 63.27, "limit_pct": 50.0}))

    assert said.startswith("Eligibility: REVIEW")
    assert "more of the monthly income than policy allows" in said
    assert "FOIR 63.27% (limit 50.0%) -- REVIEW" in said


def test_an_unassessed_case_says_so_and_invents_nothing():
    said, sources = eligibility_facts.answer(
        {"findings": [], "decisions": [], "timeline": []})

    assert said == eligibility_facts.NOT_ASSESSED
    assert "%" not in said
    assert "₹" not in said
    assert sources == []


def test_an_absence_is_named_rather_than_summarised():
    said, _ = eligibility_facts.answer(memory(
        status="SKIPPED",
        reason_codes=["OBLIGATIONS_NOT_CAPTURED", "EMI_INPUTS_MISSING"],
        foir={"status": "SKIPPED", "limit_pct": 50.0},
        inputs={"monthly_income": 50000.0, "income_source": "SALARY_SLIP_NET"}))

    assert said.startswith("Eligibility: SKIPPED")
    assert "already repays each month" in said
    assert "tenure or the interest rate" in said
    assert "FOIR: not computed" in said
    assert "%" not in said, "a ratio was quoted for a case that computed none"


def test_an_unapproved_threshold_is_declared_as_one():
    """
    A percentage read against a limit nobody signed off must not reach an
    officer looking like a lending rule.
    """
    said, _ = eligibility_facts.answer(memory(
        policy={"id": "PL_X", "version": "1", "status": "UNCONFIRMED"}))

    assert "not yet signed-off policy" in said


def test_a_demo_policy_is_named_as_one():
    said, _ = eligibility_facts.answer(memory(
        policy={"id": "PL_DUMMY_V1", "version": "1.0", "status": "DEMO_NON_PRODUCTION"}))

    assert "PL_DUMMY_V1 is a demonstration policy, not company lending policy" in said


def test_ltv_is_reported_when_the_product_has_collateral():
    said, _ = eligibility_facts.answer(memory(
        ltv={"status": "PASS", "value_pct": 66.67, "limit_pct": 80.0},
        inputs={**RECORDED["inputs"], "property_value": 3000000.0,
                "property_value_source": "DECLARED"}))

    assert "LTV 66.67% (limit 80.0%) -- PASS" in said
    assert "property ₹3,000,000 (declared)" in said


def test_a_pass_is_not_cluttered_with_what_was_absent():
    """
    Listing the checks that did not run beside a pass reads as a warning
    about it.
    """
    said, _ = eligibility_facts.answer(memory())

    assert "loan-to-value" not in said.lower()


# ==========================================================================
# THE CHATBOT COMPUTES NOTHING
# ==========================================================================


def test_the_module_performs_no_arithmetic_on_money():
    """
    Read, format, return. A FOIR recomputed here would be computed from
    whatever was readable at the moment somebody asked.
    """
    from pathlib import Path

    source = Path("app/agents/applicant/eligibility_facts.py").read_text(
        encoding="utf-8")
    code = "\n".join(line for line in source.splitlines()
                     if not line.strip().startswith("#"))

    for forbidden in ("foir_pct(", "emi(", " / ", " * 100"):
        assert forbidden not in code, f"arithmetic in a phrasing layer: {forbidden}"


def test_the_answer_is_identical_with_no_model_available(monkeypatch):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    without = eligibility_facts.answer(memory())

    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "true")
    with_model = eligibility_facts.answer(memory())

    assert without == with_model


def test_the_read_tool_reads_and_does_not_assess():
    from app.mcp.contracts import CONTRACTS

    contract = CONTRACTS["eligibility.get"]

    assert contract.writes is False
    assert contract.audited is True
    assert contract.scope_key == "verification"
    assert "IT COMPUTES NOTHING" in contract.summary


def test_no_tool_exposes_the_arithmetic():
    """
    A callable `calculate_foir` would let a caller assemble its own
    inputs and produce a second FOIR for one applicant.
    """
    from app.mcp.applicant import ALL_TOOLS

    for forbidden in ("calculate_foir", "calculate_emi",
                      "evaluate_eligibility", "eligibility.evaluate"):
        assert forbidden not in ALL_TOOLS
