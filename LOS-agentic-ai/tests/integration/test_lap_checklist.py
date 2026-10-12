"""
THE FOS CHECKLIST PER PRODUCT (industry defaults, 2026-10-11, applicant_agent.yaml documents), over the real
endpoint: LAP requires the property's title document at login, a home loan offers it as optional, and LAP is now a
product an officer can choose. Synthetic applicants only.
"""
from __future__ import annotations

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import client  # noqa: F401


def _slots(client, case_id):
    body = client.get(f"/api/v1/fos/checklist/{case_id}").json()
    items = body.get("checklist") or body.get("items") or body.get("documents") or []
    return {i.get("slot") or i.get("document_type"): bool(i.get("mandatory", i.get("required"))) for i in items}


def test_lap_requires_the_property_document_and_every_kyc_slot(client, prod):
    _, case_id = make_case(client, "Lap Applicant", product="LOAN_AGAINST_PROPERTY")
    slots = _slots(client, case_id)
    for slot in ("PAN", "ADDRESS_PROOF", "BANK_STATEMENT", "SIGNATURE", "PROPERTY_DOCUMENT"):
        assert slots.get(slot) is True, (slot, slots)
    assert slots.get("ITR") is False and slots.get("SALARY_SLIP") is False


def test_a_home_loan_offers_the_property_document_as_optional(client, prod):
    _, case_id = make_case(client, "Hl Applicant", product="HOME_LOAN")
    slots = _slots(client, case_id)
    assert slots.get("PROPERTY_DOCUMENT") is False and slots.get("PAN") is True


def test_lap_is_a_product_the_form_offers():
    from app.agents.applicant.copilot.capabilities import case_form

    assert "LOAN_AGAINST_PROPERTY" in case_form.choices("product")
