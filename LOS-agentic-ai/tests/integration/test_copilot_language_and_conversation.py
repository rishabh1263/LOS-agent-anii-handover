"""
Phase 3 final sprint: the current-stage question in every wording, the
multilingual understanding layer, and the conversation layer around the
answer (tone, handoff, channel, analytics).

THE INVARIANT UNDER TEST. However the question is worded -- English, a short
form, a typo, Hinglish, Hindi, Marathi, Tamil... -- it reaches the SAME
canonical intent and the SAME authoritative stage read, with no model call.
Tone, language and channel change the words around a fact, never the fact.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import handoff, intents, language, sentiment
from app.observability import analytics
from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository

COPILOT = "/api/v1/copilot/query"
ALL_STAGES = ("FOS", "CPA", "CREDIT", "RCU", "BOPS", "HOPS", "DISBURSEMENT")


# ==========================================================================
# 1. THE CURRENT-STAGE BUG, AND EVERY WORDING OF THE QUESTION
# ==========================================================================

CURRENT_STAGE = [
    # the reported bug, and its sibling that always worked
    "what is my stage?", "where is my stage?",
    # English paraphrases
    "What stage am I in?", "Which stage am I in?", "What is my current stage?",
    "Current stage?", "My current stage?", "Tell me my stage.",
    "Where is my application in the process?", "What step is my application at?",
    "what's the current stage", "which stage is my application at?",
    # typos and short forms
    "wat is my stage", "whats my stg", "what is my stgae", "curr stage?",
    # Hinglish / romanized Hindi
    "mera stage kya hai?", "main kis stage pe hu?",
    "abhi mera case kis stage mein hai?", "mujhe current stage batao",
    "mera application kis step pe hai?",
    # Indian scripts
    "मेरा आवेदन अभी किस चरण में है?",                 # Hindi
    "मेरा स्टेज क्या है?",                             # Hindi
    "माझं application सध्या कोणत्या stage मध्ये आहे?",   # Marathi, code-mixed
    "माझा अर्ज सध्या कोणत्या टप्प्यात आहे?",            # Marathi
    "আমার আবেদন এখন কোন পর্যায়ে আছে?",                # Bengali
    "என் விண்ணப்பம் எந்த நிலையில் உள்ளது?",             # Tamil
    "నా దరఖాస్తు ఏ దశలో ఉంది?",                       # Telugu
    "મારી અરજી કયા તબક્કે છે?",                        # Gujarati
    "ನನ್ನ ಅರ್ಜಿ ಯಾವ ಹಂತದಲ್ಲಿದೆ?",                       # Kannada
    "എന്റെ അപേക്ഷ ഏത് ഘട്ടത്തിലാണ്?",                    # Malayalam
    "ਮੇਰੀ ਅਰਜ਼ੀ ਕਿਸ ਪੜਾਅ ਤੇ ਹੈ?",                        # Punjabi
    "ମୋ ଆବେଦନ କେଉଁ ପର୍ଯ୍ୟାୟରେ ଅଛି?",                     # Odia
    "মোৰ আবেদন কোন পৰ্যায়ত আছে?",                     # Assamese
    "میری درخواست کس مرحلے میں ہے؟",                   # Urdu
    "म्हजो अर्ज खंयच्या टप्प्यार आसा?",                  # Konkani
]


@pytest.mark.parametrize("question", CURRENT_STAGE)
def test_every_wording_of_the_current_stage_question_is_one_intent(question):
    understood = intents.understand(question, has_case=True)
    assert understood.intent is intents.Intent.APPLICATION_STAGE, (
        question, understood)
    assert understood.matched_on == "current_stage", (question, understood)


@pytest.mark.parametrize("question", [
    "what happens in this stage?", "how does this stage work?",
    "what does my current stage involve?", "what is the current stage about?",
    "what does RCU check?", "what is CPA?",
])
def test_how_a_stage_works_is_still_a_process_question(question):
    """The fix did not turn process questions into case questions."""
    assert intents.understand(question, has_case=True).intent \
        is intents.Intent.STAGE_PROCESS


def test_a_named_stage_is_never_the_current_stage_question():
    assert not intents.asks_current_stage("what is the CPA stage?")
    assert not intents.asks_current_stage("which stage comes after CREDIT?")


@pytest.mark.parametrize("question,intent", [
    ("Where does my application stand?", "APPLICATION_STATUS"),   # distinction kept
    ("mera status kya hai", "APPLICATION_STATUS"),
    ("kaunse documents pending hai", "DOCUMENTS_PENDING"),
    ("kya documents baaki hai", "DOCUMENTS_PENDING"),
    ("ab mujhe kya karna hai", "NEXT_ACTION"),
    ("mera application review mein kyu hai", "CASE_HISTORY"),
    ("kya badla?", "APPLICATION_STAGE"),                 # stage history
    ("KYC kya hota hai?", "FOS_KNOWLEDGE"),
    ("mera pan verify hua?", "DOCUMENT_VERIFICATION"),
    ("mere co-applicant ka kya status hai", "APPLICATION_STATUS"),
    ("मेरे दस्तावेज़ क्या बाकी है?", "DOCUMENTS_PENDING"),
])
def test_multilingual_questions_reach_the_same_intents_as_english(question, intent):
    assert intents.understand(question, has_case=True).intent.value == intent


# ==========================================================================
# 2. LANGUAGE DETECTION -- deterministic, script first
# ==========================================================================

@pytest.mark.parametrize("text,code", [
    ("What is my stage?", "en"),
    ("mera stage kya hai?", "hi-Latn"),
    ("मेरा आवेदन किस चरण में है?", "hi"),
    ("माझा अर्ज कोणत्या टप्प्यात आहे?", "mr"),
    ("म्हजो अर्ज खंयच्या टप्प्यार आसा?", "kok"),
    ("আমার আবেদন কোন পর্যায়ে আছে?", "bn"),
    ("মোৰ আবেদন কোন পৰ্যায়ত আছে?", "as"),
    ("என் விண்ணப்பம்", "ta"), ("నా దరఖాస్తు", "te"), ("મારી અરજી", "gu"),
    ("ನನ್ನ ಅರ್ಜಿ", "kn"), ("എന്റെ അപേക്ഷ", "ml"), ("ਮੇਰੀ ਅਰਜ਼ੀ", "pa"),
    ("ମୋ ଆବେଦନ", "or"), ("میری درخواست", "ur"),
])
def test_language_is_detected_from_script_and_markers(text, code):
    assert language.detect(text).code == code


def test_english_is_never_rewritten():
    for text in ("What is my stage?", "Where is my PAN?", "main road address"):
        assert language.canonicalise(text).text == text


def test_one_hinglish_word_in_a_long_english_sentence_is_still_english():
    assert language.detect("My name is Hai and I want my status").code == "en"


def test_the_lexicons_say_they_are_unreviewed():
    assert language.detect("मेरा स्टेज").review_status == "SEED_UNREVIEWED"
    assert language.detect("What is my stage").review_status == "NATIVE"


def test_multilingual_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("MULTILINGUAL_ENABLED", "false")
    assert language.canonicalise("मेरा स्टेज क्या है?").text == "मेरा स्टेज क्या है?"


def test_a_localized_answer_only_fills_in_an_established_fact():
    assert language.localized("current_stage", "hi", stage="CPA") \
        == "आपका आवेदन अभी CPA चरण में है।"
    assert language.localized("current_stage", "en", stage="CPA") is None
    assert language.localized("no_such_fact", "hi", stage="CPA") is None


# ==========================================================================
# 3. END TO END, AT EVERY STAGE -- authoritative, deterministic, no model
# ==========================================================================

@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "lang.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


@pytest.fixture(autouse=True)
def memory_on_llm_off(monkeypatch):
    from app.agents.applicant import config as agent_config
    from app.agents.los import config as los_config

    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    los_config.reload()
    agent_config.reload()
    analytics.reset()
    yield
    monkeypatch.undo()
    los_config.reload()
    agent_config.reload()


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=['los.read'])}"})
    return c


@pytest.fixture
def demo(repo):
    from app.store import demo_seed

    demo_seed.seed(repo, force=True)
    return {c["stage"]: (c["case_id"], c["applicant_id"])
            for c in reversed(demo_seed._CASES)}


def ask(client, message, case_id, applicant_id, **extra) -> dict:
    r = client.post(COPILOT, json={"applicant_id": applicant_id,
                                   "case_id": case_id, "message": message,
                                   **extra})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("stage", ALL_STAGES)
@pytest.mark.parametrize("question", ["what is my stage?", "where is my stage?",
                                      "mera stage kya hai?"])
def test_what_is_my_stage_answers_the_authoritative_stage_everywhere(
        client, demo, stage, question):
    from app.agents.applicant import config

    case_id, applicant_id = demo[stage]
    body = ask(client, question, case_id, applicant_id)
    assert body["intent"] == "APPLICATION_STAGE"
    assert body["category"] == "CASE_ONLY"
    assert body["stage"] == stage
    assert config.stage_label(stage) in body["answer"]
    # A simple fact: no model, no retrieval of the handbook.
    assert body["answer_basis"]["composition"]["called"] is False
    assert body["timings"]["qwen_calls"] == 0


@pytest.mark.parametrize("question,code,expected", [
    ("मेरा आवेदन अभी किस चरण में है?", "hi", "चरण में है"),
    ("माझा अर्ज सध्या कोणत्या टप्प्यात आहे?", "mr", "टप्प्यात आहे"),
    ("mera stage kya hai?", "hi-Latn", "stage mein hai"),
    ("என் விண்ணப்பம் எந்த நிலையில் உள்ளது?", "ta", "நிலையில் உள்ளது"),
])
def test_a_stage_question_in_another_language_is_answered_in_it(
        client, demo, question, code, expected):
    case_id, applicant_id = demo["CPA"]
    english = ask(client, "What is my stage?", case_id, applicant_id)
    body = ask(client, question, case_id, applicant_id)
    # The SAME authoritative fact...
    assert body["stage"] == english["stage"] == "CPA"
    assert body["intent"] == english["intent"]
    assert body["status"] == english["status"]
    # ...said in the question's language, with the stage label untranslated.
    assert expected in body["answer"] and "CPA" in body["answer"]
    assert body["language"]["detected"] == code
    assert body["language"]["response_language"] == code
    assert body["language"]["localized"] is True
    assert body["answer_basis"]["composition"]["called"] is False


def test_an_explicit_language_request_wins(client, demo):
    case_id, applicant_id = demo["FOS"]
    body = ask(client, "What is my stage?", case_id, applicant_id, language="mr")
    assert body["language"]["response_language"] == "mr"
    assert "टप्प्यात" in body["answer"]


def test_a_fact_with_no_template_stays_english_and_says_so(client, demo):
    case_id, applicant_id = demo["FOS"]
    body = ask(client, "ab mujhe kya karna hai", case_id, applicant_id)
    assert body["intent"] == "NEXT_ACTION"
    assert body["language"]["detected"] == "hi-Latn"
    assert body["language"]["localized"] is False
    assert body["answer_basis"]["presentation"]["localized_template"] is None


def test_conversation_memory_never_overrides_the_stage(client, demo):
    case_id, applicant_id = demo["CPA"]
    body = ask(client, "what is my stage?", case_id, applicant_id,
               context={"last_stage": "DISBURSEMENT", "language": "hi",
                        "last_intent": "APPLICATION_STAGE"})
    assert body["stage"] == "CPA"
    assert body["context"]["last_stage"] == "CPA"


def test_the_channel_changes_nothing_but_the_echo(client, demo):
    case_id, applicant_id = demo["FOS"]
    web = ask(client, "What should I do next?", case_id, applicant_id, channel="web")
    wa = ask(client, "What should I do next?", case_id, applicant_id, channel="whatsapp")
    odd = ask(client, "What should I do next?", case_id, applicant_id, channel="<script>")
    assert web["answer"] == wa["answer"] and web["next_action"] == wa["next_action"]
    assert (web["channel"], wa["channel"], odd["channel"]) == ("web", "whatsapp", "api")
    assert web["response_contract_version"] == "3.0"


# ==========================================================================
# 4. SENTIMENT -- tone only
# ==========================================================================

@pytest.mark.parametrize("text,level", [
    ("What is my stage?", "neutral"),
    ("I don't understand what this means", "confused"),
    ("I uploaded this three times and it's still pending", "frustrated"),
    ("This is ridiculous, I uploaded it three times and it's STILL pending!!",
     "high_frustration"),
    ("kitni baar upload karu, abhi bhi pending hai", "frustrated"),
])
def test_sentiment_levels(text, level):
    assert sentiment.detect(text).level == level


def test_the_signal_never_carries_the_users_words():
    public = sentiment.detect("this is ridiculous and useless").public()
    assert "ridiculous" not in json.dumps(public)
    assert public["affects"] == "TONE_ONLY"


def test_frustration_changes_the_tone_and_nothing_else(client, demo):
    case_id, applicant_id = demo["FOS"]
    calm = ask(client, "Which documents are pending?", case_id, applicant_id)
    upset = ask(client, "Which documents are pending? I uploaded them three "
                        "times and it's still pending!", case_id, applicant_id)
    assert upset["sentiment"]["level"] in {"frustrated", "high_frustration"}
    for key in ("intent", "stage", "status", "pending_items", "next_action",
                "problems", "category"):
        assert upset[key] == calm[key], key
    # The factual answer is untouched: the tone is a prepended line only.
    assert upset["answer"].endswith(calm["answer"])
    assert upset["answer"] != calm["answer"]


def test_sentiment_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("SENTIMENT_ENABLED", "false")
    assert sentiment.detect("this is ridiculous!!").level == "neutral"
    assert sentiment.apply_tone("Answer.", sentiment.Signal("frustrated")) == "Answer."


# ==========================================================================
# 5. HUMAN HANDOFF -- a signal, never a decision
# ==========================================================================

def test_an_explicit_request_for_a_person_requires_a_handoff(client, demo):
    case_id, applicant_id = demo["FOS"]
    for message in ("I want to talk to a human", "mujhe kisi insaan se baat karni hai",
                    "connect me to customer care"):
        body = ask(client, message, case_id, applicant_id)
        assert body["handoff"]["handoff_required"] is True, message
        assert body["handoff"]["handoff_reason"] == "USER_REQUEST"
        # The answer acknowledges the request in every language it is
        # recognised in -- and claims no one has been contacted.
        assert body["intent"] == "HUMAN_HANDOFF_REQUESTED", message
        if body["language"]["response_language"] == "en":
            assert "person" in body["answer"].lower()
        else:   # acknowledged in the user's language, from the template
            assert body["answer_basis"]["presentation"]["localized_template"] \
                == "handoff_acknowledged"
            assert "vyakti" in body["answer"] or "व्यक्ति" in body["answer"]


def test_mild_unhappiness_does_not_trigger_a_handoff(client, demo):
    case_id, applicant_id = demo["FOS"]
    body = ask(client, "Why is it still pending? It's taking so long.",
               case_id, applicant_id)
    assert body["sentiment"]["level"] == "frustrated"
    assert body["handoff"]["handoff_required"] is False
    assert body["handoff"]["recommended"] is False


def test_repeated_unresolved_turns_recommend_a_person(client, demo):
    case_id, applicant_id = demo["FOS"]
    body = ask(client, "blorp zzt qqq", case_id, applicant_id,
               context={"unresolved_turns": 2})
    assert body["handoff"]["unresolved_turns"] == 3
    assert body["handoff"]["recommended"] is True
    assert "REPEATED_UNRESOLVED" in body["handoff"]["triggers"]
    # Recommended, not required: the conversation counter is untrusted.
    assert body["handoff"]["handoff_required"] is False
    assert body["context"]["unresolved_turns"] == 3


def test_a_configured_manual_review_requires_a_handoff_with_a_safe_summary():
    published = {
        "case_id": "case_x", "stage": "CPA", "intent": "NEXT_ACTION",
        "category": "CASE_ONLY", "answer": "Your application is under review.",
        "subject": {"kind": "CO_APPLICANT", "party_id": "COAPP-SECRET1"},
        "handoff": {"required": True, "reason": "MANUAL_REVIEW", "priority": None},
        "next_actions": {"primary": {"action_code": "MANUAL_REVIEW"}},
        "problems": [{"type": "NAME_MISMATCH", "message": "Names differ: RAHUL vs RAHUL K",
                      "impact": {"impact_code": "KYC_REVIEW"}}],
    }
    block = handoff.evaluate(published, message="what next?", canonical=None,
                             sentiment_level="neutral", context=None)
    assert block["handoff_required"] is True
    assert block["handoff_reason"] == "CONFIGURED_MANUAL_REVIEW"
    assert block["reason"] == "MANUAL_REVIEW"          # Slice 9 meaning kept
    summary = block["handoff_summary"]
    assert summary["stage"] == "CPA" and summary["subject"] == "CO_APPLICANT"
    assert summary["findings"] == ["NAME_MISMATCH"]
    assert summary["impacts"] == ["KYC_REVIEW"]
    assert summary["next_action"] == "MANUAL_REVIEW"
    dumped = json.dumps(summary)
    # Codes and labels only: no names, values, party ids or user text.
    assert "RAHUL" not in dumped and "COAPP-SECRET1" not in dumped
    assert "what next" not in dumped
    assert block["handoff_priority"] is None          # none configured, none invented


def test_high_frustration_recommends_but_never_requires():
    block = handoff.evaluate({"intent": "APPLICATION_STAGE", "category": "CASE_ONLY",
                              "answer": "At FOS."}, message="useless!!",
                             canonical=None, sentiment_level="high_frustration",
                             context=None)
    assert block["recommended"] is True and block["handoff_required"] is False


# ==========================================================================
# 6. ANALYTICS -- aggregate, closed-set labels, no content
# ==========================================================================

def test_analytics_aggregate_without_content(client, demo):
    case_id, applicant_id = demo["FOS"]
    ask(client, "what is my stage?", case_id, applicant_id)
    ask(client, "mera stage kya hai?", case_id, applicant_id)
    ask(client, "This is ridiculous, it's STILL pending!!", case_id, applicant_id)
    snap = analytics.snapshot()
    assert snap["requests"] == 3
    assert snap["by"]["intent"]["APPLICATION_STAGE"] == 2
    assert snap["by"]["language"]["hi-Latn"] == 1
    assert snap["by"]["stage"]["FOS"] == 3
    assert snap["counters"]["deterministic_bypass"] >= 2
    assert snap["by"]["sentiment"]["HIGH_FRUSTRATION"] == 1
    assert snap["latency_ms"]["total_ms"]["count"] == 3
    dumped = json.dumps(snap)
    for private in (case_id, applicant_id, "ridiculous", "mera stage"):
        assert private not in dumped
    assert snap["privacy"] == "AGGREGATE_ONLY"


def test_free_text_can_never_become_an_analytics_label():
    analytics.record({"intent": "what is my PAN ABCDE1234F", "stage": "<x>",
                      "language": {"detected": "RAHUL SHARMA"}, "timings": {}})
    snap = json.dumps(analytics.snapshot())
    assert "ABCDE1234F" not in snap and "RAHUL" not in snap


def test_analytics_endpoint_requires_the_ops_scope(client, make_token):
    import main

    anonymous = TestClient(main.app)
    assert anonymous.get("/ops/analytics").status_code == 401
    assert client.get("/ops/analytics").status_code == 403
    ops = TestClient(main.app)
    ops.headers["Authorization"] = "Bearer " + make_token(scopes=["los.ops.read"])
    r = ops.get("/ops/analytics")
    assert r.status_code == 200 and r.json()["privacy"] == "AGGREGATE_ONLY"


def test_a_summary_never_uses_a_record_id_as_the_applicants_name():
    from app.agents.applicant.answer import _summary_text

    text = _summary_text({"applicant": {"applicant_id": "APP-SECRET01"},
                          "stage": "BASIC_DOCUMENT_VERIFICATION"})
    assert "APP-SECRET01" not in text and text.startswith("This applicant")


# ==========================================================================
# 7. GAP-CLOSURE PASS
# ==========================================================================

@pytest.mark.parametrize("question,intent", [
    ("is my kyc done?", "OUT_OF_SCOPE"),              # same as "kyc status"
    ("mera kyc hua kya?", "OUT_OF_SCOPE"),
    ("has my kyc been completed", "OUT_OF_SCOPE"),
    ("why hasn't my application moved", "CASE_HISTORY"),
    ("why is my case not moving", "CASE_HISTORY"),
    ("I uploaded this three times and it's still pending!", "DOCUMENTS_PENDING"),
    ("my PAN is still not verified", "DOCUMENT_VERIFICATION"),
    ("माझ्या अर्जाची स्थिती काय आहे?", "APPLICATION_STATUS"),
    ("আমার আবেদনের অবস্থা কী?", "APPLICATION_STATUS"),
    ("என் விண்ணப்பத்தின் நிலை என்ன?", "APPLICATION_STATUS"),
    ("મારી અરજીની સ્થિતિ શું છે?", "APPLICATION_STATUS"),
    ("Tell me something about my application please", "UNKNOWN"),   # still ambiguous
])
def test_gap_closure_semantics(question, intent):
    assert intents.understand(question, has_case=True).intent.value == intent


def test_the_kyc_answer_is_consistent_in_every_wording():
    routed = {intents.understand(q, has_case=True).route_to
              for q in ("kyc status", "is my kyc done?", "mera kyc hua kya?")}
    assert len(routed) == 1


@pytest.mark.parametrize("question,code,expected", [
    ("मेरे कौन से दस्तावेज़ बाकी हैं?", "hi", "अभी बाकी है"),
    ("kaunse documents baaki hai", "hi-Latn", "abhi baaki hai"),
    ("माझी कोणती कागदपत्रे प्रलंबित आहेत?", "mr", "अजून बाकी आहे"),
])
def test_pending_documents_are_localized_from_the_same_facts(
        client, demo, question, code, expected):
    case_id, applicant_id = demo["FOS"]
    english = ask(client, "Which documents are pending?", case_id, applicant_id)
    body = ask(client, question, case_id, applicant_id)
    assert body["intent"] == english["intent"] == "DOCUMENTS_PENDING"
    assert body["pending_items"] == english["pending_items"]
    assert body["language"]["localized"] is True, body["answer"]
    assert expected in body["answer"]
    assert body["answer_basis"]["presentation"]["localized_template"] == "pending_documents"
    assert body["answer_basis"]["presentation"]["facts_changed"] is False
    # Every document label the English answer names is in the localized one.
    for label in re.findall(r"\b(Address Proof|PAN|Bank Statement|Salary Slip)\b",
                            english["answer"]):
        assert label in body["answer"], label


def test_the_remembered_language_applies_only_to_a_message_with_no_english(client, demo):
    case_id, applicant_id = demo["CPA"]
    short = ask(client, "stage?", case_id, applicant_id, context={"language": "hi"})
    assert short["language"]["response_language"] == "hi"
    switched = ask(client, "what is my stage?", case_id, applicant_id,
                   context={"language": "hi"})
    assert switched["language"]["response_language"] == "en"     # user switched
    bogus = ask(client, "stage?", case_id, applicant_id, context={"language": "xx"})
    assert bogus["language"]["response_language"] == "en"        # unsupported ignored
    assert short["stage"] == switched["stage"] == bogus["stage"] == "CPA"


def test_the_tone_line_is_recorded_as_presentation_not_fact(client, demo):
    case_id, applicant_id = demo["FOS"]
    body = ask(client, "This is ridiculous, why is it STILL pending!!", case_id, applicant_id)
    assert body["answer_basis"]["presentation"]["tone_opener_added"] is True
    assert body["answer_basis"]["presentation"]["facts_changed"] is False


def test_every_audit_line_carries_the_request_context(client, demo, tmp_path, monkeypatch):
    audit_file = tmp_path / "audit.jsonl"
    monkeypatch.setenv("APPLICANT_AGENT_AUDIT_PATH", str(audit_file))
    case_id, applicant_id = demo["CPA"]
    ask(client, "मेरा आवेदन अभी किस चरण में है?", case_id, applicant_id, channel="whatsapp")
    lines = [json.loads(l) for l in audit_file.read_text(encoding="utf-8").splitlines()]
    assert lines
    for line in lines:
        assert line["stage"] == "CPA" and line["channel"] == "whatsapp"
        assert line["auth_mode"] == "JWT" and line["language"] == "hi"
