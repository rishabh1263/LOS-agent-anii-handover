"""P1-A through the real route: a chat turn on POST /fos/copilot stores the agent history under (subject, chat_id)."""

from __future__ import annotations

from app.agents.applicant.copilot.capabilities import qwen_agent
from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

SUBJECT = "test-subject"          # tests/conftest.py make_token default


def test_a_turn_is_stored_under_the_caller_and_the_chat(client, prod, _store):
    qwen_agent._MEMORY.clear()
    make_case(client, "Rahul Sharma")
    r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "show my cases",
                                                 "chat_id": "http-1", "reply_language": "en"})
    assert r.status_code == 200
    qwen_agent._MEMORY.clear()                                   # as if the next turn reached another worker
    turns = qwen_agent.history((SUBJECT, "http-1"))
    assert turns and turns[0] == {"role": "user", "content": "show my cases"}
    assert qwen_agent.history((SUBJECT, "http-2")) == [] and qwen_agent.history(("someone-else", "http-1")) == []
    assert _store.get_conversation(SUBJECT, "agent:http-1") is not None


def test_a_retried_turn_with_the_same_idempotency_key_is_stored_once(client, prod, _store):
    make_case(client, "Rahul Sharma")
    body = {"action": "CUSTOM_QUERY", "message": "show my cases", "chat_id": "http-r", "reply_language": "en"}
    for _ in range(2):
        assert client.post("/api/v1/fos/copilot", json=body, headers={"Idempotency-Key": "k-1"}).status_code == 200
    assert len(qwen_agent.history((SUBJECT, "http-r"))) == 2


def _log_rows(tmp_path):
    import json

    path = tmp_path / "runtime" / "turn_log.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_the_turn_log_says_what_history_the_turn_read_counts_only(_store, tmp_path, monkeypatch):
    import contextvars

    from app.observability import turn_log

    monkeypatch.setenv("TURN_LOG_IN_TESTS", "1")
    monkeypatch.setattr(turn_log, "_ROOT", tmp_path)
    qwen_agent._MEMORY.clear()
    qwen_agent.remember((SUBJECT, "log-h"), "q1", "a1", "fos_1")
    qwen_agent._MEMORY.clear()                                   # as on a restarted process: the store answers

    def turn():
        qwen_agent.history((SUBJECT, "log-h"))
        turn_log.record(chat_key=f"{SUBJECT}|log-h", typed="q2", fixed=None, reply={"intent": "X", "answer": "a"},
                        meaning_intent=None, layer="general", started=None, request_id="fos_2", subject=SUBJECT)

    contextvars.copy_context().run(turn)
    row = _log_rows(tmp_path)[-1]
    assert row["hist"] == {"src": "shared", "n": 2} and set(row["hist"]) == {"src", "n"}     # counts, never text


def test_a_turn_that_reads_no_history_reports_none_not_a_stale_value(client, prod, _store, tmp_path, monkeypatch):
    from app.observability import turn_log

    monkeypatch.setenv("TURN_LOG_IN_TESTS", "1")
    monkeypatch.setattr(turn_log, "_ROOT", tmp_path)
    make_case(client, "Rahul Sharma")
    qwen_agent.HISTORY_READ.set(("shared", 99))                 # a value left over from an earlier request
    rid = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "show my cases",
                                                   "chat_id": "log-n", "reply_language": "en"}).json()["request_id"]
    row = next(r for r in _log_rows(tmp_path) if r["rid"] == rid)
    assert row.get("hist", {}).get("n") != 99
