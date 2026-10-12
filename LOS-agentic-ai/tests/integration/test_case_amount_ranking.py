"""
RANKED BY LOAN AMOUNT (owner 2026-10-12): "whitch case have more loan value" asked "which case?" over the whole list.
Now: the officer's OWN cases by their RECORDED loan amount (case insights, deterministic), "lowest" reverses, a policy
question about a product's maximum is not a ranking, and another officer's case is never in the list.
"""
from __future__ import annotations

import re

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_final_fix import other  # noqa: F401
from tests.integration.test_reupload_supersedes import client  # noqa: F401


def say(client, text, chat="amt"):
    r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": text, "reply_language": "en",
                                                  "chat_id": chat})
    assert r.status_code == 200, r.text
    return r.json()["markdown"]


@pytest.fixture
def cases(client, other, prod):
    small = make_case(client, "Small Loan", loan_amount=200000)[1]
    big = make_case(client, "Big Loan", loan_amount=900000)[1]
    mid = make_case(client, "Mid Loan", loan_amount=500000)[1]
    foreign = make_case(other, "Not Mine", loan_amount=9900000)[1]
    return small, mid, big, foreign


def _order(md, ids):
    return sorted(ids, key=lambda c: md.index(c))


def test_the_typed_question_ranks_by_loan_amount(client, cases):
    small, mid, big, foreign = cases
    md = say(client, "whitch case have more loan value")
    assert "Which case is this about" not in md, md
    # the shared test store holds other tests' cases too: this test's rows keep their order among those shown
    shown = [c for c in (small, mid, big) if c in md]
    assert big in md and _order(md, shown) == [c for c in (big, mid, small) if c in shown], md
    assert "₹9,00,000" in md and foreign not in md and "99,00,000" not in md


def test_lowest_reverses(client, cases):
    small, mid, _, _ = cases
    md = say(client, "which case has the lowest loan amount", chat="amt2")
    # the officer's cases from earlier tests share the store: the order of this test's own cases is what is checked
    shown = [c for c in (small, mid) if c in md]
    assert "lowest first" in md and small in md and _order(md, shown) == [c for c in (small, mid) if c in shown], md


@pytest.mark.parametrize("q", ["sabse zyada loan kis case me hai", "which file has the biggest loan"])
def test_other_phrasings(client, cases, q):
    md = say(client, q, chat="amt3")
    assert re.search(r"loan amount, (highest|lowest) first", md), md


def test_a_product_maximum_is_not_a_ranking():
    from app.agents.applicant.copilot.capabilities import case_insights

    assert case_insights.kind_of("what is the max loan amount for personal loan") is None
    assert case_insights.kind_of("what is the loan amount") is None
