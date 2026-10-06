"""
Credit Underwriting Agent -- Slice 2: deterministic planner + bounded LangGraph.

Real SQLite repository; findings written through the LOS ingest path; tools
run through the real MCP runtime. Only the bureau is the demo provider.
"""

from __future__ import annotations

import asyncio
import copy
from unittest.mock import patch

import pytest

from app.agents.applicant.permissions import Caller
from app.agents.credit import config as credit_config
from app.agents.credit import graph, planner
from app.agents.credit import policy as credit_policy
from app.agents.credit.bureau import demo as demo_bureau
from app.agents.credit.graph import RunStatus, StopReason
from app.agents.credit.planner import StepStatus
from app.agents.credit.schemas import Party, UnderwritingContext
from app.store import set_repository
from app.store.models import Applicant, Application, CaseEvent, CaseStage, StageTransition
from app.store.testing import fresh_repository

CASE, APP, CO = "CASE-CR2", "APP-CR2", "APP-CR2-CO"
OFFICER = "uw-officer"
SCOPE = "los.credit.underwrite"


# ==========================================================================
# fixtures
# ==========================================================================

@pytest.fixture(autouse=True)
def store(tmp_path):
    repository = fresh_repository(tmp_path / "credit2.sqlite3")
    repository.initialise()
    set_repository(repository)
    demo_bureau.clear_assignments()
    credit_policy.reload()
    graph.reset()
    # The memo's optional Qwen call is off here: these tests grade the agent,
    # not a live model (the memo tests enable it with a stand-in generator).
    with patch("app.agents.los.config.case_memory_enabled", return_value=True), \
            patch.dict("os.environ", {"CREDIT_MEMO_LLM_ENABLED": "false",
                                      "AGENT_RUN_AUDIT_ENABLED": "false"}):
        yield repository
    set_repository(None)
    demo_bureau.clear_assignments()


def caller(scopes=(SCOPE,), subject=OFFICER) -> Caller:
    return Caller(subject=subject, scopes=frozenset(scopes), roles=frozenset({"credit"}))


def set_stage(repository, stage="CREDIT", case_id=CASE):
    repository.apply_stage_transition(
        0, CaseStage(case_id=case_id, stage=stage, stage_status="IN_PROGRESS", version=1),
        StageTransition(transition_id=f"STG-{case_id}-{stage}", case_id=case_id, version=1,
                        kind="STAGE_ENTERED", to_stage=stage, to_status="IN_PROGRESS"),
        CaseEvent(event_id=f"EV-{case_id}-{stage}", case_id=case_id,
                  event_type="STAGE_ENTERED", stage=stage))


def seed(repository, *, employment="SALARIED", co=False, stage="CREDIT", **application):
    repository.save_applicant(Applicant(applicant_id=APP, full_name="Test Applicant"))
    if co:
        repository.save_applicant(Applicant(applicant_id=CO, full_name="Co Applicant"))
    repository.save_application(Application(
        case_id=CASE, applicant_id=APP, product="PERSONAL_LOAN", loan_amount="500000",
        employment_type=employment, co_applicant_id=CO if co else None,
        declared_monthly_income="65000", **application))
    repository.grant_access(OFFICER, "APPLICANT", APP)
    if stage:
        set_stage(repository, stage)


def persist(result: dict):
    from app.store.ingest import persist_los_result

    persist_los_result({"applicant_id": APP, "case_id": CASE, **result})


KYC = {"kyc": {"status": "PASS", "overall_score": 95,
               "fields": [{"field": "name", "status": "MATCH", "match_score": 100}]}}
INCOME = {"income_consistency": {
    "status": "PASS", "reason_codes": [],
    "bank_statement": {"type": "SALARY_CREDIT", "estimated_monthly_amount": 64000},
    "salary_slip": {"figure": "NET_PAY", "amount": 65000}}}
ELIGIBILITY = {"eligibility": {"status": "PASS", "reason_codes": [], "foir": "0.35",
                               "policy": {"id": "ELIG_DEMO"}}}
RISK = {"risk": {"risk_category": "LOW", "risk_score": 12, "final_outcome": "PASS",
                 "flags": []}}


def bank_doc(*, income_evidence=True, verification="PASS", party_id=APP, source="bank.pdf"):
    fields = {"evidence": {"reconciled": True, "transaction_count": 20}}
    if income_evidence:
        fields["income_evidence"] = {"type": "SALARY_CREDIT", "estimated_monthly_amount": 64000,
                                     "recurring_credit_count": 3, "confidence": 0.9}
    return {"source_id": source, "type": "BANK_STATEMENT", "verification": verification,
            "party_id": party_id, "extraction": {"fields": fields}}


def full_evidence(repository, *, co=False):
    documents = [bank_doc(), {"source_id": "pan.jpg", "type": "PAN", "verification": "PASS",
                              "party_id": APP}]
    persist({"documents": documents, **KYC, **INCOME, **ELIGIBILITY, **RISK})
    demo_bureau.assign(APP, "DEMO-BUREAU-CLEAN")
    if co:
        demo_bureau.assign(CO, "DEMO-BUREAU-THIN")


def run(who=None, **kwargs):
    return asyncio.run(graph.run(CASE, caller=who if who is not None else caller(),
                                 request_id="req-cr2", **kwargs))


def tools_called(state):
    return [(t["tool"], t.get("party_id")) for t in state["tool_trace"]]


def step(state, step_id):
    return next(s for s in state["plan"] if s.step_id == step_id)


def events(state, name):
    return [e for e in state["trajectory"] if e["event"] == name]


def ctx(employment="SALARIED", parties=(APP,), product="PERSONAL_LOAN"):
    return UnderwritingContext(
        case_id=CASE, applicant_id=APP, request_id="r", employment_type=employment,
        product=product,
        parties=[Party(party_id=p, role="PRIMARY_APPLICANT" if i == 0 else "CO_APPLICANT")
                 for i, p in enumerate(parties)])


def policy_with(evidence_plan, **extra):
    base = copy.deepcopy(credit_policy.get_policy())
    base["evidence_plan"] = evidence_plan
    base.update(extra)
    return base


# ==========================================================================
# planner -- deterministic, policy-driven, allowlisted
# ==========================================================================

def test_salaried_plan_follows_policy_order_and_sources():
    steps, forbidden = planner.build_plan(ctx("SALARIED"), credit_policy.get_policy())
    assert forbidden == []
    assert [s.step_id for s in steps] == [
        "application", "documents", "kyc", "income", "banking", "eligibility", "risk",
        f"bureau:{APP}"]
    income = next(s for s in steps if s.category == "income")
    assert income.sources == ["income.get", "bank_behaviour.get", "financial_documents.get"]
    assert income.reason == "policy evidence_plan"
    assert {s.step_id for s in steps if s.required} == {
        "application", "kyc", "income", "eligibility", f"bureau:{APP}"}


def test_self_employed_plan_prefers_bank_income_evidence():
    steps, _ = planner.build_plan(ctx("SELF_EMPLOYED"), credit_policy.get_policy())
    income = next(s for s in steps if s.category == "income")
    assert income.sources == ["bank_behaviour.get", "financial_documents.get", "income.get"]
    assert income.reason == "by_employment[SELF_EMPLOYED]"


def test_product_override_is_more_specific_than_employment():
    policy = copy.deepcopy(credit_policy.get_policy())
    policy["by_product"] = {"PERSONAL_LOAN": {"income": ["income.get"]}}
    steps, _ = planner.build_plan(ctx("SELF_EMPLOYED"), policy)
    income = next(s for s in steps if s.category == "income")
    assert income.sources == ["income.get"] and income.reason == "by_product[PERSONAL_LOAN]"


def test_co_applicant_expands_party_scoped_steps():
    steps, _ = planner.build_plan(ctx(parties=(APP, CO)), credit_policy.get_policy())
    bureau = [s for s in steps if s.category == "bureau"]
    assert [(s.party_id, s.party_role) for s in bureau] == [
        (APP, "PRIMARY_APPLICANT"), (CO, "CO_APPLICANT")]
    # case-scoped steps are not duplicated per party
    assert len([s for s in steps if s.category == "income"]) == 1


def test_plan_is_deterministic():
    a, _ = planner.build_plan(ctx(parties=(APP, CO)), credit_policy.get_policy())
    b, _ = planner.build_plan(ctx(parties=(APP, CO)), credit_policy.get_policy())
    assert [s.model_dump() for s in a] == [s.model_dump() for s in b]


def test_planner_drops_forbidden_sources_and_records_them():
    policy = policy_with([
        {"category": "application", "required": True, "scope": "case",
         "sources": ["application.get"]},
        {"category": "income", "required": True, "scope": "case",
         "sources": ["shell.exec", "income.get", "application.create"]},
        {"category": "exfil", "required": False, "scope": "case",
         "sources": ["http.post"]}])
    steps, forbidden = planner.build_plan(ctx(), policy)
    assert {f["tool"] for f in forbidden} == {"shell.exec", "application.create", "http.post"}
    income = next(s for s in steps if s.category == "income")
    assert income.sources == ["income.get"]
    exfil = next(s for s in steps if s.category == "exfil")
    assert exfil.sources == [] and exfil.status == StepStatus.UNRESOLVED
    assert exfil.reason == "NO_ALLOWED_SOURCE"


# ==========================================================================
# the graph -- stops
# ==========================================================================

def test_sufficient_evidence_stops_with_every_required_step_resolved(store):
    seed(store)
    full_evidence(store)
    state = run()
    assert state["status"] == RunStatus.EVIDENCE_COMPLETE
    assert state["stop_reason"] == StopReason.ALL_STEPS_SETTLED
    assert all(s.status == StepStatus.RESOLVED for s in state["plan"])
    assert tools_called(state) == [
        ("application.get", None), ("documents.get", None), ("kyc.get", None),
        ("income.get", None), ("bank_behaviour.get", None), ("eligibility.get", None),
        ("risk.get", None), ("bureau.get", APP)]
    assert state["steps"] == 8 and state["replans"] == 0
    assert state["errors"] == [] and state["forbidden_attempts"] == []
    assert events(state, "FINAL")[-1]["status"] == RunStatus.EVIDENCE_COMPLETE


def test_insufficient_evidence_stops_incomplete_and_names_the_gaps(store):
    seed(store)                       # nothing recorded, no bureau record
    state = run()
    assert state["status"] == RunStatus.EVIDENCE_INCOMPLETE
    assert state["stop_reason"] == StopReason.ALL_STEPS_SETTLED
    final = events(state, "FINAL")[-1]
    assert set(final["unresolved_required"]) == {"kyc", "income", "eligibility",
                                                 f"bureau:{APP}"}
    assert step(state, "application").status == StepStatus.RESOLVED


def test_missing_source_without_alternative_settles_unresolved(store):
    seed(store)
    full_evidence(store)
    store_findings = [f for f in store.get_current_findings(CASE)
                      if f.source_type == "ELIGIBILITY"]
    assert store_findings            # recorded ...
    with patch("app.agents.applicant.case_memory_facts.case_memory",
               return_value={"findings": [], "decisions": [], "timeline": []}):
        state = run()                # ... but not readable: eligibility.get says not recorded
    eligibility = step(state, "eligibility")
    assert eligibility.status == StepStatus.UNRESOLVED
    assert eligibility.reason.startswith("NO_ALTERNATIVE_SOURCE (MISSING)")
    assert state["status"] == RunStatus.EVIDENCE_INCOMPLETE


def test_alternative_source_is_tried_when_the_first_does_not_answer(store):
    seed(store)
    persist({"documents": [bank_doc()], **KYC, **ELIGIBILITY})   # no income consistency
    demo_bureau.assign(APP, "DEMO-BUREAU-CLEAN")
    state = run()
    income = step(state, "income")
    assert income.status == StepStatus.RESOLVED
    assert income.resolved_by == "bank_behaviour.get"
    assert income.attempted == ["income.get", "bank_behaviour.get"]
    assert state["replans"] == 1
    replan = events(state, "REPLAN")
    assert len(replan) == 1 and "income.get returned MISSING" in replan[0]["reason"]
    # banking needs bank_behaviour.get too: REUSED, not executed again
    assert tools_called(state).count(("bank_behaviour.get", None)) == 1
    assert any(e["tool"] == "bank_behaviour.get" for e in events(state, "REUSE"))
    assert step(state, "banking").status == StepStatus.RESOLVED


def test_self_employed_run_reads_bank_income_first(store):
    seed(store, employment="SELF_EMPLOYED")
    full_evidence(store)
    state = run()
    income = step(state, "income")
    assert income.resolved_by == "bank_behaviour.get" and state["replans"] == 0
    assert tools_called(state).index(("bank_behaviour.get", None)) < \
        [t for t, _ in tools_called(state)].index("eligibility.get")
    # income.get was never needed
    assert ("income.get", None) not in tools_called(state)


def test_bank_statement_without_income_evidence_does_not_answer_income(store):
    seed(store)
    persist({"documents": [bank_doc(income_evidence=False)], **KYC, **ELIGIBILITY})
    demo_bureau.assign(APP, "DEMO-BUREAU-CLEAN")
    state = run()
    income = step(state, "income")
    assert income.status == StepStatus.UNRESOLVED
    # every allowed income source was tried; none answered
    assert income.attempted == ["income.get", "bank_behaviour.get", "financial_documents.get"]
    # banking itself still resolves -- signals are present, income evidence is not
    assert step(state, "banking").status == StepStatus.RESOLVED


def test_co_applicant_bureau_runs_once_per_party(store):
    seed(store, co=True)
    full_evidence(store, co=True)
    state = run()
    bureau_calls = [c for c in tools_called(state) if c[0] == "bureau.get"]
    assert bureau_calls == [("bureau.get", APP), ("bureau.get", CO)]
    assert step(state, f"bureau:{CO}").status == StepStatus.RESOLVED
    assert state["context"].parties[1].role == "CO_APPLICANT"


def test_co_applicant_without_a_bureau_record_is_an_explicit_gap(store):
    seed(store, co=True)
    full_evidence(store)                       # CO has no demo profile
    state = run()
    co_step = step(state, f"bureau:{CO}")
    assert co_step.status == StepStatus.UNRESOLVED
    assert state["status"] == RunStatus.EVIDENCE_INCOMPLETE
    obs = state["observations"][co_step.observation_index]
    assert obs.error == "NO_BUREAU_RECORD"


# ==========================================================================
# failures, retries, bounds
# ==========================================================================

def test_tool_failure_becomes_an_unavailable_observation_and_replans(store):
    seed(store)
    full_evidence(store)
    with patch("app.agents.credit.adapters.case_memory.income_get",
               side_effect=RuntimeError("store down")):
        state = run()
    income = step(state, "income")
    assert income.resolved_by == "bank_behaviour.get"
    assert {"tool": "income.get", "party_id": None, "error": "RuntimeError"} in state["errors"]
    assert "income.get returned UNAVAILABLE" in events(state, "REPLAN")[0]["reason"]


def test_bureau_timeout_is_retried_once_then_unresolved(store):
    seed(store)
    full_evidence(store)
    demo_bureau.assign(APP, "DEMO-BUREAU-TIMEOUT")
    state = run()
    trace = next(t for t in state["tool_trace"] if t["tool"] == "bureau.get")
    assert trace["attempts"] == 2 and trace["status"] == "UNAVAILABLE"
    assert tools_called(state).count(("bureau.get", APP)) == 1     # one execution, 2 attempts
    assert step(state, f"bureau:{APP}").status == StepStatus.UNRESOLVED
    assert {"tool": "bureau.get", "party_id": APP, "error": "BUREAU_TIMEOUT"} in state["errors"]
    assert state["status"] == RunStatus.EVIDENCE_INCOMPLETE


def test_bureau_provider_error_is_not_retried(store):
    seed(store)
    full_evidence(store)
    demo_bureau.assign(APP, "DEMO-BUREAU-ERROR")
    state = run()
    trace = next(t for t in state["tool_trace"] if t["tool"] == "bureau.get")
    assert trace["attempts"] == 1 and trace["error"] == "BUREAU_ERROR"


def test_max_tool_calls_stops_the_run(store):
    seed(store)
    full_evidence(store)
    with patch.object(credit_config, "max_tool_calls", return_value=4):
        state = run()
    assert state["steps"] == 4
    assert state["stop_reason"] == StopReason.MAX_TOOL_CALLS
    skipped = [s.step_id for s in state["plan"] if s.status == StepStatus.SKIPPED_BUDGET]
    assert skipped == ["banking", "eligibility", "risk", f"bureau:{APP}"]
    assert state["status"] == RunStatus.EVIDENCE_INCOMPLETE


def test_default_budget_is_ten_and_never_exceeded(store):
    seed(store, co=True)
    full_evidence(store, co=True)
    state = run()
    assert credit_config.max_tool_calls() == 10
    assert state["steps"] <= 10 and len(state["tool_trace"]) == state["steps"]


def test_max_replans_is_enforced(store):
    seed(store)                                   # nothing recorded anywhere
    policy = policy_with([
        {"category": "application", "required": True, "scope": "case",
         "sources": ["application.get"]},
        {"category": "a", "required": True, "scope": "case", "sources": ["kyc.get", "risk.get"]},
        {"category": "b", "required": True, "scope": "case",
         "sources": ["income.get", "eligibility.get"]},
        {"category": "c", "required": True, "scope": "case",
         "sources": ["documents.get", "bank_behaviour.get"]}])
    state = run(policy=policy)
    assert state["replans"] == 2
    assert [e["step_id"] for e in events(state, "REPLAN")] == ["a", "b"]
    c = step(state, "c")
    assert c.status == StepStatus.UNRESOLVED and c.reason == "REPLAN_BUDGET_EXHAUSTED"
    assert c.attempted == ["documents.get"]
    assert ("bank_behaviour.get", None) not in tools_called(state)


def test_no_tool_runs_twice_for_the_same_party(store):
    seed(store, co=True)
    persist({"documents": [bank_doc()], **KYC, **ELIGIBILITY})
    demo_bureau.assign(APP, "DEMO-BUREAU-CLEAN")
    demo_bureau.assign(CO, "DEMO-BUREAU-CLEAN")
    # a policy that asks for the same tools repeatedly
    policy = policy_with([
        {"category": "application", "required": True, "scope": "case",
         "sources": ["application.get"]},
        {"category": "income", "required": True, "scope": "case",
         "sources": ["income.get", "bank_behaviour.get"]},
        {"category": "banking", "required": False, "scope": "case",
         "sources": ["bank_behaviour.get"]},
        {"category": "bureau", "required": True, "scope": "party", "sources": ["bureau.get"]},
        {"category": "bureau_again", "required": False, "scope": "party",
         "sources": ["bureau.get"]}])
    state = run(policy=policy)
    calls = tools_called(state)
    assert len(calls) == len(set(calls))
    assert calls.count(("bureau.get", APP)) == 1 and calls.count(("bureau.get", CO)) == 1
    assert len(events(state, "REUSE")) == 3       # banking, bureau_again x2
    assert step(state, f"bureau_again:{CO}").status == StepStatus.RESOLVED


def test_an_expired_deadline_runs_no_tool_at_all(store):
    """The harness refuses even the first read once the deadline has passed."""
    from app.agents.runtime.limits import Budget

    seed(store)
    full_evidence(store)
    # Forced, not slept into: Windows' monotonic clock ticks ~15 ms.
    with patch.object(Budget, "expired", return_value=True):
        state = run()
    assert state["status"] == RunStatus.CONTEXT_UNAVAILABLE
    assert state["tool_trace"][0]["status"] == "NOT_RUN"
    assert state["observations"][0].error == "DEADLINE_EXCEEDED"
    assert state["run"].budget.tool_calls == 0


def test_deadline_mid_run_stops_the_loop(store):
    seed(store)
    full_evidence(store)
    real_plan = graph.plan

    async def plan_then_expire(state):
        state = await real_plan(state)
        state["run"].budget.started -= 10_000      # the deadline passes after planning
        return state

    with patch.object(graph, "plan", plan_then_expire), \
            patch.dict(graph._NODES, {"plan": plan_then_expire}):
        graph.reset()
        state = run()
    graph.reset()
    assert state["stop_reason"] == StopReason.DEADLINE
    assert state["status"] == RunStatus.EVIDENCE_INCOMPLETE
    assert [t["tool"] for t in state["tool_trace"]] == ["application.get"]


def test_loop_guard_is_a_hard_stop(store):
    seed(store)
    full_evidence(store)
    with patch.object(graph, "_loop_limit", return_value=2):
        state = run()
    assert state["stop_reason"] == StopReason.LOOP_GUARD


def test_forbidden_tool_is_never_executed(store):
    seed(store)
    full_evidence(store)
    from app.mcp import runtime

    seen = []
    real = runtime.call

    async def spy(name, **kwargs):
        seen.append(name)
        return await real(name, **kwargs)

    policy = policy_with([
        {"category": "application", "required": True, "scope": "case",
         "sources": ["application.get"]},
        {"category": "write", "required": True, "scope": "case",
         "sources": ["application.update", "document.mark_for_reupload"]},
        {"category": "kyc", "required": True, "scope": "case", "sources": ["kyc.get"]}])
    with patch.object(runtime, "call", spy):
        state = run(policy=policy)
    assert seen == ["application.get"]
    assert {f["tool"] for f in state["forbidden_attempts"]} == {
        "application.update", "document.mark_for_reupload"}
    assert len(events(state, "FORBIDDEN_TOOL")) == 2
    assert step(state, "write").status == StepStatus.UNRESOLVED
    assert state["status"] == RunStatus.EVIDENCE_INCOMPLETE


def test_execute_tool_refuses_a_non_allowlisted_tool_even_if_planned():
    state = graph.initial_state(CASE, caller=caller(), request_id="r")
    state["context"] = ctx()
    state["plan"] = [planner.PlanStep(step_id="x", category="x", required=True, scope="case",
                                      sources=["shell.exec"])]
    state["current"] = {"step_id": "x", "tool": "shell.exec", "party_id": None,
                        "reused": False, "reason": "test"}
    state = asyncio.run(graph.execute_tool(state))
    assert state["steps"] == 0 and state["tool_trace"] == []
    assert state["forbidden_attempts"][0]["stage"] == "execute"
    assert state["observations"][-1].error == "TOOL_NOT_ALLOWED"


# ==========================================================================
# ownership / security propagation
# ==========================================================================

def _no_tool_ran(state):
    return state["steps"] == 0 and state["tool_trace"] == [] and state["plan"] == []


def test_no_caller_is_refused_before_any_tool(store):
    seed(store)
    state = asyncio.run(graph.run(CASE, caller=None, request_id="r"))
    assert state["status"] == RunStatus.REFUSED and state["refusal"]["code"] == "CALLER_REQUIRED"
    assert _no_tool_ran(state)


def test_missing_underwriting_scope_is_refused(store):
    seed(store)
    state = run(caller(scopes=("los.read",)))
    assert state["refusal"]["code"] == "INSUFFICIENT_SCOPE" and _no_tool_ran(state)


def test_a_caller_who_does_not_own_the_case_is_refused(store):
    seed(store)
    state = run(caller(subject="someone-else"))
    assert state["status"] == RunStatus.REFUSED
    assert state["refusal"]["code"] == "CASE_NOT_ACCESSIBLE" and _no_tool_ran(state)


def test_an_unknown_case_is_refused(store):
    seed(store)
    state = asyncio.run(graph.run("CASE-NOPE", caller=caller(), request_id="r"))
    assert state["status"] == RunStatus.REFUSED and _no_tool_ran(state)


def test_wrong_stage_is_refused(store):
    seed(store, stage="CPA")
    state = run()
    assert state["refusal"]["code"] == "STAGE_NOT_ALLOWED" and _no_tool_ran(state)


def test_unresolved_stage_is_refused(store):
    seed(store, stage=None)
    with patch("app.agents.los.stages._from_case", return_value=None):
        state = run()
    assert state["refusal"]["code"] == "STAGE_NOT_ALLOWED" and _no_tool_ran(state)


def test_allowed_stages_are_configuration(store, monkeypatch):
    seed(store, stage="CPA")
    monkeypatch.setenv("CREDIT_ALLOWED_STAGES", "CREDIT,CPA")
    state = run()
    assert state["status"] != RunStatus.REFUSED


def test_the_callers_identity_reaches_every_mcp_call(store):
    seed(store)
    full_evidence(store)
    from app.mcp import runtime

    seen = []
    real = runtime.call

    async def spy(name, **kwargs):
        seen.append((name, kwargs["caller"], kwargs["case_id"], kwargs["request_id"]))
        return await real(name, **kwargs)

    who = caller()
    with patch.object(runtime, "call", spy):
        run(who)
    assert [s[0] for s in seen] == ["application.get", "documents.get", "eligibility.get"]
    assert all(s[1] is who and s[2] == CASE and s[3] == "req-cr2" for s in seen)


def test_the_caller_and_credential_never_enter_the_trajectory(store):
    seed(store)
    full_evidence(store)
    who = Caller(subject=OFFICER, scopes=frozenset({SCOPE}), roles=frozenset(),
                 credential="SECRET-TOKEN-VALUE")
    state = run(who)
    view = graph.trajectory_view(state)
    assert "SECRET-TOKEN-VALUE" not in repr(view)
    assert "SECRET-TOKEN-VALUE" not in repr(state["tool_trace"])


def test_a_service_principal_still_needs_the_underwriting_scope(store):
    from app.security import access

    seed(store)
    state = run(caller(scopes=(access.read_all_scope(),), subject="svc"))
    assert state["refusal"]["code"] == "INSUFFICIENT_SCOPE"


# ==========================================================================
# documents.get -- the recorded verdict, never presence
# ==========================================================================

def _docs(store, rows):
    from app.store.models import Document, status_for_verdict

    for doc_type, verdict in rows:
        store.save_document(Document(
            document_id=f"{CASE}:{doc_type}", case_id=CASE, applicant_id=APP,
            document_type=doc_type, status=status_for_verdict(verdict),
            verification_status=verdict, source_id=f"{doc_type.lower()}.jpg", party_id=APP))


def _documents_observation(store):
    from app.agents.credit import tools

    observation, _ = asyncio.run(tools.execute("documents.get", ctx(), caller=caller()))
    return observation


def test_documents_present_but_unverified_are_not_counted_as_verified(store):
    seed(store)
    _docs(store, [("PAN", None), ("SALARY_SLIP", None)])
    observation = _documents_observation(store)
    assert observation.quality.value == "LOW_CONFIDENCE"
    assert observation.data["verified_pass_count"] == 0
    assert observation.data["unverified_count"] == 2
    assert all(d["verification_recorded"] is False for d in observation.data["documents"])
    assert not planner.answers("documents", "documents.get", observation)


def test_documents_carry_the_recorded_verdict(store):
    seed(store)
    _docs(store, [("PAN", "PASS"), ("SALARY_SLIP", "REVIEW"), ("BANK_STATEMENT", None)])
    observation = _documents_observation(store)
    assert observation.quality.value == "PRESENT"
    assert observation.data["verified_pass_count"] == 1
    assert observation.data["not_passed_count"] == 1
    assert observation.data["unverified_count"] == 1
    summaries = {e.field: e.value_summary for e in observation.evidence}
    assert summaries["BANK_STATEMENT.verification_status"] == "NOT_RECORDED"
    assert summaries["SALARY_SLIP.verification_status"] == "REVIEW"


def test_no_documents_is_missing(store):
    seed(store)
    assert _documents_observation(store).quality.value == "MISSING"


def test_the_derived_workflow_status_is_not_read_as_a_verdict(store):
    from app.store.models import Document, DocumentStatus

    seed(store)
    store.save_document(Document(document_id=f"{CASE}:PAN", case_id=CASE, applicant_id=APP,
                                 document_type="PAN", status=DocumentStatus.VERIFIED,
                                 verification_status=None, party_id=APP))
    observation = _documents_observation(store)
    assert observation.data["verified_pass_count"] == 0
    assert observation.data["unverified_count"] == 1


# ==========================================================================
# boundaries and trajectory
# ==========================================================================

def test_the_graph_computes_nothing_it_does_not_own(store):
    seed(store)
    full_evidence(store)
    state = run()
    eligibility = state["observations"][step(state, "eligibility").observation_index]
    assert eligibility.data["foir"] == "0.35"           # echoed as recorded
    # Slice 3 adds `assessment` deliberately; a decision / approval never appears.
    for forbidden in ("emi", "ltv", "decision", "approved", "rejected"):
        assert forbidden not in {k.lower() for k in state}
    assert "APPROVE" not in repr(graph.trajectory_view(state)).upper()


def test_langgraph_and_sequential_paths_agree(store):
    seed(store, co=True)
    persist({"documents": [bank_doc()], **KYC, **ELIGIBILITY})
    demo_bureau.assign(APP, "DEMO-BUREAU-CLEAN")

    def strip(view):
        view = copy.deepcopy(view)
        for e in view["trajectory"]:
            e.pop("duration_ms", None)
        return view

    a = strip(graph.trajectory_view(run()))
    graph.reset()
    b = strip(graph.trajectory_view(run(use_langgraph=False)))
    assert a == b


def test_trajectory_explains_selection_observation_replan_and_stop(store):
    seed(store)
    persist({"documents": [bank_doc()], **KYC, **ELIGIBILITY})
    demo_bureau.assign(APP, "DEMO-BUREAU-CLEAN")
    view = graph.trajectory_view(run())
    kinds = [e["event"] for e in view["trajectory"]]
    assert kinds[0] == "AUTHORIZED" and kinds[-1] == "FINAL"
    for e in view["trajectory"]:
        if e["event"] == "SELECT":
            assert e["tool"] and e["reason"]
        if e["event"] == "OBSERVE":
            assert e["quality"]
    assert any(e["event"] == "REPLAN" and e["reason"] for e in view["trajectory"])
    assert view["stop_reason"] == StopReason.ALL_STEPS_SETTLED
    assert [e["seq"] for e in view["trajectory"]] == list(range(1, len(view["trajectory"]) + 1))
