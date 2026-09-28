"""
The Credit Underwriting Agent's agentic eval suite -- 15 demo cases.

ALL DATA IS SYNTHETIC AND MARKED DEMO. Each case writes a synthetic case into
a temporary store through the SAME ingest path the LOS pipeline uses (no row
is forged behind the interface), assigns a DEMO bureau profile, runs the agent
through the common harness, and grades the AgentRun with the COMMON eval
runner: generic dimensions (tool selection, forbidden / unnecessary /
duplicate tools, bounded execution, retries, provenance, PII, latency, output
contract) plus credit-specific output checks (assessment status, findings,
gaps, exceptions, no approval / rejection).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.agents.runtime.evals.contracts import EvalCase, Expectations

CASE, APP, CO = "CASE-EVAL", "APP-EVAL", "APP-EVAL-CO"
OFFICER = "uw-eval-officer"
SCOPE = "los.credit.underwrite"
#: Synthetic applicant name: must never appear in any output, trajectory or audit.
SYNTHETIC_NAME = "Evaluation Applicant"

#: Never on the credit agent's path, whatever the case.
FORBIDDEN = ("application.create", "application.update", "applicant.create",
             "applicant.update", "document.mark_for_reupload", "documents.verification")


# ==========================================================================
# synthetic case building (through the ingest path)
# ==========================================================================

KYC = {"kyc": {"status": "PASS", "overall_score": 95,
               "fields": [{"field": "name", "status": "MATCH", "match_score": 100}]}}
INCOME = {"income_consistency": {
    "status": "PASS", "reason_codes": [],
    "bank_statement": {"type": "SALARY_CREDIT", "estimated_monthly_amount": 64000},
    "salary_slip": {"figure": "NET_PAY", "amount": 65000}}}
ELIGIBILITY = {"eligibility": {"status": "PASS", "reason_codes": [], "foir": "0.35"}}
RISK = {"risk": {"risk_category": "LOW", "risk_score": 12, "final_outcome": "PASS",
                 "flags": []}}


def bank_doc(extra_signals: dict | None = None, *, income_evidence: bool = True) -> dict:
    fields: dict[str, Any] = {"evidence": {"reconciled": True, "transaction_count": 20,
                                           **(extra_signals or {})}}
    if income_evidence:
        fields["income_evidence"] = {"type": "SALARY_CREDIT", "estimated_monthly_amount": 64000,
                                     "recurring_credit_count": 3, "confidence": 0.9}
    return {"source_id": "bank.pdf", "type": "BANK_STATEMENT", "verification": "PASS",
            "party_id": APP, "extraction": {"fields": fields}}


PAN_DOC = {"source_id": "pan.jpg", "type": "PAN", "verification": "PASS", "party_id": APP}
ITR_DOC = {"source_id": "itr.pdf", "type": "ITR", "verification": "PASS", "party_id": APP,
           "extraction": {"fields": {"signals": {"declared_annual_income": "960000"}}}}


@dataclass
class Setup:
    employment: str = "SALARIED"
    co: bool = False
    declared_income: str | None = "65000"
    declared_obligations: str | None = "0"
    result: dict | None = None
    bureau: dict | None = None


def _rename(value: Any, ids: dict[str, str]) -> Any:
    """The same synthetic case under other ids (several cases in one store)."""
    if isinstance(value, str):
        return ids.get(value, value)
    if isinstance(value, dict):
        return {_rename(k, ids): _rename(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [_rename(v, ids) for v in value]
    return value


def build(repository: Any, setup: Setup, *, case_id: str = CASE, app_id: str = APP,
          co_id: str = CO, stage: str | None = "CREDIT", onboard_applicant: bool = True,
          officer: str = OFFICER) -> None:
    from app.agents.credit.bureau import demo as demo_bureau
    from app.store.ingest import persist_los_result
    from app.store.models import (Applicant, Application, CaseEvent, CaseStage,
                                  StageTransition)

    ids = {APP: app_id, CO: co_id, CASE: case_id}
    if onboard_applicant:
        repository.save_applicant(Applicant(applicant_id=app_id, full_name=SYNTHETIC_NAME))
    if setup.co:
        repository.save_applicant(Applicant(applicant_id=co_id,
                                            full_name=SYNTHETIC_NAME + " Co"))
    repository.save_application(Application(
        case_id=case_id, applicant_id=app_id, product="PERSONAL_LOAN", loan_amount="500000",
        employment_type=setup.employment, co_applicant_id=co_id if setup.co else None,
        declared_monthly_income=setup.declared_income,
        declared_monthly_obligations=setup.declared_obligations))
    repository.grant_access(officer, "APPLICANT", app_id)
    if stage:
        repository.apply_stage_transition(
            0, CaseStage(case_id=case_id, stage=stage, stage_status="IN_PROGRESS", version=1),
            StageTransition(transition_id=f"STG-{case_id}", case_id=case_id, version=1,
                            kind="STAGE_ENTERED", to_stage=stage, to_status="IN_PROGRESS"),
            CaseEvent(event_id=f"EV-{case_id}", case_id=case_id, event_type="STAGE_ENTERED",
                      stage=stage))
    if setup.result:
        persist_los_result({"applicant_id": app_id, "case_id": case_id,
                            **_rename(setup.result, ids)})
    for party, profile in _rename(setup.bureau or {}, ids).items():
        demo_bureau.assign(party, profile)


# ==========================================================================
# credit-specific output checks (the runner only calls them)
# ==========================================================================

def _findings(output: dict) -> list[dict]:
    return (output.get("assessment") or {}).get("findings") or []


def status_is(expected: str) -> Callable[[dict], list[str]]:
    def check(output: dict) -> list[str]:
        got = output.get("status")
        return [] if got == expected else [f"assessment status {got} != {expected}"]
    return check


def has_finding(rule: str, status: str | None = None, party: str | None = None
                ) -> Callable[[dict], list[str]]:
    def check(output: dict) -> list[str]:
        for f in _findings(output):
            if f.get("policy_rule_id") != rule:
                continue
            if status and f.get("status") != status:
                continue
            if party and (f.get("subject") or {}).get("party_id") != party:
                continue
            if not f.get("evidence_refs"):
                return [f"{rule} has no evidence"]
            return []
        return [f"missing finding {rule}" + (f" {status}" if status else "")
                + (f" @{party}" if party else "")]
    return check


def no_finding(rule: str) -> Callable[[dict], list[str]]:
    def check(output: dict) -> list[str]:
        return [f"unexpected finding {rule}"] if any(
            f.get("policy_rule_id") == rule for f in _findings(output)) else []
    return check


def gap_mentions(text: str) -> Callable[[dict], list[str]]:
    def check(output: dict) -> list[str]:
        gaps = (output.get("assessment") or {}).get("data_gaps") or []
        return [] if any(text in g for g in gaps) else [f"no data gap mentions {text!r}"]
    return check


def exception_starts(prefix: str) -> Callable[[dict], list[str]]:
    def check(output: dict) -> list[str]:
        exc = (output.get("assessment") or {}).get("exceptions") or []
        return [] if any(e.startswith(prefix) for e in exc) else [f"no exception {prefix}"]
    return check


def income_resolved_by(tool: str) -> Callable[[dict], list[str]]:
    def check(output: dict) -> list[str]:
        for step in (output.get("trajectory") or {}).get("plan") or []:
            if step["step_id"] == "income":
                return [] if step["resolved_by"] == tool else \
                    [f"income resolved by {step['resolved_by']}, expected {tool}"]
        return ["no income step"]
    return check


def demo_labelled(output: dict) -> list[str]:
    demo = output.get("demo") or {}
    return [] if demo.get("is_demo") and "NON_PRODUCTION" in demo.get("labels", []) \
        else ["demo / non-production labels missing"]


def every_finding_grounded(output: dict) -> list[str]:
    from app.agents.credit.agent import credit_contract

    return list(credit_contract(output))


# ==========================================================================
# the cases
# ==========================================================================

def _expect(*checks, required=(), unnecessary=(), exact=None, max_calls=10, retries=0,
            **kw) -> Expectations:
    return Expectations(
        statuses=("SUCCEEDED",), required_tools=tuple(required),
        forbidden_tools=FORBIDDEN, unnecessary_tools=tuple(unnecessary),
        exact_tools=exact, no_duplicate_tools=True, max_tool_calls=max_calls,
        max_retries=retries, max_model_calls=1, max_latency_ms=5000,
        events=("AUTHORIZED", "PLANNED", "EVALUATED", "ASSESSED", "MEMO", "RUN_FINISHED"),
        forbidden_output_words=("APPROVED", "REJECTED", "SANCTIONED", "DECLINED"),
        pii_absent=(SYNTHETIC_NAME,), provenance_complete=True,
        output_checks=(demo_labelled, every_finding_grounded, *checks), **kw)


FULL = {"documents": [bank_doc(), PAN_DOC], **KYC, **INCOME, **ELIGIBILITY, **RISK}
CLEAN_TOOLS = ("application.get", "documents.get", "kyc.get", "income.get",
               "bank_behaviour.get", "eligibility.get", "risk.get", ("bureau.get", APP))

CASES: list[tuple[EvalCase, Setup]] = [
    (EvalCase("clean_salaried", "credit_agent", "Clean salaried applicant, all evidence",
              _expect(status_is("READY_FOR_DECISION"), has_finding("UW_BUR_SCORE_STRONG"),
                      has_finding("UW_REP_CLEAN"), has_finding("UW_REC_ELIGIBILITY_PASS"),
                      income_resolved_by("income.get"), exact=CLEAN_TOOLS,
                      unnecessary=("financial_documents.get", "applicant.get"))),
     Setup(result=FULL, bureau={APP: "DEMO-BUREAU-CLEAN"})),
    (EvalCase("adverse_bureau", "credit_agent", "Settlement, 60 DPD, low score, enquiries",
              _expect(status_is("REVIEW_REQUIRED"),
                      has_finding("UW_ADV_SETTLEMENT", "NEGATIVE", APP),
                      has_finding("UW_REP_DPD_30", "NEGATIVE"),
                      has_finding("UW_BUR_SCORE_LOW", "REVIEW"), no_finding("UW_REP_CLEAN"),
                      exact=CLEAN_TOOLS)),
     Setup(result=FULL, bureau={APP: "DEMO-BUREAU-ADVERSE"})),
    (EvalCase("weak_repayment", "credit_agent", "Minor repayment delays (1-29 DPD)",
              _expect(status_is("REVIEW_REQUIRED"), has_finding("UW_REP_DPD_MINOR", "REVIEW"),
                      no_finding("UW_REP_DPD_30"))),
     Setup(result=FULL, bureau={APP: "DEMO-BUREAU-WEAK-REPAYMENT"})),
    (EvalCase("banking_anomaly", "credit_agent", "Returned transactions on the statement",
              _expect(status_is("REVIEW_REQUIRED"), has_finding("UW_BNK_RETURNS", "REVIEW"))),
     Setup(result={**FULL, "documents": [bank_doc({"returned_transaction_count": 3}), PAN_DOC]},
           bureau={APP: "DEMO-BUREAU-CLEAN"})),
    (EvalCase("income_discrepancy", "credit_agent",
              "Recorded income-consistency check did not pass",
              _expect(status_is("REVIEW_REQUIRED"),
                      has_finding("UW_INC_RECORDED_REVIEW", "REVIEW"),
                      no_finding("UW_INC_RECORDED_PASS"))),
     Setup(result={**FULL, "income_consistency": {**INCOME["income_consistency"],
                                                  "status": "REVIEW",
                                                  "reason_codes": ["INCOME_MISMATCH"]}},
           bureau={APP: "DEMO-BUREAU-CLEAN"})),
    (EvalCase("missing_bank_statement", "credit_agent",
              "No bank statement: banking is an explicit optional gap",
              _expect(status_is("READY_FOR_DECISION"), gap_mentions("banking"),
                      income_resolved_by("income.get"), no_finding("UW_BNK_RETURNS"))),
     Setup(result={**FULL, "documents": [PAN_DOC]}, bureau={APP: "DEMO-BUREAU-CLEAN"})),
    (EvalCase("missing_salary_slip", "credit_agent",
              "No slip / consistency check: income from recorded bank evidence (replan)",
              _expect(status_is("READY_FOR_DECISION"), income_resolved_by("bank_behaviour.get"),
                      no_finding("UW_INC_DECLARED_VS_RECORDED"),
                      required=("income.get", "bank_behaviour.get"),
                      unnecessary=("financial_documents.get",))),
     Setup(result={"documents": [bank_doc(), PAN_DOC], **KYC, **ELIGIBILITY, **RISK},
           bureau={APP: "DEMO-BUREAU-CLEAN"})),
    (EvalCase("itr_only_self_employed", "credit_agent",
              "Self-employed, ITR only: income from the VERIFIED ITR's recorded figure",
              _expect(status_is("READY_FOR_DECISION"),
                      income_resolved_by("financial_documents.get"),
                      required=("bank_behaviour.get", "financial_documents.get"),
                      unnecessary=("income.get",))),
     Setup(employment="SELF_EMPLOYED", declared_income=None,
           result={"documents": [ITR_DOC, PAN_DOC], **KYC, **ELIGIBILITY, **RISK},
           bureau={APP: "DEMO-BUREAU-CLEAN"})),
    (EvalCase("missing_bureau", "credit_agent", "No bureau record for the applicant",
              _expect(status_is("DATA_INSUFFICIENT"), gap_mentions("NO_BUREAU_RECORD"),
                      no_finding("UW_BUR_SCORE_STRONG"))),
     Setup(result=FULL, bureau={})),
    (EvalCase("bureau_timeout", "credit_agent", "Bureau times out: one retry, then a gap",
              _expect(status_is("DATA_INSUFFICIENT"),
                      exception_starts("TOOL_UNAVAILABLE:bureau.get"),
                      gap_mentions("BUREAU_TIMEOUT"), retries=1)),
     Setup(result=FULL, bureau={APP: "DEMO-BUREAU-TIMEOUT"})),
    (EvalCase("co_applicant", "credit_agent", "Co-applicant with a thin credit history",
              _expect(status_is("REVIEW_REQUIRED"),
                      has_finding("UW_BUR_THIN_HISTORY", "REVIEW", CO),
                      has_finding("UW_BUR_SCORE_STRONG", "POSITIVE", APP),
                      required=(("bureau.get", APP), ("bureau.get", CO)))),
     Setup(co=True, result={**FULL,
                            "primary_applicant": {"party_id": APP, "kyc": KYC["kyc"]},
                            "co_applicant": {"party_id": CO, "kyc": KYC["kyc"]}},
           bureau={APP: "DEMO-BUREAU-CLEAN", CO: "DEMO-BUREAU-THIN"})),
    (EvalCase("contradictory_evidence", "credit_agent",
              "Recorded income check PASS, but declared income differs from the slip",
              _expect(status_is("REVIEW_REQUIRED"), has_finding("UW_INC_RECORDED_PASS"),
                      has_finding("UW_INC_DECLARED_VS_RECORDED", "REVIEW"),
                      exception_starts("CONTRADICTION:INCOME_CONSISTENCY"))),
     Setup(declared_income="90000", result=FULL, bureau={APP: "DEMO-BUREAU-CLEAN"})),
    (EvalCase("insufficient_evidence", "credit_agent", "Nothing recorded yet",
              _expect(status_is("DATA_INSUFFICIENT"), gap_mentions("kyc"),
                      gap_mentions("income"), gap_mentions("eligibility"))),
     Setup(result=None, bureau={})),
    (EvalCase("all_evidence_available", "credit_agent",
              "Two parties, every source recorded, both bureau reports clean",
              _expect(status_is("READY_FOR_DECISION"),
                      has_finding("UW_BUR_SCORE_STRONG", "POSITIVE", CO),
                      has_finding("UW_REC_ELIGIBILITY_PASS"),
                      required=(("bureau.get", APP), ("bureau.get", CO)))),
     Setup(co=True, result={**FULL, "documents": [bank_doc(), PAN_DOC, ITR_DOC],
                            "primary_applicant": {"party_id": APP, "kyc": KYC["kyc"]},
                            "co_applicant": {"party_id": CO, "kyc": KYC["kyc"]}},
           bureau={APP: "DEMO-BUREAU-CLEAN", CO: "DEMO-BUREAU-CLEAN"})),
    (EvalCase("provider_unavailable", "credit_agent",
              "Bureau provider error: not retried, reported, never guessed",
              _expect(status_is("DATA_INSUFFICIENT"),
                      exception_starts("TOOL_UNAVAILABLE:bureau.get"),
                      gap_mentions("BUREAU_ERROR"), retries=0)),
     Setup(result=FULL, bureau={APP: "DEMO-BUREAU-ERROR"})),
]


async def execute_in_process(case: EvalCase, setup: Setup, repository: Any) -> Any:
    """Build the synthetic case in `repository`, then run the agent on the harness."""
    from app.agents.applicant.permissions import Caller
    from app.agents.credit import agent

    from app.agents.credit.bureau import demo as demo_bureau

    demo_bureau.clear_assignments()
    build(repository, setup)
    who = Caller(subject=OFFICER, scopes=frozenset({SCOPE}), roles=frozenset({"credit"}))
    return await agent.underwrite(CASE, caller=who, request_id=f"eval-{case.case_id}",
                                  correlation_id=f"eval-{case.case_id}")


__all__ = ["APP", "CASE", "CASES", "CO", "OFFICER", "SCOPE", "SYNTHETIC_NAME", "Setup",
           "build", "execute_in_process"]
