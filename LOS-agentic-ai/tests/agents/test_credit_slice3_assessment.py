"""
Credit Underwriting Agent -- Slice 3: policy evaluation, findings, provenance, assessment.

Same harness as Slice 2 (real SQLite store, the LOS ingest path, the real MCP
runtime, the demo bureau). The graph now ends at an underwriting ASSESSMENT.
"""

from __future__ import annotations

import copy
import json
from unittest.mock import patch

import pytest

from test_credit_slice2_graph import (  # noqa: F401  (fixtures + helpers)
    APP, CASE, CO, ELIGIBILITY, INCOME, KYC, RISK, bank_doc, caller, demo_bureau,
    full_evidence, persist, run, seed, store,
)

# IMPORTED BEFORE ANY PATCH BELOW. A module first imported while one of its
# names is patched binds the mock for good (`from engine import evaluate`),
# and the leak surfaces in an unrelated suite later in the run.
import app.agents.bank_statement.income  # noqa: F401
import app.agents.bank_statement.signals  # noqa: F401
import app.agents.eligibility.agent  # noqa: F401
import app.agents.fraud_risk.engine  # noqa: F401
import app.agents.kyc.agent  # noqa: F401
import app.agents.los.flow  # noqa: F401

from app.agents.credit import findings as credit_findings
from app.agents.credit import policy as credit_policy
from app.agents.credit import provenance
from app.agents.credit.schemas import AssessmentStatus, FindingCategory, FindingStatus
from app.core.exceptions import ConfigurationError


# ==========================================================================
# helpers
# ==========================================================================

def assessed(**kwargs):
    state = run(**kwargs)
    assert state["assessment"] is not None, state.get("stop_reason")
    return state["assessment"]


def by_rule(assessment, rule_id, party_id=None):
    hits = [f for f in assessment.findings if f.policy_rule_id == rule_id
            and (party_id is None or (f.subject and f.subject.party_id == party_id))]
    return hits[0] if hits else None


def gaps(assessment):
    return [f for f in assessment.findings if f.category is FindingCategory.DATA_GAP]


def evaluation(assessment, rule_id, party_id=None):
    return next(r for r in assessment.rule_evaluations
                if r["rule_id"] == rule_id and r["party_id"] == party_id)


def policy_copy():
    return copy.deepcopy(credit_policy.get_policy())


# ==========================================================================
# bureau: positive / adverse
# ==========================================================================

def test_positive_bureau_signals_become_positive_findings(store):
    seed(store)
    full_evidence(store)
    a = assessed()
    for rule in ("UW_BUR_SCORE_STRONG", "UW_BUR_ESTABLISHED_HISTORY", "UW_REP_CLEAN"):
        f = by_rule(a, rule, APP)
        assert f is not None and f.status is FindingStatus.POSITIVE, rule
        assert f.severity.value == "INFO" and f.source.value == "BUREAU"
        assert f.confidence == "DEMO"
    assert by_rule(a, "UW_BUR_SCORE_STRONG").observed_value == "750"
    assert a.status is AssessmentStatus.READY_FOR_DECISION


def test_adverse_bureau_signals_become_negative_and_review_findings(store):
    seed(store)
    full_evidence(store)
    demo_bureau.assign(APP, "DEMO-BUREAU-ADVERSE")
    a = assessed()
    assert by_rule(a, "UW_REP_DPD_30").status is FindingStatus.NEGATIVE
    assert by_rule(a, "UW_REP_DPD_30").severity.value == "HIGH"
    assert by_rule(a, "UW_ADV_SETTLEMENT").status is FindingStatus.NEGATIVE
    assert by_rule(a, "UW_ADV_ENQUIRIES").status is FindingStatus.REVIEW
    assert by_rule(a, "UW_BUR_SCORE_LOW").observed_value == "610"
    assert by_rule(a, "UW_BUR_SCORE_STRONG") is None and by_rule(a, "UW_REP_CLEAN") is None
    assert a.status is AssessmentStatus.REVIEW_REQUIRED
    assert "NEGATIVE:UW_REP_DPD_30@" + APP in a.status_reasons


def test_thin_history_is_a_medium_review(store):
    seed(store)
    full_evidence(store)
    demo_bureau.assign(APP, "DEMO-BUREAU-THIN")
    a = assessed()
    f = by_rule(a, "UW_BUR_THIN_HISTORY")
    assert f.status is FindingStatus.REVIEW and f.severity.value == "MEDIUM"
    assert a.status is AssessmentStatus.REVIEW_REQUIRED


def test_mixed_bureau_signals_are_not_a_contradiction(store):
    seed(store)
    full_evidence(store)
    demo_bureau.assign(APP, "DEMO-BUREAU-ADVERSE")      # long history, low score
    a = assessed()
    assert by_rule(a, "UW_BUR_ESTABLISHED_HISTORY") is not None
    assert not any(e.startswith("CONTRADICTION:CREDIT_PROFILE") for e in a.exceptions)


# ==========================================================================
# banking -- the Bank Statement Agent's recorded signals
# ==========================================================================

def _bank_with(signals: dict):
    doc = bank_doc()
    doc["extraction"]["fields"]["evidence"].update(signals)
    return doc


def test_banking_finding_from_recorded_returned_transactions(store):
    seed(store)
    persist({"documents": [_bank_with({"returned_transaction_count": 2})],
             **KYC, **INCOME, **ELIGIBILITY})
    demo_bureau.assign(APP, "DEMO-BUREAU-CLEAN")
    a = assessed()
    f = by_rule(a, "UW_BNK_RETURNS")
    assert f.category is FindingCategory.BANKING and f.status is FindingStatus.REVIEW
    assert f.observed_value == "2"
    ref = next(e for e in a.evidence if e.ref_id == f.evidence_refs[0])
    assert ref.field == "evidence.returned_transaction_count"
    extraction = next(x for x in store.get_current_findings(CASE, kind="EXTRACTION"))
    assert ref.record_id == extraction.finding_id
    assert a.status is AssessmentStatus.REVIEW_REQUIRED


def test_missing_bank_count_is_unavailable_never_zero(store):
    seed(store)
    full_evidence(store)                               # statement has no returned count
    a = assessed()
    row = evaluation(a, "UW_BNK_RETURNS")
    assert row["outcome"] == credit_findings.UNAVAILABLE and "value" not in row
    assert by_rule(a, "UW_BNK_RETURNS") is None
    gap = next(g for g in gaps(a) if "UW_BNK_RETURNS" in g.rules_not_evaluated)
    assert gap.status is FindingStatus.DATA_UNAVAILABLE and gap.severity.value == "LOW"
    # a LOW data gap alone does not force review
    assert a.status is AssessmentStatus.READY_FOR_DECISION


def test_low_severity_review_does_not_force_review(store):
    seed(store)
    persist({"documents": [_bank_with({"mandate_debit_count": 3})],
             **KYC, **INCOME, **ELIGIBILITY})
    demo_bureau.assign(APP, "DEMO-BUREAU-CLEAN")
    a = assessed()
    assert by_rule(a, "UW_BNK_MANDATE_DEBITS").severity.value == "LOW"
    assert a.status is AssessmentStatus.READY_FOR_DECISION


# ==========================================================================
# upstream results -- consumed as recorded, never recalculated
# ==========================================================================

def test_recorded_eligibility_is_consumed_not_recalculated(store):
    seed(store)
    full_evidence(store)
    persist({"eligibility": {"status": "REVIEW", "reason_codes": ["FOIR_ABOVE_LIMIT"],
                             "foir": "0.61"}})
    with patch("app.agents.eligibility.agent.evaluate",
               side_effect=AssertionError("eligibility recalculated")), \
            patch("app.agents.eligibility.agent.assess",
                  side_effect=AssertionError("eligibility recalculated")):
        a = assessed()
    f = by_rule(a, "UW_REC_ELIGIBILITY_NOT_PASS")
    assert f.observed_value == "REVIEW" and f.source.value == "ELIGIBILITY"
    assert a.financial_observations["eligibility_recorded"]["foir"] == "0.61"   # echoed
    assert a.status is AssessmentStatus.REVIEW_REQUIRED


def test_recorded_risk_is_consumed_not_recalculated(store):
    seed(store)
    full_evidence(store)
    persist({"risk": {"risk_category": "HIGH", "risk_score": 77, "final_outcome": "REVIEW",
                      "flags": ["NAME_MISMATCH:x"]}})
    with patch("app.agents.fraud_risk.engine.RiskRuleEngine.__init__",
               side_effect=AssertionError("risk recalculated")):
        a = assessed()
    f = by_rule(a, "UW_REC_RISK_NOT_PASS")
    assert f.observed_value == "REVIEW" and f.source.value == "RISK"
    assert a.financial_observations["risk_recorded"]["risk_score"] == 77
    assert a.status is AssessmentStatus.REVIEW_REQUIRED


def test_recorded_income_consistency_is_consumed_not_recalculated(store):
    seed(store)
    full_evidence(store)
    persist({"income_consistency": {**INCOME["income_consistency"], "status": "REVIEW",
                                    "reason_codes": ["INCOME_MISMATCH"]}})
    with patch("app.agents.los.flow._income_consistency_for",
               side_effect=AssertionError("income consistency recalculated")):
        a = assessed()
    f = by_rule(a, "UW_INC_RECORDED_REVIEW")
    assert f.observed_value == "REVIEW" and f.source.value == "INCOME"
    assert by_rule(a, "UW_INC_RECORDED_PASS") is None


def test_no_upstream_calculation_runs_during_underwriting(store):
    seed(store, co=True)
    full_evidence(store, co=True)
    boom = AssertionError("an upstream calculation was executed again")
    targets = [
        "app.agents.eligibility.agent.evaluate", "app.agents.eligibility.agent.assess",
        "app.agents.kyc.agent.run_kyc", "app.agents.bank_statement.signals.derive",
        "app.agents.bank_statement.income.income_evidence",
        "app.agents.los.flow._income_consistency_for", "app.agents.los.flow._eligibility_for",
        "app.agents.los.flow._risk_for", "app.agents.fraud_risk.engine.RiskRuleEngine.__init__",
        "app.agents.kyc.checks.check_name", "app.agents.kyc.checks.check_pan",
    ]
    patches = [patch(t, side_effect=boom) for t in targets]
    for p in patches:
        p.start()
    try:
        a = assessed()
    finally:
        for p in patches:
            p.stop()
    assert a.status in set(AssessmentStatus)


# ==========================================================================
# missing data / DATA_INSUFFICIENT
# ==========================================================================

def test_missing_required_bureau_makes_the_assessment_data_insufficient(store):
    seed(store)
    persist({"documents": [bank_doc()], **KYC, **INCOME, **ELIGIBILITY})   # no bureau record
    a = assessed()
    assert a.status is AssessmentStatus.DATA_INSUFFICIENT
    gap = next(g for g in gaps(a) if g.subject and g.subject.party_id == APP
               and "bureau" in g.description)
    assert gap.severity.value == "HIGH" and "NO_BUREAU_RECORD" in gap.description
    assert "UW_BUR_SCORE_LOW@" + APP in gap.rules_not_evaluated
    assert f"REQUIRED_EVIDENCE_UNAVAILABLE:bureau:{APP}" in a.status_reasons
    # bureau rules produced no judgement at all
    assert not [f for f in a.findings if f.source and f.source.value == "BUREAU"
                and f.category is not FindingCategory.DATA_GAP]


def test_empty_case_is_data_insufficient_with_every_gap_named(store):
    seed(store)
    a = assessed()
    assert a.status is AssessmentStatus.DATA_INSUFFICIENT
    named = " ".join(a.data_gaps)
    for category in ("kyc", "income", "eligibility", "bureau"):
        assert category in named
    assert all(g.evidence_refs == [] for g in gaps(a))


def test_data_insufficient_takes_precedence_but_keeps_review_reasons(store):
    seed(store)
    persist({"documents": [bank_doc()], **KYC, **INCOME})            # no eligibility
    demo_bureau.assign(APP, "DEMO-BUREAU-ADVERSE")
    a = assessed()
    assert a.status is AssessmentStatus.DATA_INSUFFICIENT
    assert "REQUIRED_EVIDENCE_UNAVAILABLE:eligibility" in a.status_reasons
    assert "NEGATIVE:UW_ADV_SETTLEMENT@" + APP in a.status_reasons


def test_budget_stop_is_insufficient_and_recorded_as_an_exception(store):
    from app.agents.credit import config as credit_config

    seed(store)
    full_evidence(store)
    with patch.object(credit_config, "max_tool_calls", return_value=3):
        a = assessed()
    assert a.status is AssessmentStatus.DATA_INSUFFICIENT
    assert "STOPPED:MAX_TOOL_CALLS" in a.exceptions


# ==========================================================================
# multiple findings, severity, evidence, provenance
# ==========================================================================

def test_multiple_findings_with_their_own_severity(store):
    seed(store, declared_monthly_obligations="2000")
    full_evidence(store)
    demo_bureau.assign(APP, "DEMO-BUREAU-ADVERSE")
    a = assessed()
    severities = {f.policy_rule_id: f.severity.value for f in a.findings if f.policy_rule_id}
    assert severities["UW_REP_DPD_30"] == "HIGH"
    assert severities["UW_ADV_ENQUIRIES"] == "MEDIUM"
    assert severities["UW_BUR_ESTABLISHED_HISTORY"] == "INFO"
    assert severities["UW_OBL_BUREAU_VS_DECLARED"] == "MEDIUM"
    assert len([f for f in a.findings if f.policy_rule_id]) >= 7


def test_every_non_gap_finding_has_real_evidence(store):
    seed(store, co=True, declared_monthly_obligations="0")
    full_evidence(store, co=True)
    a = assessed()
    evidence = {e.ref_id: e for e in a.evidence}
    for f in a.findings:
        if f.category is FindingCategory.DATA_GAP:
            continue
        assert f.evidence_refs, f.policy_rule_id
        for ref_id in f.evidence_refs:
            assert ref_id in evidence
            assert evidence[ref_id].record_id, (f.policy_rule_id, ref_id)
    assert set(evidence) == {r for f in a.findings for r in f.evidence_refs}


def test_a_signal_without_evidence_can_never_become_a_finding():
    from app.agents.credit.schemas import Party, UnderwritingContext
    from app.agents.credit.signals import SignalValue

    policy = credit_policy.get_policy()
    ctx = UnderwritingContext(case_id=CASE, applicant_id=APP, request_id="r",
                              parties=[Party(party_id=APP)])
    resolved = {("bureau.score", APP): SignalValue(signal="bureau.score", party_id=APP,
                                                   available=True, value=500, refs=[])}
    rows = [{"rule_id": "UW_BUR_SCORE_LOW", "signal": "bureau.score", "party_id": APP,
             "outcome": "FIRED", "value": 500, "policy_version": "x",
             "confirmation_status": "DEMO_UNCONFIRMED"}]
    with pytest.raises(credit_findings.FindingWithoutEvidence):
        credit_findings.build_findings(policy, rows, resolved, [], [], ctx)


def test_provenance_chain_runs_from_finding_to_the_recorded_row(store):
    seed(store)
    full_evidence(store)
    persist({"risk": {"risk_category": "HIGH", "risk_score": 77, "final_outcome": "REVIEW",
                      "flags": []}})
    a = assessed()
    f = by_rule(a, "UW_REC_RISK_NOT_PASS")
    chain = provenance.chain(a.provenance, f.finding_id)
    assert [n["kind"] for n in chain] == ["FINDING", "OBSERVATION", "FACT", "TOOL"]
    finding_node, observation, fact, tool = chain
    assert observation["signal"] == "risk.final_outcome" and observation["value"] == "REVIEW"
    risk_row = store.get_current_findings(CASE, kind="RISK")[0]
    assert fact["record_id"] == risk_row.finding_id            # the real stored row
    assert fact["record_type"] == "FINDING:RISK/FRAUD_RISK" and fact["field"] == "final_outcome"
    assert fact["verified"] is True and fact["verification_basis"] == "CASE_FINDING"
    assert tool["tool"] == "risk.get"
    assert all(n["verified"] for n in chain)
    top = [n for n in a.provenance["nodes"] if n["kind"] == "ASSESSMENT"]
    assert len(top) == 1 and top[0]["status"] == a.status.value


def test_eligibility_evidence_is_resolved_to_the_recorded_finding(store):
    seed(store)
    full_evidence(store)
    a = assessed()
    f = by_rule(a, "UW_REC_ELIGIBILITY_PASS")
    fact = provenance.chain(a.provenance, f.finding_id)[2]
    row = next(x for x in store.get_current_findings(CASE, kind="FINANCIAL")
               if x.source_type == "ELIGIBILITY")
    assert fact["record_id"] == row.finding_id and fact["verified"] is True


def test_document_evidence_points_at_the_document_record(store):
    seed(store)
    full_evidence(store)
    persist({"documents": [bank_doc(), {"source_id": "slip.pdf", "type": "SALARY_SLIP",
                                        "verification": "REVIEW", "party_id": APP}]})
    a = assessed()
    f = by_rule(a, "UW_REC_DOCUMENT_NOT_PASSED")
    assert f is not None and f.observed_value == "1"
    fact = provenance.chain(a.provenance, f.finding_id)[2]
    assert fact["record_type"] == "DOCUMENT"
    assert store.get_document(fact["record_id"]).document_type == "SALARY_SLIP"
    assert fact["verified"] is True and fact["verification_basis"] == "DOCUMENT"


def test_evidence_that_does_not_verify_marks_the_finding_and_forces_review(store):
    seed(store)
    full_evidence(store)
    real = provenance._verify_fact

    def tampered(node, *args, **kwargs):
        if node.get("record_type", "").startswith("FINDING:FINANCIAL/INCOME"):
            return False, "RECORD_NOT_ON_CASE"
        return real(node, *args, **kwargs)

    with patch.object(provenance, "_verify_fact", tampered):
        a = assessed()
    f = by_rule(a, "UW_INC_RECORDED_PASS")
    assert f.confidence == "UNVERIFIED"
    assert f"UNVERIFIED_EVIDENCE:{f.finding_id}" in a.exceptions
    assert a.status is AssessmentStatus.REVIEW_REQUIRED


def test_evidence_carries_no_raw_identity_data(store):
    seed(store)
    full_evidence(store)
    a = assessed()
    blob = json.dumps([e.model_dump() for e in a.evidence]) + json.dumps(a.provenance)
    for pii in ("Test Applicant", "ABCDE1234F", "9876543210"):
        assert pii not in blob


# ==========================================================================
# parties
# ==========================================================================

def test_primary_applicant_findings_are_attributed(store):
    seed(store)
    full_evidence(store)
    a = assessed()
    f = by_rule(a, "UW_BUR_SCORE_STRONG")
    assert f.subject.party_id == APP and f.subject.role == "PRIMARY_APPLICANT"
    assert [s.party_id for s in a.subjects] == [APP]


def test_co_applicant_findings_are_attributed_separately(store):
    seed(store, co=True, declared_monthly_obligations="0")
    persist({"documents": [bank_doc()], **INCOME, **ELIGIBILITY,
             "primary_applicant": {"party_id": APP, "kyc": {"status": "PASS", "fields": []}},
             "co_applicant": {"party_id": CO, "kyc": {"status": "FAIL", "fields": []}}})
    demo_bureau.assign(APP, "DEMO-BUREAU-CLEAN")
    demo_bureau.assign(CO, "DEMO-BUREAU-ADVERSE")
    a = assessed()
    assert by_rule(a, "UW_BUR_SCORE_STRONG", APP) is not None
    assert by_rule(a, "UW_BUR_SCORE_STRONG", CO) is None
    co_settlement = by_rule(a, "UW_ADV_SETTLEMENT", CO)
    assert co_settlement.subject.role == "CO_APPLICANT"
    assert by_rule(a, "UW_REC_KYC_NOT_PASS", CO).observed_value == "FAIL"
    assert by_rule(a, "UW_REC_KYC_NOT_PASS", APP) is None
    # obligations compare the application's one declared figure with the PRIMARY only
    assert {r["party_id"] for r in a.rule_evaluations
            if r["rule_id"] == "UW_OBL_BUREAU_VS_DECLARED"} == {APP}
    parties = {p["party_id"]: p for p in a.credit_profile["parties"]}
    assert parties[CO]["bureau"]["score"] == 610 and parties[CO]["kyc_status_recorded"] == "FAIL"
    assert a.status is AssessmentStatus.REVIEW_REQUIRED


# ==========================================================================
# policy: version, DEMO_UNCONFIRMED, disabled rules, validation
# ==========================================================================

def test_findings_carry_the_policy_version_and_confirmation_status(store):
    seed(store)
    full_evidence(store)
    a = assessed()
    assert a.policy.policy_id == "UW_DEMO_V1" and a.policy.version == "0.1.0-demo"
    assert a.policy.confirmation_status == "DEMO_UNCONFIRMED"
    for f in a.findings:
        if f.policy_rule_id:
            assert f.policy_version == "0.1.0-demo"
            assert f.confirmation_status == "DEMO_UNCONFIRMED"
    assert a.is_demo is True


def test_a_rule_level_policy_version_is_honoured(store):
    seed(store)
    full_evidence(store)
    policy = policy_copy()
    for rule in policy["rules"]:
        if rule["rule_id"] == "UW_BUR_SCORE_STRONG":
            rule["policy_version"] = "0.1.1-demo"
    a = assessed(policy=policy)
    assert by_rule(a, "UW_BUR_SCORE_STRONG").policy_version == "0.1.1-demo"


def test_a_disabled_rule_produces_no_finding(store):
    seed(store)
    full_evidence(store)
    demo_bureau.assign(APP, "DEMO-BUREAU-ADVERSE")
    policy = policy_copy()
    for rule in policy["rules"]:
        if rule["rule_id"] == "UW_ADV_SETTLEMENT":
            rule["enabled"] = False
    a = assessed(policy=policy)
    assert by_rule(a, "UW_ADV_SETTLEMENT") is None
    assert evaluation(a, "UW_ADV_SETTLEMENT", APP)["outcome"] == credit_findings.DISABLED
    assert by_rule(a, "UW_REP_DPD_30") is not None


def test_policy_refuses_an_unknown_signal():
    policy = policy_copy()
    policy["rules"][0] = {**policy["rules"][0], "inputs": ["applicant.full_name"]}
    with pytest.raises(ConfigurationError, match="unknown signal"):
        credit_policy.normalise_rules(policy["rules"], policy)


@pytest.mark.parametrize("field, value", [
    ("operator", "approx"), ("severity", "CRITICAL"), ("finding_status", "APPROVED"),
    ("confirmation_status", ""), ("category", "DECISION")])
def test_policy_refuses_malformed_rules(field, value):
    policy = policy_copy()
    policy["rules"][0] = {**policy["rules"][0], field: value}
    with pytest.raises(ConfigurationError):
        credit_policy.normalise_rules(policy["rules"], policy)


def test_legacy_input_and_status_keys_are_normalised():
    policy = policy_copy()
    rule = {k: v for k, v in policy["rules"][0].items() if k not in ("inputs", "finding_status")}
    rule.update(input="bureau.score", status="review")
    out = credit_policy.normalise_rules([rule], policy)[0]
    assert out["inputs"] == ["bureau.score"] and out["finding_status"] == "REVIEW"


def test_unsigned_policy_is_refused_in_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("ALLOW_UNSIGNED_UNDERWRITING_POLICY", raising=False)
    with pytest.raises(ConfigurationError, match="not signed off"):
        credit_policy.load_policy()


@pytest.mark.parametrize("op, threshold, value, expected", [
    ("lt", 650, 600, True), ("gte", 700, 700, True), ("between", [1, 29], 30, False),
    ("in", ["REVIEW", "FAIL"], "review", True), ("not_in", ["PASS"], "FAIL", True),
    ("eq", 0, 0, True), ("ne", "PASS", "PASS", False), ("eq", True, True, True)])
def test_operators(op, threshold, value, expected):
    assert credit_policy.matches({"operator": op, "threshold": threshold}, value) is expected


def test_a_non_comparable_value_is_unavailable_not_false():
    with pytest.raises(credit_policy.NotComparable):
        credit_policy.matches({"operator": "lt", "threshold": 650}, "N/A")
    with pytest.raises(credit_policy.NotComparable):
        credit_policy.matches({"operator": "eq", "threshold": 0}, False)


# ==========================================================================
# contradictions and statuses
# ==========================================================================

def test_contradictory_evidence_is_surfaced_and_the_adverse_side_stands(store):
    seed(store)
    full_evidence(store)                                  # recorded income check: PASS
    record = store.get_application(CASE)
    record.declared_monthly_income = "90000"              # declared: 90k vs slip 65k
    store.save_application(record)
    a = assessed()
    assert by_rule(a, "UW_INC_RECORDED_PASS").status is FindingStatus.POSITIVE
    mismatch = by_rule(a, "UW_INC_DECLARED_VS_RECORDED")
    assert mismatch.status is FindingStatus.REVIEW and float(mismatch.observed_value) > 10
    assert len(mismatch.evidence_refs) == 2            # declared field + recorded slip amount
    assert any(e.startswith("CONTRADICTION:INCOME_CONSISTENCY") for e in a.exceptions)
    assert a.status is AssessmentStatus.REVIEW_REQUIRED


def test_declared_income_not_captured_is_a_gap_not_a_match(store):
    seed(store)
    full_evidence(store)
    record = store.get_application(CASE)
    record.declared_monthly_income = None
    store.save_application(record)
    a = assessed()
    row = evaluation(a, "UW_INC_DECLARED_VS_RECORDED")
    assert row["outcome"] == credit_findings.UNAVAILABLE
    assert "declared monthly income not captured" in row["reason"]


def test_ready_for_decision(store):
    seed(store, declared_monthly_obligations="0")
    full_evidence(store)
    a = assessed()
    assert a.status is AssessmentStatus.READY_FOR_DECISION
    assert a.status_reasons == ["ALL_REQUIRED_EVIDENCE_RESOLVED_NO_REVIEW_FINDINGS"]
    assert a.next_step == "DECISION_AGENT"


def test_review_required(store):
    seed(store)
    full_evidence(store)
    demo_bureau.assign(APP, "DEMO-BUREAU-THIN")
    assert assessed().status is AssessmentStatus.REVIEW_REQUIRED


def test_the_assessment_is_deterministic(store):
    seed(store, co=True)
    full_evidence(store, co=True)
    a, b = assessed(), assessed()
    assert a.assessment_id == b.assessment_id and a.input_hash == b.input_hash
    assert [f.finding_id for f in a.findings] == [f.finding_id for f in b.findings]
    assert a.status == b.status


def test_the_input_hash_changes_when_the_evidence_does(store):
    seed(store)
    full_evidence(store)
    a = assessed()
    demo_bureau.assign(APP, "DEMO-BUREAU-THIN")
    assert assessed().input_hash != a.input_hash


# ==========================================================================
# no decision leakage
# ==========================================================================

def test_assessment_status_has_exactly_three_non_decision_values():
    assert {s.value for s in AssessmentStatus} == {
        "READY_FOR_DECISION", "REVIEW_REQUIRED", "DATA_INSUFFICIENT"}


@pytest.mark.parametrize("profile", ["DEMO-BUREAU-CLEAN", "DEMO-BUREAU-ADVERSE", None])
def test_no_approved_or_rejected_anywhere(store, profile):
    seed(store)
    if profile:
        full_evidence(store)
        demo_bureau.assign(APP, profile)
    state = run()
    blob = state["assessment"].model_dump_json().upper()
    from app.agents.credit import graph

    blob += json.dumps(graph.trajectory_view(state), default=str).upper()
    for word in ("APPROVED", "REJECTED", "APPROVE", "REJECT", "DECLINED", "SANCTION"):
        assert word not in blob, word


def test_a_refused_run_produces_no_assessment(store):
    seed(store)
    state = run(caller(scopes=("los.read",)))
    assert state["assessment"] is None and state["findings"] == []


def test_every_fact_traces_to_the_tool_call_that_read_it(store):
    seed(store, co=True, declared_monthly_obligations="0")
    full_evidence(store, co=True)
    persist({"risk": {"risk_category": "LOW", "risk_score": 10, "final_outcome": "REVIEW",
                      "flags": []}})
    a = assessed()
    nodes = {n["node_id"]: n for n in a.provenance["nodes"]}
    facts = [n for n in nodes.values() if n["kind"] == "FACT"]
    assert facts
    for fact in facts:
        parents = [nodes[i] for i in fact["relation"]["derived_from"]]
        assert parents and parents[0]["kind"] == "TOOL", fact.get("record_type")
        assert fact["verified"] is True, fact
