"""
FOLLOW-UPS THE AUDIT SAW FAIL (2026-10-11, audit P2-K), over the real chat endpoint with the chat's own state
(synthetic applicants, real stored records):
    "why?"                       after a pending answer      -> the reason, not the status again
    "back to the case: <q>"      with a case open            -> <q> answered on that case, never "case closed"
    "and the first one?"         after a list, a row opened  -> the first row of that list
    a compound question                                       -> both halves (never one canonical rewrite)
The live failures came from the meaning layer re-understanding the follow-up on its own words: with meaning ON
(a spy in place of the embedding ranker) it must not be consulted for any of them.
"""
from __future__ import annotations

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_master_15_product_flow import turn
from tests.integration.test_reupload_supersedes import client  # noqa: F401


@pytest.fixture
def meaning_spy(monkeypatch):
    from app.agents.applicant.copilot.semantics import meaning

    asked: list[str] = []

    async def understand(message, **_kw):
        asked.append(message)
        return None                                   # never decides: the rules answer, as when meaning abstains

    monkeypatch.setattr(meaning, "enabled", lambda: True)
    monkeypatch.setattr(meaning, "understand", understand)
    return asked


def test_why_after_pending_gives_the_reason(client, prod, meaning_spy):
    _, a = make_case(client, "Why Asha")
    turn(client, f"open {a}", chat="fu1")
    turn(client, "what is pending?", chat="fu1")
    md = turn(client, "why?", chat="fu1")["markdown"]
    assert "why?" not in meaning_spy
    assert "not uploaded" in md and "Still pending" not in md, md


def test_back_to_the_case_answers_on_the_open_case(client, prod, meaning_spy):
    _, a = make_case(client, "Back Asha")
    turn(client, f"open {a}", chat="fu2")
    turn(client, "what is the loan amount?", chat="fu2")
    md = turn(client, "back to the case: what is pending?", chat="fu2")["markdown"]
    assert "closed" not in md.lower() and "PAN" in md, md
    md = turn(client, "what is the loan amount?", chat="fu2")["markdown"]
    assert "5,00,000" in md, md                        # still on the case


def test_how_close_to_cpa_does_not_close_the_case(client, prod):
    _, a = make_case(client, "Close Asha")
    turn(client, f"open {a}", chat="fu3")
    md = turn(client, "how close is this case to CPA?", chat="fu3")["markdown"]
    assert f"{a} closed" not in md, md


def test_a_bare_close_still_closes(client, prod):
    _, a = make_case(client, "Shut Asha")
    turn(client, f"open {a}", chat="fu4")
    md = turn(client, "close this case please", chat="fu4")["markdown"]
    assert f"{a} closed" in md, md


def test_and_the_first_one_after_opening_a_row(client, prod, meaning_spy):
    _, a = make_case(client, "First Asha")
    _, b = make_case(client, "First Bina")
    md = turn(client, "show my cases", chat="fu5")["markdown"]
    shown = [c for c in dict.fromkeys(__import__("re").findall(r"CASE-[0-9A-F]+", md))]
    turn(client, "open the second one", chat="fu5")
    md = turn(client, "and the first one?", chat="fu5")["markdown"]
    assert "and the first one?" not in meaning_spy
    assert md.startswith(shown[0]), md


def test_a_compound_question_is_not_rewritten(client, prod, meaning_spy):
    app_id, case_id = make_case(client, "Compound Asha")
    turn(client, f"open {case_id}", chat="fu6")
    md = turn(client, "who is the applicant and what loan did they ask for?", chat="fu6")["markdown"]
    assert not [m for m in meaning_spy if "who is the applicant and" in m]
    assert "Compound Asha" in md and "Personal Loan" in md, md
