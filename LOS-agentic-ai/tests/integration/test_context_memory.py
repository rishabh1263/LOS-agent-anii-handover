"""
CONTEXT MEMORY OVER HTTP (Phase 14, 2026-10-05).

Memory is advisory; live records always win. A follow-up resolves against the
previous turn ("PAN verified hai?" -> "Why?" -> "What about address?" -> "What
should I do?"), and switching cases inside one conversation never carries case
A's subject or values into case B -- the context the server returns is stamped
with its case and discarded when sent back with another.
"""

from __future__ import annotations

import pytest

from tests.integration.test_reupload_supersedes import (  # noqa: F401
    FOS_SCOPES, OTHER_PAN, RISHABH_DL, RISHABH_PAN, _BACKEND, _store, client, open_case, upload)


def _ask(client, applicant_id, case_id, message, context=None):
    body = {"applicant_id": applicant_id, "case_id": case_id, "action": "CUSTOM_QUERY", "message": message}
    if context is not None:
        body["context"] = context
    r = client.post("/api/v1/fos/copilot", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert not _BACKEND.search(out["answer"]), out["answer"]
    return out


def test_a_follow_up_chain_stays_on_its_subject(client):
    applicant_id, case_id = open_case(client)
    upload(client, applicant_id, case_id, [("pan.jpg", RISHABH_PAN)], ["PAN"])

    first = _ask(client, applicant_id, case_id, "PAN verified hai?")
    assert "PAN" in first["answer"]
    assert first["context"]["case_id"] == case_id                      # the context names its case

    why = _ask(client, applicant_id, case_id, "Why?", first["context"])
    assert (why.get("followed_up") or {}).get("interpreted_as"), why     # resolved, not guessed
    assert "PAN" in why["answer"]

    address = _ask(client, applicant_id, case_id, "What about address?", why["context"])
    assert "ddress" in address["answer"]                                  # Address proof, the next subject

    todo = _ask(client, applicant_id, case_id, "What should I do?", address["context"])
    assert todo["intent"] in {"NEXT_ACTION", "PENDING_ITEMS", "DOCUMENTS_MISSING", "READINESS"}, todo["intent"]
    assert todo["answer"]


def test_switching_cases_never_carries_the_other_case_along(client):
    a_applicant, a_case = open_case(client)
    upload(client, a_applicant, a_case, [("pan.jpg", RISHABH_PAN)], ["PAN"])
    b_applicant, b_case = open_case(client)
    upload(client, b_applicant, b_case, [("pan.jpg", OTHER_PAN)], ["PAN"])

    on_a = _ask(client, a_applicant, a_case, "give me pan details")
    assert "RISHABH" in on_a["answer"]
    # the officer switches to case B and the client (wrongly) resends case A's context
    on_b = _ask(client, b_applicant, b_case, "give me pan details", on_a["context"])
    assert "RISHABH" not in on_b["answer"] and "LAXMI" in on_b["answer"]
    why_b = _ask(client, b_applicant, b_case, "Why?", on_a["context"])
    assert "RISHABH" not in why_b["answer"]
    assert (why_b.get("followed_up") or {}).get("interpreted_as") is None   # A's subject not used on B
    assert on_b["context"]["case_id"] == b_case
