"""
A VALUE QUESTION THE HANDBOOK ANSWERS is answered from the handbook (2026-10-11, audit P2-C): the value-shape rule
abstains only when no handbook passage clears the threshold -- a company value still never comes from the glossary.
Real retrieval (BM25 over knowledge/fos), no model.
"""
import pytest

from app.agents.applicant import knowledge_answer as ka


@pytest.fixture(autouse=True)
def _lexical(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hashing")


def test_the_handbook_answers_the_bank_statement_period():
    result = ka.retrieve("what is the minimum bank statement period?")
    assert result.confident, result
    assert result.hits[0].chunk.source == "officer_howto.md", [h.chunk.source for h in result.hits]


@pytest.mark.parametrize("question", [
    "What is the processing fee on a home loan?",
    "What is the maximum interest rate we can charge on a personal loan?",
    "If the customer repays the personal loan early, is there a prepayment penalty?",
])
def test_a_company_value_the_handbook_does_not_state_is_still_not_answered(question):
    # not answered at all: neither a glossary definition nor an unrelated handbook passage (the home-loan checklist
    # is not a processing fee) may be confident for it
    result = ka.retrieve(question)
    assert not (result and result.confident), [(h.chunk.source, h.chunk.heading) for h in (result.hits if result else [])]
