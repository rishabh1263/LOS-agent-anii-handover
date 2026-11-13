"""
Credit Document Policy Agent (app/agents/docs_verify_credit_agent) -- the ten rules, the API, security, invariants
and the Credit Underwriting Agent integration.

Real PostgreSQL case store (app.store.testing); deviations raised and decided through the real deviation workflow
(app/agents/los/queries.py). The policy is the shipped SAMPLE policy unless a test writes its own policy directory.
"""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.agents.docs_verify_credit_agent.config.policy_loader import EvaluationError
from app.agents.docs_verify_credit_agent.services import audit_service, readiness_service
from app.store import set_repository
from app.store.models import (Applicant, Application, CaseEvent, CaseStage, Document, DocumentStatus, FindingKind,
                              StageTransition)
from app.store.testing import fresh_repository

CASE, APP, CO = "CASE-DVC", "APP-DVC", "APP-DVC-CO"
OFFICER, SCOPE = "credit-officer", "los.credit.underwrite"
URL = f"/api/v1/credit/document-readiness/{CASE}"
POLICIES = Path(__file__).resolve().parents[2] / "app/agents/docs_verify_credit_agent/policies"
TODAY = datetime.now(timezone.utc).date()
NOW = datetime.now(timezone.utc)
OPEN_STATUSES = {"ACTION_REQUIRED", "PENDING_VERIFICATION", "UNDER_REVIEW", "MISMATCH", "UNRESOLVED",
                 "EXCEPTION_PENDING"}


# ==========================================================================
# fixtures and builders
# ==========================================================================

@pytest.fixture(autouse=True)
def store(monkeypatch):
    repository = fresh_repository()
    repository.initialise()
    set_repository(repository)
    for name in ("DOCS_CREDIT_POLICY_DIR", "DOCS_CREDIT_ALLOW_SAMPLE_POLICY", "DOCS_CREDIT_REQUIRE_AUDIT",
                 "DOCS_CREDIT_TIMEOUT_SECONDS", "DOCS_CREDIT_SCOPES", "DOCS_CREDIT_STAGE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DOCS_CREDIT_AI_SUMMARY", "false")         # the rules note; the AI note has its own tests
    yield repository
    set_repository(None)


def set_stage(repository, stage="CREDIT", n=1):
    repository.apply_stage_transition(
        n - 1, CaseStage(case_id=CASE, stage=stage, stage_status="IN_PROGRESS", version=n),
        StageTransition(transition_id=f"STG-{CASE}-{stage}-{n}", case_id=CASE, version=n, kind="STAGE_ENTERED",
                        to_stage=stage, to_status="IN_PROGRESS"),
        CaseEvent(event_id=f"EV-{CASE}-{stage}-{n}", case_id=CASE, event_type="STAGE_ENTERED", stage=stage))


def seed(repository, *, employment="SALARIED", co=False, stage="CREDIT", product="PERSONAL_LOAN", co_job=None):
    repository.save_applicant(Applicant(applicant_id=APP, full_name="Asha Verma"))
    if co:
        repository.save_applicant(Applicant(applicant_id=CO, full_name="Ravi Verma"))
    repository.save_application(Application(case_id=CASE, applicant_id=APP, product=product, loan_amount="300000",
                                            employment_type=employment, co_applicant_id=CO if co else None,
                                            co_applicant_employment_type=co_job if co else None))
    repository.grant_access(OFFICER, "APPLICANT", APP)
    if stage:
        set_stage(repository, stage)


_N = [0]


def doc(repository, doc_type, *, party=APP, role="PRIMARY_APPLICANT", status=DocumentStatus.VERIFIED,
        verification="PASS", fields=None, uploaded_at=None):
    _N[0] += 1
    document_id = f"DOC-{_N[0]:04d}"
    repository.save_document(Document(
        document_id=document_id, case_id=CASE, applicant_id=APP, party_id=party, party_role=role,
        document_type=doc_type, status=status, verification_status=verification, extracted_fields=fields or {},
        uploaded_at=uploaded_at or datetime.now(timezone.utc) + timedelta(seconds=1)))
    return document_id


def bank_fields(months=3, end_days_ago=5):
    from app.agents.docs_verify_credit_agent.services.dates import add_months

    end = TODAY - timedelta(days=end_days_ago)
    # exactly `months` calendar months ending on `end` (the inverse of covers_months): 30-07 -> 30-04, not 01-05, so
    # the fixture is right on every day of the year, not only when the months have equal lengths
    start = add_months(end + timedelta(days=1), -months)
    return {"period_start": start.isoformat(), "period_end": end.isoformat()}


def slip_fields(months_ago=1):
    month = TODAY.replace(day=1)
    for _ in range(months_ago):
        month = (month - timedelta(days=1)).replace(day=1)
    return {"pay_period": month.strftime("%B %Y").upper()}


def complete_applicant(repository, *, skip=(), party=APP, role="PRIMARY_APPLICANT"):
    ids = {}
    if "PAN" not in skip:
        ids["PAN"] = doc(repository, "PAN", party=party, role=role)
    if "PASSPORT" not in skip:
        ids["PASSPORT"] = doc(repository, "PASSPORT", party=party, role=role,
                              fields={"valid_until": (TODAY + timedelta(days=900)).isoformat()})
    if "SIGNATURE" not in skip:
        ids["SIGNATURE"] = doc(repository, "SIGNATURE", party=party, role=role)
    if "BANK_STATEMENT" not in skip:
        ids["BANK_STATEMENT"] = doc(repository, "BANK_STATEMENT", party=party, role=role, fields=bank_fields())
    if "SALARY_SLIP" not in skip:
        ids["SALARY_SLIP"] = doc(repository, "SALARY_SLIP", party=party, role=role, fields=slip_fields())
    return ids


def complete_co_applicant(repository, *, skip=()):
    """The co-applicant's own full set: the same checklist as the applicant (policy CO_APPLICANT source: base)."""
    for kind, fields in (("PAN", None), ("SIGNATURE", None), ("VOTER_ID", None), ("SALARY_SLIP", slip_fields()),
                         ("BANK_STATEMENT", bank_fields())):
        if kind not in skip:
            doc(repository, kind, party=CO, role="CO_APPLICANT", fields=fields)


def evaluate(repository, **kwargs):
    return readiness_service.evaluate(CASE, repository, on=TODAY, **kwargs)


def items(result, **match):
    return [r for r in result.requirements if all(getattr(r, k) == v for k, v in match.items())]


def raise_deviation(code="DVM-62A2A3", actor="maker-1"):
    from app.agents.los import queries

    queries.reload()
    return queries.raise_deviation(CASE, code=code, reason="Non-standard address proof accepted",
                                   actor=actor, scopes={"los.deviation:write"})["deviation_id"]


def decide(deviation_id, decision="APPROVED", actor="checker-1"):
    from app.agents.los import queries

    return queries.decide_deviation(CASE, deviation_id, decision=decision, actor=actor,
                                    scopes={"los.deviation:approve"}, justification="Checked by credit",
                                    authorities={"ACM", "RCM", "ZCM", "HEAD_CREDIT"})


def policy_dir(tmp_path, monkeypatch, *edits, name="personal_loan.credit.json", keep_original=False):
    """A copy of the SAMPLE policy with edits applied (each: a function on the dict)."""
    folder = tmp_path / "policies"
    folder.mkdir(exist_ok=True)
    data = json.loads((POLICIES / "personal_loan.credit.json").read_text(encoding="utf-8"))
    for edit in edits:
        edit(data)
    if keep_original:
        shutil.copy(POLICIES / "personal_loan.credit.json", folder / "original.credit.json")
    (folder / name).write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("DOCS_CREDIT_POLICY_DIR", str(folder))
    return folder


# ==========================================================================
# positive
# ==========================================================================

def test_complete_case_has_no_requirements_and_allows_underwriting(store):
    seed(store)
    complete_applicant(store)
    r = evaluate(store)
    assert r.overall_status == "COMPLETE" and r.requirements == []
    assert r.credit_underwriting_allowed is True and r.recommended_action == "PROCEED"
    assert r.stage == "CREDIT" and r.product == "PERSONAL_LOAN"
    assert r.audit.recorded and not r.audit.replayed
    assert r.audit.policy_status == "SAMPLE" and r.audit.base_policy["policy_id"] == "PERSONAL_LOAN_FOS_DOCUMENTS"
    assert r.audit.rules_disabled == [] and r.audit.rules_evaluated == 11
    assert r.audit.requirements_open == 0 and r.audit.requirements_satisfied >= 5


def test_same_input_replays_the_stored_evaluation_without_a_second_record(store):
    seed(store)
    complete_applicant(store)
    first, second = evaluate(store), evaluate(store)
    assert second.audit.replayed and second.audit.evaluation_id == first.audit.evaluation_id
    assert len(store.get_case_findings(CASE, kind=FindingKind.DOCUMENT_POLICY)) == 1
    events = [e for e in store.get_case_timeline(CASE) if e.event_type == audit_service.EVENT_TYPE]
    assert len(events) == 1


def test_a_changed_document_is_a_new_evaluation(store):
    seed(store)
    complete_applicant(store, skip=("PAN",))
    first = evaluate(store)
    doc(store, "PAN")
    second = evaluate(store)
    assert first.overall_status == "ACTION_REQUIRED" and second.overall_status == "COMPLETE"
    assert second.audit.evaluation_id != first.audit.evaluation_id and not second.audit.replayed
    assert audit_service.latest(CASE, store)["overall_status"] == "COMPLETE"


def test_co_applicant_with_their_own_documents_is_complete(store):
    seed(store, co=True)
    complete_applicant(store)
    complete_co_applicant(store)
    r = evaluate(store)
    assert r.overall_status == "COMPLETE", [i.reason for i in r.requirements]


def test_approved_waiver_from_the_deviation_workflow_satisfies_the_slot(store):
    seed(store)
    complete_applicant(store, skip=("PASSPORT",))
    deviation = raise_deviation()
    decide(deviation, "APPROVED")
    r = evaluate(store)
    assert r.overall_status == "COMPLETE" and r.credit_underwriting_allowed
    assert r.audit.waivers_applied == [deviation]


def test_earlier_stage_documents_are_reused_not_asked_again(store):
    seed(store, stage="CPA")
    earlier = doc(store, "PAN", uploaded_at=NOW - timedelta(hours=2))     # collected at CPA
    set_stage(store, "CREDIT", n=2)
    complete_applicant(store, skip=("PAN",))
    r = evaluate(store)
    assert r.overall_status == "COMPLETE"
    assert earlier in r.audit.reused_documents


def test_itr_with_its_computation_is_satisfied(store):
    seed(store)
    complete_applicant(store)
    doc(store, "ITR")
    doc(store, "ITR_COMPUTATION")
    r = evaluate(store)
    assert r.overall_status == "COMPLETE" and r.requirements == []


# ==========================================================================
# negative -- one rule at a time
# ==========================================================================

def test_rule03_missing_mandatory_document(store):
    seed(store)
    complete_applicant(store, skip=("PAN",))
    r = evaluate(store)
    assert r.overall_status == "ACTION_REQUIRED" and not r.credit_underwriting_allowed
    [item] = r.requirements
    assert (item.rule_id, item.status, item.severity) == ("RULE-03", "ACTION_REQUIRED", "BLOCKING")
    assert item.missing_documents == ["PAN"] and item.recommended_action == "REQUEST_DOCUMENTS"
    assert item.party_role == "PRIMARY_APPLICANT" and r.recommended_action == "REQUEST_DOCUMENTS"


def test_rule02_co_applicant_without_documents_is_reported_per_party(store):
    seed(store, co=True)
    complete_applicant(store)
    r = evaluate(store)
    co_items = items(r, party_role="CO_APPLICANT")
    # the SAME rules as the applicant: the product's whole checklist, as Applicant Coverage, all critical (missing)
    assert {i.check_id for i in co_items} == {"CO_APPLICANT:PAN", "CO_APPLICANT:ADDRESS_PROOF",
                                              "CO_APPLICANT:BANK_STATEMENT", "CO_APPLICANT:SIGNATURE",
                                              "CO_APPLICANT:INCOME_PROOF"}
    assert all(i.rule_id == "RULE-02" and i.party_id == CO and i.severity == "BLOCKING" for i in co_items)
    assert r.overall_status == "ACTION_REQUIRED"


def test_rule02_a_co_applicants_document_never_fills_the_applicants_slot(store):
    seed(store, co=True)
    complete_applicant(store, skip=("PAN",))
    complete_co_applicant(store)
    r = evaluate(store)
    assert [i.check_id for i in r.requirements] == ["PRIMARY_APPLICANT:PAN"]


def test_rule04_unanswered_condition_is_unresolved_not_skipped(store):
    seed(store, employment=None)
    complete_applicant(store)
    r = evaluate(store)
    conditions = items(r, rule_id="RULE-04")
    assert conditions and r.requirements == conditions            # every self-employed rule, none skipped
    [item] = conditions                                            # one item per missing detail, every rule in it
    assert item.check_id == "CONDITION:employment_type" and "policy:PL_SELF_EMPLOYED" in item.evidence
    assert item.status == "UNRESOLVED" and item.recommended_action == "CAPTURE_APPLICATION_DATA"
    assert "employment type is not filled in" in item.reason
    # missing case DATA is not a missing or fake document: listed, not critical
    assert item.severity == "NON_BLOCKING" and r.overall_status == "COMPLETE"
    assert r.summary.route.decision == "MOVE_AHEAD_WITH_NOTES"


def test_rule04_self_employed_needs_the_conditional_itr(store):
    seed(store, employment="SELF_EMPLOYED")
    complete_applicant(store, skip=("SALARY_SLIP",))
    r = evaluate(store)
    assert any(i.rule_id == "RULE-04" and "ITR" in (i.missing_documents or []) for i in r.requirements)


def test_rule05_a_document_the_policy_does_not_accept_is_not_counted(store):
    seed(store)
    complete_applicant(store, skip=("PASSPORT",))
    bill = doc(store, "UTILITY_BILL")
    r = evaluate(store)
    [item] = r.requirements
    assert item.rule_id == "RULE-05" and item.status == "ACTION_REQUIRED"
    assert f"document:{bill}" in item.evidence
    assert item.recommended_action == "RAISE_EXCEPTION" and item.deviation_codes == ["DVM-62A2A3"]


def test_rule06_too_few_months_of_bank_statement(store):
    seed(store)
    complete_applicant(store, skip=("BANK_STATEMENT",))
    doc(store, "BANK_STATEMENT", fields=bank_fields(months=2))
    [item] = evaluate(store).requirements
    assert (item.rule_id, item.check_id) == ("RULE-06", "PF_BANK_STATEMENT_PERIOD:BANK_STATEMENT")
    assert "3 months are needed" in item.reason and item.recommended_action == "REQUEST_FRESH_DOCUMENT"


def test_rule06_stale_bank_statement(store):
    seed(store)
    complete_applicant(store, skip=("BANK_STATEMENT",))
    doc(store, "BANK_STATEMENT", fields=bank_fields(end_days_ago=90))
    [item] = evaluate(store).requirements
    assert item.rule_id == "RULE-06" and "within 45 days" in item.reason


def test_rule06_unreadable_date_is_unresolved_never_satisfied(store):
    seed(store)
    complete_applicant(store, skip=("SALARY_SLIP",))
    doc(store, "SALARY_SLIP", fields={"pay_period": "sometime"})
    [item] = evaluate(store).requirements
    assert item.rule_id == "RULE-06" and item.status == "UNRESOLVED"


def test_rule06_expired_passport(store):
    seed(store)
    complete_applicant(store, skip=("PASSPORT",))
    doc(store, "PASSPORT", fields={"valid_until": (TODAY - timedelta(days=1)).isoformat()})
    [item] = evaluate(store).requirements
    assert item.rule_id == "RULE-06" and "expired" in item.reason


def test_rule07_itr_without_computation_is_listed_but_not_blocking(store):
    seed(store)
    complete_applicant(store)
    doc(store, "ITR")
    r = evaluate(store)
    [item] = r.requirements
    assert item.rule_id == "RULE-07" and item.severity == "NON_BLOCKING"
    assert r.overall_status == "COMPLETE" and r.credit_underwriting_allowed


def test_rule08_bank_statement_from_an_earlier_stage_is_reused(store):
    seed(store, stage="CPA")
    earlier = doc(store, "BANK_STATEMENT", fields=bank_fields(), uploaded_at=NOW - timedelta(hours=2))
    set_stage(store, "CREDIT", n=2)
    complete_applicant(store, skip=("BANK_STATEMENT",))
    r = evaluate(store)
    assert r.requirements == [] and r.overall_status == "COMPLETE"
    assert earlier in r.audit.reused_documents


def test_rule08_a_type_the_policy_collects_at_this_stage_must_be_fresh(store, tmp_path, monkeypatch):
    policy_dir(tmp_path, monkeypatch, lambda d: d["cross_stage"].update(collect_at_stage=["BANK_STATEMENT"]))
    seed(store, stage="CPA")
    doc(store, "BANK_STATEMENT", fields=bank_fields(), uploaded_at=NOW - timedelta(hours=2))
    set_stage(store, "CREDIT", n=2)
    complete_applicant(store, skip=("BANK_STATEMENT",))
    [item] = evaluate(store).requirements
    assert item.rule_id == "RULE-08" and item.recommended_action == "REQUEST_FRESH_DOCUMENT"


def test_rule09_pending_waiver_is_exception_pending(store):
    seed(store)
    complete_applicant(store, skip=("PASSPORT",))
    raise_deviation()
    r = evaluate(store)
    assert r.overall_status == "EXCEPTION_PENDING" and not r.credit_underwriting_allowed
    [item] = r.requirements
    assert item.rule_id == "RULE-09" and item.recommended_action == "AWAIT_EXCEPTION_APPROVAL"


@pytest.mark.parametrize("decision", ["REJECTED", "WITHDRAWN"])
def test_rule09_rejected_or_withdrawn_waiver_never_waives(store, decision):
    seed(store)
    complete_applicant(store, skip=("PASSPORT",))
    decide(raise_deviation(), decision, actor="checker-1" if decision == "REJECTED" else "maker-1")
    r = evaluate(store)
    assert r.overall_status == "ACTION_REQUIRED" and r.audit.waivers_applied == []


def test_rule09_an_unmapped_deviation_code_never_waives(store):
    from app.agents.los import queries

    other = next((d["code"] for d in queries.deviation_master() if d["code"] != "DVM-62A2A3"), None)
    if other is None:
        pytest.skip("deviation master has one code only")
    seed(store)
    complete_applicant(store, skip=("PASSPORT",))
    decide(raise_deviation(code=other))
    assert evaluate(store).overall_status == "ACTION_REQUIRED"


def test_verified_document_whose_verification_failed_is_a_mismatch(store):
    seed(store)
    complete_applicant(store, skip=("PAN",))
    doc(store, "PAN", verification="FAIL")
    [item] = evaluate(store).requirements
    assert item.status == "MISMATCH" and item.recommended_action == "RECHECK_DOCUMENT"


@pytest.mark.parametrize("state,expected,severity,overall", [
    (DocumentStatus.UPLOADED, "PENDING_VERIFICATION", "NON_BLOCKING", "COMPLETE"),   # on file, being verified
    (DocumentStatus.REVIEW, "UNDER_REVIEW", "NON_BLOCKING", "COMPLETE"),            # on file, with a reviewer
    (DocumentStatus.REJECTED, "ACTION_REQUIRED", "BLOCKING", "ACTION_REQUIRED")])   # rejected: critical
def test_unverified_documents_never_satisfy(store, state, expected, severity, overall):
    seed(store)
    complete_applicant(store, skip=("PAN",))
    doc(store, "PAN", status=state, verification=None)
    r = evaluate(store)
    [item] = r.requirements                                     # never satisfied: always listed
    assert (item.status, item.severity, r.overall_status) == (expected, severity, overall)


def test_complete_documents_outside_the_credit_stage_do_not_allow_underwriting(store):
    seed(store, stage="CPA")
    complete_applicant(store)
    r = evaluate(store)
    assert r.overall_status == "COMPLETE" and r.credit_underwriting_allowed is False
    assert r.recommended_action == "AWAIT_CREDIT_STAGE"
    assert store.get_case_stage(CASE).stage in ("CPA",) or str(store.get_case_stage(CASE).stage).endswith("CPA")


# ==========================================================================
# RULE-11 file size / format, switches, co-applicant extras
# ==========================================================================

PDF = b"%PDF-1.7\n" + b"0" * 60_000
JPG = b"\xff\xd8\xff\xe0" + b"0" * 60_000


def store_file(repository, document_id, content):
    """Keep a file for a stored document, under the key the LOS uses."""
    from app.agents.los import parties
    from app.store import documents as store_module

    row = next(d for d in repository.list_documents(CASE) if d.document_id == document_id)
    repository.save_document(Document(**{**row.__dict__, "source_id": f"src-{document_id}"}))
    store_module.get_document_store().put(
        store_module.storage_key(parties.document_key(CASE, row.party_id, f"src-{document_id}")), content)


def test_rule11_detects_the_real_format_from_the_bytes():
    from app.agents.docs_verify_credit_agent.services.file_service import detect

    assert [detect(PDF[:16]), detect(JPG[:16]), detect(b"\x89PNG\r\n\x1a\n0000"), detect(b"PK\x03\x04abc"),
            detect(b"\x00\x00\x00\x18ftypheic"), detect(b"hello")] == ["PDF", "JPEG", "PNG", "DOCX", "HEIC",
                                                                         "UNKNOWN"]


def test_rule11_wrong_format_too_big_and_too_small(store, tmp_path):
    from app.store.documents import LocalDocumentStore, set_document_store

    set_document_store(LocalDocumentStore(tmp_path / "files"))
    try:
        seed(store)
        ids = complete_applicant(store)
        store_file(store, ids["BANK_STATEMENT"], JPG)                          # a photo of a statement: PDF only
        store_file(store, ids["PASSPORT"], JPG[:4] + b"0" * (11 * 1024 * 1024))  # 11 MB > 10 MB
        store_file(store, ids["PAN"], b"%PDF-1.4 tiny")                       # a few bytes: not a readable scan
        store_file(store, ids["SIGNATURE"], JPG)                              # fine
        r = evaluate(store)
    finally:
        set_document_store(None)
    found = {i.check_id: i.reason for i in items(r, rule_id="RULE-11")}
    assert set(found) == {"FILE_SIZE_FORMAT:BANK_STATEMENT", "FILE_SIZE_FORMAT:PASSPORT", "FILE_SIZE_FORMAT:PAN"}
    assert "JPEG file; allowed: PDF" in found["FILE_SIZE_FORMAT:BANK_STATEMENT"]
    assert "limit is 10 MB" in found["FILE_SIZE_FORMAT:PASSPORT"]
    assert "too small" in found["FILE_SIZE_FORMAT:PAN"]
    # a file to re-upload is not a missing or fake document: listed, not critical
    assert all(i.severity == "NON_BLOCKING" for i in items(r, rule_id="RULE-11"))
    assert r.overall_status == "COMPLETE" and r.summary.route.decision == "MOVE_AHEAD_WITH_NOTES"


def test_rule11_no_stored_file_is_skipped_by_the_sample_policy(store):
    seed(store)
    complete_applicant(store)
    assert items(evaluate(store), rule_id="RULE-11") == []


def test_rules_can_be_switched_off_by_env_and_by_policy(store, tmp_path, monkeypatch):
    seed(store)
    complete_applicant(store, skip=("PAN", "BANK_STATEMENT"))
    doc(store, "BANK_STATEMENT", fields=bank_fields(months=2))
    assert {i.rule_id for i in evaluate(store).requirements} == {"RULE-03", "RULE-06"}
    monkeypatch.setenv("DOCS_CREDIT_RULES_OFF", "RULE-06")
    r = evaluate(store)
    assert {i.rule_id for i in r.requirements} == {"RULE-03"} and "RULE-06" in r.audit.rules_disabled
    monkeypatch.setenv("DOCS_CREDIT_RULES_OFF", "")
    policy_dir(tmp_path, monkeypatch, lambda d: d["rules"].update({"RULE-03": False}))
    r = evaluate(store)
    assert {i.rule_id for i in r.requirements} == {"RULE-06"} and "RULE-03" in r.audit.rules_disabled


def test_the_audit_rule_cannot_be_switched_off(store, tmp_path, monkeypatch):
    seed(store)
    policy_dir(tmp_path, monkeypatch, lambda d: d["rules"].update({"RULE-10": False}))
    with pytest.raises(EvaluationError) as e:
        evaluate(store)
    assert e.value.code == "POLICY_INVALID"


def test_switching_off_waivers_means_an_approved_deviation_no_longer_waives(store, monkeypatch):
    seed(store)
    complete_applicant(store, skip=("PASSPORT",))
    decide(raise_deviation(), "APPROVED")
    assert evaluate(store).overall_status == "COMPLETE"
    monkeypatch.setenv("DOCS_CREDIT_RULES_OFF", "RULE-09")
    assert evaluate(store).overall_status == "ACTION_REQUIRED"


def test_co_applicant_needs_the_same_documents_as_the_applicant(store):
    seed(store, co=True)
    complete_applicant(store)
    complete_co_applicant(store, skip=("SIGNATURE", "BANK_STATEMENT"))
    open_items = evaluate(store).requirements
    assert {i.check_id for i in open_items} == {"CO_APPLICANT:SIGNATURE", "CO_APPLICANT:BANK_STATEMENT"}
    assert all(i.party_id == CO and i.severity == "BLOCKING" for i in open_items)


def test_co_applicant_documents_get_the_period_rules_too(store):
    seed(store, co=True)
    complete_applicant(store)
    complete_co_applicant(store, skip=("SALARY_SLIP",))
    doc(store, "SALARY_SLIP", party=CO, role="CO_APPLICANT", fields=slip_fields(months_ago=5))
    [item] = evaluate(store).requirements
    assert item.rule_id == "RULE-06" and item.party_role == "CO_APPLICANT" and item.severity == "NON_BLOCKING"
    assert item.reason.startswith("The salary slip of the co-applicant is dated")   # says WHOSE document


# ==========================================================================
# the summary block -- plain words, and an AI note that never decides
# ==========================================================================

def test_summary_splits_missing_from_not_satisfied(store):
    seed(store)
    complete_applicant(store, skip=("PAN", "BANK_STATEMENT"))
    doc(store, "BANK_STATEMENT", fields=bank_fields(months=2))
    s = evaluate(store).summary
    assert s.headline == "Credit underwriting cannot start yet: 1 item needs attention."
    assert s.missing == ["The PAN of the applicant is missing. Accepted: PAN."]
    assert len(s.not_satisfied) == 1 and "3 months are needed" in s.not_satisfied[0]
    assert s.next_step == "Ask the customer for the missing documents."
    assert s.ai_summary.source == "rules" and s.route.text in s.ai_summary.text
    # the missing PAN is critical; the short statement is on file, so not critical
    assert (s.route.decision, s.route.critical, s.route.non_critical) == ("REFER_TO_AUTHORITY", 1, 1)
    assert "credit manager" in s.route.text


def test_route_only_non_critical_items_open_moves_ahead(store):
    seed(store)
    complete_applicant(store)
    doc(store, "ITR")                                             # ITR without its computation: not critical
    r = evaluate(store)
    assert r.overall_status == "COMPLETE" and r.credit_underwriting_allowed
    route = r.summary.route
    assert (route.decision, route.critical, route.non_critical) == ("MOVE_AHEAD_WITH_NOTES", 0, 1)
    assert "can move ahead" in route.text


def test_route_only_a_pending_exception_waits_for_the_authority(store):
    seed(store)
    complete_applicant(store, skip=("PASSPORT",))
    raise_deviation()
    assert evaluate(store).summary.route.decision == "AWAIT_APPROVAL"


def test_route_nothing_open_moves_ahead(store):
    seed(store)
    complete_applicant(store)
    assert evaluate(store).summary.route.decision == "MOVE_AHEAD"


def test_summary_names_each_person_and_their_open_items(store):
    seed(store, co=True)
    complete_applicant(store)
    s = evaluate(store).summary
    assert s.people["PRIMARY_APPLICANT"].open == 0 and s.people["PRIMARY_APPLICANT"].name == "Asha Verma"
    assert s.people["CO_APPLICANT"].name == "Ravi Verma" and s.people["CO_APPLICANT"].open == 5
    from app.agents.docs_verify_credit_agent.services import summary_service as S

    facts = S._facts(s.model_dump(), "ACTION_REQUIRED")
    assert any("Ravi Verma (co-applicant): 5 open" == p for p in facts["people"])


def test_summary_of_a_complete_case(store):
    seed(store)
    complete_applicant(store)
    s = evaluate(store).summary
    assert s.missing == [] and s.not_satisfied == [] and "can start" in s.headline


def test_ai_note_is_refused_when_it_adds_a_figure_or_contradicts_the_status():
    from app.agents.docs_verify_credit_agent.services import summary_service as S

    summary = {"headline": "Credit underwriting cannot start yet: 1 item needs attention.",
               "missing": ["The PAN of the applicant is missing. Accepted: PAN."], "not_satisfied": [],
               "next_step": "Ask the customer for the missing documents."}
    assert S.accepted("Underwriting cannot start yet. The PAN is missing. Ask the customer for it.",
                      summary, "ACTION_REQUIRED")
    assert not S.accepted("All documents are in place, underwriting can start.", summary, "ACTION_REQUIRED")
    assert not S.accepted("The PAN is missing and 3 slips are late.", summary, "ACTION_REQUIRED")
    assert not S.accepted("Check overall_status first.", summary, "ACTION_REQUIRED")
    on_file = {**summary, "missing": [], "not_satisfied": ["The address proof of the applicant is with a reviewer."]}
    assert not S.accepted("The address proof is missing.", on_file, "ACTION_REQUIRED")
    assert S.accepted("The address proof is with a reviewer.", on_file, "ACTION_REQUIRED")


def test_ai_note_never_blocks_and_falls_back_to_rules(store, monkeypatch):
    from app.agents.docs_verify_credit_agent.services import summary_service as S

    monkeypatch.setenv("DOCS_CREDIT_AI_SUMMARY", "true")
    monkeypatch.setitem(S.wording()["ai_summary"], "cache_file", "")
    seed(store)
    complete_applicant(store, skip=("PAN",))
    with patch("app.agents.credit.repayment.ai_summary._ask", side_effect=RuntimeError("model down")):
        note = evaluate(store).summary.ai_summary
    assert note.source == "rules" and note.text.startswith("1 critical item is open")


# ==========================================================================
# policy resolution -- fail safely
# ==========================================================================

def test_no_policy_for_the_product(store):
    seed(store, product="GOLD_LOAN")             # no Credit document policy for this product
    with pytest.raises(EvaluationError) as e:
        evaluate(store)
    assert e.value.code == "POLICY_NOT_CONFIGURED" and e.value.status == 422


def test_two_matching_policies_are_ambiguous(store, tmp_path, monkeypatch):
    seed(store)
    policy_dir(tmp_path, monkeypatch, lambda d: d.update(version="PL-CREDIT-0.1.1-SAMPLE"), keep_original=True)
    with pytest.raises(EvaluationError) as e:
        evaluate(store)
    assert e.value.code == "POLICY_AMBIGUOUS"


def test_an_unknown_policy_key_is_invalid_not_ignored(store, tmp_path, monkeypatch):
    seed(store)
    policy_dir(tmp_path, monkeypatch, lambda d: d.update(blocking_rulez=True))
    with pytest.raises(EvaluationError) as e:
        evaluate(store)
    assert e.value.code == "POLICY_INVALID"


def test_approved_policy_without_an_approver_is_invalid(store, tmp_path, monkeypatch):
    seed(store)
    policy_dir(tmp_path, monkeypatch, lambda d: d.update(status="APPROVED"))
    with pytest.raises(EvaluationError) as e:
        evaluate(store)
    assert e.value.code == "POLICY_INVALID"


def test_sample_policy_is_refused_in_production(store, monkeypatch):
    seed(store)
    monkeypatch.setenv("ENVIRONMENT", "production")
    # the rules' SOURCE stays the file here (production reads them from the database: test_agent_databases.py);
    # what is tested is the desk's own refusal of a SAMPLE policy in production
    monkeypatch.setattr("app.agent_db.rule_store.production", lambda: False)
    with pytest.raises(EvaluationError) as e:
        evaluate(store)
    assert e.value.code == "POLICY_NOT_APPROVED"


def test_an_expired_policy_does_not_apply(store, tmp_path, monkeypatch):
    seed(store)
    policy_dir(tmp_path, monkeypatch, lambda d: d.update(effective_from="2020-01-01", effective_to="2020-12-31"))
    with pytest.raises(EvaluationError) as e:
        evaluate(store)
    assert e.value.code == "POLICY_NOT_CONFIGURED"


def test_the_documented_example_policy_is_valid_and_never_loaded():
    from app.agents.docs_verify_credit_agent.config.policy_loader import _strip_comments, load_all
    from app.agents.docs_verify_credit_agent.models.policy import CreditDocumentPolicy

    example = json.loads((POLICIES / "example_policy.json").read_text(encoding="utf-8"))
    assert CreditDocumentPolicy.model_validate(_strip_comments(example)).status == "APPROVED"
    assert [p.file for p in load_all(POLICIES)] == ["home_loan.credit.json", "loan_against_property.credit.json",
                                                    "personal_loan.credit.json"]


def test_a_party_role_the_policy_does_not_cover_is_unresolved(store, tmp_path, monkeypatch):
    seed(store, co=True)
    complete_applicant(store)
    policy_dir(tmp_path, monkeypatch, lambda d: d["parties"].pop("CO_APPLICANT"))
    [item] = evaluate(store).requirements
    assert item.rule_id == "RULE-02" and item.status == "UNRESOLVED"
    assert item.recommended_action == "RESOLVE_POLICY_CONFIGURATION"


def test_a_changed_policy_file_is_a_new_evaluation(store, tmp_path, monkeypatch):
    seed(store)
    complete_applicant(store)
    first = evaluate(store)
    policy_dir(tmp_path, monkeypatch, lambda d: d["period_and_freshness"][0].update(max_age_days=1))
    second = evaluate(store)
    assert second.audit.policy_file_sha256 != first.audit.policy_file_sha256
    assert first.requirements == [] and items(second, rule_id="RULE-06")       # the new rule now finds the old slip


# ==========================================================================
# failure handling
# ==========================================================================

def test_case_not_found(store):
    with pytest.raises(EvaluationError) as e:
        evaluate(store)
    assert e.value.code == "CASE_NOT_FOUND" and e.value.status == 404


def test_an_unreadable_case_store_is_503_never_an_empty_case(store):
    seed(store)
    with patch.object(store, "list_documents", side_effect=RuntimeError("down")):
        with pytest.raises(EvaluationError) as e:
            evaluate(store)
    assert e.value.code == "CASE_STORE_UNAVAILABLE" and e.value.status == 503


def test_audit_failure_is_an_error_when_audit_is_required(store):
    seed(store)
    complete_applicant(store)
    with patch.object(store, "save_finding", side_effect=RuntimeError("down")):
        with pytest.raises(EvaluationError) as e:
            evaluate(store)
    assert e.value.code == "AUDIT_UNAVAILABLE" and e.value.status == 503


def test_audit_failure_is_reported_when_audit_is_optional(store, monkeypatch):
    seed(store)
    complete_applicant(store)
    monkeypatch.setenv("DOCS_CREDIT_REQUIRE_AUDIT", "false")
    with patch.object(store, "save_finding", side_effect=RuntimeError("down")):
        r = evaluate(store)
    assert r.audit.recorded is False


def test_timeout_is_504(store, monkeypatch):
    import time

    seed(store)
    monkeypatch.setenv("DOCS_CREDIT_TIMEOUT_SECONDS", "1")
    from app.agents.docs_verify_credit_agent.services import context_service

    real = context_service.build
    with patch.object(context_service, "build", side_effect=lambda *a, **k: (time.sleep(1.5), real(*a, **k))[1]):
        with pytest.raises(EvaluationError) as e:
            evaluate(store)
    assert e.value.code == "EVALUATION_TIMEOUT" and e.value.status == 504


# ==========================================================================
# invariants
# ==========================================================================

def test_invariants_across_scenarios(store):
    seed(store, co=True)
    results = [evaluate(store)]
    complete_applicant(store)
    results.append(evaluate(store))
    complete_co_applicant(store)
    results.append(evaluate(store))
    for r in results:
        assert all(i.status in OPEN_STATUSES for i in r.requirements)          # only open items are listed
        assert r.credit_underwriting_allowed == (r.overall_status == "COMPLETE" and r.stage == "CREDIT")
        blocking = [i for i in r.requirements if i.severity == "BLOCKING"]
        assert (r.overall_status == "COMPLETE") == (not blocking)
        assert r.audit.requirements_open == len(r.requirements)
    assert [r.overall_status for r in results] == ["ACTION_REQUIRED", "ACTION_REQUIRED", "COMPLETE"]


def test_evaluation_never_changes_the_stage_or_the_documents(store):
    seed(store)
    complete_applicant(store, skip=("PAN",))
    before_stage = store.get_case_stage(CASE)
    before_docs = [(d.document_id, d.status) for d in store.list_documents(CASE)]
    evaluate(store)
    after = store.get_case_stage(CASE)
    assert (after.stage, after.version) == (before_stage.stage, before_stage.version)
    assert [(d.document_id, d.status) for d in store.list_documents(CASE)] == before_docs


def test_audit_record_holds_every_outcome_and_no_caller_identity(store):
    seed(store)
    complete_applicant(store, skip=("PAN",))
    r = evaluate(store, correlation_id="corr-1")
    payload = audit_service.latest(CASE, store)
    assert payload["audit"]["evaluation_id"] == r.audit.evaluation_id
    assert {o["status"] for o in payload["outcomes"]} >= {"SATISFIED", "ACTION_REQUIRED"}
    text = json.dumps(payload)
    assert OFFICER not in text and "corr-1" not in text and "Asha" not in text


# ==========================================================================
# API and security
# ==========================================================================

@pytest.fixture
def client():
    import main

    return TestClient(main.app)


def _auth(make_token, scopes=(SCOPE,), subject=OFFICER):
    return {"Authorization": f"Bearer {make_token(subject=subject, scopes=list(scopes))}"}


def test_api_returns_the_contract(store, client, make_token):
    seed(store)
    complete_applicant(store, skip=("PAN",))
    res = client.post(URL, json={"correlation_id": "abc-1"}, headers=_auth(make_token))
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"request_id", "application_id", "stage", "product", "policy_version", "overall_status",
                         "requirements", "recommended_action", "credit_underwriting_allowed", "audit", "summary"}
    assert list(body)[-1] == "summary"
    assert body["summary"]["missing"] == ["The PAN of the applicant is missing. Accepted: PAN."]
    assert body["summary"]["ai_summary"]["source"] == "rules"
    assert body["request_id"] == "abc-1" and body["application_id"] == CASE
    assert body["overall_status"] == "ACTION_REQUIRED" and body["credit_underwriting_allowed"] is False
    assert body["requirements"][0]["rule_name"] == "Missing Documents"


def test_api_without_a_body_works(store, client, make_token):
    seed(store)
    complete_applicant(store)
    res = client.post(URL, headers=_auth(make_token))
    assert res.status_code == 200 and res.json()["requirements"] == []


def test_api_needs_a_token(store, client):
    seed(store)
    assert client.post(URL).status_code == 401


def test_api_needs_the_scope(store, client, make_token):
    seed(store)
    res = client.post(URL, headers=_auth(make_token, scopes=("documents:read",)))
    assert res.status_code == 403 and res.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"


def test_api_scope_is_configurable(store, client, make_token, monkeypatch):
    seed(store)
    monkeypatch.setenv("DOCS_CREDIT_SCOPES", "los.credit.docs")
    assert client.post(URL, headers=_auth(make_token)).status_code == 403
    assert client.post(URL, headers=_auth(make_token, scopes=("los.credit.docs",))).status_code == 200


def test_api_refuses_a_case_the_caller_cannot_access_same_as_a_missing_one(store, client, make_token):
    seed(store)
    other = client.post(URL, headers=_auth(make_token, subject="someone-else"))
    missing = client.post("/api/v1/credit/document-readiness/CASE-NOPE", headers=_auth(make_token))
    assert other.status_code == missing.status_code == 403
    assert other.json()["detail"]["code"] == missing.json()["detail"]["code"]


@pytest.mark.parametrize("body", [{"documents": []}, {"overall_status": "COMPLETE"},
                                  {"policy_version": "X"}, {"correlation_id": "bad id!"}])
def test_api_refuses_caller_supplied_facts(store, client, make_token, body):
    seed(store)
    assert client.post(URL, json=body, headers=_auth(make_token)).status_code == 422


def test_api_policy_error_is_422_with_its_code(store, client, make_token, monkeypatch):
    seed(store)
    monkeypatch.setenv("ENVIRONMENT", "production")
    # the rules' SOURCE stays the file here (production reads them from the database: test_agent_databases.py);
    # what is tested is the desk's own refusal of a SAMPLE policy in production
    monkeypatch.setattr("app.agent_db.rule_store.production", lambda: False)
    res = client.post(URL, headers=_auth(make_token, scopes=("los.write",)))
    assert res.status_code == 422 and res.json()["detail"]["code"] == "POLICY_NOT_APPROVED"


def test_api_service_principal_gets_404_for_a_case_without_an_application(store, client, make_token):
    res = client.post(URL, headers=_auth(make_token, scopes=("los.write",), subject="svc-credit"))
    assert res.status_code == 404 and res.json()["detail"]["code"] == "CASE_NOT_FOUND"


def test_api_latency_is_small(store, client, make_token):
    import time

    seed(store, co=True)
    complete_applicant(store)
    complete_co_applicant(store)
    client.post(URL, headers=_auth(make_token))                       # warm
    doc(store, "ITR")                                                 # new input: a real evaluation, not a replay
    start = time.perf_counter()
    res = client.post(URL, headers=_auth(make_token))
    assert res.status_code == 200 and (time.perf_counter() - start) < 2.0


# ==========================================================================
# the dashboard routes (docs-credit-frontend/)
# ==========================================================================

def test_dashboard_rules_board_has_every_rule_with_colours(store, client, make_token):
    seed(store)
    complete_applicant(store, skip=("PAN",))
    doc(store, "ITR")                                             # RULE-07 open but not blocking -> WARN
    res = client.post(f"{URL}/rules", headers=_auth(make_token))
    assert res.status_code == 200, res.text
    board = {r["rule_id"]: r for r in res.json()["rules"]}
    assert list(board) == [f"RULE-{n:02d}" for n in range(1, 12)]
    assert board["RULE-01"]["status"] == "PASS"                   # the product list was found
    assert board["RULE-03"]["status"] == "FAIL"                   # PAN missing: critical
    assert board["RULE-07"]["status"] == "WARN"                   # not critical
    assert board["RULE-09"]["status"] == "N/A"                    # no exception on the case
    assert board["RULE-10"]["status"] == "PASS"                   # recorded
    assert res.json()["case"]["applicant_name"] == "Asha Verma"
    matrix = res.json()["documents"]
    assert matrix["people"] == ["PRIMARY_APPLICANT"]
    cells = {row["slot"]: row["cells"]["PRIMARY_APPLICANT"]["state"] for row in matrix["rows"]}
    assert cells["PAN"] == "MISSING" and cells["BANK_STATEMENT"] == "OK"
    sections = res.json()["sections"]
    assert sorted(r for s in sections for r in s["rules"]) == list(board)           # every rule exactly once
    assert sections[0]["counts"]["FAIL"] == 1                                     # coverage: RULE-03


def test_dashboard_routes_need_the_scope(store, client, make_token):
    seed(store)
    assert client.post(f"{URL}/rules", headers=_auth(make_token, scopes=("documents:read",))).status_code == 403
    assert client.post(f"{URL}/rules").status_code == 401


# ==========================================================================
# Credit Underwriting Agent integration -- the same engine
# ==========================================================================

def test_credit_reader_uses_the_same_evaluation_as_the_api(store):
    from app.agents.docs_verify_credit_agent.integrations import credit_underwriting_adapter as adapter

    seed(store)
    complete_applicant(store, skip=("PAN",))
    direct = evaluate(store)
    observation = adapter.document_readiness_get(CASE)
    assert observation.quality.value == "PRESENT"
    assert observation.data["status"] == direct.overall_status == "ACTION_REQUIRED"
    assert observation.data["evaluation_id"] == direct.audit.evaluation_id
    [ref] = observation.evidence
    assert ref.record_type == "FINDING:DOCUMENT_POLICY" and ref.field == "status"
    assert len(store.get_case_findings(CASE, kind=FindingKind.DOCUMENT_POLICY)) == 1    # replayed, not re-recorded


def test_credit_reader_reports_a_policy_error_as_missing_never_complete(store, monkeypatch):
    from app.agents.docs_verify_credit_agent.integrations import credit_underwriting_adapter as adapter

    seed(store)
    monkeypatch.setenv("ENVIRONMENT", "production")
    # the rules' SOURCE stays the file here (production reads them from the database: test_agent_databases.py);
    # what is tested is the desk's own refusal of a SAMPLE policy in production
    monkeypatch.setattr("app.agent_db.rule_store.production", lambda: False)
    observation = adapter.document_readiness_get(CASE)
    assert observation.quality.value == "MISSING" and observation.error == "POLICY_NOT_APPROVED"


def test_credit_agent_run_reads_document_readiness_and_finds_on_it(store):
    import asyncio

    from app.agents.applicant.permissions import Caller
    from app.agents.credit import graph
    from app.agents.credit import policy as credit_policy

    credit_policy.reload()
    graph.reset()
    seed(store)
    complete_applicant(store, skip=("PAN",))
    who = Caller(subject=OFFICER, scopes=frozenset({SCOPE}), roles=frozenset({"credit"}))
    with patch("app.agents.los.config.case_memory_enabled", return_value=True), \
            patch.dict("os.environ", {"CREDIT_MEMO_LLM_ENABLED": "false", "AGENT_RUN_AUDIT_ENABLED": "false"}):
        state = asyncio.run(graph.run(CASE, caller=who, request_id="req-dvc"))
    assert ("document_readiness.get", None) in [(t["tool"], t.get("party_id")) for t in state["tool_trace"]]
    rule_ids = {f.policy_rule_id for f in state.get("findings") or []}
    assert "UW_DOCS_ACTION_REQUIRED" in rule_ids, rule_ids
    assert store.get_case_stage(CASE).stage in ("CREDIT",) or str(store.get_case_stage(CASE).stage).endswith("CREDIT")


# ==========================================================================
# every product on its own checklist: personal loan, home loan, loan against property
# ==========================================================================

def _property_case(store, product, *, deed=True, months=6):
    seed(store, co=True, product=product)
    complete_applicant(store, skip=("BANK_STATEMENT",))
    doc(store, "BANK_STATEMENT", fields=bank_fields(months=months))
    if deed:
        doc(store, "SALE_DEED")
    complete_co_applicant(store, skip=("BANK_STATEMENT",))
    doc(store, "BANK_STATEMENT", party=CO, role="CO_APPLICANT", fields=bank_fields(months=months))


@pytest.mark.parametrize("product, months, version", [("HOME_LOAN", 6, "HL-CREDIT-0.1.0-SAMPLE"),
                                                      ("LOAN_AGAINST_PROPERTY", 12, "LAP-CREDIT-0.1.0-SAMPLE")])
def test_property_loans_use_their_own_policy_and_are_complete_with_the_deed(store, product, months, version):
    _property_case(store, product, months=months)
    r = evaluate(store)
    assert r.policy_version == version and r.product == product
    assert r.overall_status == "COMPLETE", [i.reason for i in r.requirements]


@pytest.mark.parametrize("product", ["HOME_LOAN", "LOAN_AGAINST_PROPERTY"])
def test_property_loans_need_the_sale_deed_from_the_applicant_not_the_co_applicant(store, product):
    _property_case(store, product, deed=False, months=12)
    open_items = evaluate(store).requirements
    assert open_items and all(i.party_role == "PRIMARY_APPLICANT" for i in open_items)
    assert all("SALE_DEED" in (i.missing_documents or []) and i.severity == "BLOCKING" for i in open_items)


def test_each_product_has_its_own_bank_statement_months(store):
    _property_case(store, "LOAN_AGAINST_PROPERTY", months=6)            # LAP asks for 12
    found = items(evaluate(store), rule_id="RULE-06")
    assert found and all("12 months are needed" in i.reason and i.severity == "NON_BLOCKING" for i in found)


def test_a_self_employed_applicants_conditions_do_not_apply_to_the_co_applicant(store):
    seed(store, co=True, employment="SELF_EMPLOYED")
    complete_applicant(store, skip=("SALARY_SLIP",))
    doc(store, "ITR")
    doc(store, "ITR_COMPUTATION")
    doc(store, "BUSINESS_PROOF_1")
    doc(store, "BUSINESS_PROOF_2")
    complete_co_applicant(store)                                        # a salaried co-applicant, with a salary slip
    assert evaluate(store).requirements == []


def test_the_checklist_shows_each_files_format_and_size():
    from app.agents.docs_verify_credit_agent.services.board_service import _file_line

    assert _file_line("The PAN file is a JPEG of 408 KB.") == "JPEG · 408 KB"
    assert _file_line("The bank statement file is a PDF of 2.4 MB.") == "PDF · 2.4 MB"
    assert _file_line("The PAN of the applicant is verified.") is None



# ==========================================================================
# the co-applicant's own employment
# ==========================================================================

def test_the_co_applicants_employment_is_stored_and_read_back(store):
    seed(store, co=True, co_job="SELF_EMPLOYED")
    assert store.get_application(CASE).co_applicant_employment_type == "SELF_EMPLOYED"


def test_a_self_employed_co_applicant_needs_itr_and_business_photos(store):
    seed(store, co=True, co_job="SELF_EMPLOYED")
    complete_applicant(store)
    complete_co_applicant(store)                                        # salary slip, no ITR, no photos
    co_items = items(evaluate(store), party_role="CO_APPLICANT")
    slots = {i.check_id for i in co_items}
    assert {"CO_APPLICANT:BUSINESS_PROOF_1", "CO_APPLICANT:BUSINESS_PROOF_2"} <= slots
    assert any("ITR" in (i.missing_documents or []) for i in co_items)
    assert all(i.severity == "BLOCKING" for i in co_items)              # missing documents: critical


def test_a_self_employed_co_applicant_with_their_documents_is_complete(store):
    seed(store, co=True, co_job="SELF_EMPLOYED")
    complete_applicant(store)
    complete_co_applicant(store, skip=("SALARY_SLIP",))
    for kind in ("ITR", "ITR_COMPUTATION", "BUSINESS_PROOF_1", "BUSINESS_PROOF_2"):
        doc(store, kind, party=CO, role="CO_APPLICANT")
    assert evaluate(store).requirements == []


def test_the_co_applicants_employment_never_changes_the_applicants_checklist(store):
    seed(store, co=True, co_job="SELF_EMPLOYED")                        # a SALARIED applicant
    complete_applicant(store)
    assert items(evaluate(store), party_role="PRIMARY_APPLICANT") == []


# ==========================================================================
# every sentence, rule name and error is config (config/reasons.yaml)
# ==========================================================================

def _reasons_with(tmp_path, monkeypatch, edit):
    from app.agents.docs_verify_credit_agent.config import reasons as T

    text = T.PATH.read_text(encoding="utf-8")
    broken = tmp_path / "reasons.yaml"
    broken.write_text(edit(text), encoding="utf-8")
    monkeypatch.setattr(T, "PATH", broken)
    T.get.cache_clear()
    return T


def test_every_reason_and_error_template_renders(monkeypatch):
    import string
    from datetime import date as _date

    from app.agents.docs_verify_credit_agent.config import reasons as T

    T.get.cache_clear()
    sample = {"start": _date(2026, 1, 1), "end": _date(2026, 6, 30), "when": _date(2026, 8, 12),
              "expiry": _date(2026, 9, 11), "size_mb": 12.04, "max_mb": 10.0, "size_kb": 3.2, "min_kb": 20.0,
              "mb": 1.5, "kb": 320.0}
    for section, keys in T.FIELDS.items():
        for key, allowed in keys.items():
            fields = {f: sample.get(f, f"<{f}>") for f in allowed}
            text = (T.say if section == "reasons" else T.error)(key, **fields)
            assert text and "{" not in text and "}" not in text, (section, key, text)
            used = {f for _, f, _, _ in string.Formatter().parse(T.get()[section][key]) if f}
            assert used <= allowed, (section, key, used - allowed)
    assert [T.rule_name(r) for r in T.rule_ids()][5] == "Period and Freshness" and len(T.rule_ids()) == 11


def test_reasons_config_missing_a_reason_is_refused_by_name(tmp_path, monkeypatch):
    from app.agents.docs_verify_credit_agent.config.reasons import ReasonsConfigError

    T = _reasons_with(tmp_path, monkeypatch, lambda t: "\n".join(
        line for line in t.splitlines() if not line.lstrip().startswith("missing:")))
    with pytest.raises(ReasonsConfigError, match="reasons.missing is missing"):
        T.get()
    T.get.cache_clear()


def test_reasons_config_with_an_unknown_placeholder_is_refused(tmp_path, monkeypatch):
    from app.agents.docs_verify_credit_agent.config.reasons import ReasonsConfigError

    T = _reasons_with(tmp_path, monkeypatch, lambda t: t.replace("is missing. Accepted: {accepted}.",
                                                                 "is missing. {pan_number}."))
    with pytest.raises(ReasonsConfigError, match="unknown field"):
        T.get()
    T.get.cache_clear()


def test_reasons_config_missing_a_rule_name_is_refused(tmp_path, monkeypatch):
    from app.agents.docs_verify_credit_agent.config.reasons import ReasonsConfigError

    T = _reasons_with(tmp_path, monkeypatch, lambda t: t.replace('  RULE-11: "File Size and Format"\n', ""))
    with pytest.raises(ReasonsConfigError, match="RULE-11"):
        T.get()
    T.get.cache_clear()


def test_a_reworded_reason_reaches_the_answer(store, tmp_path, monkeypatch):
    T = _reasons_with(tmp_path, monkeypatch, lambda t: t.replace(
        'missing: "The {what} of {who} is missing. Accepted: {accepted}."',
        'missing: "{Who} has not given the {what} yet. Accepted: {accepted}."'))
    try:
        seed(store)
        complete_applicant(store, skip=("PAN",))
        [item] = evaluate(store).requirements
        assert item.reason == "The applicant has not given the PAN yet. Accepted: PAN."
    finally:
        T.get.cache_clear()


def test_no_sentence_is_written_in_the_rules_code():
    """The rules write reasons only through config/reasons.yaml (no f-string sentence left in the services)."""
    import re as _re

    root = Path(__file__).resolve().parents[2] / "app" / "agents" / "docs_verify_credit_agent"
    for f in [*(root / "services").glob("*.py"), *(root / "config").glob("*.py"), *(root / "api").glob("*.py")]:
        code = f.read_text(encoding="utf-8")
        assert not _re.search(r'reason=f"|EvaluationError\("[A-Z_]+",\s*f?"[A-Z]', code), f.name
