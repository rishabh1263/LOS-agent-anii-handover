"""
PHASE 3 STEP 4 -- only the documents that need action (COPILOT_DOCUMENT_ACTIONS, default off).

Real uploads through the real API: a PAN and a driving licence of two different
people (a real KYC name mismatch) and no bank statement. The answer lists the
mismatch with both recorded values, the documents to upload again, what is still
pending -- and never a verified document for upload.
"""

from __future__ import annotations

import pytest

from app.agents.applicant.copilot.answering import document_actions
from tests.integration.test_fos_stage_boundary import open_case, upload
from tests.integration.test_reupload_supersedes import OTHER_PAN, RISHABH_DL, RISHABH_PAN, _store, client  # noqa: F401


@pytest.fixture(autouse=True)
def flag_off(monkeypatch):
    monkeypatch.delenv(document_actions.FLAG, raising=False)


def ask(client, a, c, message):
    r = client.post("/api/v1/fos/copilot", json={"applicant_id": a, "case_id": c, "action": "CUSTOM_QUERY",
                                                 "message": message})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def mismatch_case(client):
    a, c = open_case(client)
    upload(client, a, c, [("pan.jpg", OTHER_PAN), ("dl.jpg", RISHABH_DL)], ["PAN", "DRIVING_LICENCE"])
    return a, c


def test_flag_off_the_answer_is_unchanged(client, mismatch_case, monkeypatch):
    # the Phase 3 flags ship ON (config): OFF is set here, as the test's own subject
    monkeypatch.setenv(document_actions.FLAG, "false")
    body = ask(client, *mismatch_case, "what is pending?")
    assert "Please upload the correct documents" not in body["answer"]
    assert "document_actions" not in body["presentation"]


def test_only_documents_needing_action_with_exact_mismatch_values(client, mismatch_case, monkeypatch):
    monkeypatch.setenv(document_actions.FLAG, "true")
    body = ask(client, *mismatch_case, "what is pending?")
    answer = body["answer"]
    assert "Name mismatch" in answer and "LAXMI SANTOSH GUPTA" in answer and "RISHABH AJIT SINGH" in answer, answer
    assert "Please upload the correct documents:" in answer
    assert "📄 PAN [Upload]" in answer and "📄 Driving Licence [Upload]" in answer
    assert "Still pending:" in answer and "Bank Statement" in answer
    view = body["presentation"]["document_actions"]
    # SIGNATURE is mandatory for every product (owner 2026-10-09), so it is pending beside the bank statement
    assert {r["document_type"] for r in view["pending"]} == {"BANK_STATEMENT", "SIGNATURE"}
    assert all(r["action"]["type"] == "UPLOAD_DOCUMENT" for r in view["reupload"] + view["pending"])
    assert view["emphasis"] and view["emphasis"][0] == "Name"


def test_hinglish_question_gets_hinglish_headings(client, mismatch_case, monkeypatch):
    monkeypatch.setenv(document_actions.FLAG, "true")
    answer = ask(client, *mismatch_case, "kaunse docs baaki hai?")["answer"]
    assert "Kripya sahi documents upload karein:" in answer and "Abhi baaki:" in answer, answer
    assert "Naam match nahi hota" in answer and "Janmatithi match nahi hota" in answer, answer
    assert "LAXMI SANTOSH GUPTA" in answer                     # values quoted exactly, in any language


def test_a_clean_case_offers_nothing_verified_for_upload(client, monkeypatch):
    monkeypatch.setenv(document_actions.FLAG, "true")
    a, c = open_case(client)
    upload(client, a, c, [("pan.jpg", RISHABH_PAN), ("dl.jpg", RISHABH_DL)], ["PAN", "DRIVING_LICENCE"])
    body = ask(client, a, c, "what is pending?")
    view = body["presentation"]["document_actions"]
    offered = {r["document_type"] for r in view["reupload"]}
    assert "PAN" not in offered and "DRIVING_LICENCE" not in offered, view
    assert "PAN [Upload]" not in body["answer"]


# ---- the builder and renderer on their own ----------------------------------------------
VIEW = {"kyc_issues": [{"party": "CO_APPLICANT", "party_label": "Co-applicant", "field": "DATE_OF_BIRTH",
                        "field_label": "Date of birth", "status": "FAIL",
                        "values": [{"document_type": "PAN", "label": "PAN", "value": "2004-12-20"},
                                   {"document_type": "AADHAAR", "label": "Aadhaar", "value": "2002-06-12"}]}],
        "reupload": [{"party": "CO_APPLICANT", "party_label": "Co-applicant", "document_type": "VOTER_ID",
                      "label": "Voter ID", "reasons": ["The document has expired."], "source": "VERIFICATION"}],
        "pending": [{"party": "PRIMARY_APPLICANT", "party_label": "Applicant", "document_type": "SALARY_SLIP",
                     "label": "Salary Slip"}],
        "under_review": [{"party": "PRIMARY_APPLICANT", "party_label": "Applicant", "document_type": "BANK_STATEMENT",
                          "label": "Bank Statement", "state": "REVIEW"}]}


def test_render_groups_by_party_and_keeps_review_as_info_only():
    said = document_actions.render(VIEW)
    text = said["answer"]
    assert "Co-applicant's date of birth mismatch: PAN shows \"2004-12-20\", Aadhaar shows \"2002-06-12\"" in text
    assert "📄 Co-applicant's Voter ID [Upload] — The document has expired." in text
    assert "📄 Applicant's Salary Slip [Upload]" in text
    assert "⏳ Applicant's Bank Statement" in text and "Bank Statement [Upload]" not in text
    assert said["emphasis"] == ["Date of birth", "Voter ID"]


def test_nothing_to_do_is_said_plainly():
    empty = {"kyc_issues": [], "reupload": [], "pending": [], "under_review": []}
    assert document_actions.render(empty)["answer"] == "No document needs any action right now."


def test_a_party_filter_keeps_only_that_partys_rows(client, mismatch_case, _store):
    view = document_actions.build(mismatch_case[1], party="CO_APPLICANT", repository=_store)
    assert all(not rows for rows in view.values())             # the case has no co-applicant
