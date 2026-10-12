"""
English follow-ups, corrections, ambiguity and context restoration through the real chat route (master prompt §4,
2026-10-10). Synthetic applicants only; every case fact is read from the store, never from the model.
"""
from __future__ import annotations

import re

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_final_fix import other  # noqa: F401
from tests.integration.test_master_15_product_flow import FOS, _href, turn
from tests.integration.test_reupload_supersedes import FOS_SCOPES, client  # noqa: F401

ROW = re.compile(r"^\d+\. .*?\b(CASE-[0-9A-F]+)", re.M)


def shown(md: str) -> list[str]:
    return ROW.findall(md) or re.findall(r"^\| (CASE-[0-9A-F]+) \|", md, re.M)


@pytest.fixture
def seven(client, prod):
    return [make_case(client, f"Officer Case {n}")[1] for n in range(7)]


def test_what_documents_are_pending_names_them(client, prod):
    _, c1 = make_case(client, "Rahul Sharma")
    turn(client, f"open {c1}", chat="p1")
    md = turn(client, "What documents are pending?", chat="p1")["markdown"]
    assert "PAN" in md and "pending" in md.lower(), md


def test_show_me_the_next_two_continues_the_list(client, seven):
    first = shown(turn(client, "show my cases", chat="n2")["markdown"])
    assert len(first) == 5
    nxt = turn(client, "Show me the next two.", chat="n2")["markdown"]
    following = shown(nxt)
    assert len(following) == 2 and not set(following) & set(first), nxt
    full = shown(turn(client, "show 7 cases", chat="n2-all")["markdown"])
    assert following == full[5:7], (full, following)


def test_why_is_the_first_one_pending_answers_for_that_row(client, seven):
    listed = shown(turn(client, "show my cases", chat="f1")["markdown"])
    md = turn(client, "Why is the first one pending?", chat="f1")["markdown"]
    assert listed[0] in md and "pending" in md.lower(), md
    assert not any(c in md for c in listed[1:]), md


def test_no_i_meant_the_previous_case_goes_back(client, prod):
    _, c1 = make_case(client, "Rahul Sharma")
    _, c2 = make_case(client, "Priya Verma")
    turn(client, f"open {c1}", chat="pv")
    turn(client, f"open {c2}", chat="pv")
    md = turn(client, "No, I meant the previous case.", chat="pv")["markdown"]
    assert c1 in md and "Rahul" in md, md


def test_what_should_i_do_next_is_a_next_action_for_the_open_case(client, prod):
    _, c1 = make_case(client, "Rahul Sharma")
    turn(client, f"open {c1}", chat="nx")
    md = turn(client, "What should I do next?", chat="nx")["markdown"]
    assert "upload" in md.lower() or "collect" in md.lower(), md


def test_is_it_done_after_a_confirmed_edit_reports_the_stored_value(client, prod):
    from app.store import get_repository

    applicant_id, c1 = make_case(client, "Rahul Sharma")
    turn(client, f"open {c1}", chat="dn")
    proposal = turn(client, "loan amount 600000 karo", chat="dn")["markdown"]
    asked = turn(client, "Is it done?", chat="dn")["markdown"]
    # not confirmed yet: nothing is claimed as done, and nothing was written
    assert float(get_repository().get_application(c1).loan_amount) == 500000
    assert not re.search(r"(?i)\bupdated\b|\bsaved\b", asked) or "not" in asked.lower(), asked
    assert "Not saved yet" in asked and "Confirm" in asked, asked          # the truth, and the proposal kept
    r = client.post(FOS, json={"action_link": _href(asked, "Confirm"), "chat_id": "dn", "reply_language": "en"})
    assert r.status_code == 200 and "Updated" in r.json()["markdown"], r.text
    assert float(get_repository().get_application(c1).loan_amount) == 600000


def test_continue_after_a_parked_draft_does_not_write_by_itself(client, prod):
    turn(client, "naya case banao", chat="ct")
    turn(client, "Sunita Rao", chat="ct")
    md = turn(client, "Continue", chat="ct")["markdown"]
    assert client.get("/api/v1/fos/cases?search=Sunita").json()["total"] == 0, md   # nothing written without Confirm


def test_the_open_case_survives_a_reload_with_an_empty_context(client, prod):
    _, c1 = make_case(client, "Rahul Sharma")
    turn(client, f"open {c1}", chat="rl")
    md = turn(client, "what is pending", chat="rl", context={})["markdown"]   # a refreshed page sends {}
    assert c1 in md or "PAN" in md, md
    assert "Which case" not in md


def test_a_case_question_with_several_cases_and_none_open_asks_once(client, prod):
    make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    md = turn(client, "Why is it pending?", chat="am")["markdown"]
    assert md.startswith("Which case is this about?"), md


def test_an_empty_filter_says_so_plainly(client, prod):
    make_case(client, "Rahul Sharma")
    md = turn(client, "show the cases ready for CPA", chat="em")["markdown"]
    assert not shown(md) and re.search(r"(?i)\bno\b|none|0 ", md), md


def test_another_officer_cannot_reach_a_case_by_ordinal_or_id(client, other, prod):
    _, c1 = make_case(client, "Rahul Sharma")
    turn(client, "show my cases", chat="iso")
    r = other.post(FOS, json={"action": "CUSTOM_QUERY", "message": "Why is the first one pending?",
                              "reply_language": "en", "chat_id": "iso"})
    assert c1 not in r.text and "Rahul" not in r.text, r.text
    r = other.post(FOS, json={"action": "CUSTOM_QUERY", "message": f"open {c1}", "reply_language": "en",
                              "chat_id": "iso-2"})
    assert "Rahul" not in r.text, r.text


def test_a_confirm_button_of_a_dropped_change_writes_nothing_and_says_so(client, prod):
    from app.store import get_repository

    _, c1 = make_case(client, "Rahul Sharma")
    turn(client, f"open {c1}", chat="gone")
    proposal = turn(client, "loan amount 700000 karo", chat="gone")["markdown"]
    turn(client, "what is pending", chat="gone")                          # moves on: the proposal is dropped
    r = client.post(FOS, json={"action_link": _href(proposal, "Confirm"), "chat_id": "gone", "reply_language": "en"})
    assert "no longer pending" in r.json()["tts"] and "nothing was written" in r.json()["tts"], r.text
    assert float(get_repository().get_application(c1).loan_amount) == 500000


def test_a_confirm_waiting_from_before_logout_is_never_executed(client, prod):
    from app.agents.applicant.copilot.capabilities import case_form
    from app.store import get_repository

    _, c1 = make_case(client, "Rahul Sharma")
    turn(client, f"open {c1}", chat="lo")
    proposal = turn(client, "loan amount 800000 karo", chat="lo")["markdown"]
    case_form.end_session("test-subject")                                  # what /api/v1/auth/logout does
    r = client.post(FOS, json={"action_link": _href(proposal, "Confirm"), "chat_id": "lo", "reply_language": "en"})
    assert "no longer pending" in r.json()["tts"], r.text
    assert float(get_repository().get_application(c1).loan_amount) == 500000
    case_form._LOGGED_OUT.pop("test-subject", None)


def test_a_confirm_from_an_earlier_login_session_is_void_under_an_external_idp(make_token, prod):
    """A production IdP's logout never reaches this service; its next login carries a NEW `sid` -- a Confirm that
    waited from the earlier session is never executed. The same session still confirms normally."""
    from fastapi.testclient import TestClient

    import main
    from app.store import get_repository

    def session(sid):
        c = TestClient(main.app)
        c.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES, sid=sid)}"})
        return c

    old = session("sid-before-logout")
    _, c1 = make_case(old, "Rahul Sharma")
    turn(old, f"open {c1}", chat="sid")
    proposal = turn(old, "loan amount 900000 karo", chat="sid")["markdown"]
    new = session("sid-after-login")
    r = new.post(FOS, json={"action_link": _href(proposal, "Confirm"), "chat_id": "sid", "reply_language": "en"})
    assert "no longer pending" in r.json()["tts"], r.text
    assert float(get_repository().get_application(c1).loan_amount) == 500000
    # the SAME session: proposed and confirmed normally
    again = turn(new, "loan amount 900000 karo", chat="sid")["markdown"]
    ok = new.post(FOS, json={"action_link": _href(again, "Confirm"), "chat_id": "sid", "reply_language": "en"})
    assert "Updated" in ok.json()["markdown"], ok.text
    assert float(get_repository().get_application(c1).loan_amount) == 900000
