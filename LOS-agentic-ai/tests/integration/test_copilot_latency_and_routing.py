"""
The 2-3 s response contract, and the two routing bugs the latency audit found.

LATENCY. On qwen2.5:3b / CPU a NEW prompt costs ~8.5 ms per token and
generation runs ~18 tokens/s, so a composed answer can take 5-6 s. The
contract: a model is used when it can finish inside the budget
(compose 2.5 s, request 2.8 s); otherwise the deterministic answer is
published. Nothing waits past the budget, and deterministic answers never
call a model at all.

ROUTING. "show my application status" was answered as a list of cases; "can
you explain why?" after a review answer went to the handbook and a model.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.agents.applicant import followup, intents


# ==========================================================================
# ROUTING FIXES
# ==========================================================================

@pytest.mark.parametrize("question,intent", [
    ("show my application status", "APPLICATION_STATUS"),
    ("show my application", "APPLICATION_STATUS"),
    ("show me my case status", "APPLICATION_STATUS"),
    ("list my applications", "CASE_PORTFOLIO"),     # the list still works
    ("show all my cases", "CASE_PORTFOLIO"),
    ("show my cases", "CASE_PORTFOLIO"),
    ("how many cases do I have", "CASE_PORTFOLIO"),
])
def test_status_is_not_a_portfolio_question(question, intent):
    assert intents.understand(question, has_case=True).intent.value == intent


@pytest.mark.parametrize("message", [
    "why?", "Can you explain why?", "explain that", "can you explain that more simply?",
    "why is that so?", "please elaborate", "tell me more", "could you tell me why?",
])
def test_a_follow_up_to_a_review_answer_asks_for_the_recorded_reason(message):
    context = followup.Context.from_payload({"last_intent": "CASE_HISTORY",
                                             "last_query_type": "CASE_FACT"})
    resolved = followup.resolve(message, context)
    assert resolved.message == "Why is my application under review?"
    assert intents.understand(resolved.message, has_case=True).intent.value == "CASE_HISTORY"


def test_other_follow_ups_keep_their_existing_meaning():
    status = followup.Context.from_payload({"last_intent": "APPLICATION_STATUS",
                                            "last_query_type": "CASE_FACT"})
    assert followup.resolve("why?", status).message == "What is pending on this case?"
    docs = followup.Context.from_payload({"last_intent": "DOCUMENT_VERIFICATION",
                                          "last_query_type": "DOCUMENT_STATUS"})
    assert followup.resolve("can you please elaborate?", docs).message == \
        "Show me all document issues."


# ==========================================================================
# LATENCY CONTRACT
# ==========================================================================

def test_the_budgets_meet_the_response_target():
    from app.agents.applicant import config

    config.reload()
    assert config.compose_timeout_seconds() <= 2.5
    assert config.compose_request_budget_seconds() <= 2.8
    assert config.llm_timeout_seconds() <= 2.5


def test_the_model_is_shown_a_bounded_knowledge_context():
    from app.agents.applicant import knowledge_answer as ka

    result = ka.retrieve("What is KYC?", limit=3)
    context = ka._model_context(result, 3)
    assert len(context) <= 700
    assert context and context.rstrip()[-1] in ".!?" or len(context) <= 700


def test_a_slow_model_is_abandoned_and_the_passage_published(monkeypatch):
    """The handbook path: past the budget, the retrieved passage answers."""
    from app.agents.applicant import config, knowledge_answer as ka

    async def slow(question, context):
        await asyncio.sleep(10)
        return "never"

    async def bounded(question, context):
        try:
            return await asyncio.wait_for(slow(question, context),
                                          timeout=config.llm_timeout_seconds())
        except asyncio.TimeoutError:
            return None
    monkeypatch.setattr(ka, "_phrase", bounded)
    monkeypatch.setattr(config, "llm_enabled", lambda: True)
    started = time.perf_counter()
    text, source, _detail = asyncio.run(ka.answer("What is KYC?"))
    elapsed = time.perf_counter() - started
    assert elapsed < 3.0, elapsed
    assert source == "deterministic" and text


def test_keep_warm_is_a_load_only_ping(monkeypatch):
    from app.llm import keep_warm

    sent = {}

    class Response:
        status_code = 200

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            sent.update(url=url, body=json)
            return Response()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    assert asyncio.run(keep_warm.ping()) is True
    assert sent["url"].endswith("/api/generate")
    assert set(sent["body"]) == {"model", "keep_alive"}      # no prompt: nothing generated


def test_keep_warm_can_be_switched_off(monkeypatch):
    from app.llm import keep_warm

    monkeypatch.setenv("COPILOT_MODEL_KEEP_WARM_SECONDS", "0")
    assert keep_warm.interval_seconds() == 0
    assert asyncio.run(keep_warm.run_forever()) is None      # returns at once


def test_startup_wires_the_warmup_and_keep_warm():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "main.py").read_text(encoding="utf-8")
    assert "_keep_warm.run_forever()" in source
    assert "_composer_config.llm_enabled()" in source
