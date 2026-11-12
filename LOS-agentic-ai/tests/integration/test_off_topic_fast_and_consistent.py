"""
OFF-TOPIC (2026-10-18): one wording, never a "policy value", and never 4 s of Qwen first.
"""

from __future__ import annotations

import pytest

from app.agents.applicant.copilot.capabilities import product_flow, qwen_agent
from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

CHAT = "/api/v1/fos/copilot"
LINE = product_flow.cfg()["out_of_scope"]["text"]["en"]
OPENING = "That's outside what I can help with."      # the line as published (the glossary expands "KYC")


def _ask(client, message, **extra):
    r = client.post(CHAT, json={"action": "CUSTOM_QUERY", "message": message, "chat_id": "ot", "reply_language": "en",
                                **extra})
    assert r.status_code == 200, r.text
    return r.json()["markdown"]


def test_there_is_one_english_off_topic_line_and_it_keeps_the_phrase_the_evals_detect():
    assert len(LINE) == 1 and "outside what I can help with" in LINE[0]


def test_the_general_unknown_line_promises_nothing_later():
    from app.agents.applicant.copilot.capabilities import general

    said = general.cfg()["general_unknown"]["en"]
    assert "don't have an answer" in said and " yet" not in said


@pytest.mark.parametrize("message", ["best mutual fund to invest in?", "give me some stock tips",
                                     "write a python program to reverse a string", "what is the capital city of japan?"])
def test_investment_and_coding_requests_get_the_off_topic_line_without_a_case(client, prod, message):
    md = _ask(client, message)
    assert md.startswith(OPENING) and "policy" not in md.lower(), md


def test_investment_advice_is_off_topic_with_a_case_open_too(client, prod):
    applicant_id, case_id = make_case(client, "Rahul Sharma")
    md = _ask(client, "best mutual fund to invest in?", applicant_id=applicant_id, case_id=case_id)
    assert OPENING in md and "lender's policy" not in md, md


@pytest.mark.parametrize("message", ["what is a soft enquiry?", "what is pending", "what is the status of this case",
                                     "which documents are needed for a working capital loan?",
                                     "what is the capital requirement for a loan?"])
def test_lending_questions_are_never_called_off_topic(message):
    from app.api.routes import fos_api

    assert fos_api._clearly_off_topic(message) is False


@pytest.mark.parametrize("message", ["best mutual fund to invest in?", "aaj mausam kaisa hai?", "kal ka match kaun jeetega?",
                                     "write a python program to sort a list"])
def test_clear_off_topic_messages_are_recognised(message):
    from app.api.routes import fos_api

    assert fos_api._clearly_off_topic(message) is True


def test_the_switch_turns_the_shortcut_off(monkeypatch):
    from app.api.routes import fos_api

    monkeypatch.setitem(qwen_agent.cfg(), "skip_off_topic", False)
    assert fos_api._clearly_off_topic("kal ka match kaun jeetega?") is False


def test_qwen_is_never_called_for_an_off_topic_follow_up(client, prod, monkeypatch):
    """With Qwen on and a conversation under way (the path that waited 4 s), an off-topic message never reaches it."""
    from app.api.routes import fos_api

    applicant_id, case_id = make_case(client, "Rahul Sharma")
    called = []

    async def spy(*a, **kw):
        called.append(a[1] if len(a) > 1 else kw.get("message"))
        return None

    monkeypatch.setattr(qwen_agent, "enabled", lambda: True)
    monkeypatch.setattr(qwen_agent, "_ready", lambda: True)
    monkeypatch.setattr(fos_api, "_agent_reply", spy)
    ids = {"applicant_id": applicant_id, "case_id": case_id, "chat_id": "ot-f"}
    _ask(client, "what is pending", **ids)
    called.clear()
    md = _ask(client, "kal ka match kaun jeetega?", **ids)
    assert called == [] and "outside what I can help with" in md, (called, md)


@pytest.mark.parametrize("typed, fixed", [("what is stock statement", "what is stock statement"),
                                          ("give me some stock tips", "give me some stock tips")])
def test_stock_is_never_corrected_to_stuck(typed, fixed):
    from app.agents.applicant import normalize

    assert normalize.correct_typos(typed).text == fixed


def test_a_real_typo_is_still_corrected():
    from app.agents.applicant import normalize

    assert normalize.correct_typos("pendng docs").text != "pendng docs"


@pytest.mark.parametrize("message", ["what is the capital of france", "write me a poem about love",
                                     "who is the president of usa", "what's the weather today"])
def test_off_topic_with_a_case_open_is_refused_before_any_model_and_never_reworded(client, prod, monkeypatch,
                                                                                    message):
    # 2026-10-24 live: these took 4-16 s (Qwen, the definition step, then the natural rewrite) -- now no model at all
    from app.agents.applicant.copilot.answering import polish

    async def _no_model(*a, **k):
        raise AssertionError("a model was asked")

    monkeypatch.setattr(qwen_agent, "_chat", _no_model)
    monkeypatch.setattr(polish, "_ask", _no_model)
    applicant_id, case_id = make_case(client, "Rahul Sharma")
    md = _ask(client, message, applicant_id=applicant_id, case_id=case_id)
    assert md.startswith(OPENING), md
