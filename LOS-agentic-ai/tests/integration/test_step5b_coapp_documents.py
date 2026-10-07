"""
PHASE 3 STEP 5b -- co-applicant mandatory documents (LOS_COAPP_MANDATORY_DOCS, default off).

A co-applicant's mandatory documents are ONLY the configured slots
(applicant_agent.yaml readiness.co_applicant_documents): PAN, address proof (any one of
Aadhaar / passport / licence / voter ID / utility bill) and employment proof (salaried or
self-employed documents). No bank statement. Each party's slots are matched against that
party's own documents; the items count in FOS readiness and show in the document action view.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from app.agents.applicant import config as agent_config
from app.agents.applicant import workflow
from app.store.models import Document, DocumentStatus
from tests.integration.test_fos_stage_boundary import open_case, upload
from tests.integration.test_reupload_supersedes import RISHABH_DL, RISHABH_PAN, _store, client  # noqa: F401

CO = "COAPP-5B-0001"


@pytest.fixture(autouse=True)
def flags_off(monkeypatch):
    for name in (workflow.COAPP_FLAG, "COPILOT_DOCUMENT_ACTIONS"):
        monkeypatch.delenv(name, raising=False)


def add_co(repo, case_id, applicant_id, document_type, status=DocumentStatus.VERIFIED, party=CO):
    repo.save_document(Document(document_id=f"doc_{uuid.uuid4().hex[:12]}", case_id=case_id,
                                applicant_id=applicant_id, document_type=document_type, party_id=party,
                                party_role="CO_APPLICANT", status=status))


def joint(client, repo, *, upload_primary=True):
    a, c = open_case(client)
    if upload_primary:
        upload(client, a, c, [("pan.jpg", RISHABH_PAN), ("dl.jpg", RISHABH_DL)], ["PAN", "DRIVING_LICENCE"])
    repo.save_application(dataclasses.replace(repo.get_application(c), co_applicant_id=CO))
    return a, c


def state(repo, c):
    application = repo.get_application(c)
    return repo.get_applicant(application.applicant_id), application, repo.list_documents(c)


def co_items(items):
    return [i for i in items if i.get("party_role") == "CO_APPLICANT"]


def co_slots(repo, c):
    return {i["slot"]: i["code"] for i in co_items(workflow.pending_items(*state(repo, c)))}


# ---- the rule -------------------------------------------------------------------------------
def test_only_the_configured_slots_never_the_applicants_checklist():
    slots = {r.slot: set(r.accepts) for r in workflow.co_applicant_requirements()}
    assert set(slots) == {"PAN", "ADDRESS_PROOF", "EMPLOYMENT_PROOF"}          # no bank statement
    assert {"AADHAAR", "PASSPORT", "DRIVING_LICENCE", "VOTER_ID", "UTILITY_BILL"} <= slots["ADDRESS_PROOF"]
    assert {"SALARY_SLIP", "ITR", "GST_CERTIFICATE", "BUSINESS_REGISTRATION"} <= slots["EMPLOYMENT_PROOF"]
    assert all(r.mandatory for r in workflow.co_applicant_requirements())


def test_the_slots_are_configuration(monkeypatch):
    monkeypatch.setattr(agent_config, "co_applicant_documents",
                        lambda: [{"slot": "PAN", "accepts": ["PAN"]}])
    assert [r.slot for r in workflow.co_applicant_requirements()] == ["PAN"]


def test_flag_off_nothing_changes(client, _store):
    a, c = joint(client, _store)
    assert co_items(workflow.pending_items(*state(_store, c))) == []


def test_flag_on_a_co_applicant_without_documents_blocks_readiness(client, _store, monkeypatch):
    monkeypatch.setenv(workflow.COAPP_FLAG, "true")
    a, c = joint(client, _store)
    details = [i["detail"] for i in co_items(workflow.pending_items(*state(_store, c)))]
    for slot in ("PAN", "Address Proof", "Employment Proof"):
        assert f"Co-applicant's {slot} has not been uploaded." in details, details
    assert not any("Bank Statement" in d for d in details), details
    assert workflow.readiness(*state(_store, c))["status"] == "NOT_READY"


@pytest.mark.parametrize("address", ["AADHAAR", "PASSPORT", "DRIVING_LICENCE", "VOTER_ID"])
def test_any_one_address_document_satisfies_address_proof(client, _store, monkeypatch, address):
    monkeypatch.setenv(workflow.COAPP_FLAG, "true")
    a, c = joint(client, _store)
    add_co(_store, c, a, address)
    assert "ADDRESS_PROOF" not in co_slots(_store, c)


def test_aadhaar_is_no_longer_separately_mandatory(client, _store, monkeypatch):
    monkeypatch.setenv(workflow.COAPP_FLAG, "true")
    a, c = joint(client, _store)
    add_co(_store, c, a, "PASSPORT")
    assert "AADHAAR" not in co_slots(_store, c)


@pytest.mark.parametrize("employment", ["SALARY_SLIP", "ITR"])
def test_salaried_or_self_employed_proof_satisfies_employment(client, _store, monkeypatch, employment):
    monkeypatch.setenv(workflow.COAPP_FLAG, "true")
    a, c = joint(client, _store)
    add_co(_store, c, a, employment)
    assert "EMPLOYMENT_PROOF" not in co_slots(_store, c)


def test_a_complete_co_applicant_adds_nothing(client, _store, monkeypatch):
    monkeypatch.setenv(workflow.COAPP_FLAG, "true")
    a, c = joint(client, _store)
    for doc_type in ("PAN", "AADHAAR", "SALARY_SLIP"):
        add_co(_store, c, a, doc_type)
    assert co_slots(_store, c) == {}


def test_a_rejected_address_document_must_be_reuploaded(client, _store, monkeypatch):
    monkeypatch.setenv(workflow.COAPP_FLAG, "true")
    a, c = joint(client, _store)
    add_co(_store, c, a, "AADHAAR", status=DocumentStatus.REJECTED)
    assert co_slots(_store, c).get("ADDRESS_PROOF") == "DOCUMENT_REJECTED"


def test_a_co_applicants_pan_never_fills_the_applicants_slot(client, _store, monkeypatch):
    a, c = joint(client, _store, upload_primary=False)
    add_co(_store, c, a, "PAN")
    primary_pan = lambda: [i for i in workflow.pending_items(*state(_store, c))     # noqa: E731
                           if i.get("slot") == "PAN" and not i.get("party_role")]
    assert primary_pan() == []                                   # today: the co-applicant's PAN fills it
    monkeypatch.setenv(workflow.COAPP_FLAG, "true")
    assert primary_pan() and primary_pan()[0]["code"] == "DOCUMENT_MISSING"


# ---- the document action view and the chat ------------------------------------------------------
def test_document_actions_list_co_applicant_pending_grouped(client, _store, monkeypatch):
    from app.agents.applicant.copilot.answering import document_actions

    monkeypatch.setenv(workflow.COAPP_FLAG, "true")
    a, c = joint(client, _store)
    view = document_actions.build(c, repository=_store)
    co_pending = {r["document_type"] for r in view["pending"] if r["party"] == "CO_APPLICANT"}
    assert co_pending == {"PAN", "ADDRESS_PROOF", "EMPLOYMENT_PROOF"}
    text = document_actions.render(view)["answer"]
    assert "Co-applicant's PAN [Upload]" in text and "Co-applicant's Employment Proof [Upload]" in text


def test_the_chat_names_the_co_applicants_pending_documents(client, _store, monkeypatch):
    monkeypatch.setenv(workflow.COAPP_FLAG, "true")
    a, c = joint(client, _store)
    r = client.post("/api/v1/fos/copilot", json={"applicant_id": a, "case_id": c, "action": "CUSTOM_QUERY",
                                                 "message": "what is pending?"})
    assert r.status_code == 200 and "Co-applicant's PAN" in r.json()["answer"], r.text
