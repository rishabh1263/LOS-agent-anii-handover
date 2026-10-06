"""
RESPONSE PRESENTATION CONTRACT (2026-10-05).

A chat answer reads like a lending copilot, not an API dump: lists are one line
per item with a status icon, each question shows only what it is about, and no
answer carries Python structures, "item(s)", internal codes, scores, versions or
file names. Structured blocks travel beside the text for the frontend.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.integration.test_reupload_supersedes import (  # noqa: F401
    RISHABH_DL, RISHABH_PAN, _store, client, open_case, upload)

VOTER = Path("samples/documents/voter_id2.jpg")
UGLY = re.compile(r"\[\{|\}\]|\{'|'\}|item\(s\)|document\(s\)|[A-Z]+_[A-Z_]{3,}|\bscore\b|confidence \d|"
                  r"version [0-9a-f]{7}|Source:|\.(jpe?g|png|pdf)\b|None\b|null\b", re.I)
pytestmark = pytest.mark.skipif(not VOTER.exists(), reason="real samples not present")


def ask(client, a, c, q):
    r = client.post("/api/v1/fos/copilot", json={"applicant_id": a, "case_id": c, "action": "CUSTOM_QUERY",
                                                 "message": q})
    assert r.status_code == 200, r.text
    body = r.json()
    assert not UGLY.search(body["answer"]), (q, body["answer"])
    return body


@pytest.fixture
def case(client):
    a, c = open_case(client)
    # a verified PAN and a document that fails its declared type
    upload(client, a, c, [("pan.jpg", RISHABH_PAN), ("voter.jpg", VOTER)], ["PAN", "BANK_STATEMENT"])
    return a, c


def test_all_documents_are_listed_one_per_line_with_a_status(client, case):
    body = ask(client, *case, "show all documents")
    lines = body["answer"].splitlines()
    assert lines[0] == "Documents on this application:"
    assert "✓ PAN — Verified" in lines
    assert any(line.startswith("✗ ") and "Rejected" in line for line in lines)
    assert body["answer"].rstrip().endswith("needs attention.")
    assert body["documents"], "the structured documents travel beside the text"


def test_verify_documents_shows_only_what_needs_action(client, case):
    answer = ask(client, *case, "verify documents")["answer"]
    assert "Needs attention" in answer and "PAN — " not in answer          # the verified PAN is not listed


def test_pending_shows_only_pending_items_one_per_line(client, case):
    body = ask(client, *case, "what is pending?")
    lines = body["answer"].splitlines()
    assert lines[0] == "Pending:" and len(lines) >= 2
    assert all(line.startswith(("⏳ ", "• ", "✗ ")) for line in lines[1:]), lines
    assert "PAN" not in body["answer"]                                     # the verified PAN is not pending
    assert "address Proof" not in body["answer"]                           # never mis-cased
    assert body["pending_items"], "the structured items travel beside the text"


def test_readiness_lists_what_is_still_to_do(client, case):
    answer = ask(client, *case, "is my case ready for CPA?")["answer"]
    assert answer.startswith("Not ready for CPA yet. Still to do:")
    assert all(line.startswith("• ") for line in answer.splitlines()[1:])


def test_pan_details_show_only_pan_fields(client, case):
    answer = ask(client, *case, "pan details")["answer"]
    assert "PAN" in answer and "XXXXXX" in answer
    assert "Voter" not in answer and "Bank" not in answer


def test_greeting_and_unsupported_questions_stay_short_and_clean(client, case):
    hello = ask(client, *case, "hello")["answer"]
    assert len(hello) < 200
    off = ask(client, *case, "what is the capital of France?")["answer"]
    assert "loan" in off.lower() or "application" in off.lower()
