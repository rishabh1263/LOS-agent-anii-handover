"""
MASTER SPEC section 4 -- the case workspace and follow-ups, with every dev flag on and the markdown + tts reply:
open by number / id / name, the case line first, review on open, the other case, pronoun / bare follow-ups
rewritten from state, yes/no to the pending question, a correction, a new chat starting fresh.
"""

from __future__ import annotations

import logging

from app.store.models import Document, DocumentStatus
from tests.integration.master_env import make_case, prod, say  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


def _seed(_store, a, c):
    _store.save_document(Document(document_id=f"{c}:{a}:pan", case_id=c, applicant_id=a, party_id=a,
                                  document_type="PAN", status=DocumentStatus.VERIFIED))
    _store.save_document(Document(document_id=f"{c}:{a}:dl", case_id=c, applicant_id=a, party_id=a,
                                  document_type="DRIVING_LICENCE", status=DocumentStatus.REJECTED,
                                  reason_codes=["DOCUMENT_UNREADABLE"]))


def test_open_by_number_then_every_answer_starts_with_the_case_line(client, prod, _store):
    a, c = make_case(client, "Rahul Sharma")
    _seed(_store, a, c)
    say(client, "my cases")
    opened = say(client, "1")
    assert c in opened.split("\n")[0] and "Next step:" in opened, opened      # the case brief on open
    answer = say(client, "kya baaki hai?")
    assert answer.split("\n")[0].strip() == c, answer


def test_open_by_name_and_same_name_asks(client, prod):
    make_case(client, "Rahul Sharma")
    make_case(client, "Rahul Verma")
    md = say(client, "Rahul ka case kholo")
    assert "Which one" in md and md.count("](ask:") >= 2


def test_bare_follow_ups_use_the_state(client, prod, _store, caplog):
    a, c = make_case(client, "Rahul Sharma")
    _seed(_store, a, c)
    say(client, f"{c} kholo")
    first = say(client, "driving licence ka status kya hai?")
    assert "reject" in first.lower(), first
    with caplog.at_level(logging.INFO, logger="app.agents.applicant.copilot.answering.contract"):
        why = say(client, "kyu?")
    assert "unreadable" in why.lower() or "read" in why.lower(), why
    assert any("follow-up" in r.message and "original=" in r.message for r in caplog.records)


def test_aur_switches_the_document_keeping_the_question(client, prod, _store):
    a, c = make_case(client, "Rahul Sharma")
    _seed(_store, a, c)
    say(client, f"{c} kholo")
    say(client, "PAN ka status?")
    md = say(client, "aur driving licence?")
    assert "driving licence" in md.lower() and ("reject" in md.lower() or "did not pass" in md.lower()), md


def test_another_case_while_one_is_open_is_answered_then_offers_the_switch(client, prod):
    _, c1 = make_case(client, "Rahul Sharma")
    _, c2 = make_case(client, "Priya Verma")
    say(client, f"{c1} kholo")
    md = say(client, f"{c2} ka stage kya hai")
    assert c2 in md and "action:open_case" not in md.split("\n")[0]


def test_exit_and_switch(client, prod):
    _, c1 = make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    say(client, f"{c1} kholo")
    md = say(client, "bahar aao")
    assert "closed" in md.lower() and "action:open_case" in md


def test_a_new_chat_starts_fresh(client, prod, _store):
    a, c = make_case(client, "Rahul Sharma")
    _seed(_store, a, c)
    say(client, f"{c} kholo", chat_id="chat-1")
    say(client, "PAN ka status?", chat_id="chat-1")
    fresh = say(client, "kyu?", chat_id="chat-2", new_chat=True)
    assert "unreadable" not in fresh.lower()
