"""
FOS PLAN SECTION 4 -- the KYC table. Per party: check | document | value on document | value on application |
result | reason, from recorded findings only, masked per policy, with the likely odd one out. Both endpoints.
Also: which documents feed KYC is config (kyc_policies.yaml `sources`).
"""

from __future__ import annotations

import pytest

from app.store.models import CaseFinding, Document, DocumentStatus, FindingKind
from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


@pytest.fixture
def kyc_case(client, demo, _store, monkeypatch):
    monkeypatch.setenv("COPILOT_KYC_TABLE", "true")
    a, c = make_case(client, "Rahul Sharma")
    for kind, name in (("PAN", "RAHUL SHARMA"), ("DRIVING_LICENCE", "RAHUL SHARMA"), ("BANK_STATEMENT", "ROHIT VERMA")):
        doc_id = f"{c}:{a}:{kind.lower()}"
        _store.save_document(Document(document_id=doc_id, case_id=c, applicant_id=a, party_id=a, document_type=kind,
                                      status=DocumentStatus.VERIFIED, verification_status="PASS"))
        fields = {"name": name} | ({"pan_number": "ABCDE1234F"} if kind == "PAN" else {})
        _store.save_finding(CaseFinding(finding_id=f"x-{kind}", case_id=c, party_id=a, document_id=doc_id,
                                        source_id=f"{kind.lower()}.jpg",
                                        finding_kind=FindingKind.EXTRACTION, status="PASS",
                                        payload={"fields": fields}, content_hash=f"x-{kind}"))
    _store.save_finding(CaseFinding(
        finding_id="k-table", case_id=c, party_id=a, finding_kind=FindingKind.KYC, status="REVIEW",
        reason_codes=["NAME_MISMATCH"], content_hash="k-table",
        payload={"fields": [
            {"field": "NAME", "status": "FAIL", "sources": [
                {"document_type": "PAN", "value": "RAHUL SHARMA"}, {"document_type": "DRIVING_LICENCE",
                                                                   "value": "RAHUL SHARMA"},
                {"document_type": "BANK_STATEMENT", "value": "ROHIT VERMA"}]},
            {"field": "PAN_NUMBER", "status": "PASS", "sources": [{"document_type": "PAN"}]}]}))
    client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": c})
    return a, c


@pytest.mark.parametrize("endpoint", ["/api/v1/fos/copilot", "/api/v1/copilot/query"])
def test_a_kyc_answer_carries_the_table_with_the_odd_one_out(client, kyc_case, endpoint):
    body = {"message": "KYC ka kya status hai?"} | ({"action": "CUSTOM_QUERY"} if "fos" in endpoint else {})
    reply = client.post(endpoint, json=body).json()
    answer = reply["answer"]
    # MASTER SPEC section 6 superseded the single table: A (form vs document) and B (document vs document)
    assert "| Field | Form value | Document | Document value | Result |" in answer, answer
    assert "| Field | Document 1 | Value | Document 2 | Value | Result |" in answer, answer
    assert "| Name | Rahul Sharma | PAN | RAHUL SHARMA | Match |" in answer
    assert "| Name | Rahul Sharma | Bank Statement | ROHIT VERMA | Differs from the application |" in answer
    assert "| Name | PAN | RAHUL SHARMA | Bank Statement | ROHIT VERMA | Differs |" in answer
    assert answer.split("\n")[1 if answer.startswith("📍") else 0] == "Not ready for CPA. KYC is not complete."
    assert "Likely odd one out: the Bank Statement (name)" in answer
    assert "ABCDE1234F" not in str(reply)                                     # the PAN number is masked


def test_the_table_is_structured_for_the_frontend(client, kyc_case):
    reply = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY",
                                                      "message": "KYC ka kya status hai?"}).json()
    party = reply["presentation"]["kyc_table"]["parties"][0]
    assert party["party_role"] == "PRIMARY_APPLICANT"
    assert {r["document_type"] for r in party["rows"] if r["check"] == "NAME"} == {"PAN", "DRIVING_LICENCE",
                                                                                   "BANK_STATEMENT"}
    assert party["odd_one_out"][0]["fix"] == {"action": "UPLOAD_DOCUMENT", "document_type": "BANK_STATEMENT"}


def test_the_kyc_sources_are_config():
    from app.agents.kyc import config

    assert "BANK_STATEMENT" in config.source_keys("document_types", ())
    assert "account_holder" in config.source_keys("name_fields", ())
