"""
FOS PLAN SECTION 2 -- LANGUAGE LOCK. The frontend's selected language (reply_language / response_language /
language) is authoritative for EVERY text of the reply on BOTH endpoints: Hinglish typed + English selected ->
English answers, clarifications, buttons, chips and refusals. Nothing selected -> the previous behaviour.
"""

from __future__ import annotations

import re

import pytest

from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

HINGLISH = re.compile(r"\b(hai|hain|kripya|baaki|aapka|aapke|aapki|kijiye|kholiye|boliye|abhi|nahi|mein|"
                      r"jaana|karein|hua|gaya|kya|kaunse|dikhao)\b", re.I)
TURNS = ["mere cases dikhao", "1", "kya baaki hai?", "status kya hai?", "KYC ka kya status hai?", "kyu atka hai?",
         "loan kitna hai?", "PAN number kya hai?", "kaunse documents upload hue?", "CPA mein kab jayega?",
         "verify karna hai", "KYC kya hai?", "co-applicant hai kya?", "summary do", "next kya karna hai?", "salary",
         "list of kaise sajao", "manager ne approve kar diya", "mere Aadhar case ka details kaise dekhun",
         "dusra case"]
ENDPOINTS = ["/api/v1/fos/copilot", "/api/v1/copilot/query"]


def _all_text(reply: dict) -> str:
    workspace = (reply.get("presentation") or {}).get("workspace") or {}
    return " ".join([str(reply.get("answer") or "")] + [str(s) for s in reply.get("suggested_questions") or []]
                    + [str(b.get("label") or "") for b in workspace.get("buttons") or []])


@pytest.fixture
def two_cases(client, demo, monkeypatch):
    monkeypatch.setenv("COPILOT_LANGUAGE_LOCK", "true")             # ON in dev; pinned off for the rest of the suite
    make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    from app.agents.applicant.copilot.capabilities import safety

    safety.reset()


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("field", ["reply_language", "response_language", "language"])
def test_hinglish_typed_english_selected_is_english_everywhere(client, two_cases, endpoint, field):
    if (field == "language" and "fos" in endpoint) or (field == "response_language" and "fos" not in endpoint):
        pytest.skip("each endpoint's own field name plus the shared reply_language")
    leaks = []
    for message in TURNS:
        body = {"message": message, field: "en"} | ({"action": "CUSTOM_QUERY"} if "fos" in endpoint else {})
        reply = client.post(endpoint, json=body).json()
        found = sorted({w.lower() for w in HINGLISH.findall(_all_text(reply))})
        if found:
            leaks.append((message, found, _all_text(reply)[:120]))
    assert not leaks, leaks


def test_hinglish_selected_gets_the_hinglish_workspace_labels(client, two_cases):
    reply = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "show my cases",
                                                      "reply_language": "hi-Latn"}).json()
    assert "Kis case mein jaana hai?" in reply["answer"]
    opened = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "1",
                                                       "reply_language": "hi-Latn"}).json()
    labels = [b["label"] for b in opened["presentation"]["workspace"]["buttons"]]
    assert "Kya baaki hai?" in labels


def test_nothing_selected_keeps_the_previous_wording(client, two_cases):
    reply = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "my cases"}).json()
    assert "Kis case mein jaana hai?" in reply["answer"]          # the demo's Hinglish, as before the lock


def test_the_lock_can_be_switched_off(client, two_cases, monkeypatch):
    monkeypatch.setenv("COPILOT_LANGUAGE_LOCK", "false")
    reply = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "mere cases dikhao",
                                                      "reply_language": "en"}).json()
    assert "Kis case mein jaana hai?" in reply["answer"]
