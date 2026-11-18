"""
ITR-V ARITHMETIC (2026-11-02): (total tax, interest and fee payable) - (taxes paid) = (tax payable / (-) refundable).

The 4 sample mismatches were not inconsistent documents:
  1. a refund printed "(-) 12,345" was not parsed, so a row label was read instead (config itr.negative_markers);
  2. the printed payable / refundable is rounded to the nearest Rs 10 (s.288B), taxes paid are not
     (config itr.payable_rounding_rupees).
A real disagreement is still a MISMATCH and goes to review, the values kept as read.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from app.agents.itr import extract as X
from app.agents.itr.schemas import ITRResult, ITRStatus


@pytest.mark.parametrize("raw, value", [
    ("(-) 12,345", D("-12345")), ("(-)12,345", D("-12345")), ("(+) 1,000", D("1000")),
    ("12,345", D("12345")), ("-500", D("-500")), ("(-)", None), ("(-) abc", None),
])
def test_a_sign_marker_is_the_sign(raw, value):
    assert X._parse_amount(raw) == value


def test_a_refund_under_its_row_label_is_read_with_its_sign():
    lines = ["(+) Tax Payable /(-) Refundable (7-8)", "9", "(-) 12,340", "Accreted Income"]
    assert X._amount_after(lines, "Tax Payable /(-) Refundable") == D("-12340")


def _reconciled(payable, paid, net):
    result = ITRResult(status=ITRStatus.SUCCESS, total_tax_and_interest_payable=D(payable), taxes_paid=D(paid),
                       tax_payable_or_refundable=D(net))
    X._reconcile(result)
    return result


@pytest.mark.parametrize("payable, paid, net", [
    ("0", "12345", "-12345"),        # exact refund
    ("0", "12344", "-12340"),        # refund rounded to Rs 10
    ("0", "12341", "-12340"),
    ("15006", "0", "15010"),          # payable rounded up
    ("0", "0", "0"),
])
def test_consistent_figures_reconcile(payable, paid, net):
    result = _reconciled(payable, paid, net)
    assert result.reconciliation == "RECONCILED"
    assert result.status == ITRStatus.SUCCESS and not result.warnings


@pytest.mark.parametrize("payable, paid, net", [
    ("0", "12345", "-12300"),         # off by more than the rounding
    ("0", "12345", "12345"),          # the sign lost
    ("0", "12343", "-12345"),         # not a multiple of the rounding and beyond the tolerance
])
def test_figures_that_do_not_add_up_go_to_review_unchanged(payable, paid, net):
    result = _reconciled(payable, paid, net)
    assert result.reconciliation == "MISMATCH"
    assert result.status == ITRStatus.PARTIAL and result.warnings
    assert result.tax_payable_or_refundable == D(net)       # nothing corrected


def test_a_missing_figure_is_not_a_mismatch():
    result = ITRResult(status=ITRStatus.SUCCESS, taxes_paid=D("100"))
    X._reconcile(result)
    assert result.reconciliation == "INCOMPLETE" and result.status == ITRStatus.SUCCESS
