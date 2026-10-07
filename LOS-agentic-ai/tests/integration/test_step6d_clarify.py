"""
PHASE 3 STEP 6d -- clarifying questions, yes / no, option picks, corrections, unsure replies
(CHATBOT_SPEC section 3). Word lists: semantic_concepts.yaml (conversation); reply texts:
applicant_agent.yaml chatbot.conversation. Option A (a router choice context does not
confirm -> a one-tap question) is the router's AGREEMENT check (6b-tune-2), tested end to end here.
"""

from __future__ import annotations

import time

import pytest

from app.agents.applicant.copilot.conversation import state as conv


def _state(question="Do you mean your status or your pending documents?", options=None, kind=conv.EITHER_OR):
    st = conv.ConversationState(conversation_id="c1", subject_key="s1")
    st.pending_clarification = conv.PendingClarification(
        question=question, question_type=kind, expires_at=time.time() + 600,
        options=[conv.Option(label=o) for o in (options or ["What is my status?", "What is pending?"])])
    return st


# ---- d) yes / no in the context of the bot's question ------------------------------------------
@pytest.mark.parametrize("yes", ["haan", "ok", "theek hai", "kar do", "chalega", "bilkul", "hmm ok", "👍"])
def test_yes_words_accept_a_yes_no_offer(yes):
    st = _state("Want to see what is pending?", ["What is pending?"], conv.YES_NO)
    reading = conv.read_turn(yes, st)
    assert reading.outcome == conv.YES_NO_RESPONSE and reading.message == "What is pending?", (yes, reading)


@pytest.mark.parametrize("no", ["nahi", "no", "mat karo", "👎"])
def test_no_words_decline_with_one_alternative(no):
    reading = conv.read_turn(no, _state("Want to see what is pending?", ["What is pending?"], conv.YES_NO))
    assert reading.outcome == conv.USER_REJECTED_CLARIFICATION
    assert "👉" in reading.reply and reading.reply.count("?") == 1, reading.reply      # ONE alternative


@pytest.mark.parametrize("unsure", ["pata nahi", "shayad", "not sure", "kya pata"])
def test_an_unsure_reply_gets_the_question_again_more_simply(unsure):
    reading = conv.read_turn(unsure, _state())
    assert reading.outcome == conv.STILL_AMBIGUOUS
    assert "1. What is my status" in reading.reply and "2. What is pending" in reading.reply, reading.reply


def test_unsure_is_never_read_as_a_no():
    from app.agents.applicant.copilot.conversation.state import _has

    assert _has("UNSURE", "pata nahi") and not _has("NEGATE", "pata nahi")


# ---- e) option picks ----------------------------------------------------------------------------
@pytest.mark.parametrize("pick,index", [("2", 1), ("pehla wala", 0), ("second", 1), ("doosra wala", 1),
                                        ("first one", 0)])
def test_an_option_pick_maps_to_the_offered_option(pick, index):
    reading = conv.read_turn(pick, _state())
    assert reading.outcome == conv.OPTION_RESOLVED and reading.option_index == index, (pick, reading)


def test_a_pick_by_content_maps_to_that_option():
    st = _state("Applicant ke docs ya co-applicant ke?",
                ["Show the applicant's pending documents", "Show the co-applicant's pending documents"])
    reading = conv.read_turn("co-applicant wala", st)
    assert reading.option_index == 1 or "co-applicant" in reading.message.lower(), reading


# ---- f) corrections --------------------------------------------------------------------------------
def test_a_correction_drops_the_last_assumption():
    st = conv.ConversationState(conversation_id="c1", subject_key="s1")
    st.last_answer_reference = {"intent": "APPLICATION_STATUS"}
    reading = conv.read_turn("nahi, mera matlab tha pending documents", st)
    assert reading.outcome == conv.CORRECTION and "pending documents" in reading.message.lower(), reading


# ---- no pending question --------------------------------------------------------------------------
def test_a_bare_no_without_a_question_offers_one_alternative():
    reading = conv.read_turn("nahi", conv.ConversationState(conversation_id="c1", subject_key="s1"))
    assert reading.reply and "👉" in reading.reply


def test_texts_come_from_config(monkeypatch):
    from app.agents.applicant import config

    base = config.chatbot("conversation")
    monkeypatch.setattr(config, "chatbot", lambda name, _o=config.chatbot: {**base, "declined_reply": "CFG"}
                        if name == "conversation" else _o(name))
    assert conv._declined_reply() == "CFG"


# ---- option A end to end: the router is unsure -> one tap; the pick is answered ----------------------
def test_router_unsure_gives_options_and_a_pick_answers_it(client, with_bank, monkeypatch):
    from app.agents.applicant.copilot.semantics import embedding_router
    from tests.integration.test_fos_stage_boundary import open_case
    from tests.integration.test_step6b_llm_router import UNREAD, ask

    monkeypatch.setattr(embedding_router, "confident", lambda match: False)
    with_bank.reply = {"tool": "list_cases"}                           # the bank leans to case_status
    a, c = open_case(client)
    first = ask(client, a, UNREAD, c)
    assert first["clarification_required"]["reason"] == "ROUTER_UNSURE"
    options = first["clarification_required"]["options"]
    body = {"applicant_id": a, "case_id": c, "action": "CUSTOM_QUERY", "message": "1",
            "context": first.get("context")}
    picked = client.post("/api/v1/fos/copilot", json=body).json()
    expected = "CASE_PORTFOLIO" if options[0] == "Show my applications." else "APPLICATION_STATUS"
    assert picked["intent"] == expected, (options, picked["intent"], picked["answer"][:120])


from tests.integration.test_reupload_supersedes import _store, client  # noqa: E402,F401
from tests.integration.test_step6b_llm_router import fake_model, with_bank  # noqa: E402,F401
