"""The conversation-state layer, in isolation: reading a turn, the stores."""

from __future__ import annotations

import time

from app.agents.applicant.copilot.conversation import state as conv


def _state_with_pending(options, question_type=None):
    state = conv.ConversationState(conversation_id="c1", subject_key="s1", case_id="case")
    opts = [conv._option_from_label(o) for o in options]
    state.pending_clarification = conv.PendingClarification(
        question="which?", options=opts, created_turn_id=1, expires_at=time.time() + 60,
        question_type=question_type or (conv.YES_NO if len(opts) == 1 else conv.EITHER_OR))
    return state


STAGE_OPTIONS = ["What stage is my application at?", "What documents are required at this stage?"]


def test_yes_to_an_either_or_question_chooses_nothing():
    reading = conv.read_turn("yes", _state_with_pending(STAGE_OPTIONS))
    assert reading.outcome == conv.STILL_AMBIGUOUS and reading.reply and reading.options


def test_yes_to_a_yes_no_question_resolves_it():
    reading = conv.read_turn("haan", _state_with_pending(["What is my application status?"]))
    assert reading.outcome == conv.YES_NO_RESPONSE
    assert reading.message == "What is my application status?"


def test_options_by_ordinal_and_by_meaning():
    assert conv.read_turn("2", _state_with_pending(STAGE_OPTIONS)).option_index == 1
    assert conv.read_turn("the other one", _state_with_pending(STAGE_OPTIONS)).option_index == 1
    assert conv.read_turn("pehla", _state_with_pending(STAGE_OPTIONS)).option_index == 0
    assert conv.read_turn("secnd one", _state_with_pending(STAGE_OPTIONS)).option_index == 1
    by_meaning = conv.read_turn("what do I need", _state_with_pending(STAGE_OPTIONS))
    assert by_meaning.outcome == conv.OPTION_RESOLVED and by_meaning.option_index == 1


def test_a_bare_short_query_never_chooses_an_option():
    reading = conv.read_turn("stage", _state_with_pending(STAGE_OPTIONS))
    assert reading.outcome == conv.STILL_AMBIGUOUS


def test_cancellation_and_correction():
    state = _state_with_pending(STAGE_OPTIONS)
    reading = conv.read_turn("leave that. What's my loan amount?", state)
    assert reading.outcome == conv.CANCELLATION and reading.message == "What's my loan amount?"
    assert state.pending_clarification is None
    reading = conv.read_turn("no, I mean what is still pending", conv.ConversationState("c", "s"))
    assert reading.outcome == conv.CORRECTION and "pending" in reading.message


def test_a_new_topic_supersedes_the_clarification():
    state = _state_with_pending(STAGE_OPTIONS)
    reading = conv.read_turn("Actually, what's my loan amount?", state)
    assert reading.outcome in (conv.CORRECTION, conv.NEW_TOPIC)
    assert state.pending_clarification is None


def test_a_pending_clarification_expires_by_turns_and_time(monkeypatch):
    state = _state_with_pending(STAGE_OPTIONS)
    state.turns_since_pending = 99
    assert conv.read_turn("2", state).outcome != conv.OPTION_RESOLVED
    state = _state_with_pending(STAGE_OPTIONS)
    state.pending_clarification.expires_at = time.time() - 1
    assert conv.read_turn("2", state).outcome != conv.OPTION_RESOLVED


def test_the_sqlite_store_round_trips_a_state(tmp_path):
    store = conv.SqliteConversationStore(str(tmp_path / "conv.sqlite3"))
    state = store.new("subject", "case")
    state.pending_clarification = conv.PendingClarification(
        question="which?", options=[conv.Option(label="What stage is my application at?",
                                                intent="APPLICATION_STAGE")],
        created_turn_id=1, expires_at=time.time() + 60)
    state.last_documents = ["ADDRESS_PROOF"]
    store.put(state)
    back = store.get("subject", state.conversation_id)
    assert back is not None and back.last_documents == ["ADDRESS_PROOF"]
    assert back.pending_clarification.options[0].intent == "APPLICATION_STAGE"
    assert store.get("someone-else", state.conversation_id) is None


def test_state_never_holds_case_values():
    state = conv.ConversationState("c", "s")
    public = state.public()
    assert "applicant" not in public and "pan" not in str(public).lower()
