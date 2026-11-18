"""NLU frame + typo correction + turn log (2026-10-14): deterministic, no model, no case data."""
from __future__ import annotations

import json

import pytest

from app.agents.applicant import normalize
from app.agents.applicant.copilot.semantics import entities


@pytest.mark.parametrize("typed, fixed", [
    ("pndng docs kya h is file me", "pending docs kya h is file me"),
    ("is pan verfied", "is pan verified"),
    ("adress proof uploaded?", "address proof uploaded?"),
    ("is the signatre uploded", "is the signature uploaded"),
])
def test_sms_and_misspellings_become_known_words(typed, fixed):
    assert normalize.correct_typos(typed).text == fixed


@pytest.mark.parametrize("typed", ["hi", "thx a lot", "ok so its only rahul then?", "CASE-1234 open kro",
                                   "is rahul eligible for the loan?", "voter id ok?", "kya bacha h", "PAN DL"])
def test_names_ids_acronyms_and_known_words_are_never_changed(typed):
    assert normalize.correct_typos(typed).text == typed


def test_documents_in_typed_order():
    assert entities.extract("is pan verified? and bank statement?").documents == ["PAN", "BANK_STATEMENT"]


@pytest.mark.parametrize("typed", ["uske paas DL nahi h, kuch aur chalega address ke liye?",
                                   "he doesnt have DL, what else works for address proof?"])
def test_a_document_the_customer_lacks(typed):
    frame = entities.extract(typed)
    assert frame.lacks == ["DRIVING_LICENCE"] and frame.field is None


def test_a_negated_status_is_not_a_lack():
    frame = entities.extract("is signature not uploaded yet?")
    assert frame.status_not == ["SIGNATURE"] and frame.lacks == []


def test_party_stage_field_and_ids():
    assert entities.extract("co-applicant ka pan verified hai?").party == "CO_APPLICANT"
    assert entities.extract("what comes after credit?").stage == "CREDIT"
    assert entities.extract("what is the loan amount").field == "loan_amount"
    assert entities.extract("open CASE-ABC123 and show voter id status").case_ids == ["CASE-ABC123"]


def test_turn_log_writes_masked_rows_and_an_unreviewed_label(tmp_path, monkeypatch):
    from app.observability import turn_log

    monkeypatch.setenv("TURN_LOG_IN_TESTS", "1")
    monkeypatch.setattr(turn_log, "cfg", lambda: {"enabled": True, "path": str(tmp_path / "t.jsonl"),
                                                  "labelled_path": str(tmp_path / "l.jsonl")})
    monkeypatch.setattr(turn_log, "_ROOT", tmp_path.parent)
    turn_log.record(chat_key="u|c1", typed="hmm the paper ABCDE1234F", fixed=None,
                    reply={"intent": "CLARIFICATION"}, meaning_intent=None, layer="qwen", started=None)
    turn_log.record(chat_key="u|c1", typed="the pending stuff", fixed=None,
                    reply={"intent": "AGENT_ANSWER", "response_source": "AGENT", "tools_invoked": ["pending_documents"]},
                    meaning_intent=None, layer="qwen", started=None)
    rows = [json.loads(x) for x in (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["outcome"] for r in rows] == ["asked", "answered"] and "ABCDE1234F" not in json.dumps(rows)
    labelled = [json.loads(x) for x in (tmp_path / "l.jsonl").read_text(encoding="utf-8").splitlines()]
    assert labelled == [{**labelled[0], "intent": "pending_documents", "reviewed": False}]
    assert "ABCDE1234F" not in labelled[0]["q"] and rows[0]["chat"] and "u|c1" not in json.dumps(rows)


@pytest.mark.parametrize("filler", ["hmm", "hmmm", "mmm", "ok", "okk", "hn"])
def test_a_filler_is_never_turned_into_a_word(filler):
    assert normalize.correct_typos(filler).text == filler          # "hmm" once became "home"
