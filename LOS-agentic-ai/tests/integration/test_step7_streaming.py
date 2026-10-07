"""
PHASE 3 STEP 7 -- SSE streaming status + latency (COPILOT_STREAMING, default off).
The stream answers through the same pipeline as /fos/copilot: status first, then the same answer.
"""

from __future__ import annotations

import json

import pytest

from tests.integration.test_fos_stage_boundary import FOS_SCOPES, open_case
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


def events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        out.append((lines.get("event"), json.loads(lines.get("data", "{}"))))
    return out


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("COPILOT_STREAMING", "true")


def test_flag_off_is_404(client):
    a, c = open_case(client)
    assert client.post("/api/v1/fos/copilot/stream", json={"applicant_id": a, "case_id": c,
                                                           "message": "status?"}).status_code == 404


def test_status_first_then_the_same_answer_with_latency(client, on):
    a, c = open_case(client)
    body = {"applicant_id": a, "case_id": c, "message": "kaunse documents pending hain?"}
    r = client.post("/api/v1/fos/copilot/stream", json=body)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    got = events(r.text)
    assert got[0][0] == "status" and got[0][1]["text"].startswith("📄")      # documents -> the documents status
    name, answer = got[-1]
    assert name == "answer" and answer["latency"]["first_event_ms"] <= answer["latency"]["total_ms"]
    plain = client.post("/api/v1/fos/copilot", json=body).json()
    assert answer["intent"] == plain["intent"]                              # one pipeline


def test_a_refusal_is_an_error_event_not_a_dropped_stream(client, on, make_token):
    mine = client.headers.copy()
    client.headers.update({"Authorization": f"Bearer {make_token(subject='someone-else', scopes=FOS_SCOPES)}"})
    a, c = open_case(client)
    client.headers.clear()
    client.headers.update(mine)
    body = {"applicant_id": a, "case_id": c, "message": "kaunse documents pending hain?"}
    # a question that READS the case: the plain route refuses it with 403 ...
    assert client.post("/api/v1/fos/copilot", json=body).status_code == 403
    got = events(client.post("/api/v1/fos/copilot/stream", json=body).text)
    # ... and the stream says the same, as an error event
    assert got[0][0] == "status" and got[-1][0] == "error" and got[-1][1]["status"] == 403
    # NOTHING CASE-SPECIFIC before the ownership check: status lines are generic (no case / applicant id)
    for name, data in got[:-1]:
        assert name == "status" and c not in json.dumps(data) and a not in json.dumps(data), data


def test_status_lines_never_carry_ids():
    from app.agents.applicant import config

    for text in (config.chatbot("streaming") or {}).get("status", {}).values():
        assert "CASE-" not in text and "APP-" not in text and "{" not in text


def test_the_first_status_comes_from_config():
    from app.agents.applicant.copilot.answering import streaming

    assert streaming.first_status("mera KYC result kya hai?").startswith("⚠️")
    assert streaming.first_status(None, "LIST_CASES").startswith("📂")
