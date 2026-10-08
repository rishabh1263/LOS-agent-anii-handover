"""
MASTER SPEC section 9 -- every feature speaks through markdown + tts only: review on open, fix-it path and
what-if, the upload answer, timeline, customer message, visit checklist, handoff note (link when ready), checks
passed X of Y. Every dev flag on.
"""

from __future__ import annotations

import pytest

from app.store.models import Document, DocumentStatus
from tests.integration.master_env import make_case, prod, say  # noqa: F401
from tests.integration.test_master_8_contract import check
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


@pytest.fixture
def opened(client, prod, _store):
    a, c = make_case(client, "Rahul Sharma")
    _store.save_document(Document(document_id=f"{c}:{a}:pan", case_id=c, applicant_id=a, party_id=a,
                                  document_type="PAN", status=DocumentStatus.VERIFIED))
    say(client, f"{c} kholo")
    return a, c


def ask(client, message):
    r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": message, "reply_language": "en"})
    assert r.status_code == 200, r.text
    check(r.json())
    return r.json()["markdown"]


@pytest.mark.parametrize("message,expect", [
    ("CPA ke liye kya chahiye?", "checks passed"),
    ("agar bank statement upload karu toh ready ho jayega?", "Not yet"),
    ("case ka timeline", "FOS"),
    ("visit pe kya le jaun", "- [ ] "),
    ("customer ko bata do kya lana hai", "\n> "),
    ("handoff note banao", "ready for CPA"),
])
def test_each_feature_is_markdown_and_tts(client, opened, message, expect):
    assert expect in ask(client, message)


def test_a_ready_case_offers_the_handoff_note_download(client, opened, monkeypatch):
    from app.agents.applicant.copilot.answering import handoff_note

    _, c = opened
    real = handoff_note.build
    monkeypatch.setattr(handoff_note, "build", lambda case_id, officer: {**real(case_id, officer), "ready": True,
                                                                        "markdown": "# Handoff",
                                                                        "generated_at": "2026-10-08T10:00:00"})
    md = ask(client, "handoff note banao")
    assert f"(action:handoff_note?case={c})" in md, md


def test_the_upload_answer_is_markdown_and_tts(client, opened, monkeypatch):
    from app.api.routes import fos_api

    a, c = opened

    async def fake_upload(request, claims, request_id):
        return {"request_id": request_id, "case_id": c, "intent": "UPLOAD_DOCUMENT",
                "answer": "📄 PAN received -- name on document: RAHUL SHARMA. Verification is running.\n"
                          "👉 Upload the Bank Statement next."}

    monkeypatch.setattr(fos_api, "_copilot_upload", fake_upload)
    r = client.post("/api/v1/fos/copilot", data={"action": "UPLOAD_DOCUMENT"}, files={"file": ("p.jpg", b"x")})
    assert r.status_code == 200, r.text
    check(r.json())
    assert r.json()["markdown"].startswith("PAN received") and "Next step" in r.json()["tts"]
