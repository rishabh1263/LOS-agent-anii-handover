"""
APPLICATION -> EVERY AUTHORIZED CASE, summarised (copilot/capabilities/portfolio.py),
over the real FOS HTTP route:

  - the owner of an applicant gets every case: count, per-case status,
    verification counts, KYC, blockers, next step, latest vs previous
  - the documents and findings of all cases are read in ONE query each
    (no N+1 over the portfolio)
  - a caller granted ONE case sees that case only: the other is neither
    counted nor named -- its existence is never disclosed
  - another customer's applicant id is refused before any read
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import config as agent_config
from app.store import set_repository
from app.store.models import (Applicant, Application, ApplicationStatus, CaseFinding, Document,
                              DocumentStatus, FindingKind)
from app.store.testing import fresh_repository

APP, OTHER_APP = "APP-PORT0000001", "APP-PORTOTHER01"
CASE_OLD, CASE_NEW, CASE_OTHER = "CASE-PORT-OLD01", "CASE-PORT-NEW01", "CASE-PORT-OTH01"
SCOPES = ["read_applicant", "read_application", "read_documents", "read_verification",
          "read_pending_items", "read_next_action"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    agent_config.reload()
    repository = fresh_repository(tmp_path / "portfolio.sqlite3")
    repository.initialise()
    set_repository(repository)
    now = datetime.now(timezone.utc)
    repository.save_applicant(Applicant(applicant_id=APP, full_name="Asha Rao"))
    repository.save_applicant(Applicant(applicant_id=OTHER_APP, full_name="Other Person"))
    for case_id, status, product, applicant, opened in (
            (CASE_OLD, ApplicationStatus.DOCUMENT_COLLECTION, "HOME_LOAN", APP, now - timedelta(days=30)),
            (CASE_NEW, ApplicationStatus.BASIC_DOCUMENT_VERIFICATION, "PERSONAL_LOAN", APP, now),
            (CASE_OTHER, ApplicationStatus.DOCUMENT_COLLECTION, "PERSONAL_LOAN", OTHER_APP, now)):
        repository.save_application(Application(case_id=case_id, applicant_id=applicant,
                                                status=status, product=product, created_at=opened))
    repository.save_document(Document(
        document_id=f"{CASE_NEW}:{APP}:pan.jpg", case_id=CASE_NEW, applicant_id=APP, party_id=APP,
        document_type="PAN", status=DocumentStatus.REJECTED, verification_status="FAIL",
        reason_codes=["DOCUMENT_TYPE_MISMATCH"], uploaded_at=now))
    repository.save_document(Document(
        document_id=f"{CASE_OLD}:{APP}:pan.jpg", case_id=CASE_OLD, applicant_id=APP, party_id=APP,
        document_type="PAN", status=DocumentStatus.VERIFIED, verification_status="PASS",
        uploaded_at=now - timedelta(days=30)))
    repository.save_finding(CaseFinding(
        finding_id="F-KYC-NEW", case_id=CASE_NEW, party_id=APP, finding_kind=FindingKind.KYC,
        status="REVIEW", reason_codes=["NAME_MISMATCH"], content_hash="kyc-new"))
    yield repository
    set_repository(None)
    agent_config.reload()


def _client(make_token, subject: str) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers["Authorization"] = "Bearer " + make_token(subject=subject, scopes=SCOPES)
    return c


def _ask(client, applicant_id, case_id, message):
    response = client.post("/api/v1/fos/copilot", json={
        "applicant_id": applicant_id, "case_id": case_id, "action": "CUSTOM_QUERY", "message": message})
    assert response.status_code == 200, response.text
    return response.json()


def test_the_owner_gets_every_case_summarised(repo, make_token, monkeypatch):
    repo.grant_access("owner-1", "APPLICANT", APP)
    calls = {"documents": 0, "findings": 0}
    real_docs, real_findings = repo.list_documents_for_cases, repo.get_current_findings_for_cases

    def docs(case_ids):
        calls["documents"] += 1
        return real_docs(case_ids)

    def finds(case_ids, kind=None):
        calls["findings"] += 1
        return real_findings(case_ids, kind=kind)

    monkeypatch.setattr(repo, "list_documents_for_cases", docs)
    monkeypatch.setattr(repo, "get_current_findings_for_cases", finds)
    body = _ask(_client(make_token, "owner-1"), APP, CASE_NEW, f"{APP} ke saare cases ka summary do")

    assert body["response_type"] == "CASE_PORTFOLIO"
    portfolio = body["portfolio"]
    assert portfolio["count"] == 2 and portfolio["blocked"] == 1
    latest, previous = portfolio["cases"]
    assert latest["position"] == "LATEST" and previous["position"] == "PREVIOUS"
    assert {c["case_id"] for c in portfolio["cases"]} == {CASE_OLD, CASE_NEW}
    new = next(c for c in portfolio["cases"] if c["case_id"] == CASE_NEW)
    assert new["verification"] == {"FAIL": 1} and new["kyc_status"] == "REVIEW"
    assert new["next_action"]["action"] == "UPLOAD_DOCUMENT"
    assert body["answer"].startswith("Across 2 cases:")
    assert CASE_OTHER not in str(body) and "Other Person" not in str(body)
    assert calls == {"documents": 1, "findings": 1}           # batched, not one read per case


def test_the_summary_is_point_wise_with_case_ids_when_style_is_on(repo, make_token, monkeypatch):
    """COPILOT_RESPONSE_STYLE: one block per case, led by its CASE ID (not "Latest / Previous")."""
    monkeypatch.setenv("COPILOT_RESPONSE_STYLE", "true")
    repo.grant_access("owner-1", "APPLICANT", APP)
    answer = _ask(_client(make_token, "owner-1"), APP, CASE_NEW, f"{APP} ke saare cases ka summary do")["answer"]
    print("\n" + answer)

    assert "📂 **2 cases** · ⚠️ 1 blocked" in answer
    assert answer.index(CASE_NEW) < answer.index(CASE_OLD)                       # latest first
    assert f"**1. {CASE_NEW}** · Personal Loan" in answer
    assert "**: review" in answer and "• ⚠️ Blocking:" in answer          # KYC expanded on first mention (6e)
    assert "Latest --" not in answer and "Previous --" not in answer
    assert CASE_OTHER not in answer


def test_a_caller_granted_one_case_never_learns_of_the_other(repo, make_token):
    repo.grant_access("officer-1", "CASE", CASE_NEW)
    response = _client(make_token, "officer-1").post("/api/v1/fos/copilot", json={
        "applicant_id": APP, "case_id": CASE_NEW, "action": "CUSTOM_QUERY",
        "message": "mere saare cases ka summary do"})
    text = response.text
    assert CASE_OLD not in text and "HOME_LOAN" not in text and "Home Loan" not in text
    if response.status_code == 200:
        portfolio = response.json().get("portfolio")
        assert portfolio is None or portfolio["count"] <= 1


def test_another_customers_applicant_is_refused_before_any_read(repo, make_token, monkeypatch):
    repo.grant_access("owner-1", "APPLICANT", APP)
    reads = []
    for name in ("list_applications", "list_documents_for_cases", "get_current_findings_for_cases"):
        real = getattr(repo, name)
        monkeypatch.setattr(repo, name, lambda *a, _real=real, _n=name, **k: (reads.append(_n), _real(*a, **k))[1])
    body = _ask(_client(make_token, "owner-1"), APP, CASE_NEW, f"{OTHER_APP} ke saare cases ka summary do")
    assert body["intent"] == "GUARDRAIL_BLOCKED"
    assert reads == []
    assert CASE_OTHER not in str(body)


# ---- ONE CASE BY POSITION, or the cases in trouble --------------------------------
@pytest.mark.parametrize("message,focus,has,lacks", [
    ("latest case ka status kya hai?", "LATEST", "Personal Loan", "Home Loan"),
    ("pichle case ka status kya hai?", "PREVIOUS", "Home Loan", "Personal Loan"),
    ("kis case mein issue hai?", "ATTENTION", "1 of your 2 cases needs attention", "Home Loan"),
    ("which application is blocked?", "ATTENTION", "Personal Loan", "Home Loan"),
])
def test_a_case_by_position_or_by_trouble(repo, make_token, message, focus, has, lacks):
    repo.grant_access("owner-1", "APPLICANT", APP)
    body = _ask(_client(make_token, "owner-1"), APP, CASE_NEW, message)
    assert body["response_type"] == "CASE_PORTFOLIO" and body["scope"] == "APPLICANT_CASES"
    assert body["portfolio"]["focus"] == focus
    assert has in body["answer"] and lacks not in body["answer"]
    assert CASE_OTHER not in str(body)


def test_no_previous_case_is_said_plainly(repo, make_token):
    repo.grant_access("officer-1", "CASE", CASE_NEW)
    repo.grant_access("owner-2", "APPLICANT", OTHER_APP)
    body = _ask(_client(make_token, "owner-2"), OTHER_APP, CASE_OTHER, "pichle case ka status?")
    assert "no previous case" in body["answer"]
    assert CASE_NEW not in str(body) and CASE_OLD not in str(body)


def test_a_one_case_grant_never_hears_of_a_previous_case(repo, make_token):
    repo.grant_access("officer-1", "CASE", CASE_NEW)
    response = _client(make_token, "officer-1").post("/api/v1/fos/copilot", json={
        "applicant_id": APP, "case_id": CASE_NEW, "action": "CUSTOM_QUERY",
        "message": "pichle case ka status kya hai?"})
    assert CASE_OLD not in response.text and "Home Loan" not in response.text


def test_the_same_opening_time_is_never_guessed_into_a_latest_case(repo, make_token):
    for case_id in (CASE_OLD, CASE_NEW):          # both opened at one recorded instant
        record = repo.get_application(case_id)
        record.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        repo._write("UPDATE applications SET created_at = ? WHERE case_id = ?",
                    (record.created_at.isoformat(), case_id))
    repo.grant_access("owner-1", "APPLICANT", APP)
    body = _ask(_client(make_token, "owner-1"), APP, CASE_NEW, "latest case ka status kya hai?")
    assert "can't tell which of these cases is the latest" in body["answer"]
    assert "Home Loan" in body["answer"] and "Personal Loan" in body["answer"]
