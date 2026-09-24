"""
What the income documents evidence, and what they are never made to say.

THE ONE RULE THIS WHOLE FILE DEFENDS. A salary slip STATES a salary; a
bank statement EVIDENCES credits arriving. No amount of regularity turns
the second into the first, because a statement records that money came
and never why: rent, a transfer between one's own accounts, a parent's
monthly help and a payroll credit are the same row.

So a recurring credit is called salary in exactly one circumstance --
the bank itself printed a payroll token against it -- and a difference
between the two documents is a REVIEW, never a fraud finding. Salary
slips and statements disagree for ordinary reasons: a mid-month joining,
reimbursements paid separately, a deduction at source, a revision between
the slip's month and the statement's, a second account.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.agents.bank_statement.income import income_evidence
from app.agents.bank_statement.schemas import (
    BankStatementResult,
    ExtractionStatus,
    SourceKind,
    StatementPeriod,
    Transaction,
)
from app.agents.income import config as income_config
from app.agents.income import consistency


def credit(day: str, amount: str, narration: str = "NEFT CR FROM ACME") -> Transaction:
    return Transaction(date=date.fromisoformat(day), narration=narration,
                       credit=Decimal(amount), balance=Decimal("100000"))


def debit(day: str, amount: str, narration: str = "UPI/DR/SHOP") -> Transaction:
    return Transaction(date=date.fromisoformat(day), narration=narration,
                       debit=Decimal(amount), balance=Decimal("90000"))


def statement(rows: list[Transaction], *, months: float = 6.0,
              reconciles: bool = True) -> BankStatementResult:
    return BankStatementResult(
        status=ExtractionStatus.SUCCESS,
        source_kind=SourceKind.DIGITAL,
        transactions=rows,
        balance_reconciles=reconciles,
        period=StatementPeriod(start=date(2026, 1, 1), end=date(2026, 6, 30),
                               months_covered=months),
    )


SIX_SALARY_MONTHS = [
    credit("2026-01-31", "50000", "NEFT CR SALARY ACME LTD"),
    credit("2026-02-28", "50000", "NEFT CR SALARY ACME LTD"),
    credit("2026-03-31", "49500", "NEFT CR SALARY ACME LTD"),
    credit("2026-04-30", "50000", "NEFT CR SALARY ACME LTD"),
    credit("2026-05-31", "50500", "NEFT CR SALARY ACME LTD"),
    credit("2026-06-30", "50000", "NEFT CR SALARY ACME LTD"),
]

SIX_UNLABELLED_MONTHS = [
    credit("2026-01-31", "50000", "IMPS CR 5219 RAHUL K"),
    credit("2026-02-28", "50000", "IMPS CR 5219 RAHUL K"),
    credit("2026-03-31", "49500", "IMPS CR 5219 RAHUL K"),
    credit("2026-04-30", "50000", "IMPS CR 5219 RAHUL K"),
    credit("2026-05-31", "50500", "IMPS CR 5219 RAHUL K"),
    credit("2026-06-30", "50000", "IMPS CR 5219 RAHUL K"),
]


@pytest.fixture(autouse=True)
def _policy():
    """The shipped policy, read fresh, for every test in this module."""
    income_config.reset_policy_cache()
    yield
    income_config.reset_policy_cache()


# ==========================================================================
# C. RECURRING CREDITS, READ OFF THE STATEMENT
# ==========================================================================


def test_a_credit_the_bank_labelled_salary_is_reported_as_salary():
    """The bank said it. Reporting what the document says is not inference."""
    evidence = income_evidence(statement(SIX_SALARY_MONTHS))

    assert evidence["type"] == "SALARY_CREDIT"
    assert evidence["estimated_monthly_amount"] == "50000.00"
    assert evidence["recurring_credit_count"] == 6
    assert evidence["source"] == "BANK_STATEMENT"


def test_an_unlabelled_credit_is_recurring_and_not_salary():
    """
    THE DEFAULT, AND THE POINT OF THE MODULE. Six identical credits from
    one sender every month look exactly like a salary and may be rent.
    """
    evidence = income_evidence(statement(SIX_UNLABELLED_MONTHS))

    assert evidence["type"] == "RECURRING_CREDIT"
    assert evidence["estimated_monthly_amount"] == "50000.00"


def test_one_credit_is_never_a_pattern():
    evidence = income_evidence(statement([credit("2026-01-31", "50000")]))

    assert evidence["type"] == "UNKNOWN"
    assert evidence["estimated_monthly_amount"] is None
    assert evidence["recurring_credit_count"] == 0


def test_two_credits_in_one_month_are_not_a_monthly_income():
    """One event happening twice in a week is not a pattern across months."""
    evidence = income_evidence(statement([
        credit("2026-01-05", "50000"), credit("2026-01-09", "50000"),
    ]))

    assert evidence["type"] == "UNKNOWN"


def test_a_statement_that_did_not_reconcile_evidences_nothing():
    """
    Figures computed from rows known to be wrong are worse than none --
    the same view `signals.derive` takes, for the same reason.
    """
    assert income_evidence(
        statement(SIX_SALARY_MONTHS, reconciles=False)) is None
    assert income_evidence(statement([])) is None


# ==========================================================================
# J. RECURRING IS NOT THE SAME AS INCOME
# ==========================================================================


def test_frequent_small_credits_do_not_become_the_applicants_income():
    """
    FOUND ON A REAL STATEMENT. The largest cluster of identical credits
    was thirty-five UPI receipts of around 500 rupees across six months,
    and on another statement thirty-five 2-rupee refunds. Both recur.
    Ranking by count alone handed the answer to the noise.
    """
    noise = [credit(f"2026-0{m}-{d:02d}", "500", "UPI/CR/PAYTM")
             for m in range(1, 7) for d in (3, 8, 13, 18, 23)]

    evidence = income_evidence(statement(noise + SIX_UNLABELLED_MONTHS))

    assert evidence["estimated_monthly_amount"] == "50000.00"
    assert evidence["recurring_credit_count"] == 6


def test_debits_are_not_income():
    evidence = income_evidence(statement(
        SIX_UNLABELLED_MONTHS
        + [debit(f"2026-0{m}-15", "70000") for m in range(1, 7)]))

    assert evidence["estimated_monthly_amount"] == "50000.00"


# ==========================================================================
# K. NO FABRICATED SALARY, AND NO FABRICATED EMPLOYER
# ==========================================================================


def test_nothing_in_the_evidence_claims_a_salary_figure():
    evidence = income_evidence(statement(SIX_UNLABELLED_MONTHS))

    assert "salary" not in str(evidence).lower() or evidence["type"] != "SALARY_CREDIT"
    assert "employer" not in evidence
    assert "employer_name" not in evidence


def test_the_evidence_rows_are_what_the_statement_printed():
    """
    Date, amount and the bank's own narration -- fields the parser read,
    the same as any other. No OCR text, no tokens, no coordinates.
    """
    evidence = income_evidence(statement(SIX_SALARY_MONTHS))

    assert {k for row in evidence["evidence"] for k in row} == {
        "date", "amount", "narration"}


def test_money_leaves_the_module_as_a_string():
    """
    The structure is written to case memory and to the API response, and
    both are JSON. A Decimal is not serialisable in either; a float
    would round money.
    """
    evidence = income_evidence(statement(SIX_SALARY_MONTHS))

    assert isinstance(evidence["estimated_monthly_amount"], str)
    assert all(isinstance(r["amount"], str) for r in evidence["evidence"])


# ==========================================================================
# THE POLICY IS THE POLICY'S
# ==========================================================================


def test_the_tolerance_comes_from_configuration(monkeypatch):
    """No threshold in the comparison logic. Widen it and behaviour follows."""
    slip = {"net_pay": "50000", "pay_period": "MAY 2026"}
    bank = {"type": "RECURRING_CREDIT", "estimated_monthly_amount": "40000",
            "months_observed": 6.0, "confidence": 0.9,
            "recurring_credit_count": 6}

    assert consistency.check(bank_evidence=bank,
                             salary_slip=slip)["status"] == consistency.REVIEW

    monkeypatch.setattr(income_config, "amount_tolerance", lambda: 0.30)

    assert consistency.check(bank_evidence=bank,
                             salary_slip=slip)["status"] == consistency.PASS


# ==========================================================================
# E, F, G, H, I. THE COMPARISON
# ==========================================================================

SLIP = {"net_pay": "50000.00", "gross_earnings": "60000.00",
        "pay_period": "MAY 2026"}
BANK = {"type": "RECURRING_CREDIT", "estimated_monthly_amount": "48500.00",
        "months_observed": 6.0, "confidence": 0.8, "recurring_credit_count": 6}


def test_g_both_sources_within_tolerance_pass():
    result = consistency.check(bank_evidence=BANK, salary_slip=SLIP)

    assert result["status"] == consistency.PASS
    assert consistency.INCOME_CONSISTENT in result["reason_codes"]
    assert result["difference"] == "1500.00"


def test_a_pass_still_says_the_credits_were_not_labelled():
    """
    A reviewer reading a PASS is entitled to know the agreement rests on
    a credit nobody called payroll.
    """
    result = consistency.check(bank_evidence=BANK, salary_slip=SLIP)

    assert consistency.SALARY_CREDIT_NOT_IDENTIFIED in result["reason_codes"]

    labelled = consistency.check(
        bank_evidence={**BANK, "type": "SALARY_CREDIT"}, salary_slip=SLIP)

    assert labelled["reason_codes"] == [consistency.INCOME_CONSISTENT]


def test_h_both_sources_outside_tolerance_review():
    result = consistency.check(
        bank_evidence={**BANK, "estimated_monthly_amount": "32000.00"},
        salary_slip=SLIP)

    assert result["status"] == consistency.REVIEW
    assert consistency.INCOME_AMOUNT_MISMATCH in result["reason_codes"]


def test_a_difference_is_never_called_fraud():
    """
    THE WORD THIS CHECK MUST NEVER PRODUCE. Two documents disagreeing
    about income is a question for a person, not an accusation, and the
    verdict vocabulary is deliberately the same three words the rest of
    the pipeline uses.
    """
    result = consistency.check(
        bank_evidence={**BANK, "estimated_monthly_amount": "5000.00"},
        salary_slip=SLIP)

    assert result["status"] in {consistency.PASS, consistency.REVIEW,
                                consistency.SKIPPED}
    said = str(result).upper()
    for word in ("FRAUD", "FAKE", "REJECT", "FORGED", "CREDIT_RISK"):
        assert word not in said


def test_e_bank_evidence_alone_is_skipped_not_failed():
    """An absent salary slip is a document nobody uploaded, not a finding."""
    result = consistency.check(bank_evidence=BANK, salary_slip=None)

    assert result["status"] == consistency.SKIPPED
    assert result["reason_codes"] == [consistency.SALARY_SLIP_MISSING]
    assert result["bank_statement"]["estimated_monthly_amount"] == "48500.00"


def test_f_a_salary_slip_alone_is_skipped_and_keeps_its_figure():
    result = consistency.check(bank_evidence=None, salary_slip=SLIP)

    assert result["status"] == consistency.SKIPPED
    assert result["reason_codes"] == [consistency.BANK_INCOME_EVIDENCE_MISSING]
    assert result["salary_slip"]["amount"] == "50000.00"


def test_i_too_little_statement_is_insufficient_history():
    """
    CHECKED BEFORE THE AMOUNTS. A close match over one month is a
    coincidence and a distant one is not yet a disagreement.
    """
    result = consistency.check(
        bank_evidence={**BANK, "months_observed": 1.0, "confidence": 0.9},
        salary_slip=SLIP)

    assert result["status"] == consistency.REVIEW
    assert result["reason_codes"] == [consistency.INCOME_INSUFFICIENT_HISTORY]


def test_weak_evidence_is_reviewed_rather_than_passed():
    result = consistency.check(
        bank_evidence={**BANK, "confidence": 0.2}, salary_slip=SLIP)

    assert result["reason_codes"] == [consistency.INCOME_INSUFFICIENT_HISTORY]


def test_a_slip_with_no_readable_figure_is_not_comparable():
    result = consistency.check(bank_evidence=BANK,
                               salary_slip={"pay_period": "MAY 2026"})

    assert result["status"] == consistency.SKIPPED
    assert result["reason_codes"] == [consistency.INCOME_NOT_COMPARABLE]


def test_neither_document_is_two_absences():
    result = consistency.check(bank_evidence=None, salary_slip=None)

    assert result["status"] == consistency.SKIPPED
    assert set(result["reason_codes"]) == {
        consistency.SALARY_SLIP_MISSING,
        consistency.BANK_INCOME_EVIDENCE_MISSING}


def test_net_pay_is_what_the_credits_are_compared_against():
    """
    Gross never arrives anywhere. Comparing credits against it would
    report a mismatch on every correctly paid salary.
    """
    result = consistency.check(bank_evidence=BANK, salary_slip=SLIP)

    assert result["salary_slip"]["figure"] == "NET_PAY"
    assert result["salary_slip"]["amount"] == "50000.00"


def test_the_compared_figure_is_configurable(monkeypatch):
    monkeypatch.setattr(income_config, "compare_against", lambda: "GROSS")

    result = consistency.check(bank_evidence=BANK, salary_slip=SLIP)

    assert result["salary_slip"]["amount"] == "60000.00"


# ==========================================================================
# THE COMPARISON, WHERE THE FLOW ASSEMBLES IT
# ==========================================================================

VERIFIED = {"status": "PASS"}

BANK_DOCUMENT = {
    "document": {"type": "BANK_STATEMENT"}, "source_id": "b.pdf",
    "verification": VERIFIED,
    "extraction": {"fields": {"income_evidence": {
        "type": "RECURRING_CREDIT", "estimated_monthly_amount": "48500.00",
        "months_observed": 6.0, "confidence": 0.8,
        "recurring_credit_count": 6}}},
}

SLIP_DOCUMENT = {
    "document": {"type": "SALARY_SLIP"}, "source_id": "s.pdf",
    "verification": VERIFIED,
    "extraction": {"fields": {
        "signals": {"monthly_net_salary": "50000.00",
                    "monthly_gross_salary": "60000.00"},
        "detail": {"pay_period": "MAY 2026"}}},
}


def test_the_flow_compares_a_partys_own_two_documents():
    from app.agents.los.flow import _income_consistency_for

    result = _income_consistency_for([BANK_DOCUMENT, SLIP_DOCUMENT])

    assert result["status"] == consistency.PASS
    assert result["salary_slip"]["amount"] == "50000.00"
    assert result["bank_statement"]["estimated_monthly_amount"] == "48500.00"
    assert result["difference"] == "1500.00"


def test_a_figure_the_gate_withheld_is_withheld_from_the_comparison():
    """
    THE SAME GATE, A FOURTH CONSUMER. `released_extraction` decides what
    leaves the building; a comparison reading the internal envelope
    instead would compare figures the caller was never shown -- which is
    exactly the defect profile matching and cross-document KYC each had
    once.
    """
    from app.agents.los.flow import _income_consistency_for

    withheld = [{**document, "verification": {"status": "REVIEW"}}
                for document in (BANK_DOCUMENT, SLIP_DOCUMENT)]

    result = _income_consistency_for(withheld)

    assert result["status"] == consistency.SKIPPED
    assert set(result["reason_codes"]) == {
        consistency.SALARY_SLIP_MISSING,
        consistency.BANK_INCOME_EVIDENCE_MISSING}


def test_the_response_publishes_the_primary_applicants_comparison():
    """
    A co-applicant's slip is never compared against the applicant's
    statement -- the two-party defect cross-document KYC already
    learned. Each party's comparison is computed over their own
    documents, and the response publishes the primary's.
    """
    from app.agents.los.flow import _public_income_consistency

    published = _public_income_consistency({
        "applicant_id": "APP-1",
        "party_income": {"APP-1": {"status": "PASS", "reason_codes": []},
                         "COAPP-9": {"status": "REVIEW", "reason_codes": []}},
    })

    assert published["status"] == "PASS"


# ==========================================================================
# ONE INCOME FIGURE, RELEASED BY ONE POLICY
# ==========================================================================
#
# THE LIVE DEFECT. A case whose statement covered 0.7 months and whose
# recurring-credit evidence was empty -- type UNKNOWN, count 0,
# confidence 0 -- still produced a cross-document INCOME verdict of
# REVIEW with INCOME_VARIANCE_HIGH, comparing the salary slip's 358,392
# against the statement's 665,228. The 665,228 was
# `average_monthly_credit` annualised: every credit that arrived,
# divided by the months covered and multiplied by twelve. Transfers,
# refunds and reimbursements are in that number, so it sits above a
# salary by construction, and the applicant was sent to review over an
# arithmetic artefact while the new income check -- correctly -- said
# SKIPPED, BANK_INCOME_EVIDENCE_MISSING on the same response.
#
# THE RULE THIS PINS. A bank statement carries a comparable income
# figure only where `income_evidence` released one, and the figure IS
# the one it released. There is no second income calculation.


def document(document_type: str, fields: dict) -> dict:
    return {"document": {"type": document_type},
            "extraction": {"fields": fields},
            "verification": {"status": "PASS"}}


LIVE_SLIP = document("SALARY_SLIP", {
    "name": "RISHABH AJIT SINGH",
    "signals": {"monthly_net_salary": "29866.00",
                "monthly_gross_salary": "31986.00"}})

#: The live statement: plenty of credits, no recurring pattern.
LIVE_BANK = document("BANK_STATEMENT", {
    "name": "PRIYANKAROHANMORE",
    "signals": {"average_monthly_credit": "55435.71",
                "total_credits": "38805.00", "months_covered": 0.7},
    "income_evidence": {"source": "BANK_STATEMENT", "type": "UNKNOWN",
                        "estimated_monthly_amount": None,
                        "recurring_credit_count": 0, "months_observed": 0.7,
                        "confidence": 0.0, "evidence": []}})

REAL_EVIDENCE = {"source": "BANK_STATEMENT", "type": "RECURRING_CREDIT",
                 "estimated_monthly_amount": "29500.00",
                 "recurring_credit_count": 6, "months_observed": 6.0,
                 "confidence": 0.8,
                 "evidence": [{"date": "2026-01-31", "amount": "29500.00",
                               "narration": "IMPS CR 5219"}]}


def kyc_sources(*documents):
    from app.agents.los.mapping import to_kyc_source

    return [to_kyc_source(d, f"{i}.pdf") for i, d in enumerate(documents)]


def test_1_a_statement_with_no_recurring_evidence_carries_no_income():
    from app.agents.kyc.checks import check_income
    from app.agents.kyc.schemas import CheckStatus, ReasonCode

    result = check_income(kyc_sources(LIVE_SLIP, LIVE_BANK))

    assert result.status is CheckStatus.SKIPPED
    assert result.reason_codes == [ReasonCode.INCOME_SINGLE_SOURCE]


def test_2_the_live_case_produces_no_income_variance_finding():
    from app.agents.kyc.checks import check_income
    from app.agents.kyc.schemas import ReasonCode

    result = check_income(kyc_sources(LIVE_SLIP, LIVE_BANK))

    assert ReasonCode.INCOME_VARIANCE_HIGH not in result.reason_codes
    assert ReasonCode.INCOME_INCONSISTENT not in result.reason_codes


def test_3_an_average_of_every_credit_is_not_salary_evidence():
    """
    THE FIGURE ITSELF IS THE DEFECT, not the threshold it crossed.
    55,435 a month is in the statement and is not income evidence:
    nothing says what those credits were.
    """
    from app.agents.los.mapping import _comparable_bank_income, to_kyc_source

    fields = LIVE_BANK["extraction"]["fields"]

    assert _comparable_bank_income(fields) is None

    source = to_kyc_source(LIVE_BANK, "bank.pdf")
    carried = str(source.income.average_monthly_credit if source.income else "")

    assert "55435" not in carried


def test_4_released_recurring_evidence_does_reach_the_comparison():
    """
    The check is narrowed, not switched off. A statement that DOES
    evidence a recurring credit is compared as it always was.
    """
    from app.agents.kyc.checks import check_income
    from app.agents.kyc.schemas import CheckStatus

    bank = document("BANK_STATEMENT", {
        "signals": {"average_monthly_credit": "55435.71"},
        "income_evidence": REAL_EVIDENCE})

    result = check_income(kyc_sources(LIVE_SLIP, bank))

    assert result.status is CheckStatus.PASS
    assert "1 income pair(s) compared" in result.detail

    # AND THE FIGURE COMPARED IS THE ONE THE EVIDENCE RELEASED.
    source = kyc_sources(bank)[0]
    assert str(source.income.average_monthly_credit) == "29500.00"


def test_5_a_genuine_gap_still_reports_high_variance():
    from app.agents.kyc.checks import check_income
    from app.agents.kyc.schemas import CheckStatus, ReasonCode

    bank = document("BANK_STATEMENT", {
        "signals": {"average_monthly_credit": "55435.71"},
        "income_evidence": {**REAL_EVIDENCE,
                            "estimated_monthly_amount": "15000.00"}})

    result = check_income(kyc_sources(LIVE_SLIP, bank))

    assert result.status is CheckStatus.REVIEW
    assert ReasonCode.INCOME_VARIANCE_HIGH in result.reason_codes


def test_6_the_name_mismatch_is_untouched_by_any_of_this():
    """
    Identity and income are separate checks with separate policies. The
    live case is in review because the PAN and the bank account name two
    people, and that must survive the income narrowing exactly as it is.
    """
    from app.agents.kyc.checks import check_name
    from app.agents.kyc.schemas import CheckStatus, ReasonCode

    result = check_name(kyc_sources(LIVE_SLIP, LIVE_BANK))

    assert result.status in (CheckStatus.FAIL, CheckStatus.REVIEW)
    assert ReasonCode.NAME_MISMATCH in result.reason_codes


def test_7_the_two_income_paths_read_one_figure():
    """
    NO SECOND CALCULATION. Whatever the cross-document check compares on
    the bank side is the value `income_evidence` produced -- not a
    figure derived from the same rows a second way.
    """
    from app.agents.los.mapping import _comparable_bank_income

    bank = document("BANK_STATEMENT", {
        "signals": {"average_monthly_credit": "55435.71"},
        "income_evidence": REAL_EVIDENCE})

    released = _comparable_bank_income(bank["extraction"]["fields"])
    compared = consistency.check(
        bank_evidence=REAL_EVIDENCE,
        salary_slip={"net_pay": "29866.00"})["bank_statement"][
            "estimated_monthly_amount"]

    assert released == REAL_EVIDENCE["estimated_monthly_amount"]
    assert released == compared


@pytest.mark.parametrize("broken", [
    {"type": "UNKNOWN"},
    {"recurring_credit_count": 0},
    {"confidence": 0.0},
    {"evidence": []},
])
def test_each_weakness_alone_withholds_the_figure(broken):
    """
    Four conditions, each sufficient on its own -- a structure that
    fails any one of them is not evidence of income.
    """
    from app.agents.los.mapping import _comparable_bank_income

    assert _comparable_bank_income(
        {"income_evidence": {**REAL_EVIDENCE, **broken}}) is None
