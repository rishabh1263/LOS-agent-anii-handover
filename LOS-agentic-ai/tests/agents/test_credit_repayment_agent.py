"""
Credit underwriting end to end with the repayment report: the credit agent reads the vendor bank analysis through the
credit-stage MCP capability (repayment.get), the verdict becomes a policy finding, the assessment and memo carry it, and
provenance verifies it against the case's stored analysis. Synthetic data only.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

import test_credit_slice2_graph as slice2
from test_credit_slice2_graph import (  # noqa: F401  (fixtures + helpers)
    caller, demo_bureau, full_evidence, seed, store,
)
from test_credit_repayment import demo, eligibility

from app.agents.credit import agent
from app.agents.credit.repayment import service, store as bank_store
from app.mcp import credit as credit_mcp

CASE = "CASE-0000000000CA"          # a case id the bank-analysis store accepts


@pytest.fixture
def case(monkeypatch, tmp_path, store):
    """The slice-2 case under a bank-analysis-shaped id, with a temp analysis store."""
    monkeypatch.setattr(slice2, "CASE", CASE)
    monkeypatch.setattr(bank_store, "_spec", lambda: {"backend": "files", "files_dir": str(tmp_path / "bank"),
                                                      "demo_dir": str(tmp_path / "none")})
    seed(store, declared_monthly_obligations="0", stage=None)
    slice2.set_stage(store, "CREDIT", case_id=CASE)       # its default case id is bound at definition
    full_evidence(store)
    return store


def receive(story=None, decision="APPROVED"):
    bank_store.save(CASE, demo.report(CASE, 60000.0, 5000.0, story))
    return patch.object(service, "_eligibility", lambda case_id: eligibility(decision=decision))


def underwrite():
    run = asyncio.run(agent.underwrite(CASE, caller=caller(), request_id="req-rep"))
    assert run.status == "SUCCEEDED", run.error
    return run


def finding_ids(run):
    return {f["policy_rule_id"] for f in run.output["assessment"]["findings"]}


def test_a_clean_bank_analysis_is_a_positive_finding_through_mcp(case):
    seen = []
    real = credit_mcp.READ_TOOLS["repayment.get"]

    async def spy(case_id):
        seen.append(case_id)
        return await real(case_id)

    with receive(), patch.dict(credit_mcp.READ_TOOLS, {"repayment.get": spy}):
        run = underwrite()
    assert seen == [CASE]                                            # read through the MCP capability
    assert "UW_BANK_REPAYMENT_RECOMMENDED" in finding_ids(run)
    fin = run.output["assessment"]["financial_observations"]["repayment_recorded"]
    assert fin["verdict"] == "RECOMMENDED" and len(fin["rules"]) == 5
    assert run.output["memo"]["repayment"]["bank_analysis"]["can_repay"] == "YES"
    calls = [t["tool"] for t in run.trajectory["tool_calls"]]
    # the optional reads come last (repayment, then document readiness): never spend a required read's budget
    assert calls[-2:] == ["repayment.get", "document_readiness.get"]


def test_bounces_make_a_negative_finding_and_review(case):
    with receive("bounces"):
        run = underwrite()
    assert "UW_BANK_REPAYMENT_NOT_RECOMMENDED" in finding_ids(run)
    assert run.output["status"] == "REVIEW_REQUIRED"
    assert any("UW_BANK_REPAYMENT_NOT_RECOMMENDED" in r for r in run.output["assessment"]["status_reasons"])


def test_an_eligibility_refer_refers_even_a_clean_bank(case):
    with receive(decision="REFER"):
        run = underwrite()
    assert "UW_BANK_REPAYMENT_REFER" in finding_ids(run)


def test_the_bank_finding_is_verified_against_the_stored_analysis(case):
    with receive():
        run = underwrite()
    facts = [n for n in run.provenance["nodes"] if n.get("record_type") == "BANK_ANALYSIS"]
    assert facts and all(n["verified"] for n in facts)


def test_no_bank_analysis_is_assessed_without_it(case):
    run = underwrite()
    assert not any(f.startswith("UW_BANK_REPAYMENT") for f in finding_ids(run) if f)
    assert run.output["assessment"]["financial_observations"]["repayment_recorded"] is None
    assert run.output["status"] == "READY_FOR_DECISION"


def test_mcp_capability_envelopes():
    async def call(cid):
        return (await credit_mcp.repayment_get(cid)).as_tool_payload()

    with patch.object(bank_store, "_spec", lambda: {"backend": "files", "files_dir": "nowhere-x", "demo_dir": None}):
        out = asyncio.run(call(CASE))
    assert out["ok"] is True and out["result"]["recorded"] is False
    assert asyncio.run(call(""))["ok"] is False                       # case_id required
    assert credit_mcp.catalogue()[0]["annotations"]["readOnlyHint"] is True
    assert set(credit_mcp.CONTRACTS["repayment.get"].never) == set(credit_mcp.NEVER)
    json.dumps(credit_mcp.catalogue())


# ==========================================================================
# the credit MCP server over the real protocol (in-memory transport, signed token)
# ==========================================================================

def _over_mcp(arguments, token=None):
    from mcp.shared.memory import create_connected_server_and_client_session

    from app.mcp.credit_server import server
    from app.mcp.runtime import _envelope_from

    async def go():
        async with create_connected_server_and_client_session(server) as session:
            listed = {t.name for t in (await session.list_tools()).tools}
            meta = {"authorization": f"Bearer {token}"} if token else None
            return listed, await session.call_tool("repayment.get", arguments, meta=meta)

    listed, result = asyncio.run(go())
    return listed, _envelope_from("repayment.get", result)


def test_credit_server_serves_the_report_to_a_credit_officer(make_token, tmp_path, monkeypatch):
    monkeypatch.setattr(bank_store, "_spec", lambda: {"backend": "files", "files_dir": str(tmp_path / "bank"),
                                                      "demo_dir": None})
    bank_store.save(CASE, demo.report(CASE, 60000.0, 5000.0, "one_bounce"))
    with patch.object(service, "_eligibility", lambda case_id: eligibility()):
        listed, env = _over_mcp({"case_id": CASE}, make_token(subject="uw", scopes=["los.credit.underwrite"]))
    assert listed == {"repayment.get", "legal_verification.get"}
    assert env.ok and env.result["recorded"] is True and env.result["report"]["verdict"] == "REFER"


@pytest.mark.parametrize("scopes, args, code", [
    (["unrelated.scope"], {"case_id": CASE}, "INSUFFICIENT_SCOPE"),
    (None, {"case_id": CASE}, "UNAUTHENTICATED"),
    (["los.credit.underwrite"], {"case_id": "../etc/passwd"}, "INVALID_INPUT"),
])
def test_credit_server_refuses(make_token, scopes, args, code):
    from app.security import auth

    if scopes is None and not auth.auth_enabled():
        pytest.skip("authentication is off in this environment")
    token = make_token(subject="uw", scopes=scopes) if scopes else None
    _, env = _over_mcp(args, token)
    assert env.ok is False and env.error.code == code


# ==========================================================================
# a LOS case the eligibility table does not hold: the case store's application + recorded eligibility
# ==========================================================================

def _not_in_eligibility_table():
    from app.agents.eligibility import agent as eligibility_agent

    return patch.object(eligibility_agent, "assess_payload", lambda payload: {"error": "CASE_NOT_FOUND"})


def test_a_los_case_reads_its_application_and_recorded_eligibility(case):
    with _not_in_eligibility_table():
        elig = service._eligibility(CASE)
    assert elig["source"] == "case_store"
    assert elig["inputs"]["monthly_income"] == 65000.0 and elig["inputs"]["monthly_obligations"] == 0.0
    assert elig["decision"] == "APPROVED"                              # recorded PASS (policy.yaml recorded_eligibility)


def test_a_los_case_without_recorded_eligibility_is_never_recommended(monkeypatch, tmp_path, store):
    monkeypatch.setattr(slice2, "CASE", CASE)
    monkeypatch.setattr(bank_store, "_spec", lambda: {"backend": "files", "files_dir": str(tmp_path / "bank"),
                                                      "demo_dir": None})
    seed(store, declared_monthly_obligations="0", stage=None)        # an application, no findings at all
    bank_store.save(CASE, demo.report(CASE, 65000.0, 0.0, None))
    with _not_in_eligibility_table():
        report = service.report_for(CASE)
    assert report["eligibility"]["decision"] == "DATA_MISSING" and report["eligibility"]["source"] == "case_store"
    assert report["verdict"] != "RECOMMENDED"


def test_a_case_in_neither_place_is_not_found():
    with _not_in_eligibility_table(), pytest.raises(service.RepaymentError) as exc:
        service._eligibility("CASE-00000000FFFF")
    assert exc.value.code == "CASE_NOT_FOUND"
