"""
THE QUESTION REWRITE (semantics/question_rewrite.py): the slow path only, the model never answers, nothing new.
A fake model stands in for Qwen (no real model in the suite).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

FOS = "/api/v1/fos/copilot"


class FakeModel:
    def __init__(self, answers: dict[str, str] | None = None, delay: float = 0.0):
        self.answers, self.delay, self.calls = answers or {}, delay, []

    async def __call__(self, prefix: str, content: str, timeout: float) -> dict:
        self.calls.append(content)
        if self.delay:
            await asyncio.sleep(self.delay)
        q = next((v for k, v in self.answers.items() if k in content.lower()), "NONE")
        return {"message": {"content": json.dumps({"q": q})}}


@pytest.fixture
def model(monkeypatch, tmp_path):
    from app.agents.applicant.copilot.capabilities import general
    from app.agents.applicant.copilot.semantics import question_rewrite

    data = dict(general.cfg())
    monkeypatch.setattr(general, "cfg", lambda: {**data, "gaps_file": str(tmp_path / "gaps.yaml"),
                                                 "llm_rewrite": {**data["llm_rewrite"],
                                                                 "log_file": str(tmp_path / "rewrites.jsonl")}})
    monkeypatch.setenv("COPILOT_LLM_REWRITE", "true")
    fake = FakeModel()
    monkeypatch.setattr(question_rewrite, "_ollama_chat", fake)
    from app.llm import availability

    monkeypatch.setattr(availability, "provider_reachable", lambda: True)
    question_rewrite.clear_cache()
    fake.log = tmp_path / "rewrites.jsonl"
    return fake


def say(c, message, chat="rw"):
    from app.agents.applicant.copilot.capabilities import safety

    safety.reset()
    r = c.post(FOS, json={"action": "CUSTOM_QUERY", "message": message, "reply_language": "en", "chat_id": chat})
    assert r.status_code == 200, r.text
    return r.json()["markdown"]


def test_a_known_question_never_calls_the_model(client, prod, model):
    make_case(client, "Rahul Sharma")
    for message in ("what is FOIR", "how to move cpa", "EMI for 5 lakh at 12% for 3 years", "show my cases",
                    "kyc status", "1"):
        say(client, message, chat=f"known-{message}")
    assert model.calls == []


def test_an_unclear_message_is_rewritten_and_routed_again(client, prod, model):
    make_case(client, "Rahul Sharma")
    model.answers = {"paisa haath mein": "What is disbursement?"}
    md = say(client, "customer ko paisa haath mein kab tak aata hai yaar")
    assert "Understood as: What is disbursement?" in md and "release" in md
    logged = [json.loads(x) for x in model.log.read_text(encoding="utf-8").splitlines()]
    assert logged[-1]["rewrite"] == "What is disbursement?" and logged[-1]["result"] == "GENERAL_KNOWLEDGE"


def test_a_rewrite_that_adds_a_number_or_an_id_is_rejected(client, prod, model):
    from app.agents.applicant.copilot.semantics import question_rewrite

    assert question_rewrite.validate("emi kitni hogi 5 lakh pe", "What is the EMI for 5 lakh at 12% for 3 years?") is None
    assert question_rewrite.validate("is case ka status", "What is the status of CASE-1234567890AB?") is None
    assert question_rewrite.validate("8 lakh 9 percent 4 saal kist", "What is the EMI for 8 lakh at 9% for 48 months?")
    assert question_rewrite.validate("mausam", "NONE") is None


def test_timeout_or_model_down_is_the_old_behaviour(client, prod, model, monkeypatch):
    from app.agents.applicant.copilot.semantics import question_rewrite

    make_case(client, "Rahul Sharma")
    model.delay = 3.0                                   # over the 1.5 s timeout
    model.answers = {"zzz": "What is disbursement?"}
    md = say(client, "zzz paisa kab aata hai bhai bata do")
    assert "Understood as" not in md and question_rewrite.STATS["timeout"] == 1
    from app.llm import availability

    monkeypatch.setattr(availability, "provider_reachable", lambda: False)      # the model down
    assert "Understood as" not in say(client, "yyy paisa kab aata hai bhai bata do", chat="down")
    assert question_rewrite.STATS["unavailable"] == 1


def test_the_same_message_is_answered_from_the_cache(client, prod, model):
    from app.agents.applicant.copilot.semantics import question_rewrite

    make_case(client, "Rahul Sharma")
    model.answers = {"paisa haath mein": "What is disbursement?"}
    say(client, "customer ko paisa haath mein kab tak aata hai yaar", chat="c1")
    say(client, "customer ko paisa haath mein kab tak aata hai yaar", chat="c2")
    assert len(model.calls) == 1 and question_rewrite.STATS["cached"] == 1


def test_a_follow_up_uses_the_previous_question(client, prod, model):
    make_case(client, "Rahul Sharma")
    model.answers = {"previous question: what is guarantor": "What is a co-applicant?"}
    say(client, "what is guarantor", chat="fu")
    md = say(client, "aur dusra wala jo saath mein loan leta hai", chat="fu")
    assert "together" in md and any("Previous question" in c for c in model.calls)


def test_the_stream_says_understanding_while_the_model_runs(client, prod, model):
    make_case(client, "Rahul Sharma")
    model.delay = 0.3
    model.answers = {"paisa haath mein": "What is disbursement?"}
    r = client.post("/api/v1/fos/copilot/stream", json={"action": "CUSTOM_QUERY", "reply_language": "en",
                                                         "message": "customer ko paisa haath mein kab aata hai yaar",
                                                         "chat_id": "st"})
    assert r.status_code == 200 and "Understanding your question" in r.text
