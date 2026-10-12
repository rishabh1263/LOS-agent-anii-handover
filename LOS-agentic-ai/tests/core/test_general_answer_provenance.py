"""
A GENERAL (model) ANSWER IS NEVER PASSED OFF AS THE HANDBOOK (2026-10-11, directive section 9): it runs only after the
knowledge base found nothing, never with a case in scope, and says what it is. No Ollama: the model call is replaced.
"""
import asyncio

from app.agents.applicant.copilot.capabilities import general


def _no_knowledge(monkeypatch, said="A home loan is secured by the property being bought, usually by a mortgage."):
    async def nothing(*_a, **_k):
        return None

    async def model(question, request_id):
        return said

    monkeypatch.setattr(general, "_knowledge_candidate", nothing)
    monkeypatch.setattr(general, "_general_llm", model)


def test_a_general_answer_is_labelled(monkeypatch):
    _no_knowledge(monkeypatch)
    reply = asyncio.run(general._knowledge("how is a home loan secured", "r1", "en", None))
    assert reply["response_source"] == "LLM_GENERAL"
    assert "not from the company handbook" in reply["answer"]


def test_never_with_a_case_in_scope(monkeypatch):
    _no_knowledge(monkeypatch)
    assert asyncio.run(general._knowledge("how is a home loan secured", "r1", "en", "CASE-ABC123")) is None


def test_a_handbook_answer_comes_first(monkeypatch):
    called = []

    async def model(question, request_id):
        called.append(question)
        return "should not be used"

    monkeypatch.setattr(general, "_general_llm", model)
    reply = asyncio.run(general._knowledge("what is the minimum bank statement period?", "r1", "en", None))
    assert not called and reply and reply.get("response_source") != "LLM_GENERAL", reply
