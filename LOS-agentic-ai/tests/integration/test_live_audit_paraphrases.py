"""
LIVE (real nomic embeddings + the meaning adapter; opt-in LOS_LIVE_LLM_TESTS=1): the UNSEEN paraphrases of the
2026-10-12 audit are READ by the semantic layer -- nothing patched. None of these wordings is a catalogue example.
    LOS_LIVE_LLM_TESTS=1 pytest -q tests/integration/test_live_audit_paraphrases.py -s
"""
from __future__ import annotations

import os
import re

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_live_fos_scenarios import _ollama_up, _warm
from tests.integration.test_reupload_supersedes import client  # noqa: F401

pytestmark = [pytest.mark.live_llm,
              pytest.mark.skipif(os.getenv("LOS_LIVE_LLM_TESTS") != "1" or not _ollama_up(),
                                 reason="live model tests are opt-in (LOS_LIVE_LLM_TESTS=1)")]


@pytest.fixture
def live(client, prod, monkeypatch):
    from tests import conftest

    for flag in conftest._PHASE3_FLAGS:
        if flag != "LOS_LOGIN_SELF_GRANT_LEGACY":
            monkeypatch.setenv(flag, "true")
    for name in ("LOS_STAGE_GATE_IN_SERVICE", "LOS_FOS_CPA_KYC_RULE", "COPILOT_LLM_ROUTER", "COPILOT_MEANING"):
        monkeypatch.setenv(name, "true")
    monkeypatch.delenv("KNOWLEDGE_BACKEND", raising=False)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setenv("COPILOT_TURN_DIAGNOSTICS", "true")
    from app.agents.applicant.copilot.capabilities import safety
    from app.agents.applicant.copilot.semantics import meaning

    real = safety._num
    monkeypatch.setattr(safety, "_num", lambda k, d: 10_000 if k == "rate_limit_per_minute" else real(k, d))
    _warm(meaning)
    ids = [make_case(client, n)[1] for n in ("Live Pf One", "Live Pf Two", "Live Pf Three")]

    def say(text, chat):
        body = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": text,
                                                         "reply_language": "en", "chat_id": chat}).json()
        print(f"\n> {text}\n  {body.get('diagnostics')}\n  {(body.get('markdown') or '')[:160]!r}")
        return body
    return ids, say


@pytest.mark.parametrize("q", ["which files are still waiting on something", "show me the applications that aren't finished"])
def test_portfolio_list_paraphrases(live, q):
    ids, say = live
    md = say(q, f"l-{len(q)}")["markdown"]
    assert "Which case is this about" not in md and any(i in md for i in ids), md


@pytest.mark.parametrize("q", ["count my files that still need paperwork", "kitni files atki hain",
                               "how many cases have documents pending?"])
def test_portfolio_count_paraphrases(live, q):
    _, say = live
    md = say(q, f"c-{len(q)}")["markdown"]
    assert "Which case is this about" not in md and re.search(r"\b\d+ of your \d+ cases\b", md), md


@pytest.mark.parametrize("q", ["hey there, good evening", "hello ji kaise ho"])
def test_small_talk_paraphrases(live, q):
    _, say = live
    body = say(q, f"s-{len(q)}")
    assert body["markdown"].split()[0].rstrip("!.,?") in ("Hello", "Hi"), body["markdown"]
    assert body["diagnostics"]["tools"] == [], body["diagnostics"]


def test_handbook_paraphrase_matches_the_plain_question(live):
    _, say = live
    plain = say("what is the minimum bank statement period?", "k1")["markdown"]
    para = say("how long a statement history do we need from the customer", "k2")["markdown"]
    assert "not configured" in plain and "not configured" in para, (plain, para)


def test_one_cases_question_is_still_one_case(live):
    ids, say = live
    say(f"open {ids[0]}", "one")
    md = say("what is pending", "one")["markdown"]
    assert ids[0] in md and "Still pending" in md, md
