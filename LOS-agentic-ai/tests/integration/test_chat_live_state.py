"""
The chatbot answers the brief's questions FROM LIVE RECORDED STATE (2026-10-06):
"status kya hai?", "documents verify hue?", "KYC hua?", "kaunsa document pending
hai?", "what is still processing?", "am I eligible?" -- clean prose, no internal
codes, and never a fact the backend has not recorded (eligibility was not run on
this case, so the bot must not say the customer is eligible).
"""

from __future__ import annotations

import re

import pytest

from tests.integration.test_response_ux import UGLY, VOTER  # noqa: F401
from tests.integration.test_reupload_supersedes import (  # noqa: F401
    RISHABH_DL, RISHABH_PAN, _store, client, open_case, upload)

QUESTIONS = ["status kya hai?", "documents verify hue?", "KYC hua?", "kaunsa document pending hai?",
             "what is still processing?", "am I eligible?", "why is KYC review?"]


@pytest.fixture
def case(client):
    a, c = open_case(client)
    upload(client, a, c, [("pan.jpg", RISHABH_PAN), ("dl.jpg", RISHABH_DL)], ["PAN", "DRIVING_LICENCE"])
    return a, c


@pytest.mark.parametrize("question", QUESTIONS)
def test_live_state_questions_get_clean_grounded_answers(client, case, question):
    a, c = case
    r = client.post("/api/v1/fos/copilot", json={"applicant_id": a, "case_id": c, "action": "CUSTOM_QUERY",
                                                 "message": question})
    assert r.status_code == 200, r.text
    body = r.json()
    answer = body["answer"]
    assert answer.strip() and not UGLY.search(answer), (question, answer)
    # never an eligibility verdict the backend did not record
    assert not re.search(r"\byou are eligible\b|\baap eligible hain\b|\bapproved\b", answer, re.I), answer
    if question == "KYC hua?":
        assert "kyc" in answer.lower()
    assert body["presentation"]["message"] or body["presentation"]["sections"]


def test_status_api_and_chat_agree_on_the_documents(client, case):
    a, c = case
    status = client.get(f"/api/v1/applications/{c}/verification-status").json()
    verified = {d["label"] for d in status["documents"] if d["status"] == "VERIFIED"}
    answer = client.post("/api/v1/fos/copilot", json={"applicant_id": a, "case_id": c, "action": "CUSTOM_QUERY",
                                                      "message": "show all documents"}).json()["answer"]
    for label in verified:
        assert f"✓ {label} — Verified" in answer
