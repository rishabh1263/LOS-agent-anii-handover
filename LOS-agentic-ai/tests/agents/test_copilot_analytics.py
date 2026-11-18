"""
COPILOT ANALYTICS (2026-11-02): deterministic counts / totals / averages / group-by / period comparisons over the
officer's own cases (capabilities/analytics.py, config copilot_analytics.yaml). No model, no free SQL: the words become
a validated Query, the numbers are computed in code from the rows, and the reply states metric, filters, period, basis.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from app.agents.applicant.copilot.capabilities import analytics as A

TODAY = date(2026, 11, 2)                     # a Monday


def _row(case, created, amount, product="PERSONAL_LOAN", stage="FOS", status="Created", kind="DOCS", tenure="36"):
    return {"case_id": case, "created_at": created, "loan_amount": amount, "product": product, "stage": stage,
            "status": status, "status_kind": kind, "tenure_months": tenure, "declared_monthly_income": None,
            "employment_type": "SALARIED"}


ROWS = [
    # created_at is UTC; 2026-10-31 20:00 UTC is 1 Nov 01:30 in Asia/Kolkata (this month, not last)
    _row("C1", "2026-10-31 20:00:00+00:00", "500000"),
    _row("C2", "2026-11-02 05:00:00+00:00", "2,500,000", product="HOME_LOAN", stage="CPA", kind="READY"),
    _row("C3", "2026-10-15 10:00:00+00:00", "300000", kind="KYC"),
    _row("C4", "2026-10-03 10:00:00+00:00", "not captured", product="HOME_LOAN"),
    _row("C5", "2026-09-20 10:00:00+00:00", "100000"),
]


def q(message, previous=None):
    return A.understand(message, previous, ROWS, today=TODAY)


# ---- understanding ------------------------------------------------------------------------------------------------

def test_count_this_month():
    query = q("How many applications were received this month?")
    assert (query.op, query.metric, query.period.key) == ("count", "cases", "this_month")
    assert (query.period.start, query.period.end) == (date(2026, 11, 1), TODAY)


def test_total_exposure_and_average():
    assert (q("What is the total loan exposure?").op, q("What is the total loan exposure?").metric) == \
        ("sum", "loan_amount")
    avg = q("average loan amount of my cases")
    assert (avg.op, avg.metric) == ("avg", "loan_amount")


def test_group_by_stage_and_compare_months():
    assert q("count applications by workflow stage").group_by == "stage"
    cmp_ = q("compare this month against last month cases")
    assert (cmp_.period.key, cmp_.compare.key) == ("this_month", "last_month")
    assert (cmp_.compare.start, cmp_.compare.end) == (date(2026, 10, 1), date(2026, 10, 31))


def test_a_named_month_is_last_occurrence():
    assert q("how many cases in October").period.key == "month:2026-10"
    assert q("how many cases in December").period.key == "month:2025-12"


def test_policy_questions_are_not_analytics():
    assert q("what is the maximum loan amount for personal loan?") is None
    assert q("what is my loan amount?") is None
    assert q("how many documents do I need?") is None
    assert q("may I upload the PAN later?") is None


@pytest.mark.parametrize("message, topic", [
    ("Which branch has the highest application count?", "branch"),
    ("Compare approved and rejected applications", "decision"),
    ("Calculate average sanctioned amount", "decision"),
    ("how many overdue accounts are there", "overdue"),
])
def test_data_fos_does_not_record_is_said_plainly(message, topic):
    result = q(message)
    assert isinstance(result, A.Unsupported) and result.topic == topic


def test_the_allow_lists_reject_anything_else():
    with pytest.raises(A.InvalidQuery):
        A.validate(A.Query(op="drop", metric="cases"))
    with pytest.raises(A.InvalidQuery):
        A.validate(A.Query(op="sum", metric="password"))
    with pytest.raises(A.InvalidQuery):
        A.validate(A.Query(group_by="pan_number"))
    with pytest.raises(A.InvalidQuery):
        A.validate(A.Query(period=A.Period("x", date(1990, 1, 1), TODAY)))


# ---- follow-ups -----------------------------------------------------------------------------------------------------

def test_followups_change_only_what_they_name():
    first = q("show me the number of applications from October")
    assert first.period.key == "month:2026-10"
    prev = first.as_dict()
    rejected = q("How many were rejected?", prev)
    assert isinstance(rejected, A.Unsupported) and rejected.topic == "decision"
    vs = q("Compare with September", prev)
    assert (vs.op, vs.metric, vs.period.key, vs.compare.key) == ("count", "cases", "month:2026-10", "month:2026-09")
    total = q("and the total amount?", prev)
    assert (total.op, total.metric, total.period.key) == ("sum", "loan_amount", "month:2026-10")
    home = q("only home loans", prev)
    assert home.filters == {"product": "HOME_LOAN"} and home.period.key == "month:2026-10"


def test_a_question_about_the_open_case_is_not_a_followup():
    prev = q("how many cases this month").as_dict()
    assert q("what is my loan amount?", prev) is None
    assert q("is my PAN verified?", prev) is None
    assert q("September?", prev).period.key == "month:2026-09"


def test_no_previous_question_means_no_followup():
    assert q("compare with September") is None
    assert q("only home loans") is None


# ---- computing ------------------------------------------------------------------------------------------------------

def run(message, previous=None):
    query = q(message, previous)
    return A.run({}, query, rows=ROWS)


def test_period_boundaries_are_in_the_configured_timezone():
    assert run("how many cases this month").value.value == 2           # C1 (IST 1 Nov) + C2
    assert run("how many cases last month").value.value == 2           # C3, C4


def test_sum_and_average_leave_out_unreadable_amounts_and_say_so():
    total = run("total loan amount of my cases")
    assert total.value.value == D("3400000") and total.value.n == 4 and total.value.missing == 1
    text = A.render(total)
    assert "₹34,00,000" in text and "1 case(s) left out" in text
    assert run("average loan amount of my cases").value.value == D("850000")


def test_group_by_and_comparison_numbers():
    grouped = run("cases by product")
    assert dict((k, v.value) for k, v in grouped.groups) == {"PERSONAL_LOAN": 3, "HOME_LOAN": 2}
    cmp_ = run("compare this month vs last month cases")
    assert (cmp_.value.value, cmp_.compare_value.value) == (2, 2)
    assert "Change: +0 (0.0%)" in A.render(cmp_) or "Change: 0 (0.0%)" in A.render(cmp_)


def test_empty_period_and_zero_division():
    empty = run("how many cases last year")
    assert empty.value.value == 0
    cmp_ = run("compare this week with last week cases")
    assert "%" not in A.render(cmp_).split("Change:")[-1] or cmp_.compare_value.value != 0


def test_reply_states_metric_filters_period_and_basis():
    text = A.render(run("average loan amount of home loan cases this month"))
    assert "Average loan amount" in text and "Product = Home Loan" in text
    assert "01 Nov 2026 to 02 Nov 2026" in text and "not a sanctioned amount" in text
