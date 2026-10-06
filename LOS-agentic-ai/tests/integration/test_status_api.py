"""
THE FRONTEND STATUS CONTRACT, through the real HTTP API on real samples (2026-10-06).

Progress is the recorded state, never estimated; KYC/eligibility/credit come from
recorded findings; JEV signals are business-safe; reviews say what a person must
do; every read is ownership-checked with one refusal for "not yours" and "absent".
"""

from __future__ import annotations

import pytest

from tests.integration.test_reupload_supersedes import (  # noqa: F401
    FOS_SCOPES, OTHER_PAN, RISHABH_DL, RISHABH_PAN, _store, client, open_case, upload)

INTERNAL = ("prompt", "probabilities", "reasoning", "chain", "trace", "evidence_version", "jev_run_id")


@pytest.fixture
def case(client):
    a, c = open_case(client)
    # a verified PAN and a licence uploaded as a bank statement (fails its declared type)
    upload(client, a, c, [("pan.jpg", RISHABH_PAN), ("dl.jpg", RISHABH_DL)], ["PAN", "BANK_STATEMENT"])
    return a, c


def test_verification_status_is_the_real_per_document_state(client, case):
    _, c = case
    body = client.get(f"/api/v1/applications/{c}/verification-status").json()
    by_type = {d["type"]: d for d in body["documents"]}
    assert by_type["PAN"]["status"] == "VERIFIED" and by_type["PAN"]["action"] is None
    rejected = [d for d in body["documents"] if d["status"] == "REJECTED"]
    assert rejected and all(d["action"] == "REUPLOAD" for d in rejected)
    assert all(d["action"] == "UPLOAD" for d in body["documents"] if d["status"] == "PENDING")
    p = body["progress"]
    assert p["total"] == len(body["documents"]) and p["completed"] == p["verified"] + p["review"] + p["rejected"]
    assert body["overall_status"] == "ACTION_REQUIRED"
    # no file name, path or extracted value outside the opaque document_id
    shown = [{k: v for k, v in d.items() if k != "document_id"} for d in body["documents"]]
    assert "extracted_fields" not in str(body) and ".jpg" not in str(shown)


def test_summary_is_one_stable_frontend_view(client, case):
    _, c = case
    body = client.get(f"/api/v1/applications/{c}/summary").json()
    assert set(body) == {"request_id", "application", "documents", "kyc", "eligibility", "credit",
                         "risk_signals", "reviews", "next_actions"}
    assert body["documents"]["verified"] >= 1 and body["documents"]["rejected"] >= 1
    assert body["kyc"]["status"] in {"PASS", "PARTIAL", "REVIEW", "FAIL", "PENDING", "CONFIGURATION_GAP",
                                     "NOT_STARTED"}
    assert body["credit"]["status"] == "NOT_STARTED" and body["credit"]["is_decision"] is False
    assert any(a["action"] == "REUPLOAD" for a in body["next_actions"])


def test_reviews_name_reason_documents_stage_and_action(client, case):
    _, c = case
    body = client.get(f"/api/v1/applications/{c}/reviews").json()
    rejected = [r for r in body["reviews"] if r["review_type"] == "DOCUMENT_REJECTED"]
    assert rejected
    r = rejected[0]
    assert r["reason"] and r["affected_documents"] and r["assigned_stage"] and r["recommended_action"]
    assert r["severity"] in {"LOW", "MEDIUM", "HIGH", "CRITICAL"} and r["status"] == "OPEN"


def test_risk_signals_are_empty_and_honest_before_jev_ran(client, case):
    _, c = case
    body = client.get(f"/api/v1/applications/{c}/risk-signals").json()
    assert body["jev_status"] == "NOT_EVALUATED" and body["signals"] == []
    assert body["authoritative_statuses_changed"] is False
    assert not any(k in str(body).lower() for k in INTERNAL)


def test_risk_signals_carry_business_fields_only(client, case, monkeypatch):
    from app.jev import engine

    _, c = case
    monkeypatch.setattr(engine, "latest_decisions", lambda case_id, party_id=None: {
        "jev_status": "COMPLETED", "evaluated_at": "2026-10-06T00:00:00Z", "jev_run_id": "x",
        "semantic_decisions": [
            {"decision_type": "SEMANTIC_REVIEW", "answer": True, "confidence": 0.91, "confidence_band": "AUTO",
             "status": "OPEN", "severity": "MEDIUM", "recommended_action": "MANUAL_REVIEW", "target": "CASE",
             "probabilities": {"true": 0.91}},
            {"decision_type": "SEMANTIC_SEVERITY", "answer": "MEDIUM", "confidence": 0.7},
            {"decision_type": "FINANCIAL_INCONSISTENCY", "answer": False, "confidence": 0.8}]})
    body = client.get(f"/api/v1/applications/{c}/risk-signals").json()
    [signal] = body["signals"]
    assert signal["signal_type"] == "SEMANTIC_REVIEW" and signal["status"] == "OPEN"
    assert signal["confidence"] == 0.91 and signal["summary"]
    assert not any(k in str(body).lower() for k in INTERNAL)


def test_document_and_job_reads_are_ownership_checked(client, case, make_token):
    _, c = case
    doc = client.get(f"/api/v1/applications/{c}/verification-status").json()["documents"][0]
    mine = client.get(f"/api/v1/documents/{doc['document_id']}")
    assert mine.status_code == 200 and mine.json()["status"] == doc["status"]

    other = {"Authorization": f"Bearer {make_token(subject='someone-else', scopes=FOS_SCOPES)}"}
    refused = client.get(f"/api/v1/documents/{doc['document_id']}", headers=other)
    absent = client.get("/api/v1/documents/CASE-NOPE:APP:x.jpg", headers=other)
    assert refused.status_code == absent.status_code == 403
    assert refused.json()["detail"]["code"] == absent.json()["detail"]["code"]          # no existence disclosure
    for path in ("verification-status", "summary", "risk-signals", "reviews"):
        assert client.get(f"/api/v1/applications/{c}/{path}", headers=other).status_code == 403
    assert client.get("/api/v1/jobs/ocr_doesnotexist").status_code == 403
    assert client.get(f"/api/v1/applications/{c}/summary", headers={"Authorization": ""}).status_code == 401
