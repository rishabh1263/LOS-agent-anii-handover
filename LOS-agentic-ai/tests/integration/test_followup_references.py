"""
REFERENCES INTO THE PREVIOUS RESULT SET (owner directive 2026-10-12). After "show top 3 pending cases":
  "why is the first one pending?"  -> row 1 (opened, re-authorised)           [worked before]
  "what about the second one?"     -> row 2, although row 1 is now open        [was: out of scope]
  "show the next two"              -> rows 4-5 of THAT list (its filter/sort)  [was: "next step" of the open case]
  "compare the first two"          -> the first two rows of the list last shown [was: the list again]
A later topic answered outside the engine drops an older "which one do you mean?" -- "ok" no longer answers it.
A greeting inside an open case is a greeting (no case data). Only the caller's own listed rows are ever picked.
"""
from __future__ import annotations

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_final_fix import other  # noqa: F401
from tests.integration.test_reupload_supersedes import client  # noqa: F401


def say(client, text, chat):
    r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": text, "reply_language": "en",
                                                  "chat_id": chat})
    assert r.status_code == 200, r.text
    return r.json()["markdown"]


@pytest.fixture
def five(client, prod):
    """Five cases, newest first in the list: E, D, C, B, A."""
    ids = [make_case(client, n)[1] for n in ("Asha Rao", "Bina Das", "Chetan Iyer", "Dev Shah", "Esha Pal")]
    return list(reversed(ids))


def test_ordinals_and_paging_follow_the_list_shown(client, five):
    e, d, c, b, a = five
    first = say(client, "show top 3 pending cases", "r1")
    assert [x in first for x in (e, d, c, b)] == [True, True, True, False], first
    assert e in say(client, "why is the first one pending?", "r1")
    second = say(client, "what about the second one?", "r1")
    assert d in second and "outside what I can help" not in second, second
    nxt = say(client, "show the next two", "r1")
    assert b in nxt and a in nxt and e not in nxt and "Next step" not in nxt.split("\n")[0], nxt


def test_compare_the_first_two_compares_the_rows_shown(client, five):
    e, d, c, *_ = five
    say(client, "show top 3 pending cases", "r2")
    md = say(client, "compare the first two", "r2")
    assert "side by side" in md and e in md and d in md and c not in md, md


def test_a_bare_more_inside_a_case_does_not_page_the_list(client, five):
    e, d, c, b, a = five
    say(client, "show top 3 pending cases", "r3")
    say(client, "why is the first one pending?", "r3")
    md = say(client, "more", "r3")
    assert b not in md and a not in md, md          # a bare "more" in a case is not the list's next page


def test_a_new_topic_drops_an_older_which_one_question(client, five):
    say(client, "show top 3 pending cases", "r4")
    say(client, "why is the first one pending?", "r4")
    say(client, "and the co-applicant?", "r4")       # may ask "which one do you mean?"
    say(client, "what is FOIR?", "r4")               # a new topic, answered outside the engine
    md = say(client, "ok", "r4")
    assert "which one do you mean" not in md.lower() and "co-applicant" not in md.lower(), md


def test_a_greeting_inside_a_case_is_a_greeting(client, five):
    say(client, f"open {five[0]}", "r5")
    md = say(client, "Hii", "r5")
    assert md.split()[0].rstrip("!.?,") in ("Hello", "Hi") and "which one do you mean" not in md.lower(), md


def test_an_ordinal_never_reaches_another_officers_case(client, other, five, prod):
    foreign = make_case(other, "Not Mine")[1]
    say(client, "show top 3 pending cases", "r6")
    for q in ("what about the second one?", "compare the first two", "show the next two"):
        assert foreign not in say(client, q, "r6")


@pytest.mark.parametrize("ask", ["tell me more", "aur batao", "more details"])
def test_tell_me_more_on_an_open_case_is_answered_for_it(client, five, ask):
    # owner 2026-10-12: "tell me more" inside a case got "outside what I can help with"
    chat = "more-" + ask.replace(" ", "")
    say(client, f"open {five[0]}", chat)
    say(client, "what is pending", chat)
    say(client, "what is FOIR?", chat)               # another layer answered last: the engine's answer is not the topic
    md = say(client, ask, chat)
    assert "outside what I can help" not in md and five[0] in md, md


def test_tell_me_more_with_no_case_open_is_not_a_case_answer(client, five):
    md = say(client, "tell me more", "more-none")
    assert not any(c in md for c in five) or "Which case" in md, md
