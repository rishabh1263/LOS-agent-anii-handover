"""
THE SIMPLE PIPELINE (owner 2026-10-24; app/agents/applicant/copilot/simple/pipeline.py, config simple_chat.yaml):
understood -> the engine's canonical question, no model; not sure -> None (the old pipeline answers).
The meaning reader is stubbed here (its scores are the embeddings', measured live in the probe).
"""
from __future__ import annotations

import asyncio

import pytest

from app.agents.applicant.copilot.semantics import meaning
from app.agents.applicant.copilot.simple import pipeline


@pytest.fixture
def ranked(monkeypatch):
    table: dict[str, list[tuple[str, float]]] = {}
    monkeypatch.setattr(meaning, "rank", lambda message, embedder=None: table.get(message, []))
    pipeline.forget("c1")
    return table


def test_a_sure_reading_is_the_canonical_question(ranked):
    ranked["what's pending"] = [("pending_documents", 0.99), ("why_stuck", 0.86)]
    frame = pipeline.read("what's pending", "c1")
    assert frame.intent == "pending_documents" and frame.canonical == "What is pending?"


def test_an_unsure_reading_goes_to_the_old_pipeline(ranked):
    ranked["kyc me kya issue hai"] = [("kyc_status", 0.98), ("why_stuck", 0.95)]       # inside the margin
    ranked["something odd"] = [("case_status", 0.62)]                                  # under accept
    ranked["will this loan get approved"] = [("credit_decision", 1.0)]                 # not this pipeline's intent
    assert pipeline.read("kyc me kya issue hai", "c1") is None
    assert pipeline.read("something odd", "c1") is None
    assert pipeline.read("will this loan get approved", "c1") is None


def test_two_intents_asking_the_same_question_are_not_a_tie(ranked):
    ranked["pending docs kya hai"] = [("pending_documents", 0.92), ("upload_pending", 0.89), ("why_stuck", 0.7)]
    assert pipeline.read("pending docs kya hai", "c1").intent == "pending_documents"


def test_a_named_document_in_a_few_words_is_its_status(ranked):
    ranked["salary slip ok?"] = [("knowledge_documents", 0.88), ("case_record", 0.78)]
    frame = pipeline.read("salary slip ok?", "c1")
    assert frame.by == "named_document" and frame.canonical == "What is the status of the Salary Slip?"


@pytest.mark.parametrize("message", ["is voter id ok", "what can he give as address proof",
                                     "when was the pan uploaded"])
def test_acceptance_details_and_non_slots_are_not_a_status(ranked, message):
    ranked[message] = [("knowledge_documents", 0.9), ("case_record", 0.8)]
    assert pipeline.read(message, "c1") is None


def test_one_recorded_field_but_not_when_the_reader_is_sure_of_another_question(ranked):
    ranked["what is his date of birth"] = [("applicant_contact", 0.84)]
    assert pipeline.read("what is his date of birth", "c1").canonical == "what is the date of birth"
    ranked["how long has it been at this stage"] = [("time_in_stage", 1.0), ("case_stage", 0.84)]
    assert pipeline.read("how long has it been at this stage", "c1").intent == "time_in_stage"


def test_follow_ups_need_this_pipelines_last_answer(ranked):
    assert pipeline.read("why?", "c1") is None                     # nothing answered yet: the old pipeline
    pipeline.remember("c1", "cpa_readiness")
    assert pipeline.read("why?", "c1").intent == "why_stuck"
    pipeline.remember("c1", "pending_documents")
    assert pipeline.read("which one first", "c1").intent == "next_step"
    pipeline.forget("c1")                                          # another layer answered in between
    assert pipeline.read("which one first", "c1") is None


def test_the_answer_is_the_engines_and_never_calls_a_model(ranked, monkeypatch):
    from app.agents.applicant.copilot.answering import polish

    async def _no_model(*a, **k):
        raise AssertionError("a model was asked")

    monkeypatch.setattr(polish, "_ask", _no_model)
    monkeypatch.setattr(polish, "_lead", _no_model, raising=False)
    ranked["what's pending"] = [("pending_documents", 0.99)]
    asked = []

    async def lookup(canonical):
        asked.append(canonical)
        return {"intent": "PENDING_ITEMS", "answer": "Still pending:\n- Address Proof"}

    out = asyncio.run(pipeline.answer("what's pending", chat="c1", lookup=lookup))
    assert asked == ["What is pending?"] and "Address Proof" in out["answer"]
    assert out["polished"] is True and out["simple"]["intent"] == "pending_documents"
    assert pipeline.last_intent("c1") == "pending_documents"


def test_an_engine_that_cannot_answer_hands_over(ranked):
    ranked["what's pending"] = [("pending_documents", 0.99)]

    async def lookup(canonical):
        return {"intent": "CLARIFICATION", "answer": "Which case?"}

    assert asyncio.run(pipeline.answer("what's pending", chat="c1", lookup=lookup)) is None


# ---- strong routing (owner 2026-10-25): the front of the chat ------------------------------------------------------

@pytest.mark.parametrize("message", ["show me the pan details", "what does the salary slip say"])
def test_what_a_document_says_is_its_details_never_its_status(ranked, message):
    ranked[message] = [("document_status", 0.95), ("case_record", 0.8)]
    frame = pipeline.read(message, "c1")
    assert frame.by == "document_contents" and frame.canonical.lower().endswith("details")


@pytest.mark.parametrize("message,kind", [("hi", "GREETING"), ("thanks", "THANKS"), ("thank you so much", "THANKS"),
                                          ("ok bye", "GOODBYE"), ("good morning", "GREETING")])
def test_small_talk_is_its_own_route_with_no_meaning_read(ranked, message, kind):
    frame = pipeline.read(message, "c1")
    assert frame.route == "social" and frame.intent == kind


def test_small_talk_inside_a_question_is_the_question(ranked):
    ranked["hi what is pending"] = [("pending_documents", 0.99), ("why_stuck", 0.8)]
    assert pipeline.read("hi what is pending", "c1").route == "engine"


def test_a_bare_glossary_acronym_is_its_definition(ranked):
    assert pipeline.read("what is cpa", "c1").route == "definition"
    ranked["what is the tenure"] = [("knowledge_term", 0.9)]
    assert pipeline.read("what is the tenure", "c1").route == "engine"           # a recorded field, not the glossary


def test_a_field_named_like_a_glossary_term_is_asked_as_the_recorded_field(ranked):
    ranked["what is the tenure"] = [("knowledge_term", 0.9)]
    assert pipeline.read("what is the tenure", "c1").canonical == "what is the recorded tenure"


def test_social_and_definitions_answer_without_a_case_but_case_questions_need_one(ranked):
    ranked["what's pending"] = [("pending_documents", 0.99), ("why_stuck", 0.8)]

    async def lookup(canonical):
        raise AssertionError("no case open: the engine is never asked")

    async def social(frame):
        return {"intent": frame.intent, "answer": "Hello!"}

    said = asyncio.run(pipeline.answer("hi", chat="c1", lookup=lookup, handlers={"social": social}, case_open=False))
    assert said["answer"] == "Hello!" and said["polished"] and said["simple"]["by"] == "social"
    assert asyncio.run(pipeline.answer("what's pending", chat="c1", lookup=lookup, case_open=False)) is None


def test_a_handler_that_cannot_answer_hands_over(ranked):
    async def nothing(frame):
        return None

    assert asyncio.run(pipeline.answer("what is cpa", chat="c1", lookup=None, handlers={"definition": nothing})) is None


def test_a_clear_reading_skips_qwen_only_when_sure(ranked):
    ranked["when was this case created"] = [("case_record", 1.0), ("time_in_stage", 0.76)]
    ranked["how do i upload a document"] = [("process_upload", 0.977), ("upload_pending", 0.938)]   # inside margin
    ranked["sort this out"] = [("ambiguous_action", 0.99)]                                          # never clear
    assert pipeline.clear("when was this case created")
    assert not pipeline.clear("how do i upload a document")
    assert not pipeline.clear("sort this out")


def test_a_value_in_the_message_is_an_edit_never_the_fields_question(ranked):
    ranked["loan amount 600000 karo"] = [("loan_amount", 0.86), ("case_record", 0.8)]
    assert pipeline.read("loan amount 600000 karo", "c1") is None


def test_a_field_named_but_not_asked_about_is_not_its_question(ranked):
    ranked["No, I meant the previous case."] = [("cpa_readiness", 0.53)]
    assert pipeline.read("No, I meant the previous case.", "c1") is None        # names the case id, asks nothing
    assert pipeline.read("what is his date of birth", "c1").canonical == "what is the date of birth"
