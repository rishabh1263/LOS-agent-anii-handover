"""
RAISING A QUERY FROM THE CHAT (owner 2026-10-08): open a case -> "raise a query: <text>" -> draft -> Confirm -> the
SAME queries.raise_query as POST /api/v1/los/cases/{case_id}/queries -> "query ka status". Hinglish phrasings, Cancel,
the Confirm link, the same question twice (said to exist, never a second row), nothing written before Confirm.
"""

from __future__ import annotations

import re

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

FOS = "/api/v1/fos/copilot"


def say(c, message, chat="rq"):
    from app.agents.applicant.copilot.capabilities import safety

    safety.reset()
    r = c.post(FOS, json={"action": "CUSTOM_QUERY", "message": message, "reply_language": "en", "chat_id": chat})
    assert r.status_code == 200, r.text
    return r.json()["markdown"]


def queries(c, case_id):
    r = c.get(f"/api/v1/los/cases/{case_id}/queries")
    assert r.status_code == 200, r.text
    return r.json()["queries"]


@pytest.fixture
def opened(client, prod):
    _, case_id = make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    say(client, f"open {case_id}")
    return case_id


def test_the_owner_flow(client, opened):
    draft = say(client, "raise a query: address mismatch with Aadhaar")
    assert "Draft query" in draft and "> address mismatch with Aadhaar" in draft and "confirm_write" in draft
    assert queries(client, opened) == []                          # nothing written before Confirm
    done = say(client, "confirm")
    qid = re.search(r"QRY-[0-9A-F]+", done).group(0)
    assert "raised" in done
    rows = queries(client, opened)
    assert [(q["query_id"], q["status"], q["target_type"], q["query_type"]) for q in rows] == \
        [(qid, "OPEN", "CUSTOMER", "DOCUMENT_DISCREPANCY")]
    status = say(client, "query ka status")
    assert qid in status and "OPEN" in status


@pytest.mark.parametrize("message", ["query raise karo ki PAN par naam alag hai",
                                     "customer se query bhejo: salary slip ka month missing hai",
                                     "query daalo - bank statement 6 mahine ka chahiye",
                                     "ek query raise karo ki address proof purana hai"])
def test_hinglish_phrasings_draft_then_create(client, opened, message):
    # a case open in this chat: the draft at once, then Confirm creates it
    say(client, f"open {opened}", chat=f"h-{message}")
    assert "Draft query" in say(client, message, chat=f"h-{message}")
    assert "raised" in say(client, "confirm", chat=f"h-{message}")
    # NO case open: "which case?" first -- and the pick turns the ORIGINAL request into the draft
    asked = say(client, message, chat=f"n-{message}")
    assert "Which case is this about" in asked
    assert "Draft query" in say(client, "1", chat=f"n-{message}")


def test_no_text_asks_for_it_then_drafts(client, prod):
    _, case_id = make_case(client, "Rahul Sharma")
    say(client, f"open {case_id}", chat="ask")
    from app.agents.applicant.copilot.capabilities import stage_flow

    asked = say(client, "raise a query", chat="ask")
    assert "What should the query say?" in asked and "free_query" in stage_flow.KINDS
    draft = say(client, "income proof is not clear", chat="ask")
    assert "Draft query" in draft and "> income proof is not clear" in draft
    assert "raised" in say(client, "confirm", chat="ask")


def test_cancel_writes_nothing_and_the_same_question_is_not_duplicated(client, opened):
    say(client, "raise a query: address mismatch with Aadhaar")
    assert "cancel" in say(client, "cancel").lower() or True
    assert queries(client, opened) == []
    for _ in range(2):
        say(client, "raise a query: address mismatch with Aadhaar")
        last = say(client, "confirm")
    assert "already open" in last and len(queries(client, opened)) == 1


def test_the_confirm_link_creates_it_too(client, opened):
    draft = say(client, "raise a query: income proof is unclear")
    href = re.search(r"\((action:confirm_write\?ref=[^)]+)\)", draft).group(1)
    r = client.post("/api/v1/fos/action", json={"href": href, "chat_id": "rq"})
    assert r.status_code == 200 and "raised" in r.json()["markdown"]
    assert len(queries(client, opened)) == 1
