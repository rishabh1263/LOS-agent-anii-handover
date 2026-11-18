"""
THE QWEN AGENT'S CALLS ARE ON THE TURN'S MODEL LEDGER (2026-11-02 audit).

qwen_agent._chat posted to Ollama directly, past app/llm/provider.py, so `observability.model_ledger` reported 0 calls
on turns where the agent waited 4 s and timed out. Every call -- OK, TIMEOUT or ERROR -- is now recorded.
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.applicant.copilot.capabilities import qwen_agent
from app.llm import trace


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _client(post):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            return await post(url, json)

    return _Client


def _run(monkeypatch, post):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _client(post))
    tokens = trace.begin()
    try:
        try:
            asyncio.run(qwen_agent._chat([{"role": "user", "content": "hello"}], [], 0.2))
        except Exception:  # noqa: BLE001 - the outcome is what is tested
            pass
        return trace.summary()
    finally:
        trace.end(tokens)


def test_a_successful_agent_call_is_counted(monkeypatch):
    async def post(url, json):
        return _Response({"message": {"content": "{\"t\": \"case_status\"}"}})

    s = _run(monkeypatch, post)
    assert s["calls"] == 1 and s["ok"] == 1


def test_a_timed_out_agent_call_is_counted(monkeypatch):
    async def post(url, json):
        await asyncio.sleep(1)
        return _Response({})

    s = _run(monkeypatch, post)
    assert s["calls"] == 1 and s["timeouts"] == 1


def test_a_failed_agent_call_is_counted(monkeypatch):
    async def post(url, json):
        raise ConnectionError("refused")

    s = _run(monkeypatch, post)
    assert s["calls"] == 1 and s["errors"] == 1
