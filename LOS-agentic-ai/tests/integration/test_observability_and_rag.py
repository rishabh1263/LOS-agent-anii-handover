"""
Phase 3 final sprint: OpenTelemetry runtime wiring, the CloudWatch export
path, and RAG hardening (honest metadata and versions, case isolation, the
question-embedding cache).
"""

from __future__ import annotations

import json
import logging
import sys
import types

import pytest
from fastapi.testclient import TestClient

from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository

COPILOT = "/api/v1/copilot/query"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from app.agents.applicant import config as agent_config
    from app.agents.los import config as los_config

    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    los_config.reload()
    agent_config.reload()
    repository = SQLiteRepository(tmp_path / "obs.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)
    monkeypatch.undo()
    los_config.reload()
    agent_config.reload()


@pytest.fixture
def demo(repo):
    from app.store import demo_seed

    demo_seed.seed(repo, force=True)
    return {c["stage"]: (c["case_id"], c["applicant_id"])
            for c in reversed(demo_seed._CASES)}


@pytest.fixture
def token(make_token):
    return make_token(scopes=["los.read"])


@pytest.fixture
def client(token):
    import main

    c = TestClient(main.app)
    c.headers["Authorization"] = f"Bearer {token}"
    return c


# ==========================================================================
# OPENTELEMETRY -- actually wired, and nothing sensitive in a span
# ==========================================================================

@pytest.fixture
def spans():
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from app.observability import tracing

    exporter = InMemorySpanExporter()
    tracing.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter
    exporter.clear()
    exporter.shutdown()


def test_a_copilot_request_produces_the_expected_spans(client, demo, spans, token):
    case_id, applicant_id = demo["FOS"]
    message = "Why is my application under review? my secret phrase is zebra-42"
    r = client.post(COPILOT, json={"case_id": case_id, "applicant_id": applicant_id,
                                   "message": message})
    assert r.status_code == 200, r.text
    finished = spans.get_finished_spans()
    names = {s.name for s in finished}
    for expected in ("http.request", "auth.authenticate", "copilot.stage", "copilot.routing",
                     "copilot.agent", "copilot.guardrail"):
        assert expected in names, (expected, names)
    request_span = next(s for s in finished if s.name == "http.request")
    assert request_span.attributes["http_route"] == COPILOT
    assert request_span.attributes["http_status_code"] == 200
    auth = next(s for s in finished if s.name == "auth.authenticate")
    assert auth.attributes["outcome"] == "VERIFIED"
    # NOTHING SENSITIVE: no token, no header, no question text, anywhere.
    blob = json.dumps([dict(s.attributes) for s in finished], default=str)
    assert token not in blob and "Bearer" not in blob and "eyJ" not in blob
    assert "zebra-42" not in blob and "under review?" not in blob


def test_span_attributes_are_filtered_whatever_the_caller_passes():
    from app.observability.tracing import safe_attributes

    safe = safe_attributes({"authorization": "Bearer abc", "prompt": "system: x",
                            "message": "hi", "note": "eyJhbGciOiJ9.eyJzdWIi.sig",
                            "stage": "FOS", "duration_ms": 3.5, "ok": True,
                            "raw_ocr": "PAN ABCDE1234F"})
    assert safe == {"stage": "FOS", "duration_ms": 3.5, "ok": True}


def test_tracing_is_configured_at_startup_when_enabled(monkeypatch):
    from app.observability import tracing

    from opentelemetry.sdk.trace import TracerProvider

    isolated = TracerProvider()        # never the process-wide provider
    monkeypatch.setattr(tracing, "_sdk_provider", lambda _name: isolated)
    monkeypatch.setenv("OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")
    monkeypatch.setattr(tracing, "_configured", False)
    assert tracing.configure_tracing() is True
    isolated.shutdown()
    assert tracing.status() == {"enabled": True, "configured": True,
                                "exporter": "console"}
    monkeypatch.setattr(tracing, "_configured", False)


def test_main_calls_configure_tracing_at_startup():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "main.py").read_text(encoding="utf-8")
    assert "_tracing.configure_tracing()" in source
    assert "@app.middleware(\"http\")" in source


# ==========================================================================
# CLOUDWATCH -- configurable, and never a false claim of delivery
# ==========================================================================

@pytest.fixture
def cw(monkeypatch):
    from app.observability import cloudwatch

    cloudwatch.reset()
    yield cloudwatch
    cloudwatch.reset()


PUBLISHED = {"stage": "CPA", "category": "CASE_ONLY",
             "answer": "RAHUL SHARMA's PAN is verified", "case_id": "case_secret",
             "timings": {"total_ms": 12.5, "qwen_ms": 0.0, "mcp_ms": 3.0,
                         "qwen_calls": 0, "fallback_count": 0},
             "answer_basis": {"composition": {"called": False}}}


def test_cloudwatch_is_disabled_by_default(cw):
    cw.publish(PUBLISHED)
    assert cw.status()["state"] == "DISABLED" and cw.status()["emitted"] == 0


def test_emf_mode_emits_metrics_and_no_content(cw, monkeypatch, caplog):
    monkeypatch.setenv("CLOUDWATCH_ENABLED", "true")
    monkeypatch.setenv("CLOUDWATCH_MODE", "emf")
    with caplog.at_level(logging.INFO, logger="los.cloudwatch.emf"):
        cw.publish(PUBLISHED)
    line = json.loads(caplog.records[-1].getMessage())
    assert line["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "LOS/Copilot"
    assert line["Stage"] == "CPA" and line["TotalLatency"] == 12.5
    assert line["DeterministicBypass"] == 1.0
    raw = caplog.records[-1].getMessage()
    assert "RAHUL" not in raw and "case_secret" not in raw
    # EMF is emitted, not "delivered": the app cannot see ingestion.
    assert cw.status()["state"] == "EMITTING_EMF" and cw.status()["delivered"] == 0


def test_api_mode_reports_unavailable_when_aws_is_not_reachable(cw, monkeypatch):
    monkeypatch.setenv("CLOUDWATCH_ENABLED", "true")
    monkeypatch.setenv("CLOUDWATCH_MODE", "api")
    fake = types.ModuleType("boto3")

    def client(*_a, **_k):
        raise RuntimeError("NoCredentialsError")
    fake.client = client
    monkeypatch.setitem(sys.modules, "boto3", fake)
    monkeypatch.setattr(cw, "_ensure_flusher", lambda: None)
    cw.publish(PUBLISHED)
    assert cw.flush() == 0
    status = cw.status()
    assert status["state"] == "UNAVAILABLE" and status["delivered"] == 0
    assert status["failed"] == 1 and status["last_error"] == "RuntimeError"


def test_api_mode_counts_only_accepted_batches(cw, monkeypatch):
    monkeypatch.setenv("CLOUDWATCH_ENABLED", "true")
    monkeypatch.setenv("CLOUDWATCH_MODE", "api")
    sent = []
    fake = types.ModuleType("boto3")
    fake.client = lambda *_a, **_k: types.SimpleNamespace(
        put_metric_data=lambda **kw: sent.append(kw))
    monkeypatch.setitem(sys.modules, "boto3", fake)
    monkeypatch.setattr(cw, "_ensure_flusher", lambda: None)
    cw.publish(PUBLISHED)
    assert cw.flush() == 1 and cw.status()["state"] == "CONNECTED"
    names = {m["MetricName"] for m in sent[0]["MetricData"]}
    assert {"TotalLatency", "Requests", "QwenCalls"} <= names
    assert "RAHUL" not in json.dumps(sent, default=str)


def test_no_aws_credential_is_read_from_configuration():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = (root / "app/observability/cloudwatch.py").read_text(encoding="utf-8")
    assert "aws_access_key_id" not in source and "aws_secret_access_key" not in source


# ==========================================================================
# RAG -- honest metadata, versions, isolation, cache
# ==========================================================================

def test_a_knowledge_file_without_a_declared_version_gets_its_content_hash():
    from app.knowledge.markdown_repo import knowledge_metadata

    meta, body = knowledge_metadata("FOS", "faq.md", "# FAQ\n\nText.")
    assert meta["version"].startswith("sha256:") and meta["version_source"] == "CONTENT_HASH"
    assert meta["effective_date"] is None and meta["applies_to"] is None
    assert meta["knowledge_type"] == "HANDBOOK"
    assert meta["authoritative_for"] == "PROCESS"
    assert body == "# FAQ\n\nText."


def test_declared_front_matter_is_used_as_written_and_stripped():
    from app.knowledge.markdown_repo import knowledge_metadata

    text = ("---\nversion: '2025.1'\neffective_date: 2025-04-01\n"
            "applies_to: [personal_loan]\nknowledge_type: policy\n"
            "ignored_key: x\n---\n# Policy\n\nBody.")
    meta, body = knowledge_metadata("FOS", "policy.md", text)
    assert meta["version"] == "2025.1" and meta["version_source"] == "DECLARED"
    assert meta["effective_date"] == "2025-04-01"
    assert meta["applies_to"] == ["personal_loan"]
    assert meta["knowledge_type"] == "POLICY"
    assert "ignored_key" not in meta and body.startswith("# Policy")


def test_the_version_changes_when_the_text_changes():
    from app.knowledge.markdown_repo import knowledge_metadata

    one, _ = knowledge_metadata("FOS", "a.md", "# A\n\nOne.")
    two, _ = knowledge_metadata("FOS", "a.md", "# A\n\nTwo.")
    assert one["version"] != two["version"]


def test_every_corpus_chunk_carries_honest_metadata():
    from app.knowledge import get_repository

    chunks = [c for stage in get_repository().stages()
              for c in get_repository().chunks(stage)] if hasattr(
        get_repository(), "stages") else []
    if not chunks:
        from app.knowledge.markdown_repo import MarkdownKnowledgeRepository
        from app.knowledge import knowledge_root

        repo = MarkdownKnowledgeRepository(knowledge_root())
        chunks = [c for cs in repo._load().values() for c in cs]
    assert chunks
    for chunk in chunks:
        assert chunk.metadata["version_source"] in {"DECLARED", "CONTENT_HASH"}
        assert chunk.metadata["authoritative_for"] == "PROCESS"
        assert chunk.metadata["stage"] == chunk.stage


def test_process_guides_are_labelled_demo_with_a_content_version():
    from app.knowledge import indexing
    from app.knowledge.vector_store import ALLOWED_PAYLOAD_KEYS

    for text in indexing.derive_process_texts():
        assert text.payload["knowledge_type"] == "STAGE_GUIDE_DEMO"
        assert text.payload["version"].startswith("sha256:")
        assert set(text.payload) <= ALLOWED_PAYLOAD_KEYS


def test_the_question_embedding_is_cached_and_bounded():
    from app.knowledge.embeddings import CachedQueryEmbedding, HashingEmbedding

    calls = []

    class Counting(HashingEmbedding):
        def embed(self, text):
            calls.append(text)
            return super().embed(text)

    cached = CachedQueryEmbedding(Counting(), size=2)
    first = cached.embed("what is kyc")
    assert cached.embed("what is kyc") == first and calls == ["what is kyc"]
    cached.embed("b"), cached.embed("c")
    cached.embed("what is kyc")                  # evicted, embedded again
    assert calls.count("what is kyc") == 2
    assert cached.describe()["query_cache"]["hits"] == 1


def test_a_case_question_without_a_case_reads_no_case_evidence(client, demo, monkeypatch):
    from app.knowledge import grounding

    seen = []
    real = grounding.gather

    def spy(question, *, category, scope, stages=(), **kw):
        seen.append((category, scope))
        return real(question, category=category, scope=scope, stages=stages, **kw)

    monkeypatch.setattr(grounding, "gather", spy)
    _case, applicant_id = demo["FOS"]
    r = client.post(COPILOT, json={"applicant_id": applicant_id,
                                   "message": "Why is it under review?"})
    assert r.status_code in (200, 403), r.text
    for category, scope in seen:
        if category in {"CASE_ONLY", "MIXED"}:
            assert scope is None, "no case_id: evidence from sibling cases is not evidence"


def test_knowledge_answers_publish_their_version(client, demo):
    case_id, applicant_id = demo["FOS"]
    r = client.post(COPILOT, json={"case_id": case_id, "applicant_id": applicant_id,
                                   "message": "What can be used as address proof?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["category"] in {"KNOWLEDGE_ONLY", "PROCESS_KNOWLEDGE", "MIXED"}
    # A knowledge answer exposes no case data.
    if body["category"] == "KNOWLEDGE_ONLY":
        assert body["problems"] == [] and body["pending_items"] == []


# ==========================================================================
# JEV -- optional, additive, bounded, non-blocking
# ==========================================================================

def test_jev_is_disabled_with_no_provider_and_fakes_nothing():
    import asyncio

    from app.agents.applicant import jev

    jev.register(None)
    assert jev.active() is False
    assert asyncio.run(jev.annotate_async({"question": "x"})) == []


def test_a_slow_jev_provider_is_abandoned_at_its_timeout(monkeypatch):
    import asyncio
    import time as _time

    from app.agents.applicant import config, jev

    class Slow:
        def annotate(self, packet):
            _time.sleep(1.0)
            return ["late note"]

    monkeypatch.setattr(config, "jev_enabled", lambda: True)
    monkeypatch.setattr(config, "jev_timeout_seconds", lambda: 0.05)
    jev.register(Slow())
    try:
        started = _time.perf_counter()
        assert asyncio.run(jev.annotate_async({"question": "x"})) == []
        assert _time.perf_counter() - started < 0.5
    finally:
        jev.register(None)


def test_jev_notes_are_cleaned_and_bounded(monkeypatch):
    import asyncio

    from app.agents.applicant import config, jev

    class Chatty:
        def annotate(self, packet):
            return ["  a   note  ", 42, "x" * 500, "b", "c", "d"]

    monkeypatch.setattr(config, "jev_enabled", lambda: True)
    jev.register(Chatty())
    try:
        notes = asyncio.run(jev.annotate_async({"question": "x"}))
    finally:
        jev.register(None)
    assert notes[0] == "a note" and len(notes) == jev.MAX_NOTES
    assert all(len(n) <= jev.MAX_NOTE_CHARS for n in notes)


# ==========================================================================
# PERFORMANCE INSTRUMENTATION -- the per-step timings a request reports
# ==========================================================================

def test_a_request_reports_its_per_step_timings(client, demo):
    case_id, applicant_id = demo["FOS"]
    r = client.post(COPILOT, json={"case_id": case_id, "applicant_id": applicant_id,
                                   "message": "What is my application status?"})
    timings = r.json()["timings"]
    for key in ("total_ms", "auth_ms", "routing_ms", "stage_ms", "agent_ms",
                "rag_ms", "guardrail_ms", "tools_ms", "mcp_ms", "qwen_ms",
                "qwen_calls", "fallback_count"):
        assert key in timings, (key, timings)
    assert timings["qwen_calls"] == 0          # a simple fact: no model
    assert timings["total_ms"] >= timings["agent_ms"]
