"""
Sprint 4 conversation behaviours (owner 2026-10-10), the deterministic parts:
  - the case TABLE only on request (case_list.yaml `format`): a short list by default
  - "show me the table" / "don't show the table, just summarize" act on the list just shown
  - "why is the FIRST ONE pending?" resolves to that row of the list just shown, and only to it
  - "the other applicant" with exactly one other case opens it (workspace; covered live in SPRINT4 report)
"""
import pytest

from app.agents.applicant.copilot.capabilities import case_list


@pytest.mark.parametrize("message, table", [
    ("show my cases", False),
    ("show the top two cases with KYC issues", False),
    ("show my cases in a table", True),
    ("show me the table", True),
    ("don't show the table, just summarize", False),
    ("just summarize", False),
])
def test_the_table_only_when_asked(message, table):
    assert case_list.wants_table(message) is table


@pytest.mark.parametrize("message, only_format", [
    ("show me the table", True),
    ("in a table please", True),
    ("don't show the table, just summarize", True),
    ("show my cases in a table", False),          # a list request of its own (the workspace renders it as asked)
    ("why is it pending", False),
    ("what is the table of documents for home loan", False),
])
def test_a_format_only_request_reuses_the_list_just_shown(message, only_format):
    assert case_list.only_asks_for_the_table(message) is only_format


@pytest.mark.parametrize("message, expected", [
    ("Why is the first one pending?", ("CASE-1", "why is it pending")),
    ("open the second one", ("CASE-2", "open it")),
    ("what about the last one", ("CASE-2", "what about it")),
    ("show my cases", None),
    ("first time customer, what documents?", None),
])
def test_an_ordinal_names_a_row_of_the_list_just_shown(message, expected):
    assert case_list.ordinal_pick(message, ["CASE-1", "CASE-2"]) == expected


def test_an_ordinal_beyond_the_list_picks_nothing():
    assert case_list.ordinal_pick("why is the third one pending?", ["CASE-1", "CASE-2"]) is None


def test_no_list_shown_no_pick():
    assert case_list.ordinal_pick("why is the first one pending?", []) is None


def test_the_short_list_offers_the_table():
    # CHANGED DELIBERATELY (owner 2026-10-12): the "Show as a table" BUTTON is removed; typing it still asks for
    # the same list as a table
    spec = case_list.cfg().get("format") or {}
    assert spec.get("default") == "summary" and not spec.get("show_table")
    assert case_list.only_asks_for_the_table("Show as a table")
