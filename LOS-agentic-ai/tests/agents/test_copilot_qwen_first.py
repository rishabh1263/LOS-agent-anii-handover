"""
QWEN UNDERSTANDS, QWEN WORDS -- THE FACTS STAY THE TOOLS' (owner, 2026-10-13).

Router: `mode` llm_first -> Qwen reads every question (its reading wins; the rules only when Qwen is unavailable),
second_opinion -> Qwen re-reads medium / low confidence readings, fallback -> the rules first. Polish: `style: points`
-> 2-5 bullets, at most `max_emoji` emoji, and the SAME fact guard as before: a number, id or document name dropped or
invented means the engine's own text is shown.
"""
from __future__ import annotations

import pytest

from app.agents.applicant.copilot.answering import polish
from app.agents.applicant.copilot.semantics import llm_router


@pytest.fixture
def points(monkeypatch):
    real = polish._cfg

    def cfg():
        return {**real(), "style": "points", "max_emoji": 2, "max_growth_points": 1.8}

    monkeypatch.setattr(polish, "_cfg", cfg)


ORIGINAL = ("Your PAN and bank statement are verified. The salary slip is still pending, so upload it. "
            "The loan amount is 5,00,000 for 60 months.")


def test_a_bullet_rewrite_that_keeps_every_fact_is_used(points):
    new = ("- ✅ PAN and bank statement are verified\n- 📄 Salary slip is still pending: upload it\n"
           "- Loan amount: 5,00,000 for 60 months")
    assert polish.accepted(ORIGINAL, new)


@pytest.mark.parametrize("new", [
    "- PAN and bank statement verified\n- Salary slip pending\n- Loan 5,00,000 for 60 months, FOIR below 50",
    "- PAN and bank statement verified\n- Salary slip pending\n- Loan for 60 months",
    "- ✅ PAN verified 🎉\n- 📄 Salary slip pending 🙂\n- Loan 5,00,000 for 60 months, bank statement fine",
])
def test_a_rewrite_that_invents_drops_or_overdecorates_is_refused(points, new):
    """an invented limit (50), a dropped amount, more than 2 emoji -> the engine's own words"""
    assert not polish.accepted(ORIGINAL, new)


def test_points_style_rewords_lists_too(points):
    # long enough to be worth a lead (config polish.min_chars: one-fact answers stay as they are)
    assert polish._polishable("- PAN: verified\n- Salary slip: pending, please upload it before moving the case on\n"
                              "- Bank statement: verified, and the address proof still has to be collected")


@pytest.mark.parametrize("mode, levels", [("llm_first", {"high", "medium", "low"}),
                                          ("second_opinion", {"medium", "low"}), ("fallback", set())])
def test_the_router_mode_decides_who_reads_the_question(monkeypatch, mode, levels):
    monkeypatch.setenv("COPILOT_ROUTER_MODE", mode)
    monkeypatch.setattr(llm_router, "_config", lambda: {"second_opinion": ["medium", "low"]})
    assert llm_router.mode() == mode and llm_router.second_opinion_levels() == levels


def test_an_unknown_mode_is_the_safe_fallback(monkeypatch):
    monkeypatch.setenv("COPILOT_ROUTER_MODE", "keywords")
    assert llm_router.mode() == "fallback"


def test_a_one_tap_option_is_a_real_question():
    assert llm_router.canonical_question("PENDING_ITEMS") == "What is pending?"
    for intent, question in llm_router._CANONICAL.items():
        assert question.endswith("?") and "{" not in question, (intent, question)
