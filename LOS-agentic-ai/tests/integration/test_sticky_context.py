"""STICKY CONTEXT (Smart Bot plan section 3): a list never closes the open case; the last opened case is offered first."""

from __future__ import annotations

import re
from urllib.parse import unquote

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


def say(c, m, chat="st"):
    from app.agents.applicant.copilot.capabilities import safety

    safety.reset()
    return c.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": m, "reply_language": "en",
                                                "chat_id": chat}).json()["markdown"]


def test_a_list_keeps_the_open_case(client, prod):
    _, case_id = make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    say(client, f"open {case_id}")
    listed = say(client, "show my cases")
    assert "closed" not in listed and "Showing 1-2 of 2" in listed
    pending = say(client, "what is pending on this case")
    assert case_id in pending and "Still pending" in pending and "Which case" not in pending


def test_the_last_opened_case_is_offered_first(client, prod):
    _, case_id = make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    say(client, f"open {case_id}", chat="lo")
    say(client, "close", chat="lo")
    asked = say(client, "what's the loan amount", chat="lo")
    assert "Which case is this about" in asked and f"Use {case_id}, the last opened case" in asked
    tap = unquote(re.search(r"\(ask:([^)]+)\)", asked.split("\n\n")[1]).group(1))
    answer = say(client, tap, chat="lo")
    assert "5,00,000" in answer
