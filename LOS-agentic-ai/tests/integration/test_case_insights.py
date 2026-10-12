"""
COUNT / RANK / COMPARE across the officer's OWN cases (2026-10-11, audit P2-C) over the real chat endpoint, on real
stored records (synthetic applicants): another officer's case is never counted, ranked or comparable.
"""
from __future__ import annotations

import re

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_final_fix import other  # noqa: F401
from tests.integration.test_master_15_product_flow import turn
from tests.integration.test_reupload_supersedes import client  # noqa: F401


@pytest.fixture
def three(client, other, prod, monkeypatch):
    from app.agents.applicant.copilot.capabilities import case_insights

    monkeypatch.setitem(case_insights._cfg(), "max_listed", 50)        # every case of the caller shown
    from app.store.ingest import persist_los_result

    a_app, a = make_case(client, "Asha Rao")
    b_app, b = make_case(client, "Bina Shah")
    _, c = make_case(client, "Chetan Das")
    # Bina's case has three documents verified: the fewest left of the three
    persist_los_result({"request_id": "r-ins", "applicant_id": b_app, "case_id": b, "status": "SUCCESS",
                        "decision": "PASS", "next_action": "PROCEED",
                        "documents": [{"source_id": f"{d.lower()}.jpg", "type": d, "party_id": b_app,
                                       "verification": "PASS", "reason_codes": []}
                                      for d in ("PAN", "DRIVING_LICENCE", "BANK_STATEMENT")]})
    _, theirs = make_case(other, "Zed Other")
    return {"A": a, "B": b, "C": c, "theirs": theirs}


def _rows(md: str) -> list[str]:
    return re.findall(r"CASE-[0-9A-F]+", md)


def _mine(md, three):
    """The fixture's own three cases, in the order shown (other tests' cases share the module's store)."""
    return [c for c in dict.fromkeys(_rows(md)) if c in (three["A"], three["B"], three["C"])]


def test_closest_to_cpa_ranks_the_case_with_fewest_items_left_first(client, three):
    md = turn(client, "which of my cases is closest to CPA?", chat="ins1")["markdown"]
    assert _mine(md, three)[0] == three["B"], md
    assert three["theirs"] not in md


def test_most_documents_pending_puts_bina_last(client, three):
    md = turn(client, "which case has more documents pending?", chat="ins2")["markdown"]
    shown = _mine(md, three)
    assert shown[-1] == three["B"] and set(shown) == {three["A"], three["B"], three["C"]}, md


def test_count_is_over_the_callers_own_cases_only(client, three):
    md = turn(client, "how many of my cases have documents pending?", chat="ins3")["markdown"]
    found = re.search(r"\b(\d+) of your (\d+) cases", md)
    assert found and found.group(1) == found.group(2), md     # every case of the caller has documents pending
    assert three["theirs"] not in md


def test_compare_two_named_cases_side_by_side(client, three):
    md = turn(client, f"compare {three['A']} and {three['B']}", chat="ins4")["markdown"]
    assert three["A"] in md and three["B"] in md and "pending" in md.lower(), md


def test_another_officers_case_cannot_be_compared(client, three):
    md = turn(client, f"compare {three['A']} and {three['theirs']}", chat="ins5")["markdown"]
    assert "can't find" in md and "Zed" not in md, md


def test_an_ordinary_list_request_is_not_an_insight(client, three):
    md = turn(client, "show my cases", chat="ins6")["markdown"]
    assert "Closest to CPA" not in md and "side by side" not in md
