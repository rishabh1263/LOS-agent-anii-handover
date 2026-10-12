"""VAGUE / ONE-WORD MESSAGES (owner decision A; capabilities/vague.py): one question with options; the pick answers."""

from __future__ import annotations

import re

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


def say(c, m, chat):
    from app.agents.applicant.copilot.capabilities import safety

    safety.reset()
    return c.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": m, "reply_language": "en",
                                                "chat_id": chat}).json()["markdown"]


def options(md: str) -> int:
    return md.count("](ask:")


def test_one_word_with_a_case_open_asks_once_and_the_pick_answers(client, prod):
    _, case_id = make_case(client, "Rahul Sharma")
    chat = f"v1-{case_id}"                 # a fresh chat: the test store outlives a session, and so would "v1"'s state
    say(client, f"open {case_id}", chat)
    asked = say(client, "docs", chat)
    assert "?" in asked and 2 <= options(asked) <= 4 and "Which case" not in asked
    answer = say(client, "1", chat)
    assert "?" not in answer.split("\n")[0] or "Still pending" in answer
    # THE PICK ANSWERS, it does not ask again (2026-10-11): an answer now carries its follow-up suggestions (ask:
    # links), so counting links no longer tells a menu from an answer -- the reply must not be a clarification, and
    # must state the fact picked (option 1 = the required documents, PAN among them)
    assert "do you mean" not in answer.lower() and "didn't catch" not in answer, answer
    assert "PAN" in answer and ("required" in answer.lower() or "Still pending" in answer), answer


def test_a_second_vague_message_answers_directly(client, prod):
    _, case_id = make_case(client, "Rahul Sharma")
    say(client, f"open {case_id}", "v2")
    first = say(client, "docs", "v2")
    second = say(client, "docs", "v2")
    assert first != second and "What would you like to know" not in second


def test_no_case_fragments(client, prod):
    for name in ("Rahul Sharma", "Priya Verma"):
        make_case(client, name)
    top = say(client, "top 2", "v3")
    assert "needing action" in top and "latest 2" in top and "oldest 2" in top
    assert "Showing 1-2" in say(client, "2", "v3")                        # latest 2
    case = say(client, "case", "v4")
    assert "Show my cases" in case and "Create a new case" in case
    assert "Showing" in say(client, "case", "v4")                          # vague again: the first option, directly


def test_clear_short_messages_are_not_vague(client, prod):
    for name in ("Rahul Sharma", "Priya Verma"):
        make_case(client, name)
    assert "Showing 1-2" in say(client, "last 2", "v5")
    assert "What would you like to know" not in say(client, "ok", "v6")


def test_open_with_a_case_id_is_a_command_never_vague(client, prod):
    _, case_id = make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    opened = say(client, f"open {case_id}", "v-open")
    assert case_id in opened and "What would you like to know" not in opened


def test_top_2_picked_shows_exactly_two_cases(client, prod):
    for name in ("Rahul Sharma", "Priya Verma", "Amit Rao"):
        make_case(client, name)
    asked = say(client, "top 2", "v-top")
    assert "needing action" in asked and options(asked) == 3
    listed = say(client, "1", "v-top")
    assert "Which case is this about" not in listed
    assert len(re.findall(r"action:open_case\?id=", listed)) == 2


def test_picking_create_a_new_case_starts_the_form(client, prod):
    make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    asked = say(client, "case", "v-new")
    assert "Create a new case" in asked
    pick = 1 + [o for o in re.findall(r"\[([^\]]+)\]\(ask:", asked)].index("Create a new case")
    started = say(client, str(pick), "v-new")
    assert "Which case is this about" not in started and "Applicant name" in started


def test_the_upload_option_is_a_real_upload_link(client, prod):
    _, case_id = make_case(client, "Rahul Sharma")
    say(client, f"open {case_id}", "v-up")
    asked = say(client, "pan", "v-up")
    assert "(action:upload?doc=PAN&party=applicant)" in asked
    n = 1 + asked.split("\n").index(next(l for l in asked.split("\n") if "action:upload" in l)) - asked.split(
        "\n").index(next(l for l in asked.split("\n") if l.startswith("1. ")))
    picked = say(client, str(n), "v-up")
    assert "(action:upload?doc=PAN&party=applicant)" in picked
