"""
FINAL CHATBOT POLISH, through the real HTTP API on real samples (2026-10-06).

A KYC-review case (Rishabh's PAN + another person's licence): the frontend's
language wins for every common answer, localization never changes a fact (a
review is never said as "complete"), names are quoted exactly, no generic upload
action is offered when nothing is uploadable, and the integrity note is short.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.test_language_and_tts import ask, client, _store  # noqa: F401
from tests.integration.test_reupload_supersedes import RISHABH_DL, RISHABH_PAN, open_case, upload

DL_B = Path("samples/documents/sidkamble.jpg")
pytestmark = pytest.mark.skipif(not DL_B.exists(), reason="real samples not present")

LOCALIZED = ["documents verify ho gaye?", "what is pending?", "KYC kyun fail hua?", "KYC zala ka?",
             "eligibility ka status?", "what should I do?", "show all documents", "bank statement ka kya hua?",
             "PAN ki details batao", "why is this case pending?", "what do I need to upload?"]
#: what "complete" looks like in each language -- a REVIEW answer must never say it
COMPLETE = {"mr": ("पूर्ण झाले", "पूर्ण आहे"), "hi": ("पूरा हो गया", "पूर्ण हो"), "en": ("is complete", "passed")}
REVIEW = {"mr": "पुनरावलोकन", "hi": "समीक्षा", "en": "review"}


@pytest.fixture
def review_case(client):
    a, c = open_case(client)
    upload(client, a, c, [("pan.jpg", RISHABH_PAN), ("dl.jpg", DL_B)], ["PAN", "DRIVING_LICENCE"])
    return a, c


@pytest.mark.parametrize("lang", ["mr", "hi"])
def test_the_selected_language_covers_the_common_answers(client, review_case, lang):
    a, c = review_case
    missed = []
    for q in LOCALIZED:
        body = ask(client, a, c, q, response_language=lang)
        if not (body["language_contract"]["localized"] and body["language_contract"]["reply_language"] == lang):
            missed.append((q, body["answer"][:80]))
    assert not missed, missed


@pytest.mark.parametrize("lang", ["mr", "hi", "en"])
@pytest.mark.parametrize("typed", ["KYC kyun fail hua?", "why did KYC fail?", "KYC zala ka?"])
def test_localization_never_changes_the_fact(client, review_case, lang, typed):
    a, c = review_case
    answer = ask(client, a, c, typed, response_language=lang)["answer"]
    assert REVIEW[lang] in answer.lower() if lang == "en" else REVIEW[lang] in answer, answer
    assert not any(w in answer for w in COMPLETE[lang]), answer
    assert "RISHABH AJIT SINGH" in answer or REVIEW[lang] in answer


@pytest.mark.parametrize("lang", ["mr", "hi"])
def test_names_are_quoted_exactly_in_the_localized_reason(client, review_case, lang):
    a, c = review_case
    answer = ask(client, a, c, "why is this case pending?", response_language=lang)["answer"]
    assert "RISHABH AJIT SINGH" in answer and "SIDDHANT KAMBLE" in answer
    assert "does not match" not in answer                               # the clause itself is localized


def test_no_generic_upload_action_when_nothing_is_uploadable(client):
    a, c = open_case(client)
    upload(client, a, c, [("pan.jpg", RISHABH_PAN), ("dl.jpg", RISHABH_DL)], ["PAN", "DRIVING_LICENCE"])
    p = ask(client, a, c, "show all documents")["presentation"]
    labels = [x["label"] for x in p["actions"]] + [p["next_action"]["label"] if p["next_action"] else ""]
    if not any(i.get("code") == "DOCUMENT_MISSING" for i in ask(client, a, c, "what is pending?").get("pending_items") or []):
        assert "Upload a document" not in labels, labels


def test_a_missing_document_gets_its_specific_upload_action(client, review_case):
    a, c = review_case
    p = ask(client, a, c, "what is pending?")["presentation"]
    assert p["next_action"] and p["next_action"]["type"] == "UPLOAD_DOCUMENT"
    assert p["next_action"]["label"] != "Upload a document" and p["next_action"]["document_type"]


def test_the_integrity_note_is_one_short_sentence(client, review_case):
    from app.agents.applicant.copilot.answering.answer import INTEGRITY_ONLY

    a, c = review_case
    answer = ask(client, a, c, "documents verify ho gaye?", response_language="en")["answer"]
    assert "These are document checks" not in answer and len(INTEGRITY_ONLY) < 70
