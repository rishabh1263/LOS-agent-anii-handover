"""
QUERIES AND DEVIATIONS, END TO END (owner 2026-10-12; the deviation master docs/frontend/DEVIATION_MASTER.ods ->
app/config/deviation_master.yaml). NO NEW API: a deviation is raised in the chat (POST /api/v1/fos/copilot) through
the same draft -> Confirm workflow as a query, and decided on the existing decision route (four eyes for an approval).

Proven on real stored records (synthetic applicant), each person with their own token and role:
    the master is loaded (every deviation has an approving authority; NFA = Head Credit only)
    chat: raise deviation -> draft -> Confirm -> PENDING_APPROVAL with the master's approvers; nothing before Confirm
    an unclear request lists options (no write on a guess); an unknown one says so
    the wrong authority (ACM on an NFA deviation) is refused; the raiser can never approve
    the right authority's approval waits for a second person; the checker's approval makes it APPROVED
    a rejection by an allowed authority acts at once
    a pending deviation blocks the forward move (gate check)
    a stage query CPA -> FOS: raise, reply, resolve -- in the chat
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import FOS_SCOPES, _store, client  # noqa: F401

FOS = "/api/v1/fos/copilot"
DEV_WRITE = ["los.deviation:write", "los.stage:write", "los.query:write"]
APPROVE = ["los.deviation:approve"]


def _person(make_token, subject, scopes=(), roles=None):
    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(subject=subject, scopes=[*FOS_SCOPES, *scopes], roles=roles)}"})
    return c


def say(c, message, chat):
    from app.agents.applicant.copilot.capabilities import safety

    safety.reset()
    r = c.post(FOS, json={"action": "CUSTOM_QUERY", "message": message, "reply_language": "en", "chat_id": chat})
    assert r.status_code == 200, r.text
    return r.json()["markdown"]


def deviations(c, case_id):
    r = c.get(f"/api/v1/los/cases/{case_id}/queries")
    assert r.status_code == 200, r.text
    return r.json()["deviations"]


@pytest.fixture
def setup(client, prod, _store, make_token, monkeypatch):
    monkeypatch.setenv("MAKER_CHECKER_ENABLED", "true")
    from app.agents.los import queries

    queries.reload()
    app_id, case_id = make_case(client, "Deviation Person")
    people = {
        "cpa": _person(make_token, "cpa-officer", DEV_WRITE),
        "acm": _person(make_token, "acm-approver", APPROVE, roles=["ACM"]),
        "head": _person(make_token, "head-credit", APPROVE, roles=["HEAD_CREDIT"]),
        "checker": _person(make_token, "checker-officer", ["los.approvals.check"]),
        "raiser_head": _person(make_token, "cpa-officer", APPROVE, roles=["HEAD_CREDIT"]),
    }
    for subject in ("cpa-officer", "acm-approver", "head-credit", "checker-officer"):
        _store.grant_access(subject, "APPLICANT", app_id)
    return case_id, people


def test_the_master_is_loaded_with_an_authority_for_every_deviation():
    from app.agents.los import queries

    queries.reload()
    master = queries.deviation_master()
    assert len(master) >= 60 and all(d["approvers"] for d in master)
    nfa = [d for d in master if d["category"] == "NFA"]
    assert nfa and all(d["approvers"] == ["HEAD_CREDIT"] for d in nfa)
    assert queries.deviation_rules_status() == "CONFIGURED"


def _raise_ltv(setup):
    case_id, p = setup
    say(p["cpa"], f"open {case_id}", "d1")
    draft = say(p["cpa"], "raise deviation LTV > 70%: property revalued, LTV 72%", "d1")
    assert "70%" in draft and "NFA" in draft and "confirm_write" in draft and "HEAD CREDIT" in draft, draft
    assert deviations(p["cpa"], case_id) == []                       # nothing written before Confirm
    done = say(p["cpa"], "confirm", "d1")
    dev_id = re.search(r"DEV-[0-9A-F]{10}", done).group(0)
    assert "raised" in done, done
    return case_id, p, dev_id


def test_a_deviation_is_raised_in_chat_from_the_master(setup):
    case_id, p, dev_id = _raise_ltv(setup)
    rows = deviations(p["cpa"], case_id)
    assert [(d["deviation_id"], d["status"], d["category"], d["approvers"]) for d in rows] == \
        [(dev_id, "PENDING_APPROVAL", "NFA", ["HEAD_CREDIT"])]
    assert rows[0]["raised_by"] == "cpa-officer" and "LTV 72%" in rows[0]["reason"]
    listed = say(p["cpa"], "show deviations", "d1")
    assert dev_id in listed and "PENDING_APPROVAL" in listed


def test_the_reason_is_asked_when_missing(setup):
    case_id, p = setup
    say(p["cpa"], f"open {case_id}", "d2")
    asked = say(p["cpa"], "raise deviation beauty parlour", "d2")
    assert "Beauty Parlour/Saloon" in asked and "Why is this deviation needed" in asked, asked
    draft = say(p["cpa"], "customer runs a salon, income verified", "d2")
    assert "confirm_write" in draft and "income" in draft and "verified" in draft      # (emphasis may bold a word)
    assert "raised" in say(p["cpa"], "confirm", "d2")


def test_an_unclear_request_lists_options_and_writes_nothing(setup):
    case_id, p = setup
    say(p["cpa"], f"open {case_id}", "d3")
    picked = say(p["cpa"], "raise deviation itr", "d3")
    assert "Pick one" in picked and picked.count("(ask:") >= 2, picked
    unknown = say(p["cpa"], "raise deviation spaceship landing permit", "d3")
    assert "No deviation in the master matches" in unknown
    assert deviations(p["cpa"], case_id) == []


def test_authority_raiser_and_four_eyes(setup):
    case_id, p, dev_id = _raise_ltv(setup)
    url = f"/api/v1/los/cases/{case_id}/deviations/{dev_id}/decision"
    body = {"decision": "APPROVED", "justification": "LTV 72% within comfort, strong repayment"}
    wrong = p["acm"].post(url, json=body)                              # NFA needs Head Credit
    assert wrong.status_code == 403 and wrong.json()["detail"]["code"] == "DEVIATION_AUTHORITY_INSUFFICIENT", wrong.text
    own = p["raiser_head"].post(url, json=body)                        # the raiser, even holding Head Credit
    assert own.status_code == 403 and own.json()["detail"]["code"] == "RAISER_CANNOT_DECIDE", own.text
    asked = p["head"].post(url, json=body)
    assert asked.status_code == 202 and asked.json()["result"] == "PENDING_CHECK", asked.text
    assert deviations(p["cpa"], case_id)[0]["status"] == "PENDING_APPROVAL"   # nothing until checked
    approval_id = asked.json()["approval"]["approval_id"]
    checked = p["checker"].post(f"/api/v1/approvals/{approval_id}/decision", json={"decision": "APPROVE"})
    assert checked.status_code == 200, checked.text
    assert deviations(p["cpa"], case_id)[0]["status"] == "APPROVED"


def test_a_rejection_by_an_allowed_authority_acts_at_once(setup):
    case_id, p = setup
    say(p["cpa"], f"open {case_id}", "d4")
    say(p["cpa"], "raise deviation single year ITR: only FY24 filed", "d4")
    done = say(p["cpa"], "confirm", "d4")
    dev_id = re.search(r"DEV-[0-9A-F]{10}", done).group(0)
    r = p["acm"].post(f"/api/v1/los/cases/{case_id}/deviations/{dev_id}/decision",
                      json={"decision": "REJECTED", "justification": "two years of ITR required for this profile"})
    assert r.status_code == 200 and r.json()["deviation"]["status"] == "REJECTED", r.text


def test_a_pending_deviation_blocks_the_forward_move(setup):
    from app.agents.los import queries

    case_id, p, dev_id = _raise_ltv(setup)
    checks = queries.gate_checks(case_id)
    assert any(dev_id in str(c) or "DEVIATION" in str(c).upper() for c in checks), checks


def test_a_stage_query_cpa_to_fos_raise_reply_resolve(setup):
    case_id, p = setup
    say(p["cpa"], f"open {case_id}", "q1")
    draft = say(p["cpa"], "raise query to CPA: please confirm the employer name", "q1")
    assert "confirm_write" in draft, draft
    done = say(p["cpa"], "confirm", "q1")
    qid = re.search(r"QRY-[0-9A-F]+", done).group(0)
    replied = say(p["cpa"], f"reply to {qid}: employer confirmed on the salary slip", "q1")
    assert "confirm_write" in replied
    say(p["cpa"], "confirm", "q1")
    rows = {q["query_id"]: q for q in p["cpa"].get(f"/api/v1/los/cases/{case_id}/queries").json()["queries"]}
    assert rows[qid]["status"] == "RESPONDED" and rows[qid]["target_stage"] == "CPA"
    say(p["cpa"], f"resolve query {qid}", "q1")
    say(p["cpa"], "confirm", "q1")
    rows = {q["query_id"]: q for q in p["cpa"].get(f"/api/v1/los/cases/{case_id}/queries").json()["queries"]}
    assert rows[qid]["status"] == "RESOLVED"


def test_decisions_in_chat_follow_the_same_rules(setup):
    case_id, p, dev_id = _raise_ltv(setup)
    # the wrong authority (ACM on an NFA deviation): drafted, but refused at Confirm -- nothing changes
    say(p["acm"], f"open {case_id}", "a1")
    assert "Confirm" in say(p["acm"], f"approve deviation {dev_id}: within comfort", "a1")
    refused = say(p["acm"], "confirm", "a1")
    assert "not saved" in refused and "HEAD_CREDIT" in refused, refused
    # Head Credit: the approval waits for a second person (four eyes)
    say(p["head"], f"open {case_id}", "a2")
    say(p["head"], f"approve deviation {dev_id}: LTV 72% acceptable with strong repayment", "a2")
    pending = say(p["head"], "confirm", "a2")
    assert "second person" in pending, pending
    assert deviations(p["cpa"], case_id)[0]["status"] == "PENDING_APPROVAL"


def test_a_decision_needs_its_reason(setup):
    case_id, p, dev_id = _raise_ltv(setup)
    say(p["head"], f"open {case_id}", "a3")
    asked = say(p["head"], f"reject deviation {dev_id}", "a3")
    assert "needs its reason" in asked and deviations(p["cpa"], case_id)[0]["status"] == "PENDING_APPROVAL", asked
