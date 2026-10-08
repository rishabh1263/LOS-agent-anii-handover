"""
FOS PLAN SECTION 3 -- PROFESSIONAL FORMAT on BOTH endpoints: no emoji in any reply field or button, the closing hint
as one "Next step:" line, a plain-text answer without markdown. Facts are never changed by it.
"""

from __future__ import annotations

import pytest

from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

from app.agents.applicant.copilot.answering import professional

TURNS = ["mere cases dikhao", "1", "kya baaki hai?", "status kya hai?", "KYC ka kya status hai?", "loan kitna hai?",
         "CPA mein kab jayega?", "verify karna hai", "KYC kya hai?", "summary do", "manager ne approve kar diya"]


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


@pytest.fixture
def on(client, demo, monkeypatch):
    monkeypatch.setenv("COPILOT_PROFESSIONAL_FORMAT", "true")
    make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    from app.agents.applicant.copilot.capabilities import safety

    safety.reset()


@pytest.mark.parametrize("endpoint", ["/api/v1/fos/copilot", "/api/v1/copilot/query"])
def test_no_emoji_anywhere_in_any_reply(client, on, endpoint):
    found = []
    for message in TURNS:
        body = {"message": message} | ({"action": "CUSTOM_QUERY"} if "fos" in endpoint else {})
        reply = client.post(endpoint, json=body).json()
        visible = {k: reply.get(k) for k in ("answer", "answer_markdown", "answer_plain", "suggested_questions",
                                              "presentation", "actions", "clarification_required")}
        for text in _strings(visible):
            if professional._EMOJI.search(text):
                found.append((message, text[:80]))
    assert not found, found[:5]


def test_the_closing_hint_is_one_next_step_line_and_the_plain_answer_has_no_markdown(client, on):
    client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "mere cases dikhao"})
    client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "1"})
    reply = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "kya baaki hai?"}).json()
    assert reply["answer"].splitlines()[-1].startswith("**Next step:**"), reply["answer"]
    assert "**" not in reply["answer_plain"] and "Next step:" in reply["answer_plain"]


def test_facts_are_unchanged_only_pictographs_go(monkeypatch):
    monkeypatch.setenv("COPILOT_PROFESSIONAL_FORMAT", "true")
    text ="📍 CASE-AB12 · ₹5,00,000 · FOS → CPA\n👉 Upload the PAN"
    out = professional.apply({"answer": text})
    assert "CASE-AB12" in out["answer"] and "₹5,00,000" in out["answer"] and "FOS → CPA" in out["answer"]
    assert "📍" not in out["answer"] and out["answer"].endswith("**Next step:** Upload the PAN")


def test_off_keeps_the_emoji_style(client, demo, monkeypatch):
    monkeypatch.setenv("COPILOT_PROFESSIONAL_FORMAT", "false")
    make_case(client, "Rahul Sharma")
    reply = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "my cases"}).json()
    assert reply["answer"].startswith("📂")
