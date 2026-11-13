"""
Downstream questions belong to the router (OUT_OF_SCOPE + route_to), not to the general layer or the Qwen agent
(2026-10-22: "should we approve this loan?" came back CLARIFICATION, "credit score?" NOT_IN_SCOPE). Genuinely general
questions keep the general layer's own answers.
"""

from __future__ import annotations

import pytest

from app.agents.applicant.copilot.capabilities import general, qwen_agent
from tests.integration.test_fos_api import _store, ask, case, client, fos_token  # noqa: F401 -- fixtures

DOWNSTREAM = [
    ("Should we approve this loan?", "DECISION_AGENT"),
    ("What is the applicant's credit score?", "CREDIT_AGENT"),
    ("Calculate the risk for this case.", "RISK_AGENT"),
    ("Do a complete KYC.", "KYC_AGENT"),
    ("Run an RCU check.", "RCU_AGENT"),
]


@pytest.mark.parametrize("message, agent", DOWNSTREAM)
def test_the_general_layer_hands_downstream_questions_to_the_router(message, agent):
    assert general._downstream_route(message, "CASE-X") == agent


@pytest.mark.parametrize("message", [
    "best mutual fund to invest in?",          # routed to NONE: the general layer's polite out-of-scope line
    "which loan should I take?",               # routed to NONE
    "what documents are needed for a personal loan?",
    "what is FOIR?",
    "hello",
])
def test_genuinely_general_questions_stay_with_the_general_layer(message):
    assert general._downstream_route(message, "CASE-X") is None


async def test_the_qwen_agent_never_calls_the_model_for_a_downstream_question(monkeypatch):
    monkeypatch.setattr(qwen_agent, "enabled", lambda: True)
    monkeypatch.setattr(qwen_agent, "_ready", lambda: True)
    asked = qwen_agent.STATS.get("asked", 0)
    out = await qwen_agent.answer("Should we approve this loan?", chat_key=None, case_id="CASE-X", product=None,
                                  claims={}, lookup=None, handbook=None)
    assert out is None and qwen_agent.STATS.get("asked", 0) == asked


def test_investment_advice_still_gets_the_general_out_of_scope_answer(client, case):
    applicant_id, case_id = case
    body = ask(client, applicant_id, case_id, "CUSTOM_QUERY", "best mutual fund to invest in?").json()
    # the general layer's own polite line (product_flow out_of_scope): kept exactly as it was
    assert (body["intent"], body.get("route_to"), body["query_type"]) == ("NOT_IN_SCOPE", None, "CONVERSATION")
