"""
The FOS chatbot: what it answers from the case, what from the handbook, and
what it refuses.

THE LINE THIS SUITE DEFENDS. There are two sources of truth and they must
never stand in for each other:

    the STORE   answers "is this applicant's PAN verified?"
    the CORPUS  answers "what can be used as address proof?"

A knowledge base that appears to answer case questions is the dangerous
failure. It produces fluent, sourced, confident sentences about an applicant
it has never seen, and they are indistinguishable from correct ones.

And a retriever always returns its best match, including for a question the
corpus cannot answer at all. So the other thing tested here is the refusal:
below the confidence threshold the agent says it does not know, rather than
handing back the least-bad paragraph.

The model is off throughout. Every assertion is about retrieval, grounding
and refusal, none about phrasing.
"""

from __future__ import annotations

import asyncio

import pytest

from app import knowledge as knowledge_layer
from app.agents.applicant import config as agent_config
from app.agents.applicant import knowledge_answer
from app.agents.applicant.intents import Intent, classify, plan_for


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No model. Retrieval and grounding only."""
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    agent_config.reload()
    knowledge_layer.reload()
    yield
    agent_config.reload()
    knowledge_layer.reload()


def ask(question: str):
    return asyncio.run(knowledge_answer.answer(question))


# ==========================================================================
# THE CORPUS EXISTS AND IS SCOPED
# ==========================================================================

def test_the_fos_corpus_is_loaded():
    described = knowledge_layer.describe()
    stages = described["repository"]["stages"]
    assert "FOS" in stages, "no FOS knowledge was loaded"
    assert stages["FOS"]["chunks"] > 0
    assert len(stages["FOS"]["sources"]) >= 5


def test_retrieval_is_scoped_to_one_stage():
    """
    A FOS question must never be answered from another stage's knowledge.

    The scoping is in the interface rather than in a filter somebody has to
    remember: asking a stage that has no corpus returns nothing, it does not
    fall back to whatever is loaded.
    """
    retriever = knowledge_layer.get_retriever()
    assert retriever.retrieve("address proof", "FOS").hits
    assert retriever.retrieve("address proof", "CREDIT").hits == []
    assert retriever.retrieve("address proof", "RCU").hits == []


# ==========================================================================
# KNOWLEDGE QUESTIONS ARE ANSWERED FROM THE CORPUS
# ==========================================================================

@pytest.mark.parametrize("question, expected_source", [
    ("What documents can be used as address proof?", "address_proof.md"),
    ("What does CPA readiness mean?", "cpa_readiness.md"),
    ("What happens during FOS verification?", "fos_workflow.md"),
    ("Can I upload multiple documents at once?", "faq.md"),
    ("What does SKIPPED mean?", "document_statuses.md"),
])
def test_a_knowledge_question_is_answered_and_cited(question, expected_source):
    answer, _source, detail = ask(question)

    assert detail["confident"], f"{question!r} retrieved nothing usable"
    assert answer and answer != knowledge_answer.NO_ANSWER
    assert detail["citations"], "an answer with no source"
    assert any(expected_source in c for c in detail["citations"]), (
        f"{question!r} cited {detail['citations']} rather than {expected_source}"
    )


def test_the_answer_comes_from_the_retrieved_passage():
    """
    Grounding, checked rather than assumed.

    With the model off the answer IS the retrieved text, so this verifies the
    path end to end: what was retrieved is what was returned.
    """
    answer, source, detail = ask("What documents can be used as address proof?")
    assert source == "deterministic"

    result = knowledge_layer.get_retriever().retrieve(
        "What documents can be used as address proof?", "FOS", limit=3,
    )
    assert result.hits
    assert answer[:40] in " ".join(result.hits[0].chunk.text.split())


# ==========================================================================
# REFUSAL
# ==========================================================================

@pytest.mark.parametrize("question", [
    "How do I bake sourdough bread?",
    "What is the capital of France?",
    "Who won the football match last night?",
    "Write me a poem about the sea.",
    "What is the weather tomorrow?",
])
def test_a_question_the_corpus_cannot_answer_is_refused(question):
    """
    THE MOST IMPORTANT TEST HERE.

    A retriever returns its best chunk for any query. Answering from one that
    scored barely above noise produces a confident, sourced, wrong answer --
    the worst failure available to a grounded system, because it looks
    exactly like a right one.
    """
    answer, _source, detail = ask(question)

    assert not detail["confident"], (
        f"{question!r} was answered with confidence {detail['top_score']}"
    )
    assert answer == knowledge_answer.NO_ANSWER
    assert detail.get("refused") is True


def test_the_refusal_says_what_it_is_and_offers_nothing_else():
    """No hedged paragraph, no guess with a disclaimer attached."""
    answer, _source, _detail = ask("What is the capital of France?")
    assert "don't have enough information" in answer
    assert "FOS knowledge base" in answer


def test_an_off_topic_question_scores_far_below_a_real_one():
    """
    The threshold has to have room to sit in.

    If the worst in-corpus question scored near the best off-topic one, no
    threshold could separate them and `confident` would be decoration.
    """
    retriever = knowledge_layer.get_retriever()

    real = min(
        retriever.retrieve(q, "FOS").top_score
        for q in ("What documents can be used as address proof?",
                  "What does CPA readiness mean?",
                  "What happens during FOS verification?")
    )
    noise = max(
        retriever.retrieve(q, "FOS").top_score
        for q in ("How do I bake sourdough bread?",
                  "What is the capital of France?",
                  "Write me a poem about the sea.")
    )
    assert real > noise * 2, (
        f"worst real question {real:.3f} is not clearly above best noise "
        f"{noise:.3f}"
    )


# ==========================================================================
# CASE QUESTIONS NEVER REACH THE CORPUS
# ==========================================================================

@pytest.mark.parametrize("question", [
    "What documents have been uploaded?",
    "What documents are pending?",
    "Has the PAN been verified?",
    "What is the next action?",
    "Is this case ready for CPA?",
    "What is pending?",
    "Show me the case status.",
])
def test_a_case_question_is_routed_to_the_store_not_the_corpus(question):
    """
    A live fact must come from the records.

    If any of these classified as FOS_KNOWLEDGE the agent would answer a
    question about THIS applicant out of a handbook.
    """
    classification = classify(question)
    assert classification.intent is not Intent.FOS_KNOWLEDGE, (
        f"{question!r} would have been answered from the knowledge base"
    )
    assert classification.intent is not Intent.UNKNOWN
    assert plan_for(classification), (
        f"{question!r} reached no tool, so it has no source of facts"
    )


def test_the_corpus_contains_no_applicant_data():
    """
    The corpus is policy. If a real name, PAN or case id were ever added to
    it, retrieval would start returning case-shaped strings for generic
    questions.
    """
    import re

    pan = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
    case = re.compile(r"\bCASE-[0-9A-F]{8,}\b")
    applicant = re.compile(r"\bAPP-[0-9A-F]{8,}\b")

    for chunk in knowledge_layer.get_repository().chunks("FOS"):
        assert not pan.search(chunk.text), f"a PAN appears in {chunk.source}"
        assert not case.search(chunk.text), f"a case id appears in {chunk.source}"
        assert not applicant.search(chunk.text), (
            f"an applicant id appears in {chunk.source}"
        )


# ==========================================================================
# MIXED
# ==========================================================================

@pytest.mark.parametrize("question, base", [
    ("Why is this applicant not ready for CPA and what should I collect?",
     Intent.READINESS),
    ("What is pending and what can be used as address proof?",
     Intent.PENDING_ITEMS),
])
def test_a_mixed_question_keeps_its_case_intent(question, base):
    """
    The case half must still be answered from the case.

    A mixed question is not a knowledge question with extra words: the facts
    come from the store exactly as they would if the second clause were not
    there, and the knowledge is appended.
    """
    classification = classify(question)
    assert classification.intent is Intent.MIXED
    assert classification.base_intent is base
    assert plan_for(classification), "a mixed question reached no case tool"


def test_a_mixed_question_plans_the_same_tools_as_its_case_half():
    """Attaching a knowledge clause must not change which facts are read."""
    mixed = classify(
        "Why is this applicant not ready for CPA and what should I collect?"
    )
    alone = classify("Why is this applicant not ready for CPA?")
    assert plan_for(mixed) == plan_for(alone)


# ==========================================================================
# DOWNSTREAM QUESTIONS ARE ROUTED, NOT RETRIEVED
# ==========================================================================

@pytest.mark.parametrize("question, route", [
    ("What is the applicant's credit score?", "CREDIT_SCORE"),
    ("What is the risk score?", "RISK"),
    ("Is the KYC decision approved?", "KYC_DECISION"),
    ("Are there any fraud concerns?", "RCU_FRAUD"),
    ("Should we approve the loan?", "LOAN_DECISION"),
])
def test_a_downstream_question_is_routed_before_anything_is_retrieved(
    question, route
):
    """
    Out of scope is decided FIRST, ahead of both the store and the corpus.

    The corpus says what the FOS stage does and does not do, so a credit
    question could plausibly retrieve "credit is handled downstream" and be
    answered from the handbook. That would be a fluent non-answer where a
    route is required.
    """
    classification = classify(question)
    assert classification.intent is Intent.OUT_OF_SCOPE
    assert classification.route_to == route
    assert plan_for(classification) == ()


# ==========================================================================
# THE KNOWLEDGE LAYER DEGRADES, IT DOES NOT BREAK
# ==========================================================================

def test_a_missing_corpus_refuses_rather_than_raising(monkeypatch, tmp_path):
    """
    A deployment shipped without the knowledge directory still serves case
    questions. The chatbot loses the handbook and nothing else.
    """
    monkeypatch.setenv("KNOWLEDGE_ROOT", str(tmp_path / "nothing"))
    knowledge_layer.set_repository(None)
    knowledge_layer.reload()

    answer, _source, detail = ask("What can be used as address proof?")
    assert answer == knowledge_answer.NO_ANSWER
    assert detail["confident"] is False

    knowledge_layer.set_repository(None)


def test_knowledge_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_ENABLED", "false")
    answer, _source, detail = ask("What can be used as address proof?")
    assert answer == knowledge_answer.NO_ANSWER
    assert detail["confident"] is False


def test_retrieval_never_returns_a_prompt_or_a_chunk_id_to_a_caller():
    """What a caller is told about retrieval is citations and a score."""
    from app.agents.applicant.agent import _public_knowledge

    _answer, _source, detail = ask("What does CPA readiness mean?")
    published = _public_knowledge(detail)

    assert set(published) == {"stage", "grounded", "sources", "top_score"}
    assert all(isinstance(s, str) for s in published["sources"])


# ==========================================================================
# THE PHRASING PATH MUST NEVER TAKE THE ANSWER DOWN
# ==========================================================================

def test_phrasing_failures_fall_back_instead_of_raising(monkeypatch):
    """
    THE BUG THIS PINS, found only in a live run.

    `_phrase` is optional by contract: it returns None on any failure and the
    retrieved text is used instead. Its imports were written ABOVE the try
    block, and one of them named a symbol the installed framework does not
    export. The import raised straight out of a function that is supposed to
    be incapable of raising, and a question the service could answer from the
    corpus returned HTTP 500.

    Every focused test ran with the model off, so `_phrase` was never called
    and the whole suite passed.
    """
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "true")
    agent_config.reload()

    async def explode(*_args, **_kwargs):
        raise ImportError("cannot import name 'Nonexistent'")

    monkeypatch.setattr(knowledge_answer, "_phrase", explode)

    answer, source, detail = ask("What documents can be used as address proof?")

    assert detail["confident"] is True
    assert source == "deterministic"
    assert answer and answer != knowledge_answer.NO_ANSWER


def test_the_phrasing_path_uses_symbols_the_framework_actually_exports():
    """
    Checked directly, because the fallback above hides the difference.

    A broken import inside `_phrase` is invisible to every test that runs
    with the model off, and silently costs the model on every request when it
    is on -- the answers stay correct and the phrasing quietly never happens.
    """
    import inspect

    from agent_framework import Message, Role  # noqa: F401 - must import

    source = inspect.getsource(knowledge_answer._phrase)
    assert "ChatMessage" not in source, (
        "_phrase imports ChatMessage, which agent_framework does not export"
    )
    assert "from agent_framework import Message" in source
