"""
PHASE 3 STEP 6f -- "verify karna hai" diagnoses the case (COPILOT_VERIFY_DIAGNOSE, default off;
CHATBOT_SPEC section 4). Never "which document?": step 4's document action view for every party,
❌ fix first -> ⏳ pending -> ℹ️ under review; nothing left -> "✅ All documents are verified...".
Phrases: applicant_agent.yaml chatbot.verify_diagnose.
"""

from __future__ import annotations

import pytest

from app.agents.applicant.copilot.answering import document_actions
from tests.integration.test_fos_stage_boundary import open_case
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


def ask(client, a, c, message):
    r = client.post("/api/v1/fos/copilot", json={"applicant_id": a, "case_id": c, "message": message})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("COPILOT_VERIFY_DIAGNOSE", "true")


def test_flag_off_is_unchanged(client):
    a, c = open_case(client)
    body = ask(client, a, c, "verify karna hai")
    assert not body.get("verify_diagnose")


@pytest.mark.parametrize("phrase", ["verify karna hai", "kya upload karu", "KYC complete karna hai",
                                    "document dalna hai", "upload karna hai bhai"])
def test_a_verify_or_upload_request_is_diagnosed(client, on, phrase):
    a, c = open_case(client)
    body = ask(client, a, c, phrase)
    assert body.get("verify_diagnose") is True and body["intent"] == "PENDING_ITEMS", body["intent"]
    assert "document_actions" in body
    assert "which document" not in body["answer"].lower()


def test_the_phrases_are_whole_words_from_config():
    assert document_actions.asks_to_verify("mujhe verify karna hai")
    assert not document_actions.asks_to_verify("PAN verified hai kya?")


def test_nothing_left_says_all_done(client, on, monkeypatch):
    monkeypatch.setattr(document_actions, "build", lambda *a, **k: {"reupload": [], "pending": [],
                                                                    "under_review": [], "kyc_issues": []})
    a, c = open_case(client)
    body = ask(client, a, c, "verify karna hai")
    assert body["answer"].startswith("✅ All documents are verified."), body["answer"]


def test_fix_first_then_pending_then_under_review():
    view = {"kyc_issues": [], "reupload": [{"party": "A", "party_label": "Applicant", "label": "PAN",
                                            "reasons": ["blurred"]}],
            "pending": [{"party": "A", "party_label": "Applicant", "label": "Address Proof"}],
            "under_review": [{"party": "A", "party_label": "Applicant", "label": "Salary Slip", "state": "REVIEW"}]}
    text = document_actions.render(view)["answer"]
    assert text.index("PAN") < text.index("Address Proof") < text.index("Salary Slip")


# ---- user, 2026-10-07: anything in REVIEW or FAILED anywhere is listed, KYC first -----------------------------
def test_review_and_failed_documents_and_kyc_review_are_all_listed(client, on, monkeypatch, _store):
    from app.store.models import CaseFinding, Document, DocumentStatus, FindingKind

    monkeypatch.setenv("COPILOT_DOCUMENT_ACTIONS", "true")
    a, c = open_case(client)
    _store.save_document(Document(document_id=f"{c}:{a}:pan", case_id=c, applicant_id=a, party_id=a,
                                  document_type="PAN", status=DocumentStatus.REJECTED, verification_status="FAIL",
                                  reason_codes=["DOCUMENT_UNREADABLE"]))
    _store.save_document(Document(document_id=f"{c}:{a}:addr", case_id=c, applicant_id=a, party_id=a,
                                  document_type="ADDRESS_PROOF", status=DocumentStatus.REVIEW,
                                  verification_status="REVIEW", reason_codes=["NAME_MISMATCH"]))
    _store.save_finding(CaseFinding(finding_id="k-6f", case_id=c, party_id=a, finding_kind=FindingKind.KYC,
                                    status="REVIEW", reason_codes=["NAME_MISMATCH"], content_hash="k-6f"))
    answer = ask(client, a, c, "verify karna hai")["answer"]
    lines = answer.splitlines()

    assert "needs review" in lines[0] and "KYC" in lines[0]                        # the KYC verdict first
    assert any(line.startswith("📄 Address Proof [Upload] —") for line in lines)   # REVIEW: upload again, with why
    assert any("PAN" in line and "[Upload] —" in line for line in lines)            # FAIL: upload again, with why
    assert answer.count("Address Proof") == 1                                      # never pending AND to fix
    assert "no action needed" not in answer
