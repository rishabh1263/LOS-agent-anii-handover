"""
Dedupe agent (Phase A): PAN / mobile matching over the LOS case store, AP / PR codes reported not stored.

SYNTHETIC DATA ONLY: generated PANs and mobiles, invented names; nothing from a real customer.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from app.agents.dedupe import config as C
from app.agents.dedupe import identity as I
from app.agents.dedupe import service as S
from app.core.exceptions import ConfigurationError
from app.store.models import Applicant, Application, CaseFinding, Document, FindingKind
from app.store.testing import fresh_repository

PAN_A, PAN_B = "ABCPD1234K", "XYZPQ9876L"
MOBILE = "9876501234"


@pytest.fixture(autouse=True)
def _audit_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("DEDUPE_AUDIT_PATH", str(tmp_path / "dedupe.jsonl"))
    C.reload()
    yield
    C.reload()


@pytest.fixture
def repo():
    r = fresh_repository()
    r.initialise()
    return r


def _at(minutes: int = 0) -> datetime:
    return datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def _case(repo, case_id, applicant_id, *, pan=None, mobile=None, product="PERSONAL_LOAN", minutes=0,
          co_applicant_id=None, co_pan=None):
    repo.save_applicant(Applicant(applicant_id=applicant_id, full_name="Test Person", mobile=mobile))
    repo.save_application(Application(case_id=case_id, applicant_id=applicant_id, product=product,
                                      co_applicant_id=co_applicant_id, created_at=_at(minutes)))
    if pan:
        _extraction(repo, case_id, applicant_id, pan)
    if co_applicant_id and co_pan:
        _extraction(repo, case_id, co_applicant_id, co_pan)


def _extraction(repo, case_id, party_id, pan):
    repo.save_finding(CaseFinding(finding_id=uuid.uuid4().hex, case_id=case_id, party_id=party_id,
                                  finding_kind=FindingKind.EXTRACTION, status="PASS",
                                  payload={"pan_number": pan, "name": "TEST PERSON"},
                                  source_id=f"pan-{party_id}.jpg", document_id=f"{case_id}:{party_id}:pan"))


def _rule(result, rule_id):
    return next(r for r in result["rules"] if r["rule_id"] == rule_id)


def _scenario(result, sid):
    return next(s for s in result["scenarios"] if s["scenario"] == sid)


# ---- identity / HMAC ---------------------------------------------------------------------------------------------
def test_tokens_are_keyed_typed_and_never_the_plain_value():
    t = I.token("PAN", I.pan(PAN_A))
    assert t.startswith("dev1:") and PAN_A not in t
    assert I.token("PAN", "ABCPD1234K") != I.token("MOBILE", "ABCPD1234K")
    assert I.pan(" abcpd 1234k ") == PAN_A and I.pan("ABCPD12345") is None
    assert I.mobile("+91 98765-01234") == MOBILE and I.mobile("1234567890") is None


def test_rotation_matches_a_token_written_under_the_old_key(monkeypatch):
    monkeypatch.setenv(I.ENV_KEY, "old:" + "a" * 32)
    written = I.token("PAN", PAN_A)
    monkeypatch.setenv(I.ENV_KEY, "new:" + "b" * 32 + ",old:" + "a" * 32)
    assert written in I.tokens("PAN", PAN_A) and I.token("PAN", PAN_A).startswith("new:")


def test_production_refuses_to_run_without_the_hmac_key(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv(I.ENV_KEY, raising=False)
    with pytest.raises(I.IdentityKeyError):
        I.token("PAN", PAN_A)


# ---- policy configuration ----------------------------------------------------------------------------------------
def _policy_copy(tmp_path, edit):
    data = yaml.safe_load(C.DEFAULT_PATH.read_text(encoding="utf-8"))
    edit(data)
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_the_shipped_policy_loads_with_the_four_parameters():
    policy = C.load_policy()
    assert sorted(policy["parameters"]) == sorted(["PAN", "MOBILE", "AP_CODE", "PR_CODE"])
    assert policy["parameters"]["AP_CODE"]["field"] is None and policy["parameters"]["PR_CODE"]["field"] is None


@pytest.mark.parametrize("edit, message", [
    (lambda d: d["rules"][0].update(inputs=["loans.active"]), "unknown signal"),
    (lambda d: d["parameters"]["AP_CODE"].update(field="applicants.mobile"), "AP_CODE.field"),
    (lambda d: d["rules"].append(dict(d["rules"][0])), "Duplicate"),
    (lambda d: d["recommendation_precedence"].pop(), "recommendation_precedence"),
    (lambda d: d["parameters"].pop("PR_CODE"), "parameters must be exactly"),
])
def test_a_broken_policy_is_refused_by_name(tmp_path, edit, message):
    with pytest.raises(ConfigurationError, match=message):
        C.load_policy(_policy_copy(tmp_path, edit))


def test_an_unsigned_policy_is_refused_in_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(ConfigurationError, match="not signed off"):
        C.load_policy()
    monkeypatch.setenv("ALLOW_UNSIGNED_DEDUPE_POLICY", "true")
    assert C.load_policy()["signed_off"] is False


# ---- scenarios ---------------------------------------------------------------------------------------------------
def test_a_new_customer_is_no_match_and_continues_onboarding(repo):
    _case(repo, "C-NEW", "APP-NEW", pan=PAN_A, mobile=MOBILE)
    _case(repo, "C-OTHER", "APP-OTHER", pan=PAN_B, mobile="9123400000")
    r = S.assess("C-NEW", repository=repo)
    assert r["status"] == "NO_MATCH"
    assert r["recommendation"] == "CONTINUE_STANDARD_ONBOARDING"
    assert _scenario(r, "S01_NEW_CUSTOMER_CANDIDATE")["status"] == "DETECTED"
    assert r["dedupe_result"]["matched_parameters"] == []


def test_the_same_pan_on_another_customer_record_is_an_identity_conflict(repo):
    _case(repo, "C-1", "APP-1", pan=PAN_A, minutes=0)
    _case(repo, "C-2", "APP-2", pan=PAN_A, minutes=60)
    r = S.assess("C-2", repository=repo)
    assert r["status"] == "MATCH"
    [m] = r["parties"][0]["matches"]
    assert m["relation"] == "OTHER_CUSTOMER_RECORD" and m["matched_fields"] == ["PAN"]
    assert m["confidence"] == "HIGH" and m["reason_codes"] == ["PAN_EXACT"]
    assert _rule(r, "P01_IDENTITY_UNIQUENESS")["outcome"] == "TRIGGERED"
    assert r["recommendation"] == "IDENTITY_CONFLICT_REVIEW"
    assert r["dedupe_result"]["matched_parameters"] == ["pan"]
    assert r["dedupe_result"]["matched_customer_ids"] == ["APP-1"]


def test_the_customers_own_other_application_is_existing_customer_not_a_conflict(repo):
    _case(repo, "C-1", "APP-1", pan=PAN_A, minutes=0)
    repo.save_application(Application(case_id="C-1B", applicant_id="APP-1", product="PERSONAL_LOAN",
                                      created_at=_at(30)))
    _extraction(repo, "C-1B", "APP-1", PAN_A)
    r = S.assess("C-1B", repository=repo)
    assert _scenario(r, "S02_EXISTING_CUSTOMER")["status"] == "DETECTED"
    assert _scenario(r, "S10_SIMULTANEOUS_APPLICATION")["status"] == "DETECTED"
    assert _rule(r, "P05_SAME_PRODUCT_APPLICATION")["outcome"] == "TRIGGERED"
    # the other application is 30 minutes old: inside the duplicate window (dedupe_policy.yaml P05, 720 h), outside
    # the concurrent-create window (P15, 60 s) -- a duplicate application, which outranks "simultaneous"
    assert _rule(r, "P05_DUPLICATE_WINDOW")["outcome"] == "TRIGGERED"
    assert _rule(r, "P15_CONCURRENT_CREATE")["outcome"] == "NOT_TRIGGERED"
    assert r["recommendation"] == "DUPLICATE_APPLICATION"
    assert "SIMULTANEOUS_APPLICATION_REVIEW" in r["recommendations"]
    assert "LINK_EXISTING_CUSTOMER" in r["recommendations"]
    assert r["applications"]["hours_since_previous_same_product"] == 0.5
    assert r["dedupe_result"]["matched_parameters"] == []            # the customer's own case is not a dedupe hit


def test_a_different_product_is_not_a_simultaneous_application(repo):
    _case(repo, "C-1", "APP-1", pan=PAN_A, product="PERSONAL_LOAN")
    repo.save_application(Application(case_id="C-HL", applicant_id="APP-1", product="HOME_LOAN", created_at=_at(5)))
    _extraction(repo, "C-HL", "APP-1", PAN_A)
    r = S.assess("C-HL", repository=repo)
    assert _scenario(r, "S10_SIMULTANEOUS_APPLICATION")["status"] == "NOT_DETECTED"
    assert r["recommendation"] == "LINK_EXISTING_CUSTOMER"


def test_a_shared_mobile_with_a_different_pan_is_a_family_signal_not_a_match(repo):
    _case(repo, "C-1", "APP-1", pan=PAN_A, mobile=MOBILE)
    _case(repo, "C-2", "APP-2", pan=PAN_B, mobile=MOBILE)
    r = S.assess("C-2", repository=repo)
    assert r["status"] == "NO_MATCH"
    assert r["shared_signals"]["mobile_shared_with_other_customer_records"] == 1
    assert r["dedupe_result"]["matched_parameters"] == []           # report_shared_mobile: false
    assert _rule(r, "P01_POSSIBLE_MATCH")["outcome"] == "NOT_TRIGGERED"


def test_a_shared_mobile_with_no_pan_to_compare_is_only_a_possible_match(repo):
    _case(repo, "C-1", "APP-1", mobile=MOBILE)
    _case(repo, "C-2", "APP-2", mobile=MOBILE)
    r = S.assess("C-2", repository=repo)
    assert r["status"] == "POSSIBLE_MATCH"
    [m] = r["parties"][0]["matches"]
    assert m["confidence"] == "LOW" and "NO_PAN_TO_COMPARE" in m["reason_codes"]
    assert r["recommendation"] == "POSSIBLE_MATCH_REVIEW"
    assert r["dedupe_result"]["matched_parameters"] == []


def test_a_role_swap_is_reported_and_is_not_a_dedupe_hit(repo):
    # the person is the primary on C-1 and the co-applicant on C-2 (another customer's case)
    _case(repo, "C-1", "APP-1", pan=PAN_A)
    _case(repo, "C-2", "APP-2", pan=PAN_B, co_applicant_id="COAPP-2", co_pan=PAN_A)
    r = S.assess("C-2", repository=repo)
    co = next(p for p in r["parties"] if p["role"] == "CO_APPLICANT")
    [m] = co["matches"]
    assert m["status"] == "MATCH" and m["role_change"] is True and m["matched_role"] == "PRIMARY_APPLICANT"
    assert _rule(r, "P02_ROLE_HISTORY")["outcome"] == "TRIGGERED"
    assert _scenario(r, "S09_ROLE_CHANGE")["status"] == "DETECTED"
    assert _rule(r, "P01_IDENTITY_UNIQUENESS")["outcome"] == "NOT_TRIGGERED"
    assert r["dedupe_result"]["matched_parameters"] == []


def test_no_pan_and_no_mobile_is_insufficient_data_never_clean(repo):
    _case(repo, "C-1", "APP-1")
    repo.save_application(Application(case_id="C-1B", applicant_id="APP-1", product="PERSONAL_LOAN",
                                      created_at=_at(10)))
    r = S.assess("C-1", repository=repo)
    assert r["status"] == "INSUFFICIENT_DATA"
    assert r["recommendation"] == "DATA_UNAVAILABLE"
    # what the store says structurally is still reported beside it: the record's other same-product case
    assert "SIMULTANEOUS_APPLICATION_REVIEW" in r["recommendations"]
    assert _scenario(r, "S01_NEW_CUSTOMER_CANDIDATE")["status"] == "NOT_DETECTED"
    assert S.fraud_dedupe("C-1", repository=repo) is None             # the fraud agent keeps its data gap


def test_sources_the_los_lacks_are_not_evaluated_in_every_response(repo):
    _case(repo, "C-1", "APP-1", pan=PAN_A)
    r = S.assess("C-1", repository=repo)
    for rid in ("P03_ACTIVE_LOANS_EXPOSURE", "P04_REPAYMENT_HISTORY", "P11_TOP_UP", "P07_FRAUD_CASE_OUTCOMES"):
        assert (_rule(r, rid)["outcome"], _rule(r, rid)["reason"]) == ("NOT_EVALUATED", "DATA_UNAVAILABLE")
    for sid in ("S04_TOP_UP", "S07_HISTORICAL_DEFAULT", "S18_FRAUD_CASE_CLEARED"):
        assert _scenario(r, sid)["status"] == "NOT_EVALUATED"
    unavailable = {s["source"] for s in r["coverage"]["sources"] if not s["available"]}
    assert {"LOAN_MANAGEMENT_SYSTEM", "CREDIT_BUREAU", "RCU_CASE_SYSTEM"} <= unavailable
    params = {p["parameter"]: p for p in r["parameters"]}
    assert params["AP_CODE"]["status"] == params["PR_CODE"]["status"] == "INSUFFICIENT_DATA"


def test_a_scan_bound_reached_is_partial_analysis(repo, tmp_path, monkeypatch):
    for i in range(3):
        _case(repo, f"C-{i}", f"APP-{i}", pan=f"ABCPD12{i}0K", minutes=i)
    path = _policy_copy(tmp_path, lambda d: d["limits"].update(max_scan_cases=1))
    monkeypatch.setenv("DEDUPE_POLICY_PATH", str(path))
    C.reload()
    r = S.assess("C-0", repository=repo)
    assert r["coverage"]["truncated"] is True and r["coverage"]["partial"] is True
    assert _rule(r, "P13_DATA_COMPLETENESS")["outcome"] == "TRIGGERED"
    assert "PARTIAL_ANALYSIS" in r["recommendations"]


def test_an_identical_file_on_another_customers_case_is_document_reuse(repo):
    from app.store import documents as store_module
    from app.store.documents import get_document_store

    _case(repo, "C-1", "APP-1", pan=PAN_A)
    _case(repo, "C-2", "APP-2", pan=PAN_B)
    content = b"%PDF-1.4 synthetic salary slip " + uuid.uuid4().bytes
    for case_id, applicant_id in (("C-1", "APP-1"), ("C-2", "APP-2")):
        doc_id = f"{case_id}:{applicant_id}:slip.pdf"
        repo.save_document(Document(document_id=doc_id, case_id=case_id, applicant_id=applicant_id,
                                    document_type="SALARY_SLIP", party_id=applicant_id, source_id="slip.pdf"))
        get_document_store().put(store_module.storage_key(doc_id), content)
    r = S.assess("C-2", repository=repo)
    assert r["documents"]["reused_on_other_customers_cases"] == 1
    assert _rule(r, "P06_DOCUMENT_REUSE")["outcome"] == "TRIGGERED"
    assert _scenario(r, "S12_DOCUMENT_REUSE")["status"] == "DETECTED"


# ---- isolation and masking ---------------------------------------------------------------------------------------
def test_no_response_carries_a_pan_a_mobile_or_a_token(repo):
    _case(repo, "C-1", "APP-1", pan=PAN_A, mobile=MOBILE)
    _case(repo, "C-2", "APP-2", pan=PAN_A, mobile=MOBILE)
    text = json.dumps(S.assess("C-2", repository=repo)) + json.dumps(S.search("PAN", PAN_A, repository=repo))
    for secret in (PAN_A, MOBILE, "dev1:"):
        assert secret not in text


def test_a_matched_case_the_caller_cannot_open_is_redacted(repo):
    _case(repo, "C-THEIRS", "APP-THEIRS", pan=PAN_A)
    _case(repo, "C-MINE", "APP-MINE", pan=PAN_A)
    r = S.assess("C-MINE", repository=repo, can_view_case=lambda cid: cid == "C-MINE")
    [m] = r["parties"][0]["matches"]
    assert (m["case_id"], m["customer_record"], m["visible"]) == ("REDACTED", "REDACTED", False)
    assert "C-THEIRS" not in json.dumps(r["parties"])
    s = S.search("PAN", PAN_A, repository=repo, can_view_case=lambda cid: cid == "C-MINE")
    assert {m["case_id"] for m in s["matches"]} == {"C-MINE", "REDACTED"}


# ---- the dropdown lookup -----------------------------------------------------------------------------------------
def test_search_by_pan_and_mobile(repo):
    _case(repo, "C-1", "APP-1", pan=PAN_A, mobile=MOBILE)
    _case(repo, "C-2", "APP-2", mobile=MOBILE)
    assert [m["case_id"] for m in S.search("PAN", PAN_A.lower(), repository=repo)["matches"]] == ["C-1"]
    by_mobile = S.search("MOBILE", "+91" + MOBILE, repository=repo)
    assert by_mobile["status"] == "POSSIBLE_MATCH" and len(by_mobile["matches"]) == 2
    assert S.search("PAN", PAN_B, repository=repo)["status"] == "NO_MATCH"


@pytest.mark.parametrize("parameter", ["AP_CODE", "PR_CODE"])
def test_ap_and_pr_codes_are_not_stored_so_insufficient_data(repo, parameter):
    r = S.search(parameter, {"AP_CODE": "AP00731942", "PR_CODE": "PR02184467"}[parameter], repository=repo)
    assert r["status"] == "INSUFFICIENT_DATA" and r["reason_codes"] == ["PARAMETER_NOT_STORED_BY_LOS"]


def test_search_refuses_a_malformed_value(repo):
    with pytest.raises(S.InvalidParameter):
        S.search("PAN", "NOT-A-PAN", repository=repo)


# ---- concurrency -------------------------------------------------------------------------------------------------
def test_assessments_of_the_same_customer_are_serialised(repo):
    waited: list[float] = []

    def second():
        t0 = time.perf_counter()
        with S.serialised(repo, ["dedupe:app:APP-1"]):
            waited.append(time.perf_counter() - t0)

    with S.serialised(repo, ["dedupe:app:APP-1"]) as locked:
        assert locked is True
        worker = threading.Thread(target=second)
        worker.start()
        time.sleep(0.6)
    worker.join(10)
    assert waited and waited[0] >= 0.5


def test_simultaneous_creates_are_both_seen_by_concurrent_assessments(repo):
    _case(repo, "C-A", "APP-1", pan=PAN_A, minutes=0)
    repo.save_application(Application(case_id="C-B", applicant_id="APP-1", product="PERSONAL_LOAN",
                                      created_at=_at(0) + timedelta(seconds=2)))
    results: dict[str, dict] = {}

    def run(cid):
        results[cid] = S.assess(cid, repository=repo)

    threads = [threading.Thread(target=run, args=(cid,)) for cid in ("C-A", "C-B")]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    for cid in ("C-A", "C-B"):
        assert results[cid]["applications"]["same_product_other_cases"] == 1
        assert results[cid]["coverage"]["serialised"] is True
    assert results["C-B"]["rules"][0]["policy_version"] == C.get_policy()["policy_version"]


# ---- the fraud agent -------------------------------------------------------------------------------------------
def test_the_fraud_agent_reads_the_dedupe_result_through_its_existing_contract(repo):
    from app.agents.fraud_risk.agent import FraudRiskAgent
    from app.agents.fraud_risk.schemas import FraudRiskRequest

    _case(repo, "C-1", "APP-1", pan=PAN_A)
    _case(repo, "C-2", "APP-2", pan=PAN_A)
    dedupe = S.fraud_dedupe("C-2", repository=repo)
    assert dedupe.matched_parameters == ["pan"] and dedupe.matched_customer_ids == ["APP-1"]
    response = FraudRiskAgent(use_llm=False).assess(FraudRiskRequest(dedupe=dedupe))
    flag = next(f for f in response.flags if f.rule == "DEDUPE_MATCH")
    assert flag.severity == "CRITICAL"
    assert not any(g.rule == "DEDUPE_MATCH" for g in response.data_gaps)


def test_a_clean_dedupe_closes_the_fraud_agents_data_gap_without_a_flag(repo):
    from app.agents.fraud_risk.agent import FraudRiskAgent
    from app.agents.fraud_risk.schemas import FraudRiskRequest

    _case(repo, "C-1", "APP-1", pan=PAN_A)
    before = FraudRiskAgent(use_llm=False).assess(FraudRiskRequest())
    assert any(g.rule == "DEDUPE_MATCH" for g in before.data_gaps)          # unchanged when no dedupe is given
    after = FraudRiskAgent(use_llm=False).assess(FraudRiskRequest(dedupe=S.fraud_dedupe("C-1", repository=repo)))
    assert not any(f.rule == "DEDUPE_MATCH" for f in after.flags)
    assert not any(g.rule == "DEDUPE_MATCH" for g in after.data_gaps)


def test_the_assessment_is_audited_without_identifiers(repo, tmp_path):
    _case(repo, "C-1", "APP-1", pan=PAN_A, mobile=MOBILE)
    S.assess("C-1", repository=repo)
    line = (tmp_path / "dedupe.jsonl").read_text(encoding="utf-8")
    assert '"case_id": "C-1"' in line and PAN_A not in line and MOBILE not in line and "dev1:" not in line
    assert any(e.event_type == "DEDUPE_ASSESSED" for e in repo.get_case_timeline("C-1"))


# ---- the form's labels and Customer Type -------------------------------------------------------------------------
@pytest.mark.parametrize("label, parameter", [("Mobile No", "MOBILE"), ("PAN No", "PAN"), ("AP Code", "AP_CODE"),
                                              ("Customer ID", "AP_CODE"), ("PR Code", "PR_CODE"),
                                              ("Loan Account No", "PR_CODE"), ("mobile_no", "MOBILE")])
def test_dropdown_labels_resolve_to_the_four_parameters(label, parameter):
    assert S.resolve(label) == parameter


def test_an_unknown_dropdown_label_is_refused():
    with pytest.raises(S.InvalidParameter):
        S.resolve("Aadhaar")


@pytest.mark.parametrize("declared, status, result", [
    ("NEW", "NO_MATCH", "CONSISTENT"), ("NEW", "MATCH", "DECLARED_NEW_BUT_FOUND"),
    ("NEW", "POSSIBLE_MATCH", "DECLARED_NEW_POSSIBLE_MATCH"), ("EXISTING", "MATCH", "CONSISTENT"),
    ("EXISTING", "NO_MATCH", "EXISTING_NOT_FOUND_IN_LOS"), ("EXISTING", "INSUFFICIENT_DATA", "NOT_VERIFIABLE"),
])
def test_customer_type_is_checked_against_what_the_lookup_found(declared, status, result):
    assert S.customer_type_check(declared, status)["result"] == result


# ---- release-readiness additions (2026-10-22) ----------------------------------------------------------------------
def test_a_primary_who_was_a_co_applicant_elsewhere_is_a_role_swap_the_other_way(repo):
    _case(repo, "C-1", "APP-1", pan=PAN_B, co_applicant_id="COAPP-1", co_pan=PAN_A)
    _case(repo, "C-2", "APP-2", pan=PAN_A)
    r = S.assess("C-2", repository=repo)
    [m] = r["parties"][0]["matches"]
    assert (m["status"], m["role_change"], m["matched_role"]) == ("MATCH", True, "CO_APPLICANT")
    assert _rule(r, "P02_ROLE_HISTORY")["outcome"] == "TRIGGERED"
    assert _rule(r, "P01_IDENTITY_UNIQUENESS")["outcome"] == "NOT_TRIGGERED"
    assert r["dedupe_result"]["matched_parameters"] == []


def test_the_same_co_applicant_on_two_customers_cases_is_a_match_without_a_conflict(repo):
    _case(repo, "C-1", "APP-1", pan=PAN_B, co_applicant_id="COAPP-1", co_pan=PAN_A)
    _case(repo, "C-2", "APP-2", pan="LMNOP4321Q", co_applicant_id="COAPP-2", co_pan=PAN_A)
    r = S.assess("C-2", repository=repo)
    co = next(p for p in r["parties"] if p["role"] == "CO_APPLICANT")
    [m] = co["matches"]
    assert (m["status"], m["role_change"], m["relation"]) == ("MATCH", False, "OTHER_CUSTOMER_RECORD")
    assert _rule(r, "P01_IDENTITY_UNIQUENESS")["outcome"] == "NOT_TRIGGERED"   # a co-applicant is not a customer record


def test_a_co_applicants_pan_never_appears_in_the_response(repo):
    _case(repo, "C-1", "APP-1", pan=PAN_B, co_applicant_id="COAPP-1", co_pan=PAN_A)
    _case(repo, "C-2", "APP-2", pan=PAN_A)
    text = json.dumps(S.assess("C-2", repository=repo))
    assert PAN_A not in text and PAN_B not in text and "dev1:" not in text


def test_a_window_with_no_earlier_application_is_not_evaluated_never_clean(repo):
    _case(repo, "C-NEW", "APP-NEW", pan=PAN_A, mobile=MOBILE)
    r = S.assess("C-NEW", repository=repo)
    assert r["status"] == "NO_MATCH"
    assert r["completeness"]["complete"] is False
    reasons = {x["rule_id"]: x["reason"] for x in r["completeness"]["not_evaluated"]}
    # configured windows (dedupe_policy.yaml, DEMO_DUMMY), but nothing earlier to measure against
    assert reasons["P05_DUPLICATE_WINDOW"] == "DATA_UNAVAILABLE"
    assert reasons["P15_CONCURRENT_CREATE"] == "DATA_UNAVAILABLE"
    assert reasons["P04_REPAYMENT_HISTORY"] == "DATA_UNAVAILABLE"
    thresholds = {r["rule_id"]: r["threshold"] for r in C.get_policy()["rules"]}
    assert (thresholds["P05_DUPLICATE_WINDOW"], thresholds["P15_CONCURRENT_CREATE"]) == (720, 60)


def test_startup_refuses_production_without_the_hmac_key(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv(I.ENV_KEY, raising=False)
    with pytest.raises(I.IdentityKeyError, match=I.ENV_KEY):
        I.require_in_production()                 # the call main.lifespan makes before serving
    monkeypatch.setenv(I.ENV_KEY, "k1:" + "s" * 32)
    I.require_in_production()


def test_a_malformed_hmac_key_is_refused(monkeypatch):
    monkeypatch.setenv(I.ENV_KEY, "no-separator-here")
    with pytest.raises(I.IdentityKeyError):
        I.token("PAN", PAN_A)


def test_a_failing_assessment_falls_back_to_the_fraud_agents_old_data_gap(repo, monkeypatch):
    from app.agents.fraud_risk.agent import FraudRiskAgent
    from app.agents.fraud_risk.schemas import FraudRiskRequest

    def boom(*_a, **_k):
        raise RuntimeError("store down")

    monkeypatch.setattr(S, "assess", boom)
    assert S.fraud_dedupe("C-ANY", repository=repo) is None
    response = FraudRiskAgent(use_llm=False).assess(FraudRiskRequest(dedupe=None))
    assert any(g.rule == "DEDUPE_MATCH" for g in response.data_gaps)


async def test_the_los_flow_hands_released_pans_to_the_dedupe_and_survives_its_failure(repo, monkeypatch):
    from types import SimpleNamespace

    from app.agents.los import flow

    seen: dict = {}

    def fake(case_id, *, current_identifiers=None, repository=None):
        seen.update(case_id=case_id, current=current_identifiers)
        return "DEDUPE"

    monkeypatch.setattr(flow, "_released_for_matching",
                        lambda owned: [{"extraction": {"fields": {"pan_number": d}}} for d in owned])
    monkeypatch.setattr(S, "fraud_dedupe", fake)
    party = SimpleNamespace(party_id="APP-1")
    assert await flow._dedupe_for("C-1", [(party, [PAN_A])]) == "DEDUPE"
    assert seen == {"case_id": "C-1", "current": {"APP-1": {"pan_number": PAN_A}}}

    def broken(*_a, **_k):
        raise RuntimeError("dedupe down")

    monkeypatch.setattr(S, "fraud_dedupe", broken)
    assert await flow._dedupe_for("C-1", [(party, [PAN_A])]) is None
    monkeypatch.setenv("DEDUPE_ENABLED", "false")
    monkeypatch.setattr(S, "fraud_dedupe", fake)
    assert await flow._dedupe_for("C-1", [(party, [PAN_A])]) is None


@pytest.mark.parametrize("parameter, value", [("AP_CODE", "AP0001234"), ("PR_CODE", "LOAN-1"), ("AP Code", "UAT0001")])
def test_a_malformed_ap_or_pr_code_is_refused(repo, parameter, value):
    with pytest.raises(S.InvalidParameter):
        S.search(parameter, value, repository=repo)
