"""
FOS PLAN 1.1 -- implausible amounts (trigger: "loan amount: Rs 5", demo data; kept as production protection).
All amounts are in RUPEES. Intake refuses an implausible figure with a clear 422; chat never says one;
GET /ready says which build and which chatbot flags this backend runs.
"""

from __future__ import annotations

import pytest

from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

APPLICANT = {"full_name": "Asha Rao", "mobile": "9876543210", "date_of_birth": "1990-04-12", "address": "Pune"}


def create(client, **application):
    return client.post("/api/v1/fos/applicants", json={"applicant": APPLICANT,
                                                        "application": {"product": "HOME_LOAN", **application}})


@pytest.mark.parametrize("amount", [5, "5", "5 lakh", "2 cr", "50k"])
def test_an_implausible_loan_amount_is_refused_at_intake(client, amount):
    r = create(client, loan_amount=amount)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "AMOUNT_IMPLAUSIBLE" and detail["field"] == "loan_amount"
    assert "rupees" in detail["message"] and "500000" in detail["message"]


def test_a_refused_intake_leaves_nothing_behind(client, _store):
    before = len(_store._all("SELECT applicant_id FROM applicants", ()))
    assert create(client, loan_amount=5).status_code == 422
    assert len(_store._all("SELECT applicant_id FROM applicants", ())) == before


def test_a_full_rupee_amount_is_accepted(client):
    assert create(client, loan_amount=500000).status_code == 201


def test_an_update_is_checked_too():
    import asyncio

    from app.mcp import applicant as tools

    out = asyncio.run(tools.application_update(case_id="CASE-ANY", loan_amount="5"))
    assert not out.ok and out.error.code == "AMOUNT_IMPLAUSIBLE"


def test_a_stored_implausible_amount_is_never_said():
    from app.agents.applicant.copilot.answering import profile

    assert profile._said("loan_amount", "5") == \
        "The recorded loan amount needs verification; the figure on the case does not look right."
    assert "₹5,00,000" in profile._said("loan_amount", "500000")


def test_ready_says_which_build_and_which_flags(client, monkeypatch):
    monkeypatch.setenv("COPILOT_CASE_WORKSPACE", "true")
    build = client.get("/ready").json()["build"]
    assert build["phase"] and build["step"]
    assert build["chatbot_flags"]["COPILOT_CASE_WORKSPACE"] is True
    assert build["chatbot_flags_total"] == len(build["chatbot_flags"])
