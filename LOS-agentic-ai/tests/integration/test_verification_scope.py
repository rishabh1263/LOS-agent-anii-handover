"""
Document integrity is not issuer authenticity, and every surface says so.

A PAN whose format and fields are consistent, or a bank statement whose
balance reconciles, has passed INTEGRITY checks. Neither is evidence that
the government issued the PAN or the bank produced the statement, and this
service has no issuer source to ask. So every verified identity and
financial document carries:

    verification_scope   what the verdict rests on
    authenticity         NOT_ESTABLISHED
    issuer_verified      false

-- in the LOS response, the FOS upload outcome, the persisted finding and
the Copilot's own words. And where authenticity evidence is REQUIRED
(policy REQUIRE_EXTERNAL) a PASS becomes REVIEW for financial documents
too, not only identity ones: the evidence the policy asks for does not
exist here, so a human looks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.verification import authenticity

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
PAN = SAMPLES / "real_batch" / "pan_bw2.jpg"
STATEMENT = SAMPLES / "documents" / "demo_bank_statement.pdf"

pytestmark = pytest.mark.skipif(not (PAN.exists() and STATEMENT.exists()),
                                reason="sample documents not available")

FOS_SCOPES = ["read_applicant", "read_application", "read_documents",
              "read_verification", "read_pending_items", "read_next_action",
              "create_applicant", "create_application", "upload_document"]


# ==========================================================================
# THE CONTRACT
# ==========================================================================


@pytest.mark.parametrize("document_class,scope", [
    ("PAN", "DOCUMENT_STRUCTURE_AND_FIELD_CONSISTENCY"),
    ("PASSPORT", "DOCUMENT_STRUCTURE_AND_FIELD_CONSISTENCY"),
    ("BANK_STATEMENT", "DOCUMENT_STRUCTURE_AND_BALANCE_RECONCILIATION"),
    ("SALARY_SLIP", "DOCUMENT_STRUCTURE_AND_PAY_ARITHMETIC"),
])
def test_every_issued_document_states_its_scope(document_class, scope):
    assert authenticity.scope(document_class) == {
        "verification_scope": scope, "authenticity": "NOT_ESTABLISHED",
        "issuer_verified": False}


def test_no_issuer_source_is_configured():
    assert authenticity.ISSUER_VERIFICATION_AVAILABLE is False


@pytest.mark.parametrize("document_class", ["PAN", "BANK_STATEMENT", "SALARY_SLIP", "ITR"])
def test_where_authenticity_is_required_a_pass_becomes_review(document_class, monkeypatch):
    monkeypatch.setenv("VERIFICATION_AUTHENTICITY_POLICY", "REQUIRE_EXTERNAL")

    assert authenticity.cap(document_class, "PASS", []) == (
        "REVIEW", ["AUTHENTICITY_NOT_ESTABLISHED"])
    # Only ever a downgrade, and only of a PASS.
    assert authenticity.cap(document_class, "FAIL", ["X"]) == ("FAIL", ["X"])
    assert authenticity.cap(document_class, "REVIEW", ["X"]) == ("REVIEW", ["X"])


def test_by_default_a_pass_stays_a_pass_but_says_what_it_rests_on(monkeypatch):
    monkeypatch.delenv("VERIFICATION_AUTHENTICITY_POLICY", raising=False)

    assert authenticity.cap("BANK_STATEMENT", "PASS", []) == ("PASS", [])


# ==========================================================================
# THROUGH THE REAL ROUTES
# ==========================================================================


@pytest.fixture
def store(tmp_path, monkeypatch):
    from app.agents.applicant import config as agent_config
    from app.agents.los import config as los_config
    from app.store import set_repository
    from app.store.sqlite_repo import SQLiteRepository

    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    los_config.reload()
    agent_config.reload()
    repository = SQLiteRepository(tmp_path / "scope.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)
    monkeypatch.undo()
    los_config.reload()
    agent_config.reload()


def process(make_token, path, declared, case_id):
    import main

    client = TestClient(main.app)
    client.headers.update({"Authorization": f"Bearer {make_token(scopes=['los.read', 'los.write'])}"})
    response = client.post("/api/v1/los/process", data={
        "operation": "PROCESS", "applicant_id": "APP-S", "case_id": case_id,
        "expected_types": declared},
        files=[("files", (path.name, path.read_bytes(), "application/octet-stream"))])
    assert response.status_code == 200, response.text
    return response.json()["documents"][0]


@pytest.mark.parametrize("path,declared,scope", [
    (PAN, "PAN", "DOCUMENT_STRUCTURE_AND_FIELD_CONSISTENCY"),
    (STATEMENT, "BANK_STATEMENT", "DOCUMENT_STRUCTURE_AND_BALANCE_RECONCILIATION"),
])
def test_a_pass_is_published_with_its_scope(store, make_token, path, declared, scope):
    document = process(make_token, path, declared, f"CASE-{declared}")

    assert document["verification"] == "PASS"
    assert document["verification_scope"] == scope
    assert document["authenticity"] == "NOT_ESTABLISHED"
    assert document["issuer_verified"] is False

    # And it is stored that way: the finding says what the PASS rests on.
    finding = next(f for f in store.get_current_findings(f"CASE-{declared}")
                   if f.finding_kind.value == "VERIFICATION")
    assert finding.payload["verification_scope"] == scope
    assert finding.payload["issuer_verified"] is False


@pytest.mark.parametrize("path,declared", [(PAN, "PAN"), (STATEMENT, "BANK_STATEMENT")])
def test_where_authenticity_is_required_the_document_is_reviewed(
        store, make_token, monkeypatch, path, declared):
    monkeypatch.setenv("VERIFICATION_AUTHENTICITY_POLICY", "REQUIRE_EXTERNAL")

    document = process(make_token, path, declared, f"CASE-REQ-{declared}")

    assert document["verification"] == "REVIEW"
    assert "AUTHENTICITY_NOT_ESTABLISHED" in document["reason_codes"]
    assert not document.get("extraction"), "fields released behind a REVIEW"
    assert document["issuer_verified"] is False


def test_the_fos_upload_outcome_states_the_scope(store, make_token):
    import main

    client = TestClient(main.app)
    client.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES)}"})
    opened = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "Scope Test", "mobile": "9876543210"},
        "application": {"product": "PERSONAL_LOAN"}}).json()

    outcome = client.post("/api/v1/fos/copilot", data={
        "applicant_id": opened["applicant_id"], "case_id": opened["case_id"],
        "action": "UPLOAD_DOCUMENT", "document_type": "BANK_STATEMENT"},
        files={"file": ("statement.pdf", STATEMENT.read_bytes(), "application/pdf")}
    ).json()["verification"]

    assert outcome["verification"] == "PASS"
    assert outcome["verification_scope"] == "DOCUMENT_STRUCTURE_AND_BALANCE_RECONCILIATION"
    assert outcome["authenticity"] == "NOT_ESTABLISHED"
    assert outcome["issuer_verified"] is False

    # And the Copilot, asked about it, never presents a pass as issuance.
    for question in ("Is my bank statement verified?", "What is my verification status?"):
        answer = client.post("/api/v1/copilot/query", json={
            "applicant_id": opened["applicant_id"], "case_id": opened["case_id"],
            "message": question}).json()["answer"]
        from app.agents.applicant.answer import INTEGRITY_ONLY
        assert INTEGRITY_ONLY in answer, (question, answer)
        for claim in ("genuine", "authentic", "issued by the bank", "confirmed by the bank"):
            assert claim not in answer.lower().replace("the issuing authority has not confirmed", "")


def test_no_response_ever_claims_the_issuer_confirmed(store, make_token):
    body = process(make_token, STATEMENT, "BANK_STATEMENT", "CASE-NEVER")

    assert '"issuer_verified": true' not in json.dumps(body)
