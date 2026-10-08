"""
The FOS <-> CPA workflow (owner, 2026-10-08), every flag on (gate / KYC flags included):

  list        "my cases" shows the officer's 5 MOST RECENT cases
  FOS wrong   -> "Send query to customer": one query listing every problem -> Confirm -> a CUSTOMER query
  FOS clear   -> "Move to CPA" -> Confirm -> the gated move; not ready -> what to fix, nothing moved
  CPA         -> "Raise query to FOS" -> the text -> Confirm; FOS replies; the raiser closes a customer query
"""

from __future__ import annotations

import re

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

FOS = "/api/v1/fos/copilot"


def _plain(md):
    """The glossary's first-mention expansion ("FOS (Field Officer Sales)") is style, not content."""
    return re.sub(r" \((?:Field Officer Sales|Credit Processing Approval|Know Your Customer|Permanent Account Number)\)", "", md)


def turn(client, message, chat="sf", **extra):
    r = client.post(FOS, json={"action": "CUSTOM_QUERY", "message": message, "reply_language": "en",
                               "chat_id": chat, **extra})
    assert r.status_code == 200, r.text
    return _plain(r.json()["markdown"])


def link(client, markdown, label, chat="sf"):
    href = re.search(r"\[" + re.escape(label) + r"\]\(((?:action|ask):[^)]*)\)", markdown)
    assert href, (label, markdown)
    r = client.post(FOS, json={"action_link": href.group(1), "chat_id": chat, "reply_language": "en"})
    assert r.status_code == 200, r.text
    return _plain(r.json()["markdown"])


def stage_of(case_id):
    from app.agents.applicant.copilot.capabilities import workspace

    return str(workspace._stage(case_id))


@pytest.fixture
def gate_passes(monkeypatch):
    """The FOS gate passing (documents verified / KYC passed is covered by the gate's own tests)."""
    from app.agents.los import stage_gate

    real = stage_gate.evaluate_live

    def passing(case_id, stage):
        gate = real(case_id, stage)
        return {**gate, "status": "PASS", "blockers": []}

    monkeypatch.setattr(stage_gate, "evaluate_live", passing)


def test_my_cases_shows_the_five_most_recent(client, prod):
    made = [make_case(client, f"Officer Case {n}")[1] for n in range(7)]
    md = turn(client, "my cases", chat="r5")
    shown = re.findall(r"\| (CASE-[0-9A-F]+) \|", md)
    assert shown == list(reversed(made))[:5]                          # newest first, five only
    assert "Showing 1-5 of 7" in md


def test_something_wrong_at_fos_sends_one_query_to_the_customer(client, prod):
    from app.agents.los import queries

    _, case_id = make_case(client, "Rahul Sharma")
    opened = turn(client, f"open {case_id}", chat="cq")
    assert "[Send query to customer](ask:" in opened                  # offered, never sent by itself
    draft = turn(client, "Send query to customer", chat="cq")
    assert "PAN" in draft and "Address Proof" in draft and "Bank Statement" in draft and "Confirm" in draft
    assert queries.list_queries(case_id) == []                         # nothing written before Confirm
    sent = link(client, draft, "Confirm", chat="cq")
    assert "recorded for the customer" in sent and "[Copy message]" in sent
    raised = queries.list_queries(case_id)
    assert len(raised) == 1 and raised[0]["target_type"] == "CUSTOMER" and raised[0]["status"] == "OPEN"
    events = client.get(f"/api/v1/fos/cases/{case_id}/activity").json()["events"]
    assert any(e["type"] == "CUSTOMER_QUERY" for e in events)
    # the officer who raised it closes it once the customer has answered
    closed = turn(client, f"resolve query {raised[0]['query_id']}", chat="cq")
    done = link(client, closed, "Confirm", chat="cq")
    assert "marked as resolved" in done
    assert queries.list_queries(case_id)[0]["status"] == "RESOLVED"


def test_not_ready_is_never_moved(client, prod):
    _, case_id = make_case(client, "Rahul Sharma")
    turn(client, f"open {case_id}", chat="nr")
    md = turn(client, "CPA ko bhejo", chat="nr")
    assert "can't move to CPA yet" in md and "Confirm" not in md
    assert stage_of(case_id) == "FOS"


def test_all_clear_moves_fos_to_cpa_after_confirm(client, prod, gate_passes, monkeypatch):
    from app.agents.applicant.copilot.capabilities import stage_flow

    monkeypatch.setattr(stage_flow, "problems", lambda case_id, lang: [])
    _, case_id = make_case(client, "Priya Verma")
    opened = turn(client, f"open {case_id}", chat="mv")
    assert "[Move to CPA](ask:" in opened
    proposal = turn(client, "Move to CPA", chat="mv")
    assert "Move it to CPA?" in proposal and stage_of(case_id) == "FOS"     # nothing moved yet
    moved = link(client, proposal, "Confirm", chat="mv")
    assert re.search(r"moved from FOS( \(Field Officer Sales\))? to CPA", moved) and stage_of(case_id) == "CPA"
    events = client.get(f"/api/v1/fos/cases/{case_id}/activity").json()["events"]
    assert any(e["type"] == "STAGE_MOVED" for e in events)


def test_cpa_raises_a_query_to_fos_and_fos_replies(client, prod, gate_passes, monkeypatch):
    from app.agents.applicant.copilot.capabilities import stage_flow
    from app.agents.los import queries

    monkeypatch.setattr(stage_flow, "problems", lambda case_id, lang: [])
    _, case_id = make_case(client, "Priya Verma")
    turn(client, f"open {case_id}", chat="cp")
    link(client, turn(client, "Move to CPA", chat="cp"), "Confirm", chat="cp")
    assert stage_of(case_id) == "CPA"
    asked = turn(client, "Raise query to FOS", chat="cp")
    assert "What should the query to FOS say?" in asked
    proposal = turn(client, "PAN photo is blurry, please collect a clear copy", chat="cp")
    assert "from CPA to FOS" in proposal and "PAN photo is blurry" in proposal
    sent = link(client, proposal, "Confirm", chat="cp")
    assert "sent from CPA to FOS" in sent
    raised = queries.list_queries(case_id)[0]
    assert raised["target_stage"] == "FOS" and raised["source_stage"] == "CPA"
    reply = turn(client, f"reply to {raised['query_id']}: new clear PAN uploaded", chat="cp")
    assert "Confirm" in reply
    assert "Reply recorded" in link(client, reply, "Confirm", chat="cp")
    after = queries.list_queries(case_id)[0]
    assert after["status"] == "RESPONDED" and "new clear PAN" in str(after.get("response"))


def test_cancel_sends_nothing(client, prod):
    from app.agents.los import queries

    _, case_id = make_case(client, "Rahul Sharma")
    turn(client, f"open {case_id}", chat="cx")
    draft = turn(client, "customer ko query bhejo", chat="cx")
    assert "nothing was sent" in link(client, draft, "Cancel", chat="cx")
    assert queries.list_queries(case_id) == []


def test_a_move_needs_the_move_permission(client, prod, gate_passes, monkeypatch, make_token):
    from fastapi.testclient import TestClient

    import main
    from app.agents.applicant.copilot.capabilities import stage_flow

    monkeypatch.setattr(stage_flow, "problems", lambda case_id, lang: [])
    _, case_id = make_case(client, "Priya Verma")
    reader = TestClient(main.app)
    from app.security import access

    access.record_ownership("reader-only", case_id=case_id)
    reader.headers.update({"Authorization": f"Bearer {make_token(subject='reader-only', scopes=['read_application', 'read_documents'])}"})
    turn(reader, f"open {case_id}", chat="rd")
    assert "permission to move" in turn(reader, "Move to CPA", chat="rd")
    assert stage_of(case_id) == "FOS"
