"""
CASE SWITCH + CREATE-FORM EDITS (owner 2026-10-12).
SWITCH: "change case" stayed on the open case; "switch to Amit" / "change to CASE-..." / "go to Priya Verma" switched but
answered "outside what I can help with" (applicant_agent.yaml case_workspace phrases switch / open). A STAGE is never a
case ("go to CPA", "switch to CPA stage" -> not "I can't find that applicant"; workspace._names_a_stage).
CREATE: an edit typed on the summary ("change the name to ...", "mobile number should be ...") or while another field
is asked ("name galat hai, ... karo") is taken -- the case is created with the EDITED value (case_form._inline_edit,
product_flow.yaml case_form.aliases). Each value goes through the same check as a typed one.
"""
from __future__ import annotations

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import client  # noqa: F401


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    from app.agents.applicant.copilot.capabilities import safety

    real = safety._num
    monkeypatch.setattr(safety, "_num", lambda key, default: 10_000 if key == "rate_limit_per_minute" else real(key, default))


def say(client, text, chat):
    r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": text, "reply_language": "en",
                                                  "chat_id": chat})
    assert r.status_code == 200, r.text
    return r.json()["markdown"]


@pytest.fixture
def three(client, prod):
    # UNIQUE surnames: the shared test store holds other tests' "Priya Verma" -- a name two cases share is (rightly)
    # asked "which one?", never opened
    import random
    import string

    def word():
        return "".join(random.choice(string.ascii_lowercase) for _ in range(7)).capitalize()

    # three DIFFERENT surnames: near-identical ones are (rightly) fuzzy matches of each other and get "which one?"
    t = {"r": word(), "p": word(), "a": word()}
    return {"rahul": make_case(client, f"Rahul {t['r']}")[1], "priya": make_case(client, f"Priya {t['p']}")[1],
            "amit": make_case(client, f"Amit {t['a']}")[1], "pt": t["p"], "at": t["a"]}


@pytest.mark.parametrize("q", ["change case", "change the case", "case change karo", "switch case", "dusra case"])
def test_a_change_request_leaves_the_open_case(client, three, q):
    chat = "sw-" + q.replace(" ", "")
    say(client, f"open {three['rahul']}", chat)
    md = say(client, q, chat)
    assert f"{three['rahul']} closed" in md, md


@pytest.mark.parametrize("q, who", [("switch to Amit {at}", "amit"), ("go to Priya {pt}", "priya"),
                                    ("change to {amit}", "amit")])
def test_switch_to_a_named_case_opens_it_cleanly(client, three, q, who):
    chat = "to-" + who + str(len(q))
    say(client, f"open {three['rahul']}", chat)
    md = say(client, q.format(**three), chat)
    assert md.startswith(three[who]) and "outside what I can help" not in md, md      # opened: its card first
    assert three[who] in say(client, "what is the applicant name", chat)


@pytest.mark.parametrize("q", ["go to CPA", "how do I go to CPA", "switch to CPA stage"])
def test_a_stage_is_never_read_as_a_case(client, three, q):
    chat = "st-" + str(len(q))
    say(client, f"open {three['rahul']}", chat)
    assert "can't find that applicant" not in say(client, q, chat)


def _create(client, chat, steps):
    for s in ["create a new case", *steps]:
        md = say(client, s, chat)
    return md


def test_an_edit_on_the_summary_is_the_value_created(client, prod):
    md = _create(client, "cr1", ["Rahul Shrma", "9876543210", "1990-04-12", "12 MG Road, Pune", "personal loan", "500000",
                                 "change the name to Rahul Sharma", "confirm"])
    assert "created for Rahul Sharma" in md, md


def test_an_edit_while_another_field_is_asked_is_taken(client, prod):
    md = _create(client, "cr2", ["Amit Kumr", "9876543210", "name galat hai, Amit Kumar karo", "1990-04-12",
                                 "12 MG Road, Pune", "personal loan", "500000", "mobile number should be 9123456780"])
    assert "| Amit Kumar |" in md and "| 9123456780 |" in md, md
    assert "created for Amit Kumar" in say(client, "confirm", "cr2")


def test_an_invalid_edit_is_refused_and_nothing_changes(client, prod):
    md = _create(client, "cr3", ["Neha Jain", "9876543210", "1990-04-12", "Pune", "personal loan", "500000",
                                 "change mobile to 12345"])
    assert "doesn't look right" in md or "look right" in md, md
    assert "created for Neha Jain" in say(client, "confirm", "cr3")


def test_an_address_that_contains_a_field_word_is_still_the_address(client, prod):
    md = _create(client, "cr4", ["Neha Jain", "9876543210", "1990-04-12", "Near name plate shop, Pune", "personal loan",
                                 "500000"])
    assert "| Neha Jain |" in md and "Near name plate shop" in md, md


def test_what_is_pending_mid_create_is_the_drafts_status(client, prod):
    """Halfway through a new case, another question, then "what is pending": the DRAFT's status -- never another case's
    pending list without its id; "continue" resumes where it stopped."""
    make_case(client, "Old Case Person")
    for s in ["create a new case", "Kiran Mehta", "9876543210", "1991-05-20", "what is FOIR?"]:
        say(client, s, "half")
    md = say(client, "what is pending", "half")
    assert "not created yet" in md and "Kiran Mehta" in md and "Address" in md and "Upload PAN" not in md, md
    assert "Address" in say(client, "continue", "half")
