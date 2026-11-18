"""
BANK VERIFICATION AGENT -- every seeded demo scenario (config bank_verification_demo.yaml), end to end through the
services: seeded -> processed -> the expected rule statuses; then the scenario's suggested agency input -> the
expected final outcome. Synthetic data only; the AI summary is off (tests never call the model).
"""

from __future__ import annotations

import pytest
from bank_verification_fixtures import bv_home, bv_repo, bv_template  # noqa: F401 - pytest fixtures

from app.agents.credit.bank_verification.demo_data import reports
from app.agents.credit.bank_verification.demo_data import scenarios
from app.agents.credit.bank_verification.services import decision_service, verification_service
from app.agents.credit.bank_verification.services.verification_service import ServiceError

CODES = [s["code"] for s in scenarios.SCENARIOS]


def _scenario(code: str) -> dict:
    return next(s for s in scenarios.SCENARIOS if s["code"] == code)


def test_twelve_scenarios_are_seeded_and_labelled_synthetic(bv_repo):
    assert len(scenarios.SCENARIOS) == 12
    assert {c["case_id"] for c in bv_repo.cases()} == {s["case"]["case_id"] for s in scenarios.SCENARIOS}
    for s in scenarios.SCENARIOS:
        assert s["statement"]["spec"]["details"]["account_number"].startswith("9990")       # fictional numbers
        assert len(s["statement"]["spec"]["details"]["ifsc"]) == 11                      # a well-formed IFSC


@pytest.mark.parametrize("code", CODES)
def test_each_scenario_gives_its_expected_rule_results(bv_repo, code):
    s = _scenario(code)
    run, _ = verification_service.latest(bv_repo, s["case"]["case_id"])
    got = {r["rule_id"]: r["status"] for r in run["rules"]}
    assert len(run["rules"]) == 8
    for rule, status in s["expect"].items():
        if rule.startswith("R"):
            assert got[rule] == status, (code, rule, got)
    if "action" in s["expect"]:
        assert run["recommended_action"] == s["expect"]["action"]
    for r in run["rules"]:
        assert r["reason"].strip(), (code, r["rule_id"])                # every rule explains itself


@pytest.mark.parametrize("code", CODES)
def test_each_scenario_finalizes_as_expected(bv_repo, code):
    s = _scenario(code)
    agency = dict(s["agency"])
    with_report = agency.pop("with_report", True)
    case_id = s["case"]["case_id"]
    report = (reports.for_case(case_id, ownership=agency["ownership"], agency_id=agency["agency_id"]),
              "agency_report.pdf") if with_report else None
    if s["expect"]["final"] == "REPORT_REQUIRED":
        with pytest.raises(ServiceError) as caught:
            decision_service.finalize(bv_repo, case_id, agency, report=report, actor="test-agency")
        assert caught.value.code == "REPORT_REQUIRED"
        assert bv_repo.case(case_id)["status"] not in ("POSITIVE", "NEGATIVE")
        return
    out = decision_service.finalize(bv_repo, case_id, agency, report=report, actor="test-agency")
    assert out["final_status"] == s["expect"]["final"], (code, out["blocked_reasons"])
    assert bv_repo.case(case_id)["status"] == s["expect"]["final"]
    if out["final_status"] == "REVIEW_REQUIRED":
        assert out["blocked_reasons"], code                              # a non-final result always says why


def test_aa_success_prefills_from_aa_and_aa_failure_has_no_cross_check(bv_repo):
    run1, _ = verification_service.latest(bv_repo, "BV-2026-001")
    run2, _ = verification_service.latest(bv_repo, "BV-2026-002")
    assert run1["source"] == "AA" and set(run1["form"]["provenance"].values()) == {"AA"}
    assert run1["sources"]["aa"] is not None and run2["sources"]["aa"] is None
    assert {r["rule_id"]: r["status"] for r in run2["rules"]}["R6"] == "NOT_APPLICABLE"


def test_ownership_pending_never_becomes_positive(bv_repo):
    s = _scenario("CLEAN_AA_SUCCESS")
    agency = {**s["agency"], "ownership": "PENDING"}
    agency.pop("with_report")
    out = decision_service.finalize(bv_repo, "BV-2026-001", agency, report=(reports.for_case("BV-2026-001", ownership=agency["ownership"]), "r.pdf"), actor="a")
    assert out["final_status"] == "REVIEW_REQUIRED" and any("PENDING" in b for b in out["blocked_reasons"])


def test_a_positive_needs_more_than_passing_comparisons(bv_repo):
    """Every comparison passes on the clean case, yet an unverified checklist item blocks a POSITIVE."""
    s = _scenario("CLEAN_AA_SUCCESS")
    agency = {**s["agency"], "checklist": {**s["agency"]["checklist"], "statement_authentic": "NOT_VERIFIED"}}
    agency.pop("with_report")
    out = decision_service.finalize(bv_repo, "BV-2026-001", agency, report=(reports.for_case("BV-2026-001", ownership=agency["ownership"]), "r.pdf"), actor="a")
    assert out["final_status"] == "REVIEW_REQUIRED"


def test_a_negative_needs_a_recorded_finding(bv_repo):
    s = _scenario("CLEAN_AA_SUCCESS")
    agency = {**s["agency"], "requested_decision": "NEGATIVE"}
    agency.pop("with_report")
    out = decision_service.finalize(bv_repo, "BV-2026-001", agency, report=(reports.for_case("BV-2026-001", ownership=agency["ownership"]), "r.pdf"), actor="a")
    assert out["final_status"] == "REVIEW_REQUIRED" and out["negative_findings"] == []


def test_a_finalized_case_is_not_processed_again(bv_repo):
    s = _scenario("CLEAN_AA_SUCCESS")
    agency = dict(s["agency"])
    agency.pop("with_report")
    decision_service.finalize(bv_repo, "BV-2026-001", agency, report=(reports.for_case("BV-2026-001", ownership=agency["ownership"]), "r.pdf"), actor="a")
    with pytest.raises(ServiceError) as caught:
        verification_service.process(bv_repo, {"case_id": "BV-2026-001", "mode": "AA_SUCCESS"}, upload=None,
                                     actor="cpa", idempotency_key=None)
    assert caught.value.code == "CASE_FINALIZED"


def test_the_audit_history_records_every_step(bv_repo):
    events = [e["event"] for e in bv_repo.history("BV-2026-003")]
    assert events[:1] == ["CASE_SEEDED"] and "STATEMENT_READ" in events and "VERIFICATION_RUN" in events
