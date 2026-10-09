"""
PHASE 3 STEP 3 -- quick wins 2, 3 and 5, each behind its own flag (default off):

  COPILOT_SINGLE_CASE_RESOLVE    no case id: one case answered (named), several listed and asked
  COPILOT_TERMS_KNOWLEDGE        "CIBIL kya hai?" answered from the knowledge base, never refused
  COPILOT_LOCALIZED_KYC_REASONS  the recorded mismatch clause in the reply's language

With every flag off, the answers are exactly what they were.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.agents.applicant.copilot.answering.localize import _reason
from tests.integration.test_fos_stage_boundary import open_case, upload
from tests.integration.test_reupload_supersedes import OTHER_PAN, RISHABH_DL, _store, client  # noqa: F401

FLAGS = ("COPILOT_SINGLE_CASE_RESOLVE", "COPILOT_TERMS_KNOWLEDGE", "COPILOT_LOCALIZED_KYC_REASONS")


@pytest.fixture(autouse=True)
def flags_off(monkeypatch):
    for name in FLAGS:
        monkeypatch.delenv(name, raising=False)


def ask(client, applicant_id, message, case_id=None):
    body = {"applicant_id": applicant_id, "action": "CUSTOM_QUERY", "message": message}
    if case_id:
        body["case_id"] = case_id
    return client.post("/api/v1/fos/copilot", json=body)


# ---- quick win 2: no case id ----------------------------------------------------------
def test_no_case_id_flag_off_is_unchanged(client):
    a, _ = open_case(client)
    r = ask(client, a, "status batao")
    assert r.status_code == 200 and not r.json()["answer"].startswith(_ := "For application")


def test_no_case_id_with_one_case_answers_for_it_and_names_it(client, monkeypatch):
    monkeypatch.setenv("COPILOT_SINGLE_CASE_RESOLVE", "true")
    a, c = open_case(client)
    body = ask(client, a, "status batao").json()
    from app.agents.applicant.copilot.answering import contract

    prefix = str((contract.cfg().get("style") or {}).get("resolved_case_prefix") or "{case_id}: ").format(case_id=c)
    assert body["answer"].startswith(prefix), body["answer"]           # the case is named (wording: config)
    assert body.get("case_resolved_from_applicant") is True and body.get("case_id") == c


def test_no_case_id_with_several_cases_lists_them_and_asks(client, monkeypatch, _store):
    monkeypatch.setenv("COPILOT_SINGLE_CASE_RESOLVE", "true")
    a, c = open_case(client)
    second = "case_" + "2" * 32
    _store.save_application(dataclasses.replace(_store.get_application(c), case_id=second))
    _store.grant_access("test-subject", "CASE", second)
    body = ask(client, a, "status batao").json()
    assert body["intent"] == "CASE_SELECTION", body
    assert c in body["answer"] and second in body["answer"] and "Which one" in body["answer"]
    assert body["clarification_required"]["reason"] == "CASE_NOT_SPECIFIED"


def test_no_case_id_for_someone_elses_applicant_is_refused(client, monkeypatch, make_token):
    monkeypatch.setenv("COPILOT_SINGLE_CASE_RESOLVE", "true")
    a, _ = open_case(client)
    from tests.integration.test_fos_stage_boundary import FOS_SCOPES

    client.headers.update({"Authorization": f"Bearer {make_token(subject='someone-else', scopes=FOS_SCOPES)}"})
    r = ask(client, a, "status batao")
    assert r.status_code == 403, r.text


# ---- quick win 3: lending-term definitions ----------------------------------------------
def test_cibil_definition_flag_off_is_unchanged(client, monkeypatch):
    monkeypatch.setenv("COPILOT_TERMS_KNOWLEDGE", "false")      # the flag this test is about, set explicitly
    a, c = open_case(client)
    body = ask(client, a, "CIBIL kya hai?", c).json()
    assert body["intent"] == "OUT_OF_SCOPE"


@pytest.mark.parametrize("question,expect", [("CIBIL kya hai?", "credit"), ("what is CIBIL?", "bureau"),
                                             ("FOIR ka matlab", "obligation")])
def test_a_lending_term_definition_is_answered_from_knowledge(client, monkeypatch, question, expect):
    monkeypatch.setenv("COPILOT_TERMS_KNOWLEDGE", "true")
    a, c = open_case(client)
    body = ask(client, a, question, c).json()
    # the general layer (2026-10-08) answers a definition before the agent: DEFINITION, same content
    assert body["intent"] in ("FOS_KNOWLEDGE", "DEFINITION"), body
    assert expect in body["answer"].lower(), body["answer"]


@pytest.mark.parametrize("question", ["mera cibil score kitna hai?", "what is my CIBIL score?"])
def test_a_question_about_the_case_score_is_still_not_a_definition(client, monkeypatch, question):
    monkeypatch.setenv("COPILOT_TERMS_KNOWLEDGE", "true")
    a, c = open_case(client)
    body = ask(client, a, question, c).json()
    assert body["intent"] == "OUT_OF_SCOPE", body                 # the case's score: still routed downstream


# ---- quick win 5: the mismatch clause in the reply's language -----------------------------
TWO = ("the name on the PAN, LAXMI GUPTA, does not match the driving licence name, RISHABH SINGH; "
       "the date of birth didn't match: the PAN says 2004-12-20, but the driving licence says 2002-06-12")


def test_kyc_reasons_flag_off_keeps_todays_text():
    # TODAY'S OUTPUT, pinned: the first clause localized, the second left in English inside it
    assert _reason(TWO, "hi-Latn") == ("PAN par naam LAXMI GUPTA hai, lekin driving licence par RISHABH SINGH; "
                                       "the date of birth didn't match: the PAN says 2004-12-20, but the "
                                       "driving licence says 2002-06-12")


def test_kyc_reasons_every_clause_localized_values_exact(monkeypatch):
    monkeypatch.setenv("COPILOT_LOCALIZED_KYC_REASONS", "true")
    said = _reason(TWO, "hi-Latn")
    assert "does not match" not in said and "didn't match" not in said, said
    for value in ("LAXMI GUPTA", "RISHABH SINGH", "2004-12-20", "2002-06-12"):
        assert value in said
    assert "naam" in said and "janmatithi" in said


def test_kyc_reasons_unknown_clause_stays_as_recorded(monkeypatch):
    monkeypatch.setenv("COPILOT_LOCALIZED_KYC_REASONS", "true")
    assert _reason("a reviewer must look at it", "hi-Latn") == "a reviewer must look at it"


def test_the_hinglish_status_answer_carries_no_english_mismatch_clause(client, monkeypatch):
    monkeypatch.setenv("COPILOT_LOCALIZED_KYC_REASONS", "true")
    a, c = open_case(client)
    upload(client, a, c, [("pan.jpg", OTHER_PAN), ("dl.jpg", RISHABH_DL)], ["PAN", "DRIVING_LICENCE"])
    answer = ask(client, a, "status batao", c).json()["answer"]
    assert "does not match" not in answer and "didn't match" not in answer, answer
