"""
Credit Underwriting Agent -- Slice 1: contracts, G1, G2, G5, adapters, bureau, tools.

Real SQLite repository on a temp file; findings written through the same
ingest path the LOS pipeline uses, then read back by the credit adapters.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.agents.credit import config as credit_config
from app.agents.credit import tools
from app.agents.credit.adapters import bank, case_memory, risk
from app.agents.credit.bureau import BureauError, BureauTimeout, get_provider
from app.agents.credit.bureau import demo as demo_bureau
from app.agents.credit.schemas import Party, Quality, UnderwritingContext
from app.store import set_repository
from app.store.models import Applicant, Application, FindingKind
from app.store.sqlite_repo import SQLiteRepository

CASE = "CASE-CR1"
APP = "APP-CR1"


@pytest.fixture(autouse=True)
def store(tmp_path):
    repository = SQLiteRepository(tmp_path / "credit.sqlite3")
    repository.initialise()
    set_repository(repository)
    demo_bureau.clear_assignments()
    yield repository
    set_repository(None)
    demo_bureau.clear_assignments()


def memory_on():
    return patch("app.agents.los.config.case_memory_enabled", return_value=True)


def seed(repository, **application):
    repository.save_applicant(Applicant(applicant_id=APP, full_name="Test Applicant"))
    repository.save_application(Application(
        case_id=CASE, applicant_id=APP, product="PERSONAL_LOAN",
        loan_amount="500000", **application))


def ctx(**extra) -> UnderwritingContext:
    return UnderwritingContext(case_id=CASE, applicant_id=APP, stage="CREDIT",
                               request_id="req-1", parties=[Party(party_id=APP)], **extra)


def persist(result: dict):
    from app.store.ingest import persist_los_result

    with memory_on():
        return persist_los_result({"applicant_id": APP, "case_id": CASE, **result})


RISK_BLOCK = {"agent": "fraud_risk_agent", "risk_category": "MEDIUM", "risk_score": 42,
              "final_outcome": "REVIEW", "flags": ["NAME_MISMATCH:primary", "ADDRESS_GAP"],
              "summary": "MODEL WRITTEN PROSE THAT MUST NOT BE STORED"}


# ==========================================================================
# G1 -- declared monthly income
# ==========================================================================

def test_g1_declared_income_round_trips_through_sqlite(store):
    seed(store, declared_monthly_income="65000")
    assert store.get_application(CASE).declared_monthly_income == "65000"


def test_g1_declared_income_is_nullable_and_backward_compatible(store):
    seed(store)
    assert store.get_application(CASE).declared_monthly_income is None


def test_g1_declared_income_update_via_upsert(store):
    seed(store, declared_monthly_income="65000")
    record = store.get_application(CASE)
    record.declared_monthly_income = "70000"
    store.save_application(record)
    assert store.get_application(CASE).declared_monthly_income == "70000"


def test_g1_application_get_exposes_declared_income(store):
    from app.mcp import applicant as capabilities

    seed(store, declared_monthly_income="65000", declared_monthly_obligations="5000")
    envelope = asyncio.run(capabilities.application_get(CASE))
    assert envelope.ok
    assert envelope.result["application"]["declared_monthly_income"] == "65000"
    assert envelope.result["application"]["declared_monthly_obligations"] == "5000"


def test_g1_tool_contracts_and_fos_request_accept_declared_income():
    from app.api.routes.fos_api import ApplicationDetails
    from app.mcp.contracts import CONTRACTS

    assert "declared_monthly_income" in ApplicationDetails.model_fields
    for name in ("application.create", "application.update"):
        contract = CONTRACTS.get(name)
        if contract is None:
            continue
        schema = getattr(contract, "input_schema", None) or {}
        assert "declared_monthly_income" in (schema.get("properties") or {}), name


# ==========================================================================
# G2 -- the risk result is persisted, then read back
# ==========================================================================

def test_g2_risk_result_is_persisted_as_a_risk_finding(store):
    seed(store)
    persist({"risk": RISK_BLOCK})
    findings = store.get_current_findings(CASE, kind=FindingKind.RISK)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.status == "REVIEW" and finding.score == 42
    assert finding.source_type == "FRAUD_RISK" and finding.party_id == APP
    assert finding.reason_codes == ["NAME_MISMATCH", "ADDRESS_GAP"]
    assert "summary" not in finding.payload            # model prose never stored
    assert "MODEL WRITTEN" not in str(finding.payload)


def test_g2_no_risk_block_writes_no_risk_finding(store):
    seed(store)
    persist({"risk": None})
    assert store.get_current_findings(CASE, kind=FindingKind.RISK) == []


def test_g2_reprocessing_keeps_one_current_risk_finding(store):
    seed(store)
    persist({"risk": RISK_BLOCK})
    persist({"risk": RISK_BLOCK})
    assert len(store.get_current_findings(CASE, kind=FindingKind.RISK)) == 1


def test_g2_risk_get_reads_the_recorded_result_without_rescoring(store):
    seed(store)
    persist({"risk": RISK_BLOCK})
    observation = risk.risk_get(CASE)
    assert observation.quality is Quality.PRESENT
    assert observation.data["final_outcome"] == "REVIEW"
    assert observation.data["risk_score"] == 42
    assert observation.evidence and observation.evidence[0].source.value == "RISK"


def test_risk_get_missing_is_missing_not_a_default(store):
    seed(store)
    observation = risk.risk_get(CASE)
    assert observation.quality is Quality.MISSING and observation.data == {}


# ==========================================================================
# G5 -- scope and stages are configuration
# ==========================================================================

def test_g5_scope_and_stage_defaults_come_from_agents_yaml():
    assert credit_config.required_scope() == "los.credit.underwrite"
    assert credit_config.allowed_stages() == ("CREDIT",)
    assert credit_config.max_tool_calls() == 10
    assert credit_config.max_replans() == 2
    assert credit_config.bureau_retries() == 1


def test_g5_scope_and_stages_are_overridable(monkeypatch):
    monkeypatch.setenv("CREDIT_REQUIRED_SCOPE", "los.credit.custom")
    monkeypatch.setenv("CREDIT_ALLOWED_STAGES", "credit, rcu")
    assert credit_config.required_scope() == "los.credit.custom"
    assert credit_config.allowed_stages() == ("CREDIT", "RCU")


def test_bureau_retries_are_capped_at_one():
    with patch.object(credit_config, "_get", side_effect=lambda n, d: 5 if n == "bureau_retries" else d):
        assert credit_config.bureau_retries() == 1


# ==========================================================================
# adapters -- read recorded findings, never recompute
# ==========================================================================

def test_kyc_get_reads_case_level_kyc_as_the_primary_parties(store):
    seed(store)
    persist({"kyc": {"status": "PASS", "overall_score": 95, "reason_codes": [],
                     "fields": [{"field": "name", "status": "MATCH", "match_score": 100}]}})
    observation = case_memory.kyc_get(CASE, [APP], APP)
    assert observation.quality is Quality.PRESENT
    assert observation.data["parties"][APP]["status"] == "PASS"
    assert observation.data["missing_parties"] == []


def test_kyc_get_reports_a_party_without_kyc_as_missing(store):
    seed(store)
    persist({"kyc": {"status": "PASS", "fields": [{"field": "name", "status": "MATCH"}]}})
    observation = case_memory.kyc_get(CASE, [APP, "APP-CO"], APP)
    assert observation.quality is Quality.LOW_CONFIDENCE
    assert observation.data["missing_parties"] == ["APP-CO"]
    assert "APP-CO" not in observation.data["parties"]


def test_income_get_reads_the_recorded_consistency_check(store):
    seed(store)
    persist({"income_consistency": {
        "status": "PASS", "reason_codes": [],
        "bank_statement": {"type": "SALARY_CREDIT", "estimated_monthly_amount": 64000,
                           "months_observed": 3, "recurring_credit_count": 3,
                           "confidence": 0.9},
        "salary_slip": {"figure": "NET_PAY", "amount": 65000, "pay_period": "2026-03"}}})
    observation = case_memory.income_get(CASE)
    assert observation.quality is Quality.PRESENT
    assert observation.data["consistency_status"] == "PASS"
    assert observation.data["documented_monthly_income"] == 65000
    assert observation.data["bank_statement"]["estimated_monthly_amount"] == 64000


def test_income_get_without_a_record_is_missing(store):
    seed(store)
    assert case_memory.income_get(CASE).quality is Quality.MISSING


def _bank_document(evidence: dict | None):
    fields = {"income_evidence": {"type": "SALARY_CREDIT", "estimated_monthly_amount": 64000,
                                  "recurring_credit_count": 3, "confidence": 0.9}}
    if evidence is not None:
        fields["evidence"] = evidence
    return {"source_id": "bank.pdf", "type": "BANK_STATEMENT", "verification": "PASS",
            "party_id": APP, "extraction": {"fields": fields}}


def test_bank_behaviour_reads_reconciled_signals(store):
    seed(store)
    persist({"documents": [_bank_document({"reconciled": True, "transaction_count": 12,
                                           "salary_credit_count": 3,
                                           "returned_transaction_count": 1})]})
    observation = bank.bank_behaviour_get(CASE, APP)
    assert observation.quality is Quality.PRESENT
    assert observation.data["returned_transaction_count"] == 1
    assert observation.data["salary_credit_count"] == 3


def test_bank_behaviour_absent_count_is_not_zero(store):
    """signals.derive omits a narration count when no row matched."""
    seed(store)
    persist({"documents": [_bank_document({"reconciled": True, "transaction_count": 12})]})
    observation = bank.bank_behaviour_get(CASE, APP)
    assert "returned_transaction_count" not in observation.data
    assert "salary_credit_count" not in observation.data


def test_bank_behaviour_without_signals_is_low_confidence(store):
    seed(store)
    persist({"documents": [_bank_document(None)]})
    assert bank.bank_behaviour_get(CASE, APP).quality is Quality.LOW_CONFIDENCE


def test_bank_behaviour_without_a_statement_is_missing(store):
    seed(store)
    assert bank.bank_behaviour_get(CASE, APP).quality is Quality.MISSING


# ==========================================================================
# bureau -- demo provider, typed failures, one retry for transient only
# ==========================================================================

def test_demo_bureau_reports_are_marked_demo():
    report = asyncio.run(get_provider().fetch("APP-3D51FFAC6342", timeout=1))
    assert report is not None and report.is_demo is True
    assert report.provider == "DEMO_BUREAU" and report.max_dpd_12m == 0


def test_demo_bureau_never_invents_a_record():
    assert asyncio.run(get_provider().fetch("APP-UNKNOWN", timeout=1)) is None


def test_bureau_unmapped_party_is_a_missing_observation():
    observation, call = asyncio.run(tools.execute("bureau.get", ctx(), party_id=APP))
    assert observation.quality is Quality.MISSING and observation.error == "NO_BUREAU_RECORD"
    assert call.attempts == 1


def test_bureau_transient_failure_is_retried_exactly_once():
    demo_bureau.assign(APP, "DEMO-BUREAU-TIMEOUT")
    observation, call = asyncio.run(tools.execute("bureau.get", ctx(), party_id=APP))
    assert observation.quality is Quality.UNAVAILABLE and observation.error == "BUREAU_TIMEOUT"
    assert call.attempts == 2


def test_bureau_non_transient_failure_is_not_retried():
    demo_bureau.assign(APP, "DEMO-BUREAU-ERROR")
    observation, call = asyncio.run(tools.execute("bureau.get", ctx(), party_id=APP))
    assert observation.quality is Quality.UNAVAILABLE and observation.error == "BUREAU_ERROR"
    assert call.attempts == 1


def test_bureau_transient_then_success_uses_the_retry():
    class Flaky:
        is_demo = True
        calls = 0

        async def fetch(self, party_id, *, timeout):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise BureauTimeout("once")
            return await demo_bureau.DemoBureauProvider().fetch("APP-3D51FFAC6342",
                                                                timeout=timeout)

    observation, call = asyncio.run(
        tools.execute("bureau.get", ctx(), party_id=APP, bureau_provider=Flaky()))
    assert observation.quality is Quality.PRESENT and call.attempts == 2
    assert observation.is_demo and all(e.is_demo for e in observation.evidence)


def test_bureau_present_observation_carries_demo_evidence():
    demo_bureau.assign(APP, "DEMO-BUREAU-ADVERSE")
    observation, _ = asyncio.run(tools.execute("bureau.get", ctx(), party_id=APP))
    assert observation.quality is Quality.PRESENT
    assert observation.data["score"] == 610 and observation.data["max_dpd_12m"] == 60
    assert observation.evidence and all(e.is_demo for e in observation.evidence)


def test_demo_bureau_is_refused_in_production(monkeypatch):
    from app.core.exceptions import ConfigurationError

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("ALLOW_DEMO_BUREAU", raising=False)
    with pytest.raises(ConfigurationError):
        get_provider()


def test_unknown_bureau_provider_is_refused(monkeypatch):
    from app.core.exceptions import ConfigurationError

    monkeypatch.setenv("CREDIT_BUREAU_PROVIDER", "cibil")
    with pytest.raises(ConfigurationError):
        get_provider()


def test_bureau_error_types():
    assert issubclass(BureauTimeout, BureauError)


# ==========================================================================
# tools -- closed allowlist; existing capabilities through the MCP runtime
# ==========================================================================

def test_tool_outside_the_allowlist_is_refused():
    for name in ("application.create", "document.mark_for_reupload", "shell", "bureau.pull_all"):
        with pytest.raises(tools.ToolNotAllowed):
            asyncio.run(tools.execute(name, ctx()))


def test_allowlist_is_read_only_and_exact():
    assert set(tools.TOOLS) == {
        "applicant.get", "application.get", "documents.get", "eligibility.get",
        "kyc.get", "income.get", "bank_behaviour.get", "risk.get", "bureau.get",
        "financial_documents.get"}
    assert not any(n.endswith((".create", ".update", ".delete")) for n in tools.TOOLS)


def test_application_get_runs_through_the_mcp_runtime(store):
    seed(store, declared_monthly_income="65000")
    from app.mcp import runtime

    real = runtime.call
    seen = []

    async def spy(name, **kwargs):
        seen.append(name)
        return await real(name, **kwargs)

    with patch.object(runtime, "call", spy):
        observation, call = asyncio.run(tools.execute("application.get", ctx()))
    assert seen == ["application.get"]
    assert observation.quality is Quality.PRESENT and call.status == "OK"
    assert observation.data["declared_monthly_income"] == "65000"
    assert any(e.field == "declared_monthly_income" for e in observation.evidence)


def test_eligibility_not_recorded_is_missing(store):
    seed(store)
    observation, _ = asyncio.run(tools.execute("eligibility.get", ctx()))
    assert observation.quality is Quality.MISSING


def test_applicant_get_carries_no_identifiers(store):
    seed(store)
    observation, _ = asyncio.run(tools.execute("applicant.get", ctx(), party_id=APP))
    assert observation.data == {"exists": True}


def test_a_failing_tool_becomes_an_unavailable_observation(store):
    with patch("app.agents.credit.adapters.case_memory.income_get",
               side_effect=RuntimeError("store down")):
        observation, call = asyncio.run(tools.execute("income.get", ctx()))
    assert observation.quality is Quality.UNAVAILABLE and call.status == "ERROR"


# ==========================================================================
# policy + fixture files
# ==========================================================================

def test_every_underwriting_rule_is_demo_unconfirmed():
    import yaml

    policy = yaml.safe_load(credit_config.policy_path().read_text(encoding="utf-8"))
    assert policy["signed_off"] is False
    assert policy["confirmation_status"] == "DEMO_UNCONFIRMED"
    assert policy["rules"]
    assert {r["confirmation_status"] for r in policy["rules"]} == {"DEMO_UNCONFIRMED"}
    # No rule may read a bounce count's ABSENCE as a clean record.
    assert not any(r["inputs"] == ["bank.returned_transaction_count"]
                   and r["operator"] == "eq" for r in policy["rules"])
    # Every evidence-plan source is allowlisted.
    for step in policy["evidence_plan"]:
        for source in step["sources"]:
            assert source in tools.TOOLS, source


def test_context_forbids_unknown_fields():
    with pytest.raises(Exception):
        UnderwritingContext(case_id=CASE, applicant_id=APP, request_id="r",
                            parties=[Party(party_id=APP)], decision="APPROVED")
