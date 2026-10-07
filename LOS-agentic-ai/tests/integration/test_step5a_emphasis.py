"""
PHASE 3 STEP 5a -- bold key words (COPILOT_EMPHASIS, default off).

Sent three ways (frontend markdown support unconfirmed): `emphasis` (structured, with
positions), `answer_markdown` (**bold**) and `answer_plain` (no markers). `answer` itself
never changes. At most two short terms, taken from the response's structured data.
"""

from __future__ import annotations

import pytest

from app.agents.applicant.copilot.answering import emphasis
from tests.integration.test_fos_stage_boundary import open_case, upload
from tests.integration.test_reupload_supersedes import OTHER_PAN, RISHABH_DL, RISHABH_PAN, _store, client  # noqa: F401


@pytest.fixture(autouse=True)
def flag_off(monkeypatch):
    monkeypatch.delenv(emphasis.FLAG, raising=False)
    monkeypatch.delenv("COPILOT_DOCUMENT_ACTIONS", raising=False)


# ---- the rules ---------------------------------------------------------------------------
def test_status_and_document_name_are_bolded():
    out = emphasis.apply({"answer": "Your PAN is verified.", "documents": [{"document_type": "PAN"}]})
    assert out["answer_markdown"] == "Your **PAN** is **verified**."
    assert [t["text"] for t in out["emphasis"]] == ["PAN", "verified"]
    assert out["answer"] == "Your PAN is verified."                   # unchanged


def test_never_more_than_two_terms_and_never_a_sentence():
    out = emphasis.apply({"answer": "PAN is verified, Bank Statement is pending, Salary Slip was rejected.",
                          "documents": [{"document_type": t} for t in ("PAN", "BANK_STATEMENT", "SALARY_SLIP")]})
    assert len(out["emphasis"]) <= 2
    assert all(len(t["text"].split()) <= emphasis.MAX_WORDS for t in out["emphasis"])


def test_an_amount_is_bolded_when_there_is_no_status():
    out = emphasis.apply({"answer": "The loan amount on the application is ₹5,00,000."})
    assert "**₹5,00,000**" in out["answer_markdown"]


def test_the_mismatch_field_from_document_actions_comes_first():
    out = emphasis.apply({"answer": "Name mismatch: PAN shows \"A\", Driving Licence shows \"B\". PAN is verified.",
                          "document_actions": {"emphasis": ["Name"]}, "documents": [{"document_type": "PAN"}]})
    assert out["emphasis"][0]["text"] == "Name"


def test_positions_point_at_the_terms_in_answer_plain():
    out = emphasis.apply({"answer": "Your **PAN** is verified.", "documents": [{"document_type": "PAN"}]})
    assert out["answer_plain"] == "Your PAN is verified."               # existing markdown removed
    for t in out["emphasis"]:
        assert out["answer_plain"][t["start"]:t["end"]] == t["text"]


def test_a_word_inside_another_word_is_not_bolded():
    out = emphasis.apply({"answer": "The PANEL review is pending.", "documents": [{"document_type": "PAN"}]})
    assert "**PAN**" not in out["answer_markdown"]


def test_nothing_to_emphasise_leaves_markdown_equal_to_plain():
    out = emphasis.apply({"answer": "Hello! How can I help with the application today?"})
    assert out["emphasis"] == [] and out["answer_markdown"] == out["answer_plain"]


# ---- through the real API ----------------------------------------------------------------
def ask(client, a, c, message):
    r = client.post("/api/v1/fos/copilot", json={"applicant_id": a, "case_id": c, "action": "CUSTOM_QUERY",
                                                 "message": message})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def case(client):
    a, c = open_case(client)
    upload(client, a, c, [("pan.jpg", RISHABH_PAN), ("dl.jpg", RISHABH_DL)], ["PAN", "DRIVING_LICENCE"])
    return a, c


def test_flag_off_the_response_has_no_emphasis_fields(client, case):
    body = ask(client, *case, "is my PAN verified?")
    assert "answer_markdown" not in body and "answer_markdown" not in body["presentation"]


def test_flag_on_a_real_answer_carries_all_three(client, case, monkeypatch):
    monkeypatch.setenv(emphasis.FLAG, "true")
    body = ask(client, *case, "is my PAN verified?")
    assert body["answer_plain"] and "**" not in body["answer_plain"]
    assert body["emphasis"] and all(body["answer_plain"][t["start"]:t["end"]] == t["text"] for t in body["emphasis"])
    assert body["answer_markdown"].count("**") == 2 * len(body["emphasis"])
    assert body["presentation"]["answer_markdown"] == body["answer_markdown"]


def test_flag_on_hinglish_answer_is_emphasised_too(client, case, monkeypatch):
    monkeypatch.setenv(emphasis.FLAG, "true")
    body = ask(client, *case, "PAN verify hua kya?")
    assert body["emphasis"], body["answer"]


def test_positions_survive_the_routes_pii_masking(client, monkeypatch):
    monkeypatch.setenv(emphasis.FLAG, "true")
    a, c = open_case(client)
    upload(client, a, c, [("pan.jpg", OTHER_PAN), ("dl.jpg", RISHABH_DL)], ["PAN", "DRIVING_LICENCE"])
    body = ask(client, a, c, "PAN number kya hai?")
    for t in body["emphasis"]:
        assert body["answer_plain"][t["start"]:t["end"]] == t["text"]
