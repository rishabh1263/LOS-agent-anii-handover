"""
LANGUAGE PRECEDENCE AND TTS, through the real HTTP API (2026-10-06).

The frontend's selected `response_language` wins over the language the question
was typed in; the spoken text is the same canonical message in the same
language's voice; with no TTS service configured the contract says so instead of
faking audio.
"""

from __future__ import annotations

import re

import pytest

from tests.integration.test_reupload_supersedes import (  # noqa: F401
    RISHABH_DL, RISHABH_PAN, _store, client, open_case, upload)

DEVANAGARI = re.compile(r"[ऀ-ॿ]")


@pytest.fixture
def case(client):
    a, c = open_case(client)
    upload(client, a, c, [("pan.jpg", RISHABH_PAN), ("dl.jpg", RISHABH_DL)], ["PAN", "DRIVING_LICENCE"])
    return a, c


def ask(client, a, c, message, **extra):
    r = client.post("/api/v1/fos/copilot", json={"applicant_id": a, "case_id": c, "action": "CUSTOM_QUERY",
                                                 "message": message, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_the_frontend_selection_overrides_the_input_language(client, case):
    a, c = case
    body = ask(client, a, c, "KYC kyun fail hua?", response_language="mr")      # typed in Hinglish
    contract = body["language_contract"]
    assert contract["response_language"] == "mr" and contract["selected_by"] == "FRONTEND"
    if contract["localized"]:                     # a Marathi template covers this fact
        assert DEVANAGARI.search(body["answer"]) and body["presentation"]["language"] == "mr"
    assert body["presentation"]["audio"]["language"] == contract["reply_language"]


def test_without_a_selection_the_input_language_decides(client, case):
    a, c = case
    contract = ask(client, a, c, "KYC zala ka?")["language_contract"]
    assert contract.get("selected_by") != "FRONTEND" and contract["response_language"] == "mr"


def test_an_unsupported_selection_is_ignored_not_trusted(client, case):
    a, c = case
    contract = ask(client, a, c, "what is kyc status", response_language="xx-not-real")["language_contract"]
    assert contract.get("selected_by") != "FRONTEND" and contract["response_language"] == "en"


def test_the_audio_block_is_the_same_message_spoken_and_honest_without_a_provider(client, case, monkeypatch):
    monkeypatch.delenv("TTS_PROVIDER_URL", raising=False)
    a, c = case
    body = ask(client, a, c, "show all documents")
    audio = body["presentation"]["audio"]
    assert audio["enabled"] is False and audio["status"] == "CONFIGURATION_GAP" and audio["endpoint"] is None
    assert "PAN" in audio["text"] and "✓" not in audio["text"] and "—" not in audio["text"]
    r = client.post("/api/v1/tts", json={"text": audio["text"], "language": "en"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "CONFIGURATION_GAP"


def test_a_configured_provider_speaks_in_the_response_language_voice(client, monkeypatch):
    import httpx

    from app.tts import service

    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen.update(json)
        return httpx.Response(200, content=b"RIFF....WAVE", headers={"content-type": "audio/wav"})

    monkeypatch.setenv("TTS_PROVIDER_URL", "http://tts.local")
    monkeypatch.setattr(httpx, "post", fake_post)
    r = client.post("/api/v1/tts", json={"text": "✓ PAN — Verified", "language": "en"})
    assert r.status_code == 200 and r.content.startswith(b"RIFF")
    assert seen["voice"] == service.voice_for("en") and seen["text"] == "PAN, Verified."


def test_a_language_without_a_voice_is_refused_never_read_by_another_voice(client, monkeypatch):
    from app.tts import service

    monkeypatch.setenv("TTS_PROVIDER_URL", "http://tts.local")
    assert not service.voice_for("mr")                       # no Marathi voice installed here
    r = client.post("/api/v1/tts", json={"text": "तुमचा KYC पूर्ण झाला आहे.", "language": "mr"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "NO_VOICE_FOR_LANGUAGE"
    assert service.audio_block("x", "mr")["status"] == "NO_VOICE_FOR_LANGUAGE"


def test_a_configured_but_down_tts_service_is_degraded_and_chat_still_answers(client, monkeypatch):
    import socket

    from app.tts import service

    with socket.socket() as s:                               # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("TTS_PROVIDER_URL", f"http://127.0.0.1:{port}")
    service._REACH.clear()
    block = service.audio_block("PAN is verified.", "en")
    assert block["status"] == "DEGRADED" and block["enabled"] is False and block["endpoint"] is None


def test_tts_needs_a_token(client):
    r = client.post("/api/v1/tts", json={"text": "hello", "language": "en"}, headers={"Authorization": ""})
    assert r.status_code == 401
