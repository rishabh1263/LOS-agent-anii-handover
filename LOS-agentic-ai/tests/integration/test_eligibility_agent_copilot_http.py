"""
THE ELIGIBILITY AGENT'S RESULT, END TO END over HTTP: produced by the agent inside
/api/v1/los/process, persisted as the case's authoritative finding, served by
GET /api/v1/eligibility/{case_id}, and CONSUMED -- never computed -- by the Copilot.
"""

from __future__ import annotations

import pytest

from tests.integration.test_eligibility_end_to_end import (  # noqa: F401 - fixtures by import
    SLIP, _environment, _kyc_gate_unset, ask, client, open_case, process_slip)

pytestmark = pytest.mark.skipif(not SLIP.exists(), reason="demo salary slip not generated")


def test_the_agent_result_is_persisted_and_served(client):
    applicant_id, case_id = open_case(client)
    process_slip(client, applicant_id, case_id)
    body = client.get(f"/api/v1/eligibility/{case_id}", params={"applicant_id": applicant_id}).json()
    recorded = body.get("eligibility") or {}
    assert recorded["state"] == "ELIGIBLE" and recorded["eligible"] is True
    assert "FOIR" in recorded["passed_rules"]
    assert any(r["rule_id"] == "FOIR" and r["source"] == "COMPUTED" for r in recorded["rules"])
    assert set(recorded["configuration_gaps"]) >= {"AGE", "KYC_PREREQUISITE"}   # unset, reported


def test_the_copilot_answers_from_the_agent_result_with_the_structured_block(client):
    applicant_id, case_id = open_case(client)
    process_slip(client, applicant_id, case_id)
    reply = ask(client, applicant_id, case_id, "Is the applicant eligible?")
    assert reply["answer"].startswith("Eligibility: ELIGIBLE")
    assert (reply.get("eligibility") or {}).get("state") == "ELIGIBLE"


def test_missing_obligations_are_pending_and_the_copilot_says_who_supplies_them(client):
    applicant_id, case_id = open_case(client, declared_monthly_obligations=None)
    process_slip(client, applicant_id, case_id)
    reply = ask(client, applicant_id, case_id, "What is missing for eligibility?")
    assert "pending" in reply["answer"].lower()
    assert "monthly obligations" in reply["answer"] and "(fos)" in reply["answer"]
    summary = ask(client, applicant_id, case_id, "Am I eligible?")
    assert summary["answer"].startswith("Eligibility: PENDING")
    assert "Next: Capture the applicant's existing monthly loan obligations" in summary["answer"]


def test_a_failed_rule_is_named_with_its_figures(client):
    applicant_id, case_id = open_case(client, declared_monthly_obligations=40000)
    process_slip(client, applicant_id, case_id)
    reply = ask(client, applicant_id, case_id, "Which eligibility rule failed?")
    assert "Failed: FOIR --" in reply["answer"] and "against max 50%" in reply["answer"]
    assert "under review" in reply["answer"]                    # the demo policy reviews breaches


def test_the_evidence_and_the_next_step_are_read_back(client):
    applicant_id, case_id = open_case(client, declared_monthly_obligations=40000)
    process_slip(client, applicant_id, case_id)
    evidence = ask(client, applicant_id, case_id, "What documents are behind the eligibility result?")
    assert "salary slip" in evidence["answer"] and "computed" in evidence["answer"]
    nxt = ask(client, applicant_id, case_id, "What should I do next for eligibility?")
    assert "Review affordability" in nxt["answer"]
