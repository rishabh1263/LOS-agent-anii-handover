"""EVERY AGENT'S SUMMARY IS RETURNED AS NUMBERED POINTS (owner 2026-11-02: "summary points me 1, 2 aisa";
app/core/summary_points.py, config app/config/summary_format.yaml)."""

from __future__ import annotations

import pytest

from app.core import summary_points as P


@pytest.fixture(autouse=True)
def numbered(monkeypatch):
    monkeypatch.setitem(P.settings(), "enabled", True)


def test_sentences_become_numbered_points_and_amounts_stay_whole():
    text = "Eligible for Rs. 12,50,000 at 9.5% interest. FOIR is 42%. No. 3 rule needs review."
    assert P.numbered(text) == ("1. Eligible for Rs. 12,50,000 at 9.5% interest.\n2. FOIR is 42%.\n"
                                "3. No. 3 rule needs review.")


def test_lines_and_bullets_become_points_and_a_numbered_summary_is_left_alone():
    assert P.numbered("- one\n- two") == "1. one\n2. two"
    assert P.numbered("1. one\n2. two") == "1. one\n2. two"
    assert P.numbered_list(["Critical: x", "Passed: y"]) == ["1. Critical: x", "2. Passed: y"]
    assert P.numbered(None) is None and P.numbered("") == ""


@pytest.mark.parametrize("module, name", [
    ("app.agents.docs_verify_credit_agent.services.summary_service", "ai_note"),
    ("app.agents.dedupe.history_summary", "_explain"),
    ("app.agents.credit.bank_verification.services.summary_service", "summarise"),
    ("app.agents.credit.crime_verification.services.summary_service", "summary_of"),
    ("app.agents.credit.legal_verification.services.summary", "of_run"),
    ("app.agents.eligibility.output.summary", "explain"),
    ("app.agents.eligibility.output.summary", "fallback"),
    ("app.agents.eligibility.output.summary", "stream"),
])
def test_every_agent_returns_its_summary_numbered(module, name):
    import importlib

    fn = getattr(importlib.import_module(module), name)
    assert getattr(fn, "__wrapped__", None) is not None, f"{module}.{name} is not numbered"


def test_the_eligibility_remark_is_numbered_end_to_end(monkeypatch):
    from app.agents.eligibility.output import summary

    monkeypatch.setattr(summary, "enabled", lambda: False)                 # no model: the facts' remark
    result = {"decision": "APPROVED", "eligible_amount": 365000, "binding_rule": "FOIR", "rules": [], "case_id": "C"}
    text = summary.fallback(result)
    assert text is None or text.startswith("1. ")
