"""
What a salary slip states, read off a real one.

THE SLIP IS THE AUTHORITATIVE SOURCE FOR STATED PAY. A bank statement
evidences credits arriving; only the slip says what the employer paid,
for which month, and how it was made up. So these fields are the ones
another document's evidence gets compared against, and reading them
wrongly is worse than not reading them.

THE ARITHMETIC IS THE EVIDENCE. Net pay must equal gross earnings minus
deductions -- the one check a fabricated or misread slip cannot survive,
and the same role balance reconciliation plays for a bank statement.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.agents.salary_slip import extract_salary_slip
from app.agents.salary_slip.schemas import SalarySlipStatus

SAMPLE = Path("samples/real_batch/salary_slip.pdf")

pytestmark = pytest.mark.skipif(
    not SAMPLE.exists(), reason="the real salary slip sample is not present")


@pytest.fixture(scope="module")
def slip():
    return extract_salary_slip(str(SAMPLE))


# ==========================================================================
# D. THE FIELDS A SLIP STATES
# ==========================================================================


def test_the_identity_and_the_period_are_read(slip):
    assert slip.status is SalarySlipStatus.SUCCESS
    assert slip.employee_name == "VENKATESH GOUD MARAGOUNI"
    assert slip.employer_name == "SHRIRAM FINANCE LIMITED"
    assert slip.employee_code == "55588"
    assert slip.pay_period == "MAY 2026"


def test_the_pay_figures_are_read(slip):
    assert slip.gross_earnings == Decimal("31986.00")
    assert slip.total_deductions == Decimal("2120.00")
    assert slip.net_pay == Decimal("29866.00")


def test_basic_pay_and_allowances_are_read(slip):
    """
    THE TWO FIELDS THIS PHASE ADDED. Basic is a line on the earnings
    table; allowances is what is left of the printed total once basic is
    taken out.
    """
    assert slip.basic_salary == Decimal("10800.00")
    assert slip.allowances == Decimal("21186.00")


def test_allowances_are_not_a_sum_of_lines_called_allowance(slip):
    """
    THE REASON FOR SUBTRACTING RATHER THAN ADDING. This slip's earnings
    are BASIC, HOUSE RENT ALLOWANCE, CONVEYANCE, OTHER ALLOWANCE and
    RETAINING ALLOWANCE. Adding only the lines carrying the word would
    silently drop CONVEYANCE and under-report by 5,107.
    """
    assert slip.basic_salary + slip.allowances == slip.gross_earnings


def test_the_arithmetic_is_checked(slip):
    assert slip.net_pay_reconciles is True
    assert slip.gross_earnings - slip.total_deductions == slip.net_pay


def test_a_missing_file_fails_rather_than_guessing():
    result = extract_salary_slip("samples/does-not-exist.pdf")

    assert result.status is SalarySlipStatus.FAILED
    assert result.net_pay is None
    assert result.basic_salary is None
    assert result.allowances is None


def test_nothing_is_derived_when_one_side_was_not_read(monkeypatch):
    """
    Allowances is present only when BOTH figures behind it were read. A
    basic line larger than the printed total means one of the two was
    misread, and a negative allowance would publish that error as a
    figure.
    """
    from app.agents.salary_slip import extract as extractor

    monkeypatch.setattr(extractor, "_amount_after",
                        lambda lines, captions: None)

    result = extract_salary_slip(str(SAMPLE))

    assert result.allowances is None
