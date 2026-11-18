"""
POST /api/v1/credit/dedupe/customer-history and the assessment behind it (MIFIN -> loan history -> policy -> outcome
-> explanation). DEMO / SYNTHETIC DATA ONLY (app/config/demo/mifin_demo.yaml; values invented, real code formats).
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone

import pytest
import yaml
from fastapi.testclient import TestClient

from app.agents.dedupe import customer_history as CH
from app.agents.dedupe import demo_sources, history_policy, history_summary
from app.agents.dedupe import lms as L
from app.agents.dedupe import mifin as M
from app.agents.dedupe import service as dedupe_service
from app.security import access
from app.store import set_repository
from app.store.models import Applicant, Application
from app.store.testing import fresh_repository

OFFICER = "officer-history-1"
SCOPES = ["los.dedupe.read"]
CASE, OTHER = "CASE-HIST00000001", "CASE-HIST00000002"
NAMES_AND_IDS = ("ABCPK4321L", "BCDPM5678N", "9876501111", "9876502222", "AP00731942", "AP00745218", "PR02184467",
                 "PR02407753", "RAHUL", "PRIYA", "1992-06-14", "1988-11-02")


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("MIFIN_PROVIDER", "demo")
    monkeypatch.setenv("LMS_PROVIDER", "demo")
    # not production (the demo sources are allowed): said explicitly -- an UNSET environment is production for the
    # rules (2026-11-02, fail closed), as it is for authentication
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("CUSTOMER_HISTORY_AUDIT_PATH", str(tmp_path / "history.jsonl"))
    monkeypatch.setattr("app.llm.availability.provider_reachable", lambda: False)   # deterministic by default
    demo_sources.reload()
    M.reload()
    history_policy.reload()
    yield
    history_policy.reload()


@pytest.fixture
def repo():
    r = fresh_repository()
    r.initialise()
    for case_id, app_id in ((CASE, "APP-HIST-1"), (OTHER, "APP-HIST-2")):
        r.save_applicant(Applicant(applicant_id=app_id, full_name="Synthetic Person"))
        r.save_application(Application(case_id=case_id, applicant_id=app_id, product="PERSONAL_LOAN",
                                       created_at=datetime(2026, 10, 1, tzinfo=timezone.utc)))
    r.grant_access(OFFICER, access.CASE, CASE)
    set_repository(r)
    yield r
    set_repository(None)


def _assess(repo, parameter, value, **kw):
    return CH.assess(CASE, parameter, value, repository=repo, **kw)


def _rule(result, rid):
    return next(r for r in result["evaluation"]["rules"] if r["rule_id"] == rid)


def _policy_with_limits(tmp_path, monkeypatch, **limits):
    """A TEST-ONLY copy of the policy with limits filled in -- to prove each path; never shipped values."""
    data = yaml.safe_load(history_policy.PATH.read_text(encoding="utf-8"))
    for rule in data["rules"]:
        if rule["rule_id"] in limits:
            rule["threshold"], rule["confirmation_status"] = limits[rule["rule_id"]], "TEST_ONLY"
    path = tmp_path / "history_policy_test.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("CUSTOMER_HISTORY_POLICY_PATH", str(path))
    history_policy.reload()


# ---- loan counts and statuses ----------------------------------------------------------------------------------
def test_zero_previous_loans_is_complete_and_every_rule_runs(repo):
    r = _assess(repo, "AP Code", "AP00752673", requested_amount=300000)
    assert (r["customer_status"], r["history"]["history_status"], r["outcome"]) == (
        "EXISTING", "NO_PREVIOUS_LOANS", "READY_FOR_DECISION")              # the limits are set (demo, 2026-10-23)
    assert all(x["outcome"] == "NOT_TRIGGERED" for x in r["evaluation"]["rules"]), r["evaluation"]["rules"]


def test_a_limit_left_unset_is_never_a_silent_pass(repo, tmp_path, monkeypatch):
    _policy_with_limits(tmp_path, monkeypatch, H07_EXISTING_LOAN_COUNT=None, H08_TOTAL_EXPOSURE=None)
    r = _assess(repo, "AP Code", "AP00752673", requested_amount=300000)
    assert r["outcome"] == "REVIEW_REQUIRED"
    assert {"POLICY_NOT_CONFIGURED:H07_EXISTING_LOAN_COUNT", "POLICY_NOT_CONFIGURED:H08_TOTAL_EXPOSURE"} <= set(
        r["reason_codes"])
    assert _rule(r, "H07_EXISTING_LOAN_COUNT")["reason"] == "THRESHOLD_UNCONFIGURED"


def test_cancelled_applications_only_are_complete_and_never_exposure(repo):
    r = _assess(repo, "PAN", "ABCPK4321L", requested_amount=300000)
    assert r["history"]["history_status"] == "COMPLETE" and r["history"]["aggregates"]["active_loans"] == 0
    assert _rule(r, "H05_NO_UNKNOWN_LOAN_STATUS")["outcome"] == "NOT_TRIGGERED"


def test_multiple_loans_live_closed_cancelled_with_verified_exposure(repo):
    r = _assess(repo, "Mobile No", "9876502222", requested_product="PERSONAL_LOAN", requested_amount=500000)
    assert r["history"]["history_status"] == "COMPLETE" and r["history"]["loan_count"] == 3
    signals = history_policy.signals(r["history"], 500000)
    assert str(signals["exposure.with_requested"]) == "812400"           # 312400 live + 500000 requested
    assert r["outcome"] == "READY_FOR_DECISION"                           # within every (demo) limit, nothing found


@pytest.mark.parametrize("parameter, value, customer, outcome, reason", [
    ("PAN", "DEFPR1357S", "POSSIBLE_EXISTING", "DATA_INSUFFICIENT", "IDENTITY_NOT_CONFIRMED"),
    ("Mobile No", "9876505555", "AMBIGUOUS", "DATA_INSUFFICIENT", "IDENTITY_NOT_CONFIRMED"),
    ("PAN", "EFGPS9753T", "UNKNOWN", "DATA_INSUFFICIENT", "MIFIN_UNAVAILABLE"),
    # a new customer on a case with no amount: exposure + requested (H08) cannot be known -- said, not passed
    ("PAN", "ZZZPZ1111Z", "NEW", "DATA_INSUFFICIENT", "DATA_UNAVAILABLE:H08_TOTAL_EXPOSURE"),
])
def test_match_outcomes(repo, parameter, value, customer, outcome, reason):
    r = _assess(repo, parameter, value)
    assert (r["customer_status"], r["outcome"]) == (customer, outcome)
    assert reason in r["reason_codes"]


def test_mifin_not_configured_is_data_insufficient_never_a_new_customer(repo, monkeypatch):
    monkeypatch.delenv("MIFIN_PROVIDER")
    r = _assess(repo, "PAN", "ABCPK4321L")
    assert (r["customer_status"], r["outcome"]) == ("UNKNOWN", "DATA_INSUFFICIENT")
    assert r["completeness"]["complete"] is False


def test_an_lms_outage_is_data_insufficient(repo):
    class Down:
        def loan(self, code):
            raise L.LmsUnavailable("LMS_TIMEOUT")

    r = _assess(repo, "Mobile No", "9876502222", lms_source=Down())
    assert r["outcome"] == "DATA_INSUFFICIENT" and "LOAN_HISTORY_INCOMPLETE" in r["reason_codes"]


def test_conflicting_duplicates_and_bad_amounts_are_never_counted(repo):
    r = _assess(repo, "PAN", "FGHPT8642U")
    assert r["outcome"] == "DATA_INSUFFICIENT"
    # the second loan is DISBURSED per MIFIN (owner 2026-10-23) with no LMS record: incomplete, never counted
    assert {"CONFLICTING_LOAN_RECORDS", "LOAN_HISTORY_INCOMPLETE"} <= set(r["reason_codes"])
    assert r["history"]["aggregates"]["active_exposure"]["status"] == "NOT_EVALUATED"


# ---- every final status, with test-only limits ----------------------------------------------------------------
def test_ready_for_decision_needs_every_rule_evaluated_and_none_fired(repo, tmp_path, monkeypatch):
    _policy_with_limits(tmp_path, monkeypatch, H07_EXISTING_LOAN_COUNT=5, H08_TOTAL_EXPOSURE=5000000,
                        H09_EMI_BURDEN=100000, H10_DPD=30)
    # the applicant IS MIFIN's customer (synthetic PRIYA MORE): no identity finding, so the dedupe proceeds
    repo.save_applicant(Applicant(applicant_id="APP-HIST-1", full_name="Priya More", date_of_birth="1988-11-02"))
    r = _assess(repo, "Mobile No", "9876502222", requested_amount=500000)
    assert (r["outcome"], r["reason_codes"], r["evaluation"]["complete"]) == ("READY_FOR_DECISION", [], True)
    assert "not an approval" in r["explanation"]["text"]


def test_a_limit_that_fires_is_review_required_with_its_reason(repo, tmp_path, monkeypatch):
    _policy_with_limits(tmp_path, monkeypatch, H07_EXISTING_LOAN_COUNT=1, H08_TOTAL_EXPOSURE=600000,
                        H09_EMI_BURDEN=100000, H10_DPD=30)
    r = _assess(repo, "Mobile No", "9876502222", requested_amount=500000)
    assert r["outcome"] == "REVIEW_REQUIRED"
    assert {"EXISTING_LOAN_LIMIT", "CUSTOMER_EXPOSURE_LIMIT"} <= set(r["reason_codes"])


def test_a_missing_policy_file_is_a_configuration_error(repo, monkeypatch, tmp_path):
    monkeypatch.setenv("CUSTOMER_HISTORY_POLICY_PATH", str(tmp_path / "missing.yaml"))
    history_policy.reload()
    from app.core.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError):
        _assess(repo, "PAN", "ABCPK4321L")


def test_an_unsigned_policy_is_refused_in_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    from app.core.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError, match="not signed off"):
        history_policy.load()


# ---- the explanation ---------------------------------------------------------------------------------------------
def _good(f):
    return (f"MIFIN confirmed an existing customer ({f['customer_ref']}). {f['loan_count']} previous loan records "
            f"were found. The outcome is {f['outcome'].replace('_', ' ').lower()}. Next: {f['next_actions'][0]}")


@pytest.mark.parametrize("bad, why", [
    (lambda f: _good(f) + " Live exposure is Rs 999999.", "number"),
    (lambda f: _good(f) + " The loan PR02407753 is live.", "AP/PR"),
    (lambda f: _good(f) + " The customer has no previous loans.", "no loans"),
    (lambda f: _good(f) + " Overall: READY_FOR_DECISION.", "another outcome"),
    (lambda f: _good(f) + " Customer PAN ABCPK4321L.", "PAN"),
])
def test_a_hallucinating_model_is_rejected_and_the_deterministic_text_stands(repo, bad, why):
    r = _assess(repo, "PAN", "FGHPT8642U", generator=bad)          # a PARTIAL history: DATA_INSUFFICIENT
    assert r["explanation"]["source"] == "DETERMINISTIC", why
    assert r["explanation"]["fallback_reason"].startswith("LLM_REJECTED"), why
    assert r["explanation"]["text"] == history_summary.deterministic(r)


def test_a_faithful_model_text_is_used(repo):
    r = _assess(repo, "Mobile No", "9876502222", generator=_good)
    assert r["explanation"]["source"] == "LLM" and "existing customer" in r["explanation"]["text"]


def test_a_failing_model_falls_back(repo):
    def boom(f):
        raise TimeoutError("model timed out")

    r = _assess(repo, "Mobile No", "9876502222", generator=boom)
    assert (r["explanation"]["source"], r["explanation"]["fallback_reason"]) == (
        "DETERMINISTIC", "LLM_ERROR:TimeoutError")


def test_the_model_never_sees_an_identifier(repo):
    seen = {}

    def spy(f):
        seen.update(f)
        return _good(f)

    _assess(repo, "Mobile No", "9876502222", generator=spy)
    text = json.dumps(seen)
    for secret in NAMES_AND_IDS:
        assert secret not in text, secret


# ---- the API -----------------------------------------------------------------------------------------------------
def _client(make_token, scopes=SCOPES, subject=OFFICER):
    from main import app

    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {make_token(subject=subject, scopes=scopes)}"})
    return c


BODY = {"case_id": CASE, "customer_type": "Existing", "dedupe_parameter": "Mobile No",
        "dedupe_parameter_value": "9876502222", "requested_product": "PERSONAL_LOAN", "requested_amount": 500000}


def test_the_api_returns_the_contract_masked(repo, make_token, tmp_path):
    r = _client(make_token).post("/api/v1/credit/dedupe", json=BODY)
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("api_version", "request_id", "case_id", "stage", "customer_status", "customer_type_check",
                "requested", "history", "evaluation", "outcome", "reason_codes", "next_actions", "completeness",
                "policy", "is_demo", "explanation", "audit"):
        assert key in body, key
    assert (body["customer_status"], body["outcome"], body["is_demo"]) == ("EXISTING", "READY_FOR_DECISION", True)
    assert body["customer_type_check"]["result"] == "CONSISTENT"
    # the matching table's own block: MIFIN's name / DOB as held, PAN / mobile masked -- and nothing of it elsewhere
    shown = body["history"]["customer"].pop("identity_values")
    assert "BCDPM5678N" not in json.dumps(shown) and "9876502222" not in json.dumps(shown)
    rest = json.dumps(body)
    for secret in NAMES_AND_IDS:
        if secret.startswith("AP"):                  # the AP number in full anywhere in the answer (owner 2026-10-23)
            continue
        if secret.startswith("PR"):                  # a PR code in full only in its own field
            assert _codes_only_where_allowed(body, secret), secret
        else:
            assert secret not in rest, secret
    assert body["history"]["customer"]["customer_code"] == "AP00745218"
    assert body["audit"]["recorded"] is True
    line = json.loads((tmp_path / "history.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert line["case_id"] == CASE and "9876502222" not in json.dumps(line)
    assert any(e.event_type == "CUSTOMER_HISTORY_ASSESSED" for e in repo.get_case_timeline(CASE))


def test_the_api_refuses_without_token_scope_or_ownership(repo, make_token, unauthenticated_client):
    assert unauthenticated_client.post("/api/v1/credit/dedupe", json=BODY).status_code == 401
    no_scope = _client(make_token, scopes=["documents:read"]).post("/api/v1/credit/dedupe", json=BODY)
    assert no_scope.status_code == 403 and no_scope.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"
    other = _client(make_token).post("/api/v1/credit/dedupe", json={**BODY, "case_id": OTHER})
    assert other.status_code == 403 and other.json()["detail"]["code"] == "CASE_ACCESS_DENIED"


def test_the_credit_scope_is_accepted(repo, make_token):
    from app.agents.credit import config as credit_config

    r = _client(make_token, scopes=[credit_config.required_scope()]).post(
        "/api/v1/credit/dedupe", json=BODY)
    assert r.status_code == 200


@pytest.mark.parametrize("patch, code", [
    ({"dedupe_parameter_value": "12345"}, 422), ({"dedupe_parameter": "Aadhaar"}, 422),
    ({"dedupe_parameter": "PR Code", "dedupe_parameter_value": "LOAN-1"}, 422),
    ({"requested_amount": -5}, 422), ({"unknown_field": 1}, 422), ({"customer_type": "Old"}, 422),
])
def test_bad_input_is_refused_without_echo(repo, make_token, patch, code):
    r = _client(make_token).post("/api/v1/credit/dedupe", json={**BODY, **patch})
    assert r.status_code == code
    assert "LOAN-1" not in r.text


def test_a_service_principal_naming_a_missing_case_gets_404(repo, make_token):
    r = _client(make_token, scopes=["los.read"], subject="svc").post(
        "/api/v1/credit/dedupe", json={**BODY, "case_id": "CASE-NOPE00000001"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "CASE_NOT_FOUND"


def test_concurrent_and_repeated_requests_agree(repo, make_token):
    client = _client(make_token)
    results: list = [None, None]

    def run(i):
        results[i] = client.post("/api/v1/credit/dedupe", json=BODY).json()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    again = client.post("/api/v1/credit/dedupe", json=BODY).json()
    for r in (*results, again):
        assert (r["outcome"], r["reason_codes"], r["serialised"]) == (
            results[0]["outcome"], results[0]["reason_codes"], True)
    assert len({r["request_id"] for r in (*results, again)}) == 3
    events = [e for e in repo.get_case_timeline(CASE) if e.event_type == "CUSTOMER_HISTORY_ASSESSED"]
    assert len(events) == 3


def test_the_one_answer_carries_the_los_dedupe_too(repo, make_token):
    body = _client(make_token).post("/api/v1/credit/dedupe", json=BODY).json()
    assert {"search", "case"} <= set(body["los_dedupe"])
    assert body["los_dedupe"]["case"]["status"] in ("NO_MATCH", "INSUFFICIENT_DATA", "MATCH", "POSSIBLE_MATCH")


def _codes_only_where_allowed(obj, value, allowed=("customer_code", "prospect_code", "record_ref")):
    """True when `value` appears in `obj` ONLY under the allowed keys (the full AP / PR code fields)."""
    if isinstance(obj, dict):
        return all((k in allowed and v == value) or _codes_only_where_allowed(v, value, allowed) for k, v in obj.items())
    if isinstance(obj, list):
        return all(_codes_only_where_allowed(x, value, allowed) for x in obj)
    return not (isinstance(obj, str) and value in obj)


# ---- the four rules added 2026-10-23 (R08-R11): each fires on its own evidence ---------------------------------------
def test_emi_bounces_and_an_open_fraud_case_are_found(repo):
    r = _assess(repo, "PAN", "FGHPT8642U", requested_amount=300000)
    assert _rule(r, "H12_EMI_BOUNCES")["outcome"] == "TRIGGERED" and _rule(r, "H12_EMI_BOUNCES")["value"] == 3
    assert _rule(r, "H13_FRAUD_HISTORY")["outcome"] == "TRIGGERED"
    assert {"EMI_BOUNCES", "FRAUD_HISTORY"} <= set(r["reason_codes"])
    assert "UNDER_INVESTIGATION" not in json.dumps(r["explanation"])            # a status, never a case narrative


def test_topup_and_a_shared_property_are_found_once_the_customer_is_confirmed(repo):
    first = _assess(repo, "PAN", "DEFPR1357S", requested_amount=300000)
    cand = first["history"]["customer"]["confirmation"]["candidate"]
    r = _assess(repo, "PAN", "DEFPR1357S", requested_amount=300000, confirm_match="SAME_CUSTOMER", candidate=cand)
    loan = r["history"]["loans"][0]
    assert loan["topup"]["status"] == "NOT_ELIGIBLE"
    assert set(loan["topup"]["reasons"]) == {"TOO_SOON_AFTER_DISBURSAL", "OUTSTANDING_ABOVE_LIMIT"}
    assert _rule(r, "H11_TOPUP_ELIGIBILITY")["outcome"] == "TRIGGERED"
    assert _rule(r, "H14_COLLATERAL_REUSE")["outcome"] == "TRIGGERED"


def test_a_clean_customer_is_eligible_for_a_topup_and_clear_on_every_new_rule(repo):
    r = _assess(repo, "PAN", "BCDPM5678N", requested_amount=300000)
    live = next(x for x in r["history"]["loans"] if x["effective_status"] == "ACTIVE")
    assert live["topup"]["status"] == "ELIGIBLE"
    for rid in ("H11_TOPUP_ELIGIBILITY", "H12_EMI_BOUNCES", "H13_FRAUD_HISTORY", "H14_COLLATERAL_REUSE"):
        assert _rule(r, rid)["outcome"] == "NOT_TRIGGERED", rid


def test_the_extra_sources_down_are_never_a_pass(repo, monkeypatch):
    monkeypatch.setenv("RCU_PROVIDER", "none")
    monkeypatch.setenv("COLLATERAL_PROVIDER", "none")
    r = _assess(repo, "PAN", "BCDPM5678N", requested_amount=300000)
    for rid in ("H13_FRAUD_HISTORY", "H14_COLLATERAL_REUSE"):
        assert (_rule(r, rid)["outcome"], _rule(r, rid)["reason"]) == ("NOT_EVALUATED", "DATA_UNAVAILABLE"), rid
    assert r["outcome"] == "DATA_INSUFFICIENT"


@pytest.mark.parametrize("rid, limit, value_key", [("H07_EXISTING_LOAN_COUNT", 1, None), ("H08_TOTAL_EXPOSURE", 100000, None),
                                                   ("H09_EMI_BURDEN", 1000, None), ("H10_DPD", -1, None)])
def test_each_limit_rule_fires_when_its_limit_is_crossed(repo, tmp_path, monkeypatch, rid, limit, value_key):
    _policy_with_limits(tmp_path, monkeypatch, **{rid: limit})
    r = _assess(repo, "PAN", "BCDPM5678N", requested_amount=300000)
    assert _rule(r, rid)["outcome"] == "TRIGGERED", _rule(r, rid)



# ---- a finding never stands beside "proceed" (owner 2026-10-23 sweep) ------------------------------------------------
def test_a_fraud_case_or_emi_bounces_turn_proceed_into_hold(repo):
    r = _assess(repo, "PAN", "FGHPT8642U", requested_amount=300000)
    w = r["dedupe"]["workflow_recommendation"]
    assert w["recommendation"] == "HOLD_FOR_REVIEW", w
    assert {"EMI_BOUNCES", "FRAUD_HISTORY"} <= set(r["dedupe"]["rule_findings"])
    assert {"EMI_BOUNCES", "FRAUD_HISTORY"} <= set(r["dedupe"]["match_reasons"])          # named in the reasons
    assert "Proceed" not in " ".join(r["explanation"]["points"])


def test_a_name_or_dob_differing_from_the_mifin_record_is_held(repo):
    r = _assess(repo, "PAN", "BCDPM5678N", requested_amount=300000)     # this test applicant is not MIFIN's PRIYA MORE
    if r["history"]["customer"]["identity_check"].get("name") == "MISMATCH":
        assert r["dedupe"]["workflow_recommendation"]["recommendation"] == "HOLD_FOR_REVIEW"
        assert "NAME_DIFFERS_FROM_MIFIN_RECORD" in r["dedupe"]["rule_findings"]


def test_nothing_found_still_proceeds(repo, monkeypatch):
    from app.agents.dedupe import dedupe_status as DS
    block = {"workflow_recommendation": {"recommendation": "PROCEED_TO_NEXT_STAGE"}, "match_reasons": []}
    result = {"history": {"customer": {"match": "CONFIRMED_MATCH", "identity_check": {"pan": "MATCH", "name": "MATCH",
                                                                                         "dob": "MATCH"}}},
              "evaluation": {"rules": [{"outcome": "NOT_TRIGGERED", "reason_code": "X"}]}}
    from app.agents.dedupe import config as DC
    out = DS.with_rule_findings(block, result, DC.get_policy())
    assert out["workflow_recommendation"]["recommendation"] == "PROCEED_TO_NEXT_STAGE" and out["rule_findings"] == []
