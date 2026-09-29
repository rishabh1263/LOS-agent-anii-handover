"""The per-turn observability record and the request-scoped read memo."""

from __future__ import annotations

import json
import logging

from app.observability import turn
from app.store import request_cache


def _result() -> dict:
    return {
        "intent": "APPLICANT_PROFILE", "category": "CASE_ONLY", "query_type": "CASE_FACT",
        "response_source": "STRUCTURED", "tools_invoked": ["applicant.get", "application.get"],
        "tool_trace": [{"tool": "applicant.get", "ok": True}, {"tool": "application.get", "ok": True}],
        "answer": "The mobile number on your application is 9876501234.",
        "understanding": {
            "frame": {"language": "en", "parse_ms": 0.3}, "decided_by": "RULES",
            "llm": {"consulted": False}, "model_routing": {"route": "NEVER", "reason": "quoted"},
            "conversation": {"conversation_id": "abc", "turn_id": 3, "turn_type": "FOLLOW_UP",
                             "outcome": "REPLAY"},
        },
        "_timings": {"security_ms": 1.5, "routing_ms": 2.0, "total_ms": 20.0},
    }


def test_the_record_carries_codes_counts_and_latency_but_no_values():
    record = turn.build(request_id="fos_1", surface="fos", claims={"sub": "x", "scope": "fos.read"},
                        result=_result(), timings={"summary_ms": 0.1})
    assert record["correlation_id"] == "fos_1" and record["conversation_id"] == "abc"
    assert record["capability"] in ("APPLICANT_PROFILE", "APPLICATION_METADATA", "CASE_IDENTIFIER")
    assert record["model_route"] == "NEVER" and record["model_used"] is None
    assert record["tools"] == ["applicant.get", "application.get"] and record["tool_calls"] == 2
    assert record["latency"]["security_ms"] == 1.5 and record["latency"]["summary_ms"] == 0.1
    assert record["turn_type"] == "FOLLOW_UP" and record["clarification"] is False
    text = json.dumps(record)
    assert "9876501234" not in text and "mobile number" not in text


def test_a_model_phrased_turn_names_the_model_and_a_fallback_is_counted():
    result = _result()
    result["understanding"]["model_routing"] = {"route": "MODEL", "phrased_by_model": True}
    record = turn.build(request_id="r", surface="fos", claims={}, result=result, timings=None)
    assert record["model_used"] and record["model_attempted"] == 1
    assert record["model_calls"] == 0              # attempted, but its words were not published
    assert record["model_fallbacks"] == 1          # so the recorded answer was published instead


def test_emit_writes_one_json_line(caplog):
    with caplog.at_level(logging.INFO, logger="los.copilot.turn"):
        turn.emit({"correlation_id": "r1", "intent": "X"})
    lines = [r.getMessage() for r in caplog.records if r.name == "los.copilot.turn"]
    assert len(lines) == 1 and json.loads(lines[0])["event"] == "copilot_turn"


class _Repo:
    def __init__(self) -> None:
        self.calls = 0

    def get_application(self, case_id):
        self.calls += 1
        return {"case_id": case_id}


def test_the_memo_reads_once_per_scope_and_live_outside_it():
    repo = _Repo()
    with request_cache.scoped():
        assert request_cache.read(repo, "get_application", "c1") == {"case_id": "c1"}
        request_cache.read(repo, "get_application", "c1")
        request_cache.read(repo, "get_application", "c2")
        with request_cache.scoped():                     # nested: the same memo
            request_cache.read(repo, "get_application", "c1")
    assert repo.calls == 2
    request_cache.read(repo, "get_application", "c1")   # outside a scope: live
    assert repo.calls == 3 and not request_cache.active()
