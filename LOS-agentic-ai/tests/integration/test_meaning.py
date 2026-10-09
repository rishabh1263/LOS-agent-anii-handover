"""
UNDERSTANDING BY MEANING (semantics/meaning.py) with a FAKE embedder and a FAKE chooser (no model in the suite).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


class FakeEmbedder:
    """Bag of words hashed into 256 dims: near-identical wording scores high, unrelated wording low."""

    def embed(self, text: str) -> list[float]:
        v = [0.0] * 256
        for w in re.findall(r"[a-z0-9]+", text.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 256] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_all(self, texts):
        return [self.embed(t) for t in texts]


@pytest.fixture
def fake(monkeypatch):
    from app.agents.applicant.copilot.semantics import meaning

    monkeypatch.setenv("COPILOT_MEANING", "true")
    emb = FakeEmbedder()
    monkeypatch.setattr(meaning, "_embedder", lambda: emb)
    monkeypatch.setattr("app.knowledge.retriever._cached_embed_all", lambda p, texts: p.embed_all(texts))
    meaning._BANK["key"] = None
    return emb


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_a_clear_question_becomes_its_canonical(fake):
    from app.agents.applicant.copilot.semantics import meaning

    d = run(meaning.understand("what is pending", case_open=True))
    assert d.intent == "pending_documents" and d.canonical == "What is pending?" and d.decided_by == "embedding"
    d = run(meaning.understand("what is the KYC status", case_open=True))
    assert d.intent == "kyc_status" and d.canonical == "What is the KYC status?"


def test_entities_fill_the_canonical(fake):
    from app.agents.applicant.copilot.semantics import meaning

    d = run(meaning.understand("is the PAN uploaded", case_open=True))
    assert d.intent == "document_status" and d.canonical == "What is the status of the PAN?"
    assert meaning.party_in("what is pending for the co-applicant") == "CO_APPLICANT"


def test_follow_ups_inherit_the_previous_case_intent(fake):
    from app.agents.applicant.copilot.semantics import meaning

    prev = {"intent": "pending_documents", "party": None, "document": None}
    d = run(meaning.understand("and the co-applicant?", case_open=True, previous=prev))
    assert d.decided_by == "followup" and d.canonical == "What is pending for the co-applicant?"
    d = run(meaning.understand("what about PAN?", case_open=True, previous=prev))
    assert d.intent == "document_status" and "PAN" in d.canonical


def test_an_ambiguous_message_is_chosen_by_the_model_among_the_candidates(fake, monkeypatch):
    from app.agents.applicant.copilot.semantics import meaning

    monkeypatch.setattr(meaning, "_decide_cfg", lambda: {"accept": 0.99, "margin": 0.5, "floor": 0.05,
                                                         "model_candidates": 3})
    seen = {}

    async def chooser(prefix, content, timeout):
        seen["content"] = content
        first = re.search(r"- ([a-z_]+):", content).group(1)
        return {"message": {"content": json.dumps({"i": first})}}

    d = run(meaning.understand("is anything still not uploaded", case_open=True, generator=chooser))
    assert d.decided_by == "model" and "CANDIDATES" in seen["content"] and d.canonical


def test_a_bad_or_failed_choice_falls_back_to_the_rules(fake, monkeypatch):
    from app.agents.applicant.copilot.semantics import meaning

    monkeypatch.setattr(meaning, "_decide_cfg", lambda: {"accept": 0.99, "margin": 0.5, "floor": 0.05,
                                                         "model_candidates": 3})

    async def nonsense(prefix, content, timeout):
        return {"message": {"content": json.dumps({"i": "transfer_money_now"})}}

    async def slow(prefix, content, timeout):
        await asyncio.sleep(10)

    assert run(meaning.understand("is anything still not uploaded", case_open=True, generator=nonsense)) is None
    assert run(meaning.understand("is anything still not uploaded", case_open=True, generator=slow)) is None


def test_no_embedding_model_means_the_rules(monkeypatch):
    from app.agents.applicant.copilot.semantics import meaning

    monkeypatch.setenv("COPILOT_MEANING", "true")
    monkeypatch.setattr(meaning, "_embedder", lambda: None)
    assert run(meaning.understand("what is pending", case_open=True)) is None


def test_through_the_api_docs_and_follow_ups_answer_for_the_open_case(client, prod, fake):
    _, case_id = make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    say = lambda m: client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": m,  # noqa: E731
                                                              "reply_language": "en", "chat_id": "mx"}).json()["markdown"]
    say(f"open {case_id}")
    pending = say("what is pending")
    assert "Still pending" in pending and case_id in pending
    assert "Bank Statement" in say("what about bank statement?")
    assert "5,00,000" in say("whats the loan amount")
    # commands are never rewritten: the list still lists, and the case stays open
    assert "Showing 1-2 of 2" in say("show my cases")
    assert "Still pending" in say("what is pending")
