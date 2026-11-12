"""
THE HANDBOOK OUTRANKS THE GENERAL MODEL (2026-11-02 audit, general.py `_knowledge` / `general_chat`).

When handbook retrieval is confident but the passage fails the relevance guard, the general model used to answer
(it only stayed silent because general_llm was disabled in config). Now the general model is never asked when the
handbook covers the topic -- even with COPILOT_GENERAL_LLM / COPILOT_GENERAL_CHAT on.
"""

from __future__ import annotations

import asyncio

from app.agents.applicant.copilot.capabilities import general


def _setup(monkeypatch, covered: bool):
    calls = []

    async def no_candidate(*a, **k):
        return None

    async def model(*a, **k):
        calls.append(a)
        return "A general answer that would contradict the handbook policy for this lender somewhere."

    monkeypatch.setattr(general, "_knowledge_candidate", no_candidate)
    monkeypatch.setattr(general, "_handbook_confident", lambda q: covered)
    monkeypatch.setattr(general, "_general_llm", model)
    from app.agents.applicant.copilot.capabilities import faq

    monkeypatch.setattr(faq, "enabled", lambda: False)
    return calls


def test_general_llm_is_not_asked_when_the_handbook_covers_the_topic(monkeypatch):
    calls = _setup(monkeypatch, covered=True)
    reply = asyncio.run(general._knowledge("what documents are accepted as address proof", "r1", "en", None))
    assert calls == []
    assert reply["intent"] == "UNKNOWN_TERM" and reply.get("response_source") != "LLM_GENERAL"


def test_general_llm_still_answers_a_topic_the_handbook_lacks(monkeypatch):
    calls = _setup(monkeypatch, covered=False)
    reply = asyncio.run(general._knowledge("what is a repo rate", "r2", "en", None))
    assert calls and reply["response_source"] == "LLM_GENERAL"
