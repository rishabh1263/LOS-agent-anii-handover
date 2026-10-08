"""
MASTER SPEC section 8 -- every chat reply is ONLY {request_id, markdown, tts}: no sentence twice in the markdown,
tts plain (no markdown, links, symbols, ids by character), tts facts a subset of the markdown's, every action link
registered and scope-checked when used, the language lock on both.
"""

from __future__ import annotations

import os
import re

import pytest

from app.agents.applicant.copilot.answering import contract
from app.store.models import Document, DocumentStatus
from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import FOS_SCOPES, _store, client  # noqa: F401

FOS, UNIVERSAL, STREAM = "/api/v1/fos/copilot", "/api/v1/copilot/query", "/api/v1/fos/copilot/stream"
SYMBOLS = re.compile(r"[*_`#|>\[\]]|\(action:|\(ask:|https?://")


@pytest.fixture
def on(monkeypatch, demo):
    for flag in ("COPILOT_MD_TTS_CONTRACT", "COPILOT_LANGUAGE_LOCK", "COPILOT_PROFESSIONAL_FORMAT",
                 "COPILOT_READINESS_REPORT", "COPILOT_KYC_TABLE", "COPILOT_CUSTOMER_MESSAGE"):
        monkeypatch.setenv(flag, "true")


def post(client, path, **body):
    if path == FOS:
        body.setdefault("action", "CUSTOM_QUERY")
    r = client.post(path, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _facts(text: str) -> set[str]:
    """Numbers and ids-tails a sentence states (what a fact check compares)."""
    return set(re.findall(r"\b\d+\b", text))


def check(reply: dict, lang: str = "en") -> None:
    assert set(reply) == {"request_id", "markdown", "tts"}, set(reply)
    md, spoken = reply["markdown"], reply["tts"]
    if os.getenv("DUMP_CONTRACT"):
        print(f"\n----- markdown\n{md}\n----- tts\n{spoken}\n")
    assert md.strip() and spoken.strip()
    lines = [re.sub(r"[\s*_>#-]+", " ", ln).strip().lower() for ln in md.split("\n")]
    lines = [ln for ln in lines if ln and not ln.startswith("|")]
    assert len(lines) == len(set(lines)), md                       # no sentence twice
    assert not SYMBOLS.search(spoken), spoken                      # plain speech
    assert not re.search(r"\b(CASE|APP|COAPP)-[0-9A-F]{6,}", spoken), spoken     # no id read out in full
    assert len(re.findall(r"[.!?।](?:\s|$)", spoken)) <= contract.cfg()["tts"]["max_sentences"], spoken
    for kind, name, params in (contract.parse(h) for _, h in contract.LINK.findall(md)):
        if kind == "action":
            spec = contract.registry()[name]                           # every action registered
            assert set(spec.get("params") or []) <= set(params) | {"party"}, (name, params)


# ---- unit: the renderer --------------------------------------------------------------------------------------
def test_links_come_only_from_the_registry():
    assert contract.link("open_case", id="CASE-852C") == "[Open](action:open_case?id=CASE-852C)"
    with pytest.raises(KeyError):
        contract.link("delete_everything")
    assert contract.request_for("action:open_case?id=CASE-852C") == {"action": "OPEN_CASE", "case_id": "CASE-852C"}
    assert contract.request_for("action:copy?ref=draft-1") is None               # client only
    assert contract.request_for(contract.ask("What is pending?").split("](")[1][:-1]) == \
        {"action": "CUSTOM_QUERY", "message": "What is pending?"}


def test_tts_speaks_amounts_dates_and_ids():
    spoken = contract.speakable("Loan ₹5,00,000 on CASE-DA842B2E38A9, filed 2026-10-08.", "en")
    assert "five lakh rupees" in spoken and "case ending 38A9" in spoken and "8 October 2026" in spoken


def test_tts_summarises_a_list_by_status_and_keeps_the_next_step():
    md = ("CPA readiness: 2 of 4 checks passed.\n\n**Applicant documents**\n"
          "- Address Proof: **PENDING** -- not uploaded yet.\n- Bank Statement: **PENDING** -- not uploaded yet.\n\n"
          "**Next step:** Upload the Address Proof.")
    spoken = contract.tts(md, "en")
    assert spoken == ("CPA readiness: 2 of 4 checks passed. Pending: Address Proof and Bank Statement. "
                      "Next step: Upload the Address Proof.")


def test_dedupe_drops_a_repeated_sentence():
    assert contract.dedupe("Two documents pending.\n- PAN\nTwo documents pending.") == "Two documents pending.\n- PAN"


# ---- end to end, both endpoints -----------------------------------------------------------------------------
@pytest.mark.parametrize("path", [FOS, UNIVERSAL])
def test_the_case_list_is_a_table_with_open_links(client, on, path):
    _, c = make_case(client, "Rahul Sharma")
    reply = post(client, path, message="my cases", reply_language="en")
    check(reply)
    assert f"[Open](action:open_case?id={c})" in reply["markdown"] and "| Case |" in reply["markdown"]


@pytest.mark.parametrize("path", [FOS, UNIVERSAL])
def test_an_in_case_answer(client, on, _store, path):
    a, c = make_case(client, "Rahul Sharma")
    _store.save_document(Document(document_id=f"{c}:{a}:pan", case_id=c, applicant_id=a, party_id=a,
                                  document_type="PAN", status=DocumentStatus.VERIFIED))
    post(client, FOS, action="OPEN_CASE", case_id=c)
    reply = post(client, path, message="CPA ke liye kya chahiye?", reply_language="en")
    check(reply)
    assert "checks passed" in reply["markdown"] and "Next step" in reply["tts"]
    assert _facts(reply["tts"]) <= _facts(reply["markdown"])          # tts facts are the screen's facts


def test_the_language_lock_holds_for_both_fields(client, on):
    make_case(client, "Rahul Sharma")
    reply = post(client, FOS, message="mere cases dikhao", reply_language="hi")
    check(reply, "hi")
    assert re.search(r"[ऀ-ॿ]", reply["markdown"]) and re.search(r"[ऀ-ॿ]", reply["tts"])


def test_a_clarification_offers_its_options_as_ask_links(client, on):
    make_case(client, "Rahul Sharma")
    make_case(client, "Rahul Verma")
    reply = post(client, FOS, message="Rahul ka case kholo", reply_language="en")
    check(reply)
    assert reply["markdown"].count("](ask:") >= 2


def test_an_action_link_is_posted_back_and_scope_checked_again(client, on, make_token):
    from fastapi.testclient import TestClient

    import main

    other = TestClient(main.app)
    other.headers.update({"Authorization": f"Bearer {make_token(subject='other-officer', scopes=FOS_SCOPES)}"})
    _, theirs = make_case(other, "Sunita Rao")
    _, mine = make_case(client, "Rahul Sharma")
    opened = post(client, FOS, action_link=f"action:open_case?id={mine}", reply_language="en")
    assert mine in opened["markdown"]
    assert client.post(FOS, json={"action_link": f"action:open_case?id={theirs}"}).status_code == 403


def test_the_customer_message_is_a_quote_with_a_copy_link(client, on, monkeypatch):
    _, c = make_case(client, "Rahul Sharma")
    post(client, FOS, action="OPEN_CASE", case_id=c)
    reply = post(client, FOS, message="customer ko bata do kya lana hai", reply_language="en")
    check(reply)
    assert "\n> " in reply["markdown"] and "(action:copy?ref=draft-1)" in reply["markdown"]


def test_follow_ups_work_without_an_echoed_context(client, on):
    _, c = make_case(client, "Rahul Sharma")
    post(client, FOS, action="OPEN_CASE", case_id=c)
    first = post(client, FOS, message="PAN ka status kya hai?", reply_language="en")
    again = post(client, FOS, message="aur bank statement?", reply_language="en")
    check(first)
    check(again)
    assert "bank statement" in again["markdown"].lower()


def test_the_stream_ends_with_the_same_contract(client, on, monkeypatch):
    monkeypatch.setenv("COPILOT_STREAMING", "true")
    make_case(client, "Rahul Sharma")
    with client.stream("POST", STREAM, json={"action": "CUSTOM_QUERY", "message": "my cases",
                                             "reply_language": "en"}) as r:
        body = "".join(r.iter_text())
    import json

    finals = [json.loads(b.split("data: ", 1)[1]) for b in body.split("\n\n")
              if b.startswith("event: answer") or b.startswith("event: final")]
    assert finals and set(finals[-1]) - {"latency"} == {"request_id", "markdown", "tts"}


def test_the_compatibility_flag_restores_the_old_envelope(client, on, monkeypatch):
    monkeypatch.setenv("COPILOT_MD_TTS_CONTRACT", "false")
    make_case(client, "Rahul Sharma")
    assert "answer" in post(client, FOS, message="my cases")
