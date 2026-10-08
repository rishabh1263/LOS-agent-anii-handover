"""
MASTER SPEC section 7 -- the FAQ from config: shown on "help", on case open; context-aware (hidden when it does not
apply, boosted when it matters now); rendered as ask: links inside the markdown; EVERY item answered correctly in
the state it is shown in (golden).
"""

from __future__ import annotations

import pytest

from app.agents.applicant.copilot.capabilities import faq
from app.store.models import CaseFinding, Document, DocumentStatus, FindingKind
from tests.integration.master_env import make_case, prod, say  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

REFUSED_OR_LOST = {"UNKNOWN", "GUARDRAIL_BLOCKED", "OUT_OF_SCOPE"}


def _kyc_fail(_store, a, c):
    _store.save_finding(CaseFinding(finding_id="k", case_id=c, party_id=a, finding_kind=FindingKind.KYC, status="FAIL",
                                    reason_codes=["NAME_MISMATCH"], content_hash="k",
                                    payload={"fields": [{"field": "NAME", "status": "FAIL", "sources": []}]}))
    _store.save_document(Document(document_id=f"{c}:{a}:pan", case_id=c, applicant_id=a, party_id=a,
                                  document_type="PAN", status=DocumentStatus.VERIFIED))


def test_help_without_a_case_shows_the_my_cases_questions(client, prod):
    make_case(client, "Rahul Sharma")
    md = say(client, "help")
    assert "**My cases:**" in md and "(ask:" in md and "**Documents:**" not in md, md


def test_help_inside_a_case_is_about_that_case(client, prod):
    _, c = make_case(client, "Rahul Sharma")
    say(client, f"{c} kholo")
    md = say(client, "kya pooch sakta hoon")
    assert md.split("\n")[0] == c and "**This case:**" in md and "**My cases:**" not in md, md


def test_help_me_upload_is_not_the_faq(client, prod):
    _, c = make_case(client, "Rahul Sharma")
    say(client, f"{c} kholo")
    assert "**This case:**" not in say(client, "help me upload the PAN")


def test_the_faq_shows_on_open_after_the_review(client, prod):
    _, c = make_case(client, "Rahul Sharma")
    md = say(client, f"{c} kholo")
    assert "Common questions for this case:" in md
    if "Review:" in md:
        assert md.index("Review:") < md.index("Common questions for this case:")


def test_kyc_failed_boosts_the_kyc_questions(client, prod, _store):
    a, c = make_case(client, "Rahul Sharma")
    _kyc_fail(_store, a, c)
    shown = [i["id"] for i in faq.items(c, "en", 4)]
    assert "kyc_status" in shown[:2], shown
    assert "coapp_docs" not in [i["id"] for i in faq.items(c, "en")]          # no co-applicant: hidden


def test_a_new_faq_item_in_config_appears_without_code(client, prod, monkeypatch):
    _, c = make_case(client, "Rahul Sharma")
    item = {"id": "x_new", "category": "general", "q": {"en": "Who is my branch manager?"}, "priority": 999}
    monkeypatch.setitem(faq.cfg(), "items", list(faq.cfg()["items"]) + [item])
    assert "Who is my branch manager?" in say(client, "help")


@pytest.mark.parametrize("state", ["no_case", "open", "kyc_failed"])
def test_every_shown_faq_item_is_answered(client, prod, _store, monkeypatch, state):
    """Golden: each item shown in this state, sent as its link sends it, gets a real answer (never lost / refused)."""
    a, c = make_case(client, "Rahul Sharma")
    if state == "kyc_failed":
        _kyc_fail(_store, a, c)
    case_id = None if state == "no_case" else c
    shown = faq.items(case_id, "en")
    assert shown
    monkeypatch.setenv("COPILOT_MD_TTS_CONTRACT", "false")            # read the intent of each answer
    for item in shown:
        if case_id:
            client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": c})
        else:
            client.post("/api/v1/fos/copilot", json={"action": "EXIT_CASE"})
        r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": item["send"],
                                                     "reply_language": "en"})
        assert r.status_code == 200, (item, r.text)
        body = r.json()
        assert body.get("intent") not in REFUSED_OR_LOST, (item["id"], body.get("intent"), body.get("answer"))
        assert not body.get("clarification_required") or body.get("intent") == "CASE_SELECTION", \
            (item["id"], body.get("answer"))
