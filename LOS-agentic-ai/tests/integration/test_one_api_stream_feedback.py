"""
ONE CHAT API (2026-10-18, contract 1.1): POST /api/v1/fos/copilot also streams (`"stream": true` or
`Accept: text/event-stream`) and takes a 👍 / 👎 (`"action": "FEEDBACK"`). /fos/copilot/stream stays for old clients.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

CHAT = "/api/v1/fos/copilot"
ASK = {"action": "CUSTOM_QUERY", "message": "show my cases", "chat_id": "one-api", "reply_language": "en"}


def _events(text: str) -> list[str]:
    return re.findall(r"^event: (\w+)", text, re.M)


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    """The turn log ON, written under tmp_path (never the real runtime/ files)."""
    from app.observability import turn_log

    monkeypatch.setenv("TURN_LOG_IN_TESTS", "1")
    monkeypatch.setattr(turn_log, "_ROOT", tmp_path)
    return tmp_path


def _rows(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


# ---- streaming ------------------------------------------------------------------------------------------------------

def test_stream_true_on_the_main_route_streams(client, prod):
    make_case(client, "Rahul Sharma")
    r = client.post(CHAT, json={**ASK, "stream": True})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert "final" in _events(r.text)


def test_accept_event_stream_header_streams_too(client, prod):
    make_case(client, "Rahul Sharma")
    r = client.post(CHAT, json=ASK, headers={"Accept": "text/event-stream"})
    assert r.headers["content-type"].startswith("text/event-stream") and "final" in _events(r.text)


def test_without_stream_the_reply_is_plain_json(client, prod):
    make_case(client, "Rahul Sharma")
    for body in (ASK, {**ASK, "stream": False}):
        r = client.post(CHAT, json=body)
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
        assert sorted(r.json()) == ["markdown", "request_id", "tts"]


def test_the_old_stream_route_still_works(client, prod):
    make_case(client, "Rahul Sharma")
    r = client.post(CHAT + "/stream", json=ASK)
    assert "final" in _events(r.text)


def test_streamed_and_json_answers_say_the_same(client, prod):
    make_case(client, "Rahul Sharma")
    plain = client.post(CHAT, json={**ASK, "chat_id": "same-a"}).json()["markdown"]
    streamed = client.post(CHAT, json={**ASK, "chat_id": "same-b", "stream": True}).text
    final = [b for b in streamed.split("\n\n") if b.startswith("event: final")]
    assert final and json.loads(re.search(r"^data: (.+)$", final[0], re.M).group(1))["markdown"] == plain


# ---- feedback -------------------------------------------------------------------------------------------------------

def test_feedback_is_recorded_masked_and_joined_to_its_turn(client, prod, log_dir):
    make_case(client, "Rahul Sharma")
    reply = client.post(CHAT, json=ASK).json()
    r = client.post(CHAT, json={"action": "FEEDBACK", "reply_to": reply["request_id"], "rating": "DOWN",
                                "reason": "wrong, PAN ABCDE1234F shown", "chat_id": "one-api"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "RECORDED" and body["rating"] == "down" and body["reply_to"] == reply["request_id"]
    rows = _rows(log_dir / "runtime" / "feedback.jsonl")
    assert len(rows) == 1 and rows[0]["rid"] == reply["request_id"] and rows[0]["reviewed"] is False
    assert "ABCDE1234F" not in json.dumps(rows) and rows[0]["by"]
    turns = [t for t in _rows(log_dir / "runtime" / "turn_log.jsonl") if t.get("rid") == reply["request_id"]]
    assert turns and turns[0]["by"] == rows[0]["by"]          # the same caller: the rating can be trusted at review


def test_feedback_with_the_log_off_is_not_recorded_but_not_an_error(client, prod):
    r = client.post(CHAT, json={"action": "FEEDBACK", "reply_to": "fos_abc", "rating": "up"})
    assert r.status_code == 200 and r.json()["status"] == "NOT_RECORDED"


@pytest.mark.parametrize("body", [
    {"action": "FEEDBACK", "reply_to": "fos_abc", "rating": "meh"},
    {"action": "FEEDBACK", "rating": "up"},
    {"action": "FEEDBACK", "reply_to": "", "rating": "up"},
    {"action": "FEEDBACK", "reply_to": "fos_abc'; drop table", "rating": "up"},
    {"action": "FEEDBACK", "reply_to": "x" * 129, "rating": "up"},
    {"action": "FEEDBACK", "reply_to": "fos_abc", "rating": "up", "reason": "y" * 1001},
    {"action": "feedback", "reply_to": "fos_abc", "rating": "up"},          # exact upper case, like every action
])
def test_bad_feedback_is_a_422_with_the_contract_error(client, prod, body):
    r = client.post(CHAT, json=body)
    assert r.status_code == 422
    assert sorted(r.json()["error"]) == ["code", "message", "request_id", "retryable"]


def test_feedback_never_streams_and_never_answers(client, prod):
    r = client.post(CHAT, json={"action": "FEEDBACK", "reply_to": "fos_abc", "rating": "up", "stream": True,
                                "message": "show my cases"})
    assert r.headers["content-type"].startswith("application/json")
    assert set(r.json()) == {"request_id", "status", "reply_to", "rating"}


def test_feedback_needs_a_token(client, prod):
    client.headers.pop("Authorization")
    r = client.post(CHAT, json={"action": "FEEDBACK", "reply_to": "fos_abc", "rating": "up"})
    assert r.status_code == 401
