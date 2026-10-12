"""
CLOSE CALL, DECIDED INTERNALLY (2026-10-12; meaning._clarify, intent_catalogue.yaml decide.clarify).

Two close readings of a configured confusable pair are never guessed and never asked back with buttons: the previous
turn's reading continues, else both are answered through the engine's own compound split.
"""

from __future__ import annotations

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_meaning import fake, run  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


@pytest.fixture
def m():
    from app.agents.applicant.copilot.semantics import meaning

    return meaning


def test_a_close_pair_with_a_case_open_answers_both(m):
    d = m._clarify("x", [("case_status", 0.70), ("case_stage", 0.69)], True, None)
    assert d.kind == "case" and d.decided_by == "both"
    assert d.canonical == "What is the status of this case and which stage is this case in?"


def test_the_previous_turn_decides_a_close_pair(m):
    d = m._clarify("x", [("case_status", 0.70), ("case_stage", 0.69)], True, {"intent": "case_stage"})
    assert (d.intent, d.decided_by, d.canonical) == ("case_stage", "context", "Which stage is this case in?")


@pytest.mark.parametrize("ranked,case_open", [
    ([("case_status", 0.75), ("case_stage", 0.69)], True),     # a clear lead is answered as it is
    ([("case_status", 0.70), ("case_stage", 0.69)], False),    # no case open: "which case?" comes first
    ([("case_status", 0.70), ("loan_amount", 0.69)], True),    # not a configured pair
    ([("case_status", 0.58), ("case_stage", 0.575)], True),    # below the floor
])
def test_no_close_call_outside_its_rule(m, ranked, case_open):
    assert m._clarify("x", ranked, case_open, None) is None


def test_every_configured_pair_splits_into_two_questions(m):
    from app.agents.applicant.copilot.semantics import intents

    for a, b in m._decide_cfg()["clarify"]["pairs"]:
        d = m._clarify("x", [(a, 0.70), (b, 0.69)], True, None)
        assert d is not None and len(intents.compound_parts(d.canonical) or []) == 2, (a, b)


def test_through_the_api_a_close_call_answers_both_without_buttons(client, prod, fake, monkeypatch, m):
    _, case_id = make_case(client, "Rahul Sharma")
    say = lambda t: client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": t,  # noqa: E731
                                                             "reply_language": "en", "chat_id": "cc"}).json()
    say(f"open {case_id}")
    monkeypatch.setattr(m, "rank", lambda message, embedder=None: [("case_status", 0.70), ("case_stage", 0.69)])
    reply = say("where does this one stand right now")
    md = reply["markdown"]
    print("\nCLOSE_CALL_REPLY\n" + md)
    assert case_id in md and not reply.get("faq_block"), md
    assert "Pending" in md and "stage" in md.lower(), md            # both readings answered, nothing asked back
    assert run(m.understand("where does this one stand right now", case_open=True)).decided_by == "both"
