"""
MASTER SPEC section 11 -- the real-time feel: typing first, one generic status only when slow, the markdown in
deltas (answer line first), `final` = {request_id, markdown, tts}; a newer message cancels; replay by request_id
(own subject only); an Idempotency-Key never reruns; changes since the last look on open and as pushed updates;
a short greeting with what needs attention.
"""

from __future__ import annotations

import json
import time

from app.store.models import Document, DocumentStatus
from tests.integration.master_env import make_case, prod, say  # noqa: F401
from tests.integration.test_reupload_supersedes import FOS_SCOPES, _store, client  # noqa: F401

STREAM = "/api/v1/fos/copilot/stream"


def _events(client, **body):
    started = time.perf_counter()
    seen = []
    with client.stream("POST", STREAM, json={"action": "CUSTOM_QUERY", "reply_language": "en", **body}) as r:
        buf = ""
        for text in r.iter_text():
            buf += text
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                name = block.split("\n", 1)[0].removeprefix("event: ")
                data = json.loads(block.split("data: ", 1)[1])
                seen.append((name, data, round((time.perf_counter() - started) * 1000)))
    return seen


def test_typing_first_then_deltas_then_final(client, prod):
    make_case(client, "Rahul Sharma")
    _events(client, message="my cases")                                      # warm (imports, first request)
    seen = _events(client, message="my cases")
    names = [n for n, _, _ in seen]
    assert names[0] == "typing" and seen[0][2] < 300
    assert names[-1] == "final" and "delta" in names
    final = seen[-1][1]
    assert set(final) - {"latency"} == {"request_id", "markdown", "tts"}
    streamed = "".join(d["markdown"] for n, d, _ in seen if n == "delta")
    assert streamed == final["markdown"]                                       # the deltas ARE the markdown
    first_delta = next(d["markdown"] for n, d, _ in seen if n == "delta")
    assert first_delta.strip() == final["markdown"].split("\n")[0].strip()      # the answer line first
    assert all(n != "status" or "Working" in d["text"] or "moment" in d["text"] or "Checking" in d["text"]
               for n, d, _ in seen)                                             # generic, if any


def test_a_newer_message_cancels_the_running_stream(client, prod, monkeypatch):
    from app.agents.applicant.copilot.answering import realtime

    make_case(client, "Rahul Sharma")
    real_start = realtime.start

    def supersede(subject, chat_id, request_id):
        real_start(subject, chat_id, request_id)
        real_start(subject, chat_id, "a-newer-request")                       # a new message arrives meanwhile

    monkeypatch.setattr(realtime, "start", supersede)
    names = [n for n, _, _ in _events(client, message="my cases", chat_id="c1")]
    assert names[-1] == "cancelled" and "final" not in names


def test_replay_by_request_id_is_own_subject_only(client, prod, make_token):
    make_case(client, "Rahul Sharma")
    first = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "my cases"}).json()
    again = client.get(f"/api/v1/fos/copilot/replay/{first['request_id']}")
    assert again.status_code == 200 and again.json() == first
    from fastapi.testclient import TestClient

    import main

    other = TestClient(main.app)
    other.headers.update({"Authorization": f"Bearer {make_token(subject='other-officer', scopes=FOS_SCOPES)}"})
    assert other.get(f"/api/v1/fos/copilot/replay/{first['request_id']}").status_code == 404


def test_an_idempotency_key_never_reruns(client, prod):
    make_case(client, "Rahul Sharma")
    headers = {"Idempotency-Key": "msg-42"}
    one = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "my cases"}, headers=headers)
    two = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "my cases"}, headers=headers)
    assert one.json()["request_id"] == two.json()["request_id"]


def test_changes_since_the_last_look_on_open_and_pushed(client, prod, _store):
    a, c = make_case(client, "Rahul Sharma")
    doc = Document(document_id=f"{c}:{a}:pan", case_id=c, applicant_id=a, party_id=a, document_type="PAN",
                   status=DocumentStatus.PROCESSING)
    _store.save_document(doc)
    say(client, f"{c} kholo", chat_id="c1")
    _store.save_document(Document(document_id=f"{c}:{a}:pan", case_id=c, applicant_id=a, party_id=a,
                                  document_type="PAN", status=DocumentStatus.VERIFIED))
    pushed = client.get("/api/v1/fos/copilot/updates", params={"chat_id": "c1"}).json()["messages"]
    assert pushed and f"Update on {c}: PAN verified." in pushed[0]["markdown"], pushed
    assert client.get("/api/v1/fos/copilot/updates", params={"chat_id": "c1"}).json()["messages"] == []
    _store.save_document(Document(document_id=f"{c}:{a}:pan", case_id=c, applicant_id=a, party_id=a,
                                  document_type="PAN", status=DocumentStatus.REJECTED))
    say(client, "bahar aao", chat_id="c1")
    reopened = say(client, f"{c} kholo", chat_id="c1")
    line = next(ln for ln in reopened.split("\n") if ln.startswith("Since your last check:"))
    assert "PAN" in line and line.endswith("rejected."), reopened


def test_hi_on_a_new_chat_is_a_short_greeting_with_what_needs_attention(client, prod):
    make_case(client, "Rahul Sharma")
    md = say(client, "hi")
    assert md.startswith(("Hello.", "Good to see you.")) and "need action" in md and "(ask:" in md, md
