"""
THE LLM EXPLAINS, IT NEVER DECIDES (2026-10-13; app/agents/eligibility/explain.py).

An explanation is shown only when every figure in it is the result's own, it uses no decision word that contradicts
the decision, and it is short; the model down / slow gives no explanation, never an error; the decision is never
touched.
"""
from __future__ import annotations

import pytest

from app.agents.eligibility.output import summary as explain

RESULT = {"case_id": "CASE-E11D00000009", "decision": "CONDITIONAL", "requested_amount": 800000.0,
          "eligible_amount": 365000.0, "emi": 9974.16, "foir": 49.95, "limiting_factor": "FOIR",
          "rules": [{"rule": "FOIR", "result": "fail", "value": 73.72, "limit": 50}],
          "reasons": ["FOIR 73.72% on the requested loan against the limit 50%."]}


@pytest.mark.parametrize("text", [
    "The loan can be given up to ₹3,65,000 instead of ₹8,00,000 because FOIR would be 73.72%.",
    "Existing EMIs push FOIR to 73.72%, so the eligible amount is 365000 with an EMI of 9974.16.",
    "Loan ₹3,65,000 tak ho sakta hai, kyunki FOIR 73.72% ho raha tha.",
])
def test_an_explanation_with_the_results_own_figures_is_shown(text):
    assert explain.accepted(RESULT, text)


@pytest.mark.parametrize("text", [
    "The loan can be given up to ₹4,00,000.",                           # a figure the result does not hold
    "FOIR is 73.72%, above the bank's limit of 55%.",                   # an invented limit
    "The application is rejected because FOIR is too high.",            # contradicts CONDITIONAL
    "x" * 400,                                                          # too long
    "",
])
def test_an_explanation_that_invents_or_contradicts_is_never_shown(text):
    assert not explain.accepted(RESULT, text)


def test_a_model_that_cannot_be_reached_still_gives_the_remark_from_the_facts(monkeypatch):
    # owner 2026-10-15: the summary is shown for EVERY case -- the model down -> the same remark built from the facts
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")
    text = explain.explain(dict(RESULT))
    assert text == explain.fallback(dict(RESULT)) and text and explain.accepted(dict(RESULT), text)


def test_the_explanation_never_changes_the_result(monkeypatch):
    import json
    import urllib.request

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):                    # Ollama's stream: one JSON line per piece, the last one done
            yield json.dumps({"message": {"content": "Eligible for ₹3,65,000 "}}).encode() + b"\n"
            yield json.dumps({"message": {"content": "because FOIR binds."}, "done": True}).encode() + b"\n"

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Reply())
    result = dict(RESULT)
    before = json.dumps(result, sort_keys=True)
    assert explain.explain(result) == "Eligible for ₹3,65,000 because FOIR binds."
    assert json.dumps(result, sort_keys=True) == before
