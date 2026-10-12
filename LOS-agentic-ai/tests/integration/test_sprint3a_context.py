"""
Sprint 3A, found on the real UI path: the React client sends `context: {}` on every turn. The server recalled the
chat's context only when `context` was None, so EVERY UI turn started a new conversation (turn_id 1 each time) and a
follow-up ("why?") had nothing to refer to. The chat must keep ONE conversation whether the client sends no context
or an empty one -- read from the per-turn log line the server writes (`los.copilot.turn`).
"""
import json
import logging

import pytest

from tests.integration.master_env import make_case
from tests.integration.master_env import prod  # noqa: F401
from tests.integration.test_reupload_supersedes import FOS_SCOPES, _store, client  # noqa: F401


@pytest.mark.parametrize("context", [None, {}], ids=["no-context", "empty-context"])
def test_the_chat_keeps_one_conversation_with_or_without_an_empty_context(client, prod, make_token, caplog, context):
    client.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES, expires_in=3600)}"})
    _, case_id = make_case(client, "Context Test")
    chat = f"s3a-{'none' if context is None else 'empty'}"
    caplog.set_level(logging.INFO, logger="los.copilot.turn")
    for message in (f"open {case_id}", "status", "what is pending?", "why?"):
        body = {"action": "CUSTOM_QUERY", "message": message, "chat_id": chat, "reply_language": "en"}
        if context is not None:
            body["context"] = context
        assert client.post("/api/v1/fos/copilot", json=body).status_code == 200
    turns = [json.loads(r.getMessage()) for r in caplog.records
             if r.name == "los.copilot.turn" and r.getMessage().startswith("{")]
    ids = [t.get("conversation_id") for t in turns if t.get("conversation_id")]
    assert len(ids) >= 2, turns
    assert len(set(ids)) == 1, f"a new conversation per turn: {ids}"
    numbers = [t.get("turn_id") for t in turns if t.get("turn_id")]
    assert numbers == sorted(numbers) and numbers[-1] > numbers[0], numbers
