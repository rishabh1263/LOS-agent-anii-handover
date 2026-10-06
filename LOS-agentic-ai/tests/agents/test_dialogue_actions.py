"""
A PROPOSED ACTION ANSWERED IN WORDS (conversation/state.py ACTION_CONFIRM,
conversation/actions.py). The pending interaction holds the STRUCTURED action the
copilot proposed; the reply only says yes, no, which one, or something else. Nothing
here keys on a phrase to build or choose an action.
"""

from __future__ import annotations

import time

import pytest

from app.agents.applicant.copilot.conversation import actions
from app.agents.applicant.copilot.conversation import state as conv


def _action(label="Raise Query", case="CASE1", text="Please review the PAN."):
    return {"action": "RAISE_QUERY", "action_id": "RAISE_QUERY", "label": label, "available": True,
            "requires_confirmation": True, "method": "POST",
            "endpoint": f"/api/v1/los/cases/{case}/queries", "body": {"target_type": "CASE", "text": text}}


def _state(*proposed, ttl=300):
    s = conv.ConversationState(conversation_id="c1", subject_key="u1", case_id="CASE1", turn_id=3)
    s.pending_clarification = conv.PendingClarification(
        reason=conv.ACTION_CONFIRM, question="I can raise a query ...", question_type=conv.YES_NO
        if len(proposed) == 1 else conv.EITHER_OR, options=[conv.Option(label=a["label"]) for a in proposed],
        created_turn_id=3, actions=[actions.stored(a) for a in proposed], expires_at=time.time() + ttl)
    return s


@pytest.mark.parametrize("reply", ["yes", "haan", "haan kar do", "kar do", "ok", "okay", "go ahead", "ho",
                                   "हाँ", "theek hai", "proceed", "yes please", "करा"])
def test_yes_in_any_language_confirms_the_one_proposed_action(reply):
    s = _state(_action())
    r = conv.read_turn(reply, s)
    assert r.outcome == conv.ACTION_CONFIRMED and r.action["action"] == "RAISE_QUERY", reply
    assert r.action_key == "chat:c1:3:0" and s.pending_clarification is None


@pytest.mark.parametrize("reply", ["no", "nahi", "mat karo", "rehne do", "नको", "don't", "cancel", "chhodo",
                                   "nako", "not now", "मत करो"])
def test_no_in_any_language_declines_and_does_nothing(reply):
    s = _state(_action())
    r = conv.read_turn(reply, s)
    assert r.outcome == conv.ACTION_DECLINED and r.action is None and s.pending_clarification is None, reply


@pytest.mark.parametrize("reply", ["haan mat karo", "yes no", "ok wait", "haan nahi", "hmm", "ek minute"])
def test_mixed_or_hesitant_replies_are_asked_again_never_guessed(reply):
    s = _state(_action())
    r = conv.read_turn(reply, s)
    assert r.outcome == conv.STILL_AMBIGUOUS and r.action is None, reply
    assert s.pending_clarification is not None and "yes or no" in r.reply


def test_yes_but_change_is_not_a_confirmation():
    s = _state(_action())
    r = conv.read_turn("yes but change the text to income proof", s)
    assert r.outcome == conv.STILL_AMBIGUOUS and r.action is None


def test_negation_plus_a_new_request_drops_the_proposal_and_answers_the_request():
    s = _state(_action())
    r = conv.read_turn("nahi, PAN details dikhao", s)
    assert r.outcome != conv.ACTION_CONFIRMED and "PAN" in r.message and s.pending_clarification is None


def test_an_unrelated_turn_drops_the_proposal():
    s = _state(_action())
    r = conv.read_turn("what is my loan amount?", s)
    assert r.outcome not in (conv.ACTION_CONFIRMED, conv.STILL_AMBIGUOUS) and s.pending_clarification is None
    assert conv.read_turn("yes", s).outcome == conv.ACKNOWLEDGEMENT    # a later yes confirms nothing


def test_yes_to_several_actions_asks_which_then_selection_needs_its_own_yes():
    s = _state(_action("Raise Query to CPA"), _action("Raise Query to CREDIT"))
    r = conv.read_turn("yes", s)
    assert r.outcome == conv.STILL_AMBIGUOUS and "Which one" in r.reply
    r = conv.read_turn("the second one", s)
    assert r.outcome == conv.OPTION_RESOLVED and r.action is None and "CREDIT" in r.reply
    r = conv.read_turn("haan", s)
    assert r.outcome == conv.ACTION_CONFIRMED and r.action["label"] == "Raise Query to CREDIT"


def test_an_expired_proposal_is_never_carried_out():
    s = _state(_action(), ttl=-1)
    r = conv.read_turn("yes", s)
    assert r.outcome == conv.ACTION_EXPIRED and r.action is None and "expired" in r.reply


def test_the_pending_action_survives_serialisation():
    s = _state(_action())
    again = conv._from_json(__import__("json").dumps(conv._to_json(s)))
    assert again.pending_clarification.actions[0]["endpoint"].endswith("/CASE1/queries")
    assert conv.read_turn("haan", again).outcome == conv.ACTION_CONFIRMED


def test_only_registered_ready_actions_are_confirmable():
    blank = _action(text="")
    unknown = dict(_action(), action="MOVE_STAGE", action_id="MOVE_STAGE")
    no_confirm = dict(_action(), requires_confirmation=False)
    assert actions.confirmable([blank, unknown, no_confirm]) == []
    assert actions.confirmable([_action()]) == [_action()]


def test_an_action_for_another_case_is_refused_before_any_write(monkeypatch):
    called = []
    monkeypatch.setitem(actions.EXECUTORS, "RAISE_QUERY", (lambda *a, **k: called.append(1), lambda a: True))
    with pytest.raises(actions.ActionRefused) as refused:
        actions.execute(_action(case="OTHER"), case_id="CASE1", claims={}, idempotency_key="k", request_id="r")
    assert refused.value.code == "ACTION_CASE_MISMATCH" and not called


def test_proposals_are_recorded_only_from_unrefused_answers():
    s = conv.ConversationState(conversation_id="c1", subject_key="u1", case_id="CASE1")
    base = {"intent": "CASE_QUERY_RAISE", "answer": "I can raise ...", "actions": [_action()], "errors": []}
    conv.update_from_response(s, "raise a query", dict(base), conv.Reading(conv.NEW_TOPIC, "raise a query"))
    assert s.pending_clarification.reason == conv.ACTION_CONFIRM
    s.pending_clarification = None
    conv.update_from_response(s, "x", dict(base, intent="GUARDRAIL_BLOCKED"), conv.Reading(conv.NEW_TOPIC, "x"))
    assert s.pending_clarification is None
