"""
NATURAL CONVERSATION (2026-11-02, capabilities/natural_chat.py): "hows you ?" is answered by Qwen from the actual
message, without the option menu; the plain configured line only when the model cannot answer. LOS questions are
never social. The model is a fake here (the suite never calls the live model).
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.applicant.copilot.capabilities import general, natural_chat


@pytest.fixture
def natural_on(monkeypatch):
    monkeypatch.setenv("COPILOT_NATURAL_CHAT", "true")
    monkeypatch.setattr(general, "_general_chat_ready", lambda spec: True)


@pytest.mark.parametrize("message", ["hows you ?", "hello ji kaise ho", "how are you", "kya haal hai", "sup"])
def test_short_social_messages_are_social(message):
    assert natural_chat.is_social(message)


@pytest.mark.parametrize("message", ["what is my loan amount?", "how are you doing with the pan upload for this case",
                                     "which documents are pending?"])
def test_case_questions_are_not_social(message):
    assert not natural_chat.is_social(message)


def test_the_model_answers_the_actual_message(natural_on, monkeypatch):
    seen = {}

    async def model(messages, spec):
        seen["messages"] = messages
        return "I'm doing well, thanks for asking! How can I help you today?"

    monkeypatch.setattr(general, "_chat_model", model)
    reply = asyncio.run(natural_chat.social("hows you ?", None, "r1", "en"))
    assert reply["answer"].startswith("I'm doing well")
    assert reply["intent"] == "SMALL_TALK" and not reply.get("faq_block")
    assert seen["messages"][-1] == {"role": "user", "content": "hows you ?"}
    assert "English" in seen["messages"][0]["content"]


@pytest.mark.parametrize("said", [None, "", "see http://127.0.0.1:11434/api/chat", "x" * 900])
def test_no_usable_answer_means_the_caller_falls_back(natural_on, monkeypatch, said):
    async def model(messages, spec):
        return said

    monkeypatch.setattr(general, "_chat_model", model)
    assert asyncio.run(natural_chat.social("hello", None, "r1", "en")) is None


def test_off_means_no_model_call(monkeypatch):
    monkeypatch.setenv("COPILOT_NATURAL_CHAT", "false")

    async def model(messages, spec):
        raise AssertionError("the model must not be called")

    monkeypatch.setattr(general, "_chat_model", model)
    assert asyncio.run(natural_chat.social("hello", None, "r1", "en")) is None


def test_the_greeting_has_no_menu_when_natural_chat_is_on(natural_on):
    from app.agents.applicant.copilot.capabilities import workspace

    class State:
        turn_id = 0
        active_case_id = None

    reply = workspace.greeting_reply({"sub": "officer-1"}, "r1", State())
    assert not reply.get("faq_block")


def test_out_of_domain_is_a_fixed_los_line_in_the_officers_language():
    en = natural_chat.out_of_domain_reply("r1", "en")
    assert en["intent"] == "OUT_OF_DOMAIN" and "LOS work" in en["answer"] and not en.get("faq_block")
    assert "LOS ke kaam" in natural_chat.out_of_domain_reply("r1", "hi-Latn")["answer"]
    assert natural_chat.out_of_domain_reply("r1", "fr")["answer"] == en["answer"]      # unknown language -> English
