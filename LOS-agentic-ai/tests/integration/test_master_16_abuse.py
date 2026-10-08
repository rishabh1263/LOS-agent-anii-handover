"""
MASTER SPEC sections 16 and 19 -- the abuse rule, ONE shared module (capabilities/abuse_guard.py) on every path:
a message with foul language is never answered in any part (no case read, no tool, no model); the warning shows the
word bold and masked; tts says the warning only; the stream sends it whole with no status; repeated abuse cools
down; logs / audit / generated text carry the masked form only; clean look-alikes are never flagged.
"""

from __future__ import annotations

import json

import pytest

from app.agents.applicant.copilot.answering import contract
from app.agents.applicant.copilot.capabilities import abuse_guard
from tests.integration.master_env import make_case, prod, say  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

FOS, UNIVERSAL, STREAM = "/api/v1/fos/copilot", "/api/v1/copilot/query", "/api/v1/fos/copilot/stream"
RAW = ("madarchod", "chutiya", "fuck", "मादरचोद", "झवाड्या", "bitch")


@pytest.fixture
def guard(monkeypatch):
    monkeypatch.setenv("COPILOT_ABUSE_GUARD", "true")
    abuse_guard.reset()
    yield
    abuse_guard.reset()


def _no_raw(*texts: str) -> None:
    for text in texts:
        low = text.lower()
        assert not any(w in low for w in RAW), text


# ---- unit: detection -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "madarchod what is my kyc status", "MADARCHOD", "M A D A R C H O D", "m.a.d.a.r.c.h.o.d", "fuuuuck this",
    "b!tch", "ch00tiya", "f***", "f u c k off", "Bhenchod bata", "मादरचोद KYC बताओ", "तू झवाड्या आहे", "kamina hai tu",
])
def test_variants_are_detected(guard, text):
    assert abuse_guard.blocked(text), text


@pytest.mark.parametrize("text", [
    "what is my kyc status", "Scunthorpe passport class", "Randhir from Assam, bcom graduate",
    "Dickens Road address proof", "assistant manager salary slip", "mere pending cases dikhao", "pagal ho gaya hai system",
])
def test_clean_look_alikes_are_not_flagged(guard, text):
    assert not abuse_guard.detect(text), text


def test_masked_bold_in_markdown_and_never_in_tts(guard, monkeypatch):
    monkeypatch.setenv("COPILOT_MD_TTS_CONTRACT", "true")
    reply = abuse_guard.screen("madarchod what is my kyc status", "u1", "r1")
    published = contract.publish(reply)
    assert set(published) == {"request_id", "markdown", "tts"}
    assert "**m*******d**" in published["markdown"] and "foul language" in published["markdown"]
    assert "*" not in published["tts"] and "foul language" in published["tts"]
    _no_raw(published["markdown"], published["tts"])
    assert "kyc" not in published["markdown"].lower() and "kyc" not in published["tts"].lower()


def test_language_follows_the_selection_and_the_script(guard):
    assert "Kripya" in abuse_guard.screen("chutiya", "u1", "r1", lang="hinglish")["answer"]
    assert "कृपया" in abuse_guard.screen("मादरचोद", "u2", "r2")["answer"]
    assert "वापरू" in abuse_guard.screen("झवाड्या", "u3", "r3", lang="mr")["answer"]


def test_generated_text_is_masked_and_tts_drops_the_word(guard):
    assert abuse_guard.mask_text("Customer said madarchod twice") == "Customer said m*******d twice"
    assert abuse_guard.mask_text("Customer said madarchod twice", spoken=True) == "Customer said twice"
    assert abuse_guard.mask_payload({"a": ["chutiya"], "n": 3}) == {"a": ["c*****a"], "n": 3}


def test_cooldown_after_repeated_abuse(guard):
    cfg = abuse_guard.cfg()
    replies = [abuse_guard.screen("chutiya", "u9", f"r{i}", now=1000.0 + i)
               for i in range(int(cfg["cooldown_after"]))]
    assert replies[-1]["intent"] == "COOLDOWN"
    clean = abuse_guard.screen("what is my kyc status", "u9", "rx", now=1010.0)
    assert clean is not None and clean["intent"] == "COOLDOWN"            # even a clean message waits
    assert abuse_guard.screen("what is my kyc status", "u9", "ry",
                              now=1010.0 + float(cfg["cooldown_seconds"]) + 1) is None


def test_flag_off_means_nothing_is_screened(monkeypatch):
    monkeypatch.setenv("COPILOT_ABUSE_GUARD", "false")
    assert abuse_guard.screen("madarchod", "u1", "r1") is None and not abuse_guard.detect("madarchod")


# ---- every path: both endpoints, the stream, audit -----------------------------------------------------------
def _trap_reads(monkeypatch, store):
    def boom(*_a, **_k):
        raise AssertionError("a blocked message must not read the case")

    for name in ("get_application", "list_documents", "get_applicant"):
        monkeypatch.setattr(type(store), name, boom)


def test_fos_copilot_blocks_before_any_read(client, prod, guard, _store, monkeypatch):
    _, case_id = make_case(client, "Rahul Sharma")
    audits = []
    from app.agents.applicant import audit

    monkeypatch.setattr(audit, "record", lambda **kw: audits.append(kw))
    _trap_reads(monkeypatch, _store)
    r = client.post(FOS, json={"action": "CUSTOM_QUERY", "message": "madarchod what is my kyc status",
                               "case_id": case_id, "reply_language": "en"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"request_id", "markdown", "tts"}
    assert "**m*******d**" in body["markdown"] and "kyc" not in body["markdown"].lower()
    _no_raw(body["markdown"], body["tts"])
    abuse = [a for a in audits if a.get("intent") == "ABUSE"]
    assert abuse and abuse[0]["tools"] == [] and "madarchod" not in abuse[0]["detail"]


def test_universal_endpoint_gives_the_same_outcome(client, prod, guard, _store, monkeypatch):
    _, case_id = make_case(client, "Rahul Sharma")
    fos = client.post(FOS, json={"action": "CUSTOM_QUERY", "message": "chutiya PAN status?",
                                 "case_id": case_id, "reply_language": "en"}).json()
    _trap_reads(monkeypatch, _store)
    uni = client.post(UNIVERSAL, json={"message": "chutiya PAN status?", "case_id": case_id,
                                       "reply_language": "en"})
    assert uni.status_code == 200, uni.text
    uni = uni.json()
    assert set(uni) == {"request_id", "markdown", "tts"}
    assert "**c*****a**" in fos["markdown"] and "**c*****a**" in uni["markdown"]
    assert "PAN" not in uni["markdown"] and "PAN" not in uni["tts"]
    _no_raw(fos["markdown"], fos["tts"], uni["markdown"], uni["tts"])


def test_stream_sends_the_warning_whole_with_no_status(client, prod, guard):
    seen = []
    with client.stream("POST", STREAM, json={"action": "CUSTOM_QUERY", "message": "fuck you, my cases?",
                                             "reply_language": "en"}) as r:
        buf = ""
        for text in r.iter_text():
            buf += text
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                seen.append((block.split("\n", 1)[0].removeprefix("event: "),
                             json.loads(block.split("data: ", 1)[1])))
    names = [n for n, _ in seen]
    assert "status" not in names and names.count("delta") == 1 and names[-1] == "final"
    final = seen[-1][1]
    assert "foul language" in final["markdown"] and "**f**k**" in final["markdown"]
    _no_raw(final["markdown"], final["tts"])


def test_a_blocked_turn_is_not_remembered(client, prod, guard, monkeypatch):
    from app.agents.applicant.copilot.answering import realtime

    make_case(client, "Rahul Sharma")
    say(client, "my cases", chat_id="c16")
    stored = []
    monkeypatch.setattr(contract, "remember", lambda *a, **k: stored.append(a))
    monkeypatch.setattr(realtime, "keep", lambda *a, **k: stored.append(a))
    r = client.post(FOS, json={"action": "CUSTOM_QUERY", "message": "bhenchod 2", "chat_id": "c16",
                               "reply_language": "en"})
    assert r.status_code == 200 and "foul language" in r.json()["markdown"]
    assert stored == []                                  # no pending intent, no context, no replay of the turn
