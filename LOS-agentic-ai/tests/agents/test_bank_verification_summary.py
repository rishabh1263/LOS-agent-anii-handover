"""
BANK VERIFICATION AGENT -- the summary in easy words (services/summary_service.py): the checks on the model's text,
the grouped facts, the reuse of an identical summary, the rules-written fallback. The model is stubbed: no Ollama.
"""

from __future__ import annotations

import pytest
from bank_verification_fixtures import bv_home, bv_repo, bv_template  # noqa: F401 - pytest fixtures

from app.agents.credit.bank_verification.services import summary_service as ss


def _facts(repo, case_id="BV-2026-012"):
    case, run, decision = ss._context(repo, case_id)
    return ss.facts(case, run, decision)


def test_facts_are_grouped_counted_and_carry_no_account_number(bv_repo):
    f = _facts(bv_repo)
    assert f["counts"] == {"checks_run": 7, "passed": 1, "problems_found": 4, "needs_a_person": 2}
    assert len(f["problems_found"]) == 4 and len(f["needs_a_person"]) == 2
    assert "99901223445566" not in str(f)


def test_each_problem_carries_its_exact_values(bv_repo):
    """Owner 2026-10-26 "AI summary me proper info nhi deti": every problem says WHAT differs, with both values."""
    problems = {p["check"]: p["what_differs"] for p in _facts(bv_repo)["problems_found"]}
    # case 012 has AA data: the form is checked against the AA data (rule 6 compares the AA data with the statement)
    assert problems["Bank details"] == ["Account type: Current vs AA data Savings"]
    assert any("₹1,200.50" in d and "₹1,240.50" in d for d in problems["Last 3 transactions"])
    assert any("Pooja R Deshmukh" in d and "Prakash Deshmukh" in d for d in problems["AA data vs statement"])
    text = ss.fallback(*ss._context(bv_repo, "BV-2026-012"))
    assert "1 of 7 checks passed" in text and "Current vs AA data Savings" in text and "₹1,240.50" in text


@pytest.mark.parametrize("text, ok", [
    ("Some checks found problems. 4 checks did not match and 2 need a person.", True),
    ("Some checks found problems. 13 checks did not match.", False),          # a figure the facts do not hold
    ("The loan is approved.", False),                                        # a banned word: it never decides
    ("This statement looks like fraud.", False),
    ("R1 failed because of the account_type.", False),                       # rule codes / field names
    ("", False),
])
def test_the_check_on_the_models_text(bv_repo, text, ok):
    assert ss.accepted(_facts(bv_repo), text) is ok


def test_count_words_become_digits_so_the_check_can_see_them():
    assert ss._clean("The last three transactions match.") == "The last 3 transactions match."
    assert ss._clean("<think>x</think>- One item is open.") == "One item is open."


def test_without_the_model_the_rules_summary_shows_and_covers_all_eight(bv_repo):
    out = ss.summarise(bv_repo, "BV-2026-012")
    assert out["source"] == "rules" and out["pending"] is False and len(out["checklist"]) == 8
    assert "Pooja Deshmukh" in out["text"]


def test_an_accepted_summary_is_stored_and_reused_for_identical_facts(bv_repo, monkeypatch):
    monkeypatch.setenv("BANK_VERIFICATION_AI_SUMMARY", "on")
    calls = []
    monkeypatch.setattr(ss, "ask", lambda shown: calls.append(1) or "Some checks found problems.")
    ss.enqueue(bv_repo, "BV-2026-012")
    ss._QUEUE.join()
    first = ss.summarise(bv_repo, "BV-2026-012")
    assert first["source"] == "ai" and first["text"] == "Some checks found problems." and calls == [1]
    # the same facts again (another run with identical results): the stored text, no second model call
    case, run, decision = ss._context(bv_repo, "BV-2026-012")
    other_run = bv_repo.latest_run("BV-2026-001")["run_id"]          # a real run (the store keeps summaries per run)
    bv_repo.put_summary(other_run, None, "refused", None)
    assert bv_repo.summary_by_hash(ss.facts_hash(ss.facts(case, run, decision))) == "Some checks found problems."
    assert ss.enqueue(bv_repo, "BV-2026-012") is False and calls == [1]


def test_a_refused_model_text_never_shows(bv_repo, monkeypatch):
    monkeypatch.setenv("BANK_VERIFICATION_AI_SUMMARY", "on")
    monkeypatch.setattr(ss, "ask", lambda shown: "The account is approved and safe.")
    ss.enqueue(bv_repo, "BV-2026-001")
    ss._QUEUE.join()
    out = ss.summarise(bv_repo, "BV-2026-001")
    assert out["source"] == "rules" and "approved" not in out["text"]


def test_the_model_down_falls_back_to_the_rules_summary(bv_repo, monkeypatch):
    monkeypatch.setenv("BANK_VERIFICATION_AI_SUMMARY", "on")

    def down(shown):
        raise ConnectionError("ollama down")

    monkeypatch.setattr(ss, "ask", down)
    ss.enqueue(bv_repo, "BV-2026-003")
    ss._QUEUE.join()
    assert ss.summarise(bv_repo, "BV-2026-003", start=False)["source"] == "rules"
