"""KYC's structured explanation: checks passed/failed, missing information, next actions."""

from app.agents.kyc.agent import run_kyc
from app.agents.kyc.schemas import KycRequest
from tests.agents.test_kyc_agent import dl_doc, pan_doc


def test_a_pass_lists_its_passed_checks_and_needs_no_action():
    r = run_kyc(KycRequest(applicant_id="A", documents=[pan_doc(), dl_doc()]))
    assert r.state == "PASS" and "NAME" in r.passed_checks and "DOB" in r.passed_checks
    assert r.failed_checks == [] and r.next_actions == []


def test_a_mismatch_names_the_failed_check_and_a_coded_next_action():
    r = run_kyc(KycRequest(applicant_id="A", documents=[pan_doc(), dl_doc(dob="1990-01-01")]))
    assert r.state in {"REVIEW", "FAIL"} and "DOB" in r.failed_checks
    assert r.next_actions and r.next_actions[0]["code"] in {"MANUAL_REVIEW", "COLLECT_CORRECT_DOCUMENTS"}
    assert r.next_actions[0]["label"] == r.next_action


def test_one_document_is_pending_with_an_upload_action():
    r = run_kyc(KycRequest(applicant_id="A", documents=[pan_doc()]))
    assert r.state == "PENDING" and r.next_actions[0]["code"] == "UPLOAD_DOCUMENT"


def test_checks_that_could_not_run_are_missing_information_not_failures():
    r = run_kyc(KycRequest(applicant_id="A", documents=[pan_doc(), dl_doc()]))
    assert set(r.missing_information).isdisjoint(r.failed_checks)
    assert set(r.missing_information).isdisjoint(r.passed_checks)


def test_the_explanation_is_persisted_with_the_kyc_finding():
    from app.store import ingest

    out = ingest._kyc_explanation({"state": "REVIEW", "reason": "x", "failed_checks": ["DOB"],
                                   "next_actions": [{"code": "MANUAL_REVIEW", "label": "y"}], "fields": [1]})
    assert out == {"state": "REVIEW", "reason": "x", "failed_checks": ["DOB"],
                   "next_actions": [{"code": "MANUAL_REVIEW", "label": "y"}]}
