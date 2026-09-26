"""
Slice 11: Qwen as a LANGUAGE COMPOSER -- skipped when it adds nothing, one
bounded call when it does, compact safe context, and a deterministic answer
whenever it fails.

Deterministic tests use a fake model client (the suite never depends on a
developer's Ollama). One opt-in test composes with the REAL local model:
    LOS_LIVE_LLM_TESTS=1 pytest -m live_llm
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

from app.agents.los import stage_lifecycle
from app.store import set_repository
from app.store.models import CaseFinding, FindingKind
from app.store.sqlite_repo import SQLiteRepository

CASE, APP, COAPP = ("case_q1100000000000000000000000000001",
                    "APP-QWEN11PRIM", "COAPP-QWEN11CO")
COPILOT = "/api/v1/copilot/query"
SCOPES = ["read_applicant", "read_application", "read_documents",
          "read_verification", "read_pending_items", "read_next_action"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from app.agents.applicant import config as agent_config
    from app.agents.los import config as los_config

    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    los_config.reload()
    agent_config.reload()
    stage_lifecycle.reload()
    repository = SQLiteRepository(tmp_path / "q11.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)
    monkeypatch.undo()
    los_config.reload()
    agent_config.reload()


@pytest.fixture
def case(repo):
    from app.store.ingest import persist_los_result

    persist_los_result({
        "request_id": "r-q11", "applicant_id": APP, "co_applicant_id": COAPP,
        "case_id": CASE, "status": "PARTIAL", "decision": "REVIEW",
        "next_action": "MANUAL_REVIEW",
        "documents": [
            {"source_id": "pan.jpg", "type": "PAN", "party_id": APP,
             "party_role": "PRIMARY_APPLICANT", "verification": "PASS",
             "reason_codes": []},
            {"source_id": "pan.jpg", "type": "PAN", "party_id": COAPP,
             "party_role": "CO_APPLICANT", "verification": "FAIL",
             "reason_codes": ["DOCUMENT_TYPE_MISMATCH"]}]})
    repo.save_finding(CaseFinding(
        finding_id="F-C", case_id=CASE, party_id=COAPP,
        finding_kind=FindingKind.KYC, status="REVIEW",
        reason_codes=["NAME_MISMATCH"], content_hash="c1",
        payload={"fields": [{"field": "NAME", "status": "FAIL", "sources": [
            {"document_type": "PAN", "value": "MEENA RAO"},
            {"document_type": "BANK_STATEMENT", "value": "M RAO"}]}]}))
    repo.grant_access("q11-officer", "APPLICANT", APP)
    return CASE


@pytest.fixture
def client(make_token):
    import main

    c = TestClient(main.app)
    c.headers["Authorization"] = "Bearer " + make_token(
        subject="q11-officer", scopes=SCOPES)
    return c


def ask(client, message, **extra):
    r = client.post(COPILOT, json={"applicant_id": APP, "case_id": CASE,
                                   "message": message, **extra})
    assert r.status_code == 200, r.text
    return r.json()


class _Model:
    """A stand-in for Ollama: counts calls, records what it was sent."""

    def __init__(self, reply="", delay=0.0, error=None):
        self.reply, self.delay, self.error, self.sent = reply, delay, error, []

    async def get_response(self, messages, **options):
        self.sent.append({"messages": messages, "options": options})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error

        class _R:
            text = self.reply
        return _R()


@pytest.fixture
def model(monkeypatch):
    """Case composition ON, against a fake client."""
    from app.agents.applicant import config as agent_config
    from app.llm import availability, provider

    fake = _Model()
    monkeypatch.setattr(agent_config, "compose_case_answers", lambda: True)
    monkeypatch.setattr(availability, "provider_reachable", lambda: True)
    monkeypatch.setattr(provider, "create_ollama_client", lambda: fake)
    return fake


def _sent(fake):
    out = []
    for call in fake.sent:
        for message in call["messages"]:
            for content in getattr(message, "contents", None) or []:
                out.append(getattr(content, "text", None) or str(content))
    return "\n".join(out)


SUMMARY = "Give me a complete summary of this case"


# ==========================================================================
# THE DETERMINISTIC BYPASS
# ==========================================================================

@pytest.mark.parametrize("question,reason", [
    ("What stage am I in?", "SIMPLE_INTENT"),
    ("Which documents are pending?", "SIMPLE_INTENT"),
    ("What is my application status?", "RECORDED_VALUES"),
    ("What should I do now?", "RECORDED_VALUES"),
    ("What changed?", "RECORDED_VALUES"),
    ("Why is my application under review?", "RECORDED_VALUES"),
    ("Why is the co-applicant under review?", "RECORDED_VALUES"),
])
def test_a_deterministic_answer_never_calls_the_model(case, client, model,
                                                      question, reason):
    body = ask(client, question)
    assert model.sent == []
    composition = body["answer_basis"]["composition"]
    assert composition["called"] is False
    assert composition["skipped"] in (reason, "DETERMINISTIC_ANSWER")
    assert body["timings"]["qwen_ms"] == 0 and body["timings"]["qwen_calls"] == 0
    assert body["response_source"] != "LLM"


def test_a_mixed_answer_keeps_both_halves_without_a_model(case, client, model):
    body = ask(client, "Why is my application under review and what does KYC "
                       "mean?")
    assert model.sent == [] and "In general:" in body["answer"]


# ==========================================================================
# ONE CALL, WHEN COMPOSITION ADDS SOMETHING
# ==========================================================================

def test_a_summary_is_composed_with_exactly_one_call(case, client, model):
    structured = ask(client, SUMMARY)["answer"]       # fake returns nothing
    model.sent.clear()
    model.reply = structured                          # a faithful phrasing
    body = ask(client, SUMMARY)
    assert len(model.sent) == 1
    assert body["timings"]["qwen_calls"] == 1
    assert body["answer_basis"]["composition"]["called"] is True


def test_a_knowledge_answer_is_phrased_once_not_twice(case, client, model,
                                                      monkeypatch):
    from app.agents.applicant import config as agent_config

    monkeypatch.setattr(agent_config, "llm_enabled", lambda: True)
    model.reply = "KYC checks that the documents describe the same person."
    ask(client, "What is KYC?")
    assert len(model.sent) <= 1                       # never agent + copilot


def test_a_rejected_phrasing_is_not_retried(case, client, model, monkeypatch):
    from app.agents.applicant import config as agent_config

    monkeypatch.setattr(agent_config, "regenerate_attempts", lambda: 2)
    model.reply = "Your loan is approved."
    body = ask(client, SUMMARY)
    assert len(model.sent) == 1
    assert body["answer_basis"]["composition"]["outcome"] == "REJECTED"
    assert body["timings"]["fallback_count"] == 1


def test_generation_is_bounded(case, client, model):
    ask(client, SUMMARY)
    options = model.sent[0]["options"]["options"]
    assert options["max_tokens"] and options["temperature"] <= 0.2
    assert options["keep_alive"]


# ==========================================================================
# COMPACT, SAFE CONTEXT
# ==========================================================================

def test_the_composer_context_is_compact_and_carries_no_internals(case, client,
                                                                  model):
    ask(client, SUMMARY)
    sent = _sent(model)
    for internal in (CASE, APP, COAPP, "F-C", "record_id", "source_rule",
                     "impact_rules", "tool_trace", "documents.get", "eyJ",
                     "Bearer", ".py", "sqlite", "_meta", "jsonrpc"):
        assert internal not in sent, internal
    assert len(sent) < 6000                           # a packet, not a case


def test_a_context_carrying_a_secret_is_never_sent(case, client, model, repo):
    # A recorded value that is itself a credential or a path: nothing goes.
    repo.save_finding(CaseFinding(
        finding_id="F-S", case_id=CASE, party_id=APP,
        finding_kind=FindingKind.KYC, status="REVIEW",
        reason_codes=["NAME_MISMATCH"], content_hash="s1",
        payload={"fields": [{"field": "NAME", "status": "FAIL", "sources": [
            {"document_type": "PAN", "value": "C:\\secrets\\key.pem"},
            {"document_type": "BANK_STATEMENT", "value": "X"}]}]}))
    body = ask(client, SUMMARY)
    assert model.sent == []
    assert body["answer_basis"]["composition"]["called"] is False or \
        body["answer_basis"]["composition"].get("outcome") != "ACCEPTED"


def test_the_prompt_is_small_and_states_the_truth_boundary(case, client, model):
    ask(client, SUMMARY)
    system = str(model.sent[0]["messages"][0].contents[0])
    assert len(system) < 3000
    assert "ONLY" in system and "never" in system.lower()


# ==========================================================================
# FAILURE IS SAFE AND SILENT
# ==========================================================================

@pytest.mark.parametrize("fake", [
    _Model(error=ConnectionError("ollama down at http://127.0.0.1:11434")),
    _Model(reply=""),                                  # malformed / empty
])
def test_a_failed_model_gives_the_recorded_answer(case, client, monkeypatch,
                                                  fake):
    from app.agents.applicant import config as agent_config
    from app.llm import availability, provider

    monkeypatch.setattr(agent_config, "compose_case_answers", lambda: True)
    monkeypatch.setattr(availability, "provider_reachable", lambda: True)
    monkeypatch.setattr(provider, "create_ollama_client", lambda: fake)
    body = ask(client, SUMMARY)
    assert body["response_source"] != "LLM"
    assert body["answer_basis"]["composition"]["outcome"] == "FALLBACK"
    for word in ("ollama", "11434", "model", "timeout", "error"):
        assert word not in body["answer"].lower()


def test_a_slow_model_is_bounded_by_the_timeout(case, client, model,
                                                monkeypatch):
    from app.agents.applicant import config as agent_config

    model.delay = 5
    monkeypatch.setattr(agent_config, "compose_timeout_seconds", lambda: 0.5)
    body = ask(client, SUMMARY)
    assert body["answer_basis"]["composition"]["outcome"] == "FALLBACK"
    assert body["timings"]["total_ms"] < 4000


def test_an_exhausted_request_budget_skips_the_model(case, client, model,
                                                     monkeypatch):
    from app.api.routes import copilot_api

    monkeypatch.setattr(copilot_api, "_budget_left", lambda envelope: 0.2)
    body = ask(client, SUMMARY)
    assert model.sent == []
    assert body["answer_basis"]["composition"]["skipped"] == "REQUEST_BUDGET"


# ==========================================================================
# THE MODEL CANNOT CHANGE THE TRUTH
# ==========================================================================

@pytest.mark.parametrize("claim", [
    "You're still in the RCU stage and everything is fine.",     # stage
    "Your loan is approved.",                                    # decision
    "Please visit the branch and pay the processing fee.",       # action
    "The primary applicant's PAN has a problem.",                # subject
    "Your application has 7 open issues.",                       # number
])
def test_a_phrasing_that_changes_a_fact_is_replaced(case, client, model, claim):
    structured = ask(client, SUMMARY)["answer"]
    model.sent.clear()
    model.reply = claim
    body = ask(client, SUMMARY)
    assert body["answer"] == structured
    assert body["answer_basis"]["composition"]["outcome"] == "REJECTED"
    assert body["answer_basis"]["provenance"]["worded_by_model"] is False


def test_a_long_but_faithful_phrasing_keeps_whole_sentences(case, client,
                                                            model):
    structured = ask(client, SUMMARY)["answer"]
    model.sent.clear()
    first_two = " ".join(structured.split(". ")[:2]).rstrip(".") + "."
    model.reply = f"{first_two} And there is more to say. And more again."
    body = ask(client, SUMMARY)
    # Either accepted as two WHOLE sentences, or rejected for a dropped
    # fact -- never cut mid-sentence.
    assert body["answer"].endswith(".")
    sentences = [s for s in body["answer"].split(". ") if s]
    assert len(sentences) <= max(2, len(structured.split(". ")))


# ==========================================================================
# LANGUAGE (foundation) AND LIVE COMPOSITION (opt-in)
# ==========================================================================

def test_the_language_is_accepted_and_changes_no_fact(case, client):
    english = ask(client, "What stage am I in?")
    other = ask(client, "What stage am I in?", language="hi")
    assert other["stage"] == english["stage"]
    assert other["answer_basis"]["composition"]["language"] == "hi"


def _ollama_up() -> bool:
    try:
        import httpx

        return httpx.get("http://127.0.0.1:11434/api/tags", timeout=2).status_code == 200
    except Exception:
        return False


@pytest.mark.live_llm
@pytest.mark.skipif(os.getenv("LOS_LIVE_LLM_TESTS") != "1" or not _ollama_up(),
                    reason="live model tests are opt-in (LOS_LIVE_LLM_TESTS=1)")
def test_the_real_model_composes_a_grounded_answer(case, client, monkeypatch):
    from app.agents.applicant import config as agent_config
    from app.llm import availability, provider

    monkeypatch.setattr(agent_config, "compose_case_answers", lambda: True)
    availability.reset()
    provider.reset_clients()
    body = ask(client, SUMMARY)
    composition = body["answer_basis"]["composition"]
    assert composition["called"] is True
    assert body["timings"]["qwen_calls"] == 1
    # Whatever the wording, the facts are the record's.
    assert body["stage"] == "FOS"
    assert "approved" not in body["answer"].lower()
    assert json.dumps(body).count(CASE) <= 3          # never in the answer
    assert CASE not in body["answer"]
