"""
A GREETING / ACKNOWLEDGEMENT READS NO CASE DATA (owner directive 2026-10-12: "Hii" answered with the case counts and the
pending list). Root cause: workspace.handle -> realtime.greeting ran case_list.run + product_flow.notifications on every
greeting (the old MASTER SPEC section-11 greeting). Now (copilot_reply.yaml greeting.case_summary: false) a greeting is
a social reply with the capability options; a bare yes / no / ok / thanks with NOTHING asked is an acknowledgement that
reads nothing; with a question pending the same word still answers it. Spies prove no case tool ran.
"""
from __future__ import annotations

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import client  # noqa: F401


def say(client, text, chat):
    r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": text, "reply_language": "en",
                                                  "chat_id": chat})
    assert r.status_code == 200, r.text
    return r.json()["markdown"]


@pytest.fixture
def spied(client, prod, monkeypatch):
    """Two cases on the caller; every case-list read and notification read counted from here on."""
    from app.agents.applicant.copilot.capabilities import case_list, product_flow

    ids = [make_case(client, "Rahul Sharma")[1], make_case(client, "Priya Verma")[1]]
    reads = []
    real_run, real_notes = case_list.run, product_flow.notifications
    monkeypatch.setattr(case_list, "run", lambda *a, **k: reads.append("case_list.run") or real_run(*a, **k))
    monkeypatch.setattr(product_flow, "notifications",
                        lambda *a, **k: reads.append("notifications") or real_notes(*a, **k))
    return ids, reads


@pytest.mark.parametrize("greeting", ["Hii", "Hi", "Hello", "Good morning", "hey", "namaste"])
def test_a_greeting_reads_no_case_data(client, spied, greeting):
    ids, reads = spied
    md = say(client, greeting, chat=f"g-{greeting}")
    assert reads == [], reads
    assert not any(i in md for i in ids) and "need action" not in md and "pending" not in md.lower(), md
    assert md.split()[0].rstrip("!.?,") in ("Hello", "Hi"), md


@pytest.mark.parametrize("word", ["thanks", "okay", "haan", "nahi", "yes", "no", "ji"])
def test_a_bare_acknowledgement_with_nothing_asked_reads_nothing(client, spied, word):
    ids, reads = spied
    md = say(client, word, chat=f"a-{word}")
    assert reads == [] and not any(i in md for i in ids), (reads, md)
    assert md.startswith(("Okay.", "Sure.")), md
    assert "what would you like to know about" not in md.lower() and "only help with" not in md.lower(), md


def test_an_acknowledgement_after_a_list_does_not_repeat_it(client, spied):
    ids, reads = spied
    say(client, "show my cases", chat="seq")
    reads.clear()
    for word in ("thanks", "okay", "Hii"):
        md = say(client, word, chat="seq")
        assert reads == [] and not any(i in md for i in ids), (word, reads, md)


def test_a_pending_yes_no_question_is_still_answered_by_yes_no(client, spied):
    say(client, "show my cases", chat="p1")
    assert "Okay. Anything else?" not in say(client, "haan", chat="p1")       # answers "a particular case?"
    say(client, "show my cases", chat="p2")
    assert "Anything else?" not in say(client, "nahi", chat="p2")


def test_asking_for_the_cases_still_reads_them(client, spied):
    ids, reads = spied
    md = say(client, "show my cases", chat="ask")
    assert "case_list.run" in reads and all(i in md for i in ids), md
