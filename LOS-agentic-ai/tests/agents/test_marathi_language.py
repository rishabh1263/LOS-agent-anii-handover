"""
MARATHI, typed in Devanagari or in Latin letters, is understood and answered in Marathi
(2026-10-04). Everything is configuration (app/config/languages.yaml, impact_rules.yaml
phrases); no question is mapped to an answer.

  - "KYC zala ka?" was REFUSED as cross-customer data: the question tag "ka" read as a
    possessive made "zala" look like a person's name
  - Roman Marathi is its own language (mr-Latn), answered with the Marathi wording
  - "का लागतो" (why needed), "जुळत नाही" (not matching), "मधून ... माहिती" (details read
    from) and "झाले का" (done?) reach the same intents as their English questions
  - a Devanagari question with no marker word may be told Marathi by the optional
    statistical detector (lingua); a plainly Hindi word vetoes it
  - verification, next step, checklist, KYC, status and document details are answered
    from their structured blocks in Marathi; values and labels are kept as recorded
  - the loan agent hears about the customer: "तुमचा PAN" -> "ग्राहकाचा PAN"
"""

from __future__ import annotations

import pytest

from app.agents.applicant import language
from app.agents.applicant.copilot.answering import localize, voice
from app.agents.applicant.copilot.routing import subjects
from app.agents.applicant.copilot.semantics import intents


@pytest.fixture(autouse=True)
def _fresh_config():
    language.reload()
    yield
    language.reload()


@pytest.mark.parametrize("question, intent", [
    ("KYC zala ka?", "KYC_RESULT"),
    ("majha PAN verify zala ka?", "DOCUMENT_VERIFICATION"),
    ("pudhe kay karaycha?", "NEXT_ACTION"),
    ("kay baki ahe?", "DOCUMENTS_PENDING"),
    ("case cha status kay ahe?", "APPLICATION_STATUS"),
    ("co-applicant cha KYC zala ka?", "KYC_RESULT"),
    ("bank statement madhun kay mahiti milali?", "DOCUMENT_DETAILS"),
    ("PAN ka lagto?", "FOS_KNOWLEDGE"),
    ("naav julat nahi ka?", "KYC_RESULT"),
    ("KYC झाले का?", "KYC_RESULT"),
    ("PAN का लागतो?", "FOS_KNOWLEDGE"),
    ("नाव जुळत नाही का?", "KYC_RESULT"),
    ("कागदपत्रांची पडताळणी झाली का?", "DOCUMENT_VERIFICATION"),
    ("बँक स्टेटमेंट मधून काय माहिती मिळाली?", "DOCUMENT_DETAILS"),
    ("पुढे काय करायचे?", "NEXT_ACTION"),
])
def test_marathi_questions_reach_the_english_intents(question, intent):
    assert intents.understand(question, has_case=True).intent.value == intent


@pytest.mark.parametrize("question", ["KYC zala ka?", "majha PAN verify zala ka?", "co-applicant cha KYC zala ka?"])
def test_a_marathi_question_tag_is_not_a_persons_name(question):
    assert subjects.names_a_person(question) is False


def test_a_name_with_a_possessive_is_still_a_name():
    assert subjects.names_a_person("Priya ka PAN") is True


@pytest.mark.parametrize("text, code", [
    ("KYC zala ka?", "mr-Latn"), ("pudhe kay karaycha?", "mr-Latn"), ("kay baki ahe?", "mr-Latn"),
    ("mera PAN verify hua kya?", "hi-Latn"), ("PAN ka number kya hai", "hi-Latn"),
    ("KYC झाले का?", "mr"), ("मेरा KYC हुआ?", "hi"), ("What is my stage?", "en"),
])
def test_detection_tells_roman_marathi_from_hinglish(text, code):
    assert language.detect(text).code == code


def test_roman_marathi_is_answered_in_marathi():
    assert language.response_language(None, language.detect("KYC zala ka?")) == "mr"
    assert language.response_language(None, language.detect("mera KYC hua kya?")) == "hi-Latn"


def test_a_letter_only_marathi_writes_decides_without_a_marker_word(monkeypatch):
    monkeypatch.setenv("LANGUAGE_DETECTOR", "off")
    assert language.detect("नाव जुळत?").code == "mr"


def test_a_word_both_languages_share_follows_the_conversation():
    shared = language.detect("धन्यवाद")
    assert shared.code == "hi" and shared.ambiguous
    assert language.response_language(None, shared, "mr", "धन्यवाद") == "mr"
    assert language.response_language(None, shared, None, "धन्यवाद") == "hi"
    # a plainly Hindi question is never re-read as the conversation's Marathi
    hindi = language.detect("मेरा आवेदन किस चरण में है?")
    assert not hindi.ambiguous and language.response_language(None, hindi, "mr", "x") == "hi"


def test_the_statistical_detector_is_a_hint_vetoed_by_a_hindi_word():
    pytest.importorskip("lingua")
    assert language.detect("कागदपत्रांची पडताळणी झाली का?").code == "mr"
    assert language.detect("दस्तावेज़ कौन से चाहिए?").code == "hi"      # lingua alone says Marathi
    assert language.detect("PAN क्यों चाहिए?").code == "hi"


@pytest.mark.parametrize("customer, agent", [
    ("तुमचा पॅन क्रमांक XXXXXX189E आहे.", "ग्राहकाचा पॅन क्रमांक XXXXXX189E आहे."),
    ("तुम्ही तुमचा पत्ता अजून दिलेला नाही.", "ग्राहकाने त्यांचा पत्ता अजून दिलेला नाही."),
    ("तुमचा अर्ज सध्या FOS टप्प्यात आहे.", "अर्ज सध्या FOS टप्प्यात आहे."),
    ("तुमचे KYC पूर्ण झाले आहे.", "ग्राहकाचे KYC पूर्ण झाले आहे."),
    ("आपने अपना पता अभी तक नहीं दिया है।", "ग्राहक ने अपना पता अभी तक नहीं दिया है।"),
])
def test_the_loan_agent_hears_about_the_customer_in_marathi_and_hindi(customer, agent):
    assert voice.for_audience(customer, "agent") == agent


@pytest.mark.parametrize("text", ["तुमचे स्वागत आहे 🙂", "तुम्हाला एखाद्या व्यक्तीकडून मदत हवी आहे.",
                                  "You're welcome 🙂 Anything else?"])
def test_courtesy_to_the_agent_is_left_alone(text):
    assert voice.for_audience(text, "agent") == text


# -- answers from the structured blocks ------------------------------------------------

def _localized(result: dict, message: str = "x") -> dict:
    result.setdefault("language_contract", {"response_language": "mr"})
    return localize.apply(result, message)


def test_verification_is_answered_in_marathi_keeping_scores_and_the_recorded_reason():
    out = _localized({
        "intent": "DOCUMENT_VERIFICATION",
        "answer": ("PAN is VERIFIED (verification score 100, confidence 90). These are document checks; "
                   "the issuing authority has not confirmed the document. However, the application is under "
                   "review because the name on the PAN, A B, does not match the bank account holder name, C D."),
        "verification": {"documents": [{"document_type": "PAN", "label": "PAN", "status": "VERIFIED",
                                        "score": 100, "confidence": 90, "score_recorded": True}]},
    })
    assert out["answer"].startswith("PAN ची पडताळणी पास झाली (स्कोअर 100, विश्वास 90).")
    assert "the name on the PAN, A B, does not match the bank account holder name, C D" in out["answer"]
    assert out["language_contract"]["localized"] is True and out["answer_en"].startswith("PAN is VERIFIED")


def test_kyc_keeps_the_quoted_values_and_the_score():
    out = _localized({
        "intent": "KYC_RESULT",
        "answer": ("The customer's KYC check needs review because the name didn't match: the PAN says A B, "
                   "but the bank statement says C D. The recorded score is 31."),
        "kyc": {"parties": [{"party_role": "PRIMARY_APPLICANT", "status": "REVIEW", "score": 31,
                             "score_recorded": True, "reason_codes": ["NAME_MISMATCH"]}]},
    })
    assert "पुनरावलोकनात" in out["answer"] and "PAN वर A B आहे, पण bank statement वर C D." in out["answer"]
    assert "31" in out["answer"]


def test_a_kyc_reason_without_a_template_keeps_the_english_answer():
    english = "The customer's KYC check needs review."
    out = _localized({"intent": "KYC_RESULT", "answer": english,
                      "kyc": {"parties": [{"party_role": "PRIMARY_APPLICANT", "status": "REVIEW",
                                           "reason_codes": ["SOMETHING_UNCONFIGURED"]}]}})
    assert out["answer"] == english and out["language_contract"]["localized"] is False


def test_the_checklist_keeps_labels_and_says_it_is_provisional():
    out = _localized({
        "intent": "DOCUMENTS_REQUIRED",
        "answer": "2 required: ... 1 optional: ... This list is not final: employment type has not been captured, ...",
        "policy": {"unevaluated_rules": [{"missing_attributes": ["employment_type"]}]},
        "checklist": [
            {"slot": "PAN", "status": "VERIFIED", "mandatory": True, "accepts": ["PAN"]},
            {"slot": "ADDRESS_PROOF", "status": "MISSING", "mandatory": True,
             "accepts": ["DRIVING_LICENCE", "PASSPORT", "VOTER_ID"]},
            {"slot": "SIGNATURE", "status": "MISSING", "mandatory": False, "accepts": ["SIGNATURE"]},
        ],
    })
    assert out["answer"].startswith("2 आवश्यक: PAN — पडताळले; Address Proof — बाकी (यापैकी कोणतेही एक:")
    assert "1 पर्यायी: Signature — बाकी." in out["answer"] and "employment type" in out["answer"]


def test_the_case_status_is_answered_with_its_reason_quoted():
    out = _localized({"intent": "APPLICATION_STATUS",
                      "answer": "The application is currently under Basic Document Verification and is "
                                "under review because the name differs."})
    assert out["answer"] == ("अर्ज सध्या Basic Document Verification मध्ये आहे. "
                             "तो पुनरावलोकनात आहे, कारण: the name differs.")


def test_document_details_keep_every_field_line():
    lines = "- name: A B\n- account number masked: XXXX6789"
    out = _localized({"intent": "DOCUMENT_DETAILS",
                      "answer": f"Details read from the customer's bank statement (it passed verification):\n{lines}"})
    assert out["answer"] == f"bank statement मधून वाचलेली माहिती (पडताळणी पास झाली):\n{lines}"


def test_a_fixed_sentence_has_its_configured_translation():
    out = _localized({"intent": "KYC_RESULT", "answer": "There is no co-applicant on this application."})
    assert out["answer"] == "या अर्जावर सह-अर्जदार नाही."


def test_the_next_step_is_rebuilt_from_the_same_actions():
    out = _localized({
        "intent": "NEXT_ACTION",
        "answer": "The next step is to capture the applicant's address. Also, wait for a reviewer to check "
                  "the recorded issue.",
        "next_action": {"action": "CAPTURE_APPLICANT_INFORMATION", "detail": "Capture the applicant's address.",
                        "target": None},
        "_nba_internal": {
            "primary": {"action_code": "CAPTURE_APPLICANT_INFORMATION", "source_rule": "workflow.next_action",
                        "subject": {"scope": "CASE", "party_role": None}},
            "additional": [{"action_code": "MANUAL_REVIEW", "subject": {"scope": "CASE", "party_role": None}}],
        },
    })
    assert out["answer"] == ("पुढची पायरी: अर्जदाराचा पत्ता नोंदवा. "
                             "तसेच, समीक्षक नोंदलेला मुद्दा तपासेपर्यंत वाट पहा.")


def test_a_next_step_worded_otherwise_keeps_the_english():
    english = "The case is held because the PAN is under review."
    out = _localized({"intent": "NEXT_ACTION", "answer": english,
                      "next_action": {"action": "RESOLVE_DOCUMENT_REVIEW", "detail": "x", "target": "PAN"},
                      "_nba_internal": {"primary": {"action_code": "MANUAL_REVIEW", "source_rule": "impact",
                                                    "subject": {"scope": "CASE"}}, "additional": []}})
    assert out["answer"] == english
