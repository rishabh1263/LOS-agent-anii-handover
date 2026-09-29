"""
The semantic frame: meaning, not sentences.

Every case below is graded on the FRAME (task / object / qualifiers /
referents) and the ROUTE it maps to -- never on matching a listed phrasing.
The paraphrase table is the offline half of the semantic eval suite; the
same cases run through the real HTTP API in evals/copilot/semantic.py.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.agents.applicant import normalize
from app.agents.applicant.copilot.conversation import followup
from app.agents.applicant.copilot.semantics import intents
from app.agents.applicant.copilot.semantics import semantic_frame as sf
from app.agents.applicant.copilot.semantics.semantic_frame import Object, Party, Qualifier, Scope, Task

LR, LP, LS, CV = (Task.LIST_REQUIREMENTS, Task.LIST_PENDING, Task.LIST_SUBMITTED,
                  Task.CHECK_VERIFICATION)
D = Object.DOCUMENTS


def frame(q: str) -> sf.SemanticFrame:
    n = normalize.normalise(q)
    return sf.parse(n.text or q, original=q)


# (question, task, object, route, stage referent)
PARAPHRASES = [
    # -- English ----------------------------------------------------------------
    ("What documents are mandatory?", LR, D, "DOCUMENTS_REQUIRED", None),
    ("What documents are mandatory for this stage?", LR, D, "DOCUMENTS_REQUIRED", "CURRENT"),
    ("Which documents do I need?", LR, D, "DOCUMENTS_REQUIRED", None),
    ("Which paperwork is required?", LR, D, "DOCUMENTS_REQUIRED", None),
    ("What paperwork do I need at this stage?", LR, D, "DOCUMENTS_REQUIRED", "CURRENT"),
    ("What do I need to submit?", LR, D, "DOCUMENTS_MISSING", None),
    ("What documents are compulsory?", LR, D, "DOCUMENTS_REQUIRED", None),
    ("What is the mandatory document required for", LR, D, "DOCUMENTS_REQUIRED", None),
    ("Which documents do I need at this stage?", LR, D, "DOCUMENTS_REQUIRED", "CURRENT"),
    ("Which documents do I need for CPA?", LR, D, "DOCUMENTS_REQUIRED", "CPA"),
    ("documents needed for CPA stage?", LR, D, "DOCUMENTS_REQUIRED", "CPA"),
    ("What papers are necessary?", LR, D, "DOCUMENTS_REQUIRED", None),
    ("Tell me what papers I need for the current step.", LR, D, "DOCUMENTS_REQUIRED", "CURRENT"),
    ("What do I need to upload now?", LR, D, "DOCUMENTS_MISSING", "CURRENT"),
    ("Which paperwork is compulsory at this stage?", LR, D, "DOCUMENTS_REQUIRED", "CURRENT"),
    ("what docs r needed", LR, D, "DOCUMENTS_REQUIRED", None),
    ("list of required documents please", LR, D, "DOCUMENTS_REQUIRED", None),
    ("required paperwork for my application?", LR, D, "DOCUMENTS_REQUIRED", None),
    ("what must I provide at this step", LR, D, "DOCUMENTS_MISSING", "CURRENT"),
    ("At this point in my application, what documents do I still need?", LP, D,
     "DOCUMENTS_MISSING", "CURRENT"),
    ("What documents are still pending?", LP, D, "DOCUMENTS_PENDING", None),
    ("which docs are left?", LP, D, "DOCUMENTS_MISSING", None),
    ("what is remaining on the documents", LP, D, "DOCUMENTS_MISSING", None),
    ("Which documents have been uploaded?", LS, D, "DOCUMENTS_UPLOADED", None),
    ("what papers have I already submitted", LS, D, "DOCUMENTS_UPLOADED", None),
    ("are my documents verified?", CV, D, "DOCUMENT_VERIFICATION", None),
    ("has my paperwork been checked", CV, D, "DOCUMENT_VERIFICATION", None),
    ("is the bank statement verified?", CV, Object.DOCUMENT, "DOCUMENT_VERIFICATION", None),
    # -- Hinglish -------------------------------------------------------------------
    ("Kaunse documents chahiye?", LR, D, "DOCUMENTS_REQUIRED", None),
    ("Is stage pe kaunse papers chahiye?", LR, D, "DOCUMENTS_REQUIRED", "CURRENT"),
    ("Is stage pe kya submit karna hai?", LR, D, "DOCUMENTS_MISSING", "CURRENT"),
    ("Mandatory docs kya hain?", LR, D, "DOCUMENTS_REQUIRED", None),
    ("Is stage ke liye kya chahiye?", LR, D, "DOCUMENTS_REQUIRED", "CURRENT"),
    ("Is stage pe kya documents lagenge?", LR, D, "DOCUMENTS_REQUIRED", "CURRENT"),
    ("kaunse kagaz zaroori hain", LR, D, "DOCUMENTS_REQUIRED", None),
    ("kya kya documents dene honge", LR, D, "DOCUMENTS_MISSING", None),
    ("kaunse documents baaki hain?", LP, D, "DOCUMENTS_PENDING", None),
    ("mere documents verify hue kya?", CV, D, "DOCUMENT_VERIFICATION", None),
    # -- Hindi ------------------------------------------------------------------------
    ("इस चरण में कौन से दस्तावेज़ चाहिए?", LR, D, "DOCUMENTS_REQUIRED", "CURRENT"),
    ("कौन से दस्तावेज़ अनिवार्य हैं?", LR, D, "DOCUMENTS_REQUIRED", None),
    ("कौन से दस्तावेज़ बाकी हैं?", LP, D, "DOCUMENTS_PENDING", None),
    ("क्या मेरे दस्तावेज़ सत्यापित हैं?", CV, D, "DOCUMENT_VERIFICATION", None),
    # -- Marathi ------------------------------------------------------------------------
    ("या स्टेजसाठी कोणती कागदपत्रे लागतील?", LR, D, "DOCUMENTS_REQUIRED", None),
    ("कोणते documents mandatory आहेत?", LR, D, "DOCUMENTS_REQUIRED", None),
    ("कोणती कागदपत्रे बाकी आहेत?", LP, D, "DOCUMENTS_PENDING", None),
    # -- typos / short forms -------------------------------------------------------------
    ("mandtory docs?", LR, D, "DOCUMENTS_REQUIRED", None),
    ("documnts required?", LR, D, "DOCUMENTS_REQUIRED", None),
    ("which documnets i need?", LR, D, "DOCUMENTS_REQUIRED", None),
    ("requird paperwrk?", LR, D, "DOCUMENTS_REQUIRED", None),
    ("docs pendng?", LP, D, "DOCUMENTS_PENDING", None),
    # -- the wider family ------------------------------------------------------------------
    ("What stage am I in?", Task.CURRENT_STAGE, Object.STAGE, "APPLICATION_STAGE", None),
    ("what is my stage?", Task.CURRENT_STAGE, Object.STAGE, "APPLICATION_STAGE", "CURRENT"),
    ("mera stage kya hai", Task.CURRENT_STAGE, Object.STAGE, "APPLICATION_STAGE", "CURRENT"),
    ("what is my application status?", Task.STATUS, Object.APPLICATION, "APPLICATION_STATUS",
     None),
    ("what should i do next?", Task.NEXT_ACTION, Object.NONE, "NEXT_ACTION", None),
    ("Ab next kya submit karna hai?", Task.NEXT_ACTION, Object.NONE, "NEXT_ACTION", None),
    ("is this case ready for CPA?", Task.READINESS, Object.APPLICATION, "READINESS", "CPA"),
    ("what is pending?", LP, Object.NONE, "PENDING_ITEMS", None),
    ("What is KYC?", Task.EXPLAIN, Object.PROCESS_TERM, "FOS_KNOWLEDGE", None),
    ("What does mandatory document mean?", Task.EXPLAIN, D, "FOS_KNOWLEDGE", None),
    ("What documents are required for a personal loan?", LR, D, "FOS_KNOWLEDGE", None),
]


@pytest.mark.parametrize("question, task, obj, route, stage", PARAPHRASES,
                         ids=[p[0][:40] for p in PARAPHRASES])
def test_paraphrases_reach_the_same_frame(question, task, obj, route, stage):
    f = frame(question)
    assert f.task is task, (question, f.public())
    assert f.object is obj, (question, f.public())
    assert sf.route(f) == route, (question, f.public())
    assert f.referents.get("stage") == stage, (question, f.referents)
    assert f.is_confident()


def test_the_suite_has_at_least_fifty_paraphrases():
    assert len(PARAPHRASES) >= 50


@pytest.mark.parametrize("question", [
    "What documents are mandatory for this stage?", "Is stage pe kya submit karna hai?",
    "documents needed for CPA stage?", "Which paperwork is compulsory at this stage?",
    "इस चरण में कौन से दस्तावेज़ चाहिए?", "या स्टेजसाठी कोणती कागदपत्रे लागतील?",
])
def test_a_documents_question_never_routes_to_the_stage_answer(question):
    c = intents.understand(question, has_case=True)
    assert c.intent is not intents.Intent.APPLICATION_STAGE, c
    assert c.intent.value.startswith("DOCUMENTS_"), c
    assert c.understanding == "FRAME"


def test_the_bare_stage_catch_all_is_gone():
    import inspect

    source = inspect.getsource(intents)
    assert 'r"\\b(current\\s+)?stage\\b", Intent.APPLICATION_STAGE' not in source


def test_qualifiers_are_read():
    f = frame("What documents are mandatory for this stage?")
    assert Qualifier.MANDATORY in f.qualifiers
    f = frame("what documents do I still need right now")
    assert {Qualifier.STILL, Qualifier.NOW} <= set(f.qualifiers)


def test_party_and_scope():
    assert frame("what documents does the co-applicant need").party is Party.CO_APPLICANT
    assert frame("what documents are needed in general").scope is Scope.GENERAL
    assert frame("what documents are required for a home loan").scope is Scope.GENERAL
    assert frame("what documents are required").scope is Scope.CURRENT_CASE


# ==========================================================================
# referents
# ==========================================================================

def test_this_stage_resolves_to_the_case_record_not_the_conversation():
    f = frame("What documents are mandatory for this stage?")
    ctx = followup.Context.from_payload({"last_intent": "APPLICATION_STAGE",
                                         "last_stage": "FOS"})
    resolved = sf.resolve_referents(f, ctx, case_stage="CPA")
    assert resolved.frame.stage == "CPA"                      # the record, not "FOS"
    assert resolved.resolutions["stage"].startswith("CURRENT -> CPA")
    assert resolved.clarification is None


def test_a_named_stage_is_kept_and_a_difference_is_noted():
    f = frame("Which documents do I need for CPA?")
    resolved = sf.resolve_referents(f, followup.Context(), case_stage="FOS")
    assert resolved.frame.stage == "CPA"
    assert "stage_note" in resolved.resolutions


def test_that_document_resolves_from_the_previous_answer():
    f = frame("is that document verified?")
    assert f.referents.get("document") == "THAT"
    resolved = sf.resolve_referents(f, followup.Context(last_slot="ADDRESS_PROOF"),
                                    case_stage="CPA")
    assert resolved.frame.document_type == "ADDRESS_PROOF"
    assert resolved.clarification is None


def test_an_unresolvable_document_referent_asks_a_targeted_question():
    f = frame("is that document verified?")
    resolved = sf.resolve_referents(f, followup.Context(), case_stage="CPA")
    assert resolved.clarification and "Which document" in resolved.clarification


def test_context_carries_semantic_state_without_values():
    payload = followup.context_from_response({
        "intent": "APPLICATION_STAGE", "query_type": "CASE_FACT",
        "stage": {"stage": "CPA", "status": "IN_PROGRESS"},
        "understanding": {"frame": {"task": "CURRENT_STAGE", "object": "STAGE",
                                    "language": "en"}},
        "response_source": "STRUCTURED", "applicant": {"full_name": "SECRET NAME"}})
    assert payload["last_task"] == "CURRENT_STAGE" and payload["last_object"] == "STAGE"
    assert payload["last_stage"] == "CPA" and payload["last_language"] == "en"
    assert "SECRET" not in json.dumps(payload)
    ctx = followup.Context.from_payload(payload)
    assert ctx.last_task == "CURRENT_STAGE" and ctx.last_stage == "CPA"


# ==========================================================================
# targeted clarification
# ==========================================================================

def test_a_bare_object_is_not_guessed():
    f = frame("which docs?")
    assert f.task is Task.UNKNOWN and not f.is_confident()
    c = sf.clarification(f, has_case=True)
    assert c and c["reason"] == "TASK_UNCLEAR" and "required" in c["question"]
    assert intents.understand("which docs?", has_case=True).intent is intents.Intent.UNKNOWN
    assert sf.clarification(sf.SemanticFrame(), has_case=True) is None


# ==========================================================================
# short / ambiguous queries
# ==========================================================================

from app.agents.applicant.copilot.semantics import short_query as sq  # noqa: E402


@pytest.mark.parametrize("word", ["name", "status", "documents", "income", "address", "mobile",
                                  "PAN", "my name", "docs?", "naam"])
def test_a_bare_word_with_no_context_is_clarified_not_guessed(word):
    r = sq.short_query(word, followup.Context())
    assert r is not None and r.resolved_to is None
    assert r.clarification["reason"] == "AMBIGUOUS_SHORT_QUERY"
    assert len(r.clarification["options"]) >= 2


@pytest.mark.parametrize("word, context, expected", [
    ("status", {"last_object": "DOCUMENTS", "last_task": "LIST_PENDING"}, "Are my documents verified?"),
    ("documents", {"last_object": "STAGE", "last_task": "CURRENT_STAGE"},
     "What documents are required at this stage?"),
    ("name", {"last_slot": "PAN"}, "What name is on my PAN?"),
    ("status", {"last_object": "APPLICATION", "last_task": "STATUS"}, "What is my application status?"),
    ("documents", {"last_object": "DOCUMENTS", "last_task": "LIST_PENDING"},
     "Which documents are pending?"),
])
def test_context_settles_a_short_query_when_unambiguous(word, context, expected):
    r = sq.short_query(word, followup.Context.from_payload(context))
    assert r is not None and r.resolved_to == expected and r.clarification is None


@pytest.mark.parametrize("question", ["what is my name", "which documents are required",
                                      "status of my application", "is my PAN verified"])
def test_a_real_question_is_not_a_short_query(question):
    assert sq.short_query(question, followup.Context()) is None


# ==========================================================================
# the Qwen fallback: bounded, validated, powerless
# ==========================================================================

def _run(coro):
    return asyncio.run(coro)


def test_llm_frame_is_validated_against_the_closed_schema(monkeypatch):
    monkeypatch.setenv("COPILOT_UNDERSTANDING_LLM", "true")

    async def good(_):
        return json.dumps({"task": "LIST_REQUIREMENTS", "object": "DOCUMENTS",
                           "qualifiers": ["MANDATORY"], "referents": {"stage": "CURRENT"},
                           "party": "SELF", "scope": "CURRENT_CASE", "language": "en"})

    f, trace = _run(sf.llm_frame("anything", generator=good))
    assert f is not None and f.source == "LLM" and sf.route(f) == "DOCUMENTS_REQUIRED"
    assert trace["status"] == "OK" and trace["consulted"]


@pytest.mark.parametrize("payload", [
    '{"task": "APPROVE_LOAN", "object": "DOCUMENTS"}',              # not an enum
    '{"task": "LIST_REQUIREMENTS", "object": "DOCUMENTS", "tool": "documents.checklist"}',
    '{"answer": "Your loan is approved"}', 'not json at all',
    '{"task": "UNKNOWN", "object": "NONE"}',
])
def test_llm_output_outside_the_schema_is_rejected(monkeypatch, payload):
    monkeypatch.setenv("COPILOT_UNDERSTANDING_LLM", "true")

    async def bad(_):
        return payload

    f, trace = _run(sf.llm_frame("x", generator=bad))
    if f is not None:
        # extra keys are ignored; the frame carries nothing but the enums
        assert "tool" not in json.dumps(f.public())
    else:
        assert trace["status"] in ("INVALID_JSON", "INVALID_FRAME")


def test_llm_frame_times_out_to_none(monkeypatch):
    monkeypatch.setenv("COPILOT_UNDERSTANDING_LLM", "true")

    async def slow(_):
        await asyncio.sleep(1)
        return "{}"

    f, trace = _run(sf.llm_frame("x", timeout=0.05, generator=slow))
    assert f is None and trace["status"] == "TIMEOUT"


def test_llm_frame_can_be_disabled(monkeypatch):
    monkeypatch.setenv("COPILOT_UNDERSTANDING_LLM", "false")
    called = {"n": 0}

    async def gen(_):
        called["n"] += 1
        return "{}"

    f, trace = _run(sf.llm_frame("x", generator=gen))
    assert f is None and trace["status"] == "DISABLED" and called["n"] == 0


def test_a_confident_parse_never_needs_the_model():
    assert frame("What documents are mandatory for this stage?").is_confident()
    assert frame("mandtory docs?").is_confident()
