"""
FOS PLAN 7.3 -- case timeline and turnaround: the dated timeline from recorded facts, days in the current stage
against its configured target, "case kab bana tha", "kal wala verify hua?"; a case past its target is flagged in
the case list and on open, with its main blocker.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.store.models import Document, DocumentStatus
from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


@pytest.fixture
def case(client, demo, _store, monkeypatch):
    monkeypatch.setenv("COPILOT_CASE_TIMELINE", "true")
    a, c = make_case(client, "Rahul Sharma")
    # the store keeps a case's creation time on every save (as it should): the test backdates it directly
    _store._write("UPDATE applications SET created_at = ? WHERE case_id = ?",
                  (datetime.now(timezone.utc) - timedelta(days=6), c))
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    _store.save_document(Document(document_id=f"{c}:{a}:pan", case_id=c, applicant_id=a, party_id=a,
                                  document_type="PAN", status=DocumentStatus.VERIFIED, uploaded_at=yesterday))
    client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": c})
    return a, c


def ask(client, message):
    return client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": message}).json()


def test_the_timeline_is_dated_from_the_records(client, case):
    reply = ask(client, "case ka timeline dikhao")
    assert reply["intent"] == "CASE_TIMELINE"
    assert f"Case timeline -- {case[1]}" in reply["answer"] and "Case created" in reply["answer"]
    assert "PAN uploaded (verified)" in reply["answer"]


def test_days_in_stage_against_the_target(client, case):
    answer = ask(client, "kitne din se FOS mein hai")["answer"]
    assert "6 day(s) in FOS." in answer and "Past the FOS target of 3 day(s)." in answer


@pytest.mark.parametrize("message, expected", [
    ("case kab bana tha", "The case was created on"),
    ("kal wala verify hua?", "Uploaded yesterday: PAN (verified)."),
    ("aaj kuch upload hua?", "Nothing was uploaded today."),
])
def test_time_questions(client, case, message, expected):
    assert expected in ask(client, message)["answer"]


def test_a_case_past_target_is_flagged_in_the_list_and_on_open(client, case):
    listed = ask(client, "mere cases dikhao")["answer"]
    assert "6d in FOS (target 3)" in listed
    opened = client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": case[1]}).json()["answer"]
    assert "Past the FOS target of 3 day(s)." in opened and "Main blocker:" in opened
