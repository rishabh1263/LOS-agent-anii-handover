"""
PHASE 3 STEP 6-MVP -- the case workspace (COPILOT_CASE_WORKSPACE, default off).

List the caller's OWN cases (live grants only; counts, 0 hidden; "Rahul S."), select by
click / number / ordinal / id / name, open (snapshot, header, buttons), answer inside the
opened case, exit / switch, another case named inside one, out of scope -> 403. Flag off:
the workspace actions are refused and nothing else changes.
"""

from __future__ import annotations

import pytest

from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

COPILOT = "/api/v1/fos/copilot"


def make_case(client, name: str) -> tuple[str, str]:
    r = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": name, "mobile": "9876543210", "date_of_birth": "1990-04-12", "address": "Mumbai"},
        "application": {"product": "PERSONAL_LOAN", "loan_amount": 500000}})
    assert r.status_code == 201, r.text
    return r.json()["applicant_id"], r.json()["case_id"]


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("COPILOT_CASE_WORKSPACE", "true")


def ws(client, message=None, action="CUSTOM_QUERY", case_id=None, applicant_id=None, context=None, status=200):
    body = {"action": action}
    for key, value in (("message", message), ("case_id", case_id), ("applicant_id", applicant_id),
                       ("context", context)):
        if value is not None:
            body[key] = value
    r = client.post(COPILOT, json=body)
    assert r.status_code == status, r.text
    return r.json()


# ---- flag off -------------------------------------------------------------------------------
def test_flag_off_refuses_the_workspace_actions_and_keeps_applicant_id_required(client):
    ws(client, action="LIST_CASES", status=422)
    ws(client, message="status batao", status=422)                  # no applicant_id: still required


# ---- the list ---------------------------------------------------------------------------------
def test_my_cases_lists_only_the_callers_cases_with_counts_and_short_names(client, on, make_token):
    a1, c1 = make_case(client, "Rahul Sharma")
    a2, c2 = make_case(client, "Priya Verma")
    from tests.integration.test_fos_stage_boundary import FOS_SCOPES

    other = client.headers.copy()
    client.headers.update({"Authorization": f"Bearer {make_token(subject='someone-else', scopes=FOS_SCOPES)}"})
    make_case(client, "Not Mine")
    client.headers.clear()
    client.headers.update(other)

    body = ws(client, message="mere cases dikhao")
    assert body["intent"] == "CASE_LIST"
    rows = body["presentation"]["case_list"]
    ids = {r["case_id"] for r in rows}
    assert {c1, c2} <= ids and len(ids) == len(rows)
    assert all("Not" not in r["applicant_name"] for r in rows)       # someone else's case is never listed
    names = {r["applicant_name"] for r in rows}
    assert "Rahul S." in names and "Priya V." in names               # "Rahul S." in the list
    assert all(r["action"] == {"type": "open_case", "case_id": r["case_id"]} for r in rows)
    assert "Kis case mein jaana hai?" in body["answer"]
    counts = body["presentation"]["counts"]
    assert counts["total"] == len(rows) and "rejected" not in counts      # a 0 count is hidden


@pytest.mark.parametrize("phrase", ["my cases", "kaunse cases hai", "case list dikhao"])
def test_list_phrases_from_config(client, on, phrase):
    make_case(client, "Rahul Sharma")
    assert ws(client, message=phrase)["intent"] == "CASE_LIST"


# ---- select and open --------------------------------------------------------------------------
def test_click_opens_the_case_with_a_snapshot_and_buttons(client, on):
    a, c = make_case(client, "Rahul Sharma")
    body = ws(client, action="OPEN_CASE", case_id=c)
    assert body["intent"] == "CASE_OPENED" and body["case_id"] == c
    assert f"🔓 **{c} (Rahul Sharma)** opened." in body["answer"]                 # full name inside
    assert "📍 Stage:" in body["answer"]
    block = body["presentation"]["workspace"]
    assert block["header"] == f"📍 {c}" and {b["type"] for b in block["buttons"]} >= {"exit_case", "switch_case", "ask"}


def test_select_by_number_ordinal_id_and_name(client, on):
    _, c1 = make_case(client, "Rahul Sharma")
    a2, c2 = make_case(client, "Priya Verma")
    rows = ws(client, message="mere cases")["presentation"]["case_list"]
    second = rows[1]["case_id"]
    assert ws(client, message="2")["case_id"] == second
    ws(client, message="list dikhao")
    assert ws(client, message="doosra wala")["case_id"] == second
    assert ws(client, message=f"{c2[:12]} kholo")["case_id"] == c2                  # an id prefix
    assert ws(client, message=f"{a2} ka case")["case_id"] == c2                      # the applicant id
    assert ws(client, message="Priya ka case kholo")["case_id"] == c2                # a name, fuzzily
    assert ws(client, message="Pria ka case kholo")["case_id"] == c2                 # with a typo


def test_the_list_is_grouped_by_applicant_and_a_number_with_words_opens(client, on, _store):
    """One applicant, several cases: grouped under the applicant id; "2 kholo" opens row 2 as shown."""
    from app.store.models import Application, ApplicationStatus

    a1, c1 = make_case(client, "Rahul Sharma")
    a2, c2 = make_case(client, "Priya Verma")
    _store.save_application(Application(case_id="CASE-0B0B0B0B0B0B", applicant_id=a1,
                                        status=ApplicationStatus.DOCUMENT_COLLECTION))
    body = ws(client, message="list all cases")
    groups = body["presentation"]["applicant_groups"]
    mine = next(g for g in groups if g["applicant_id"] == a1)
    assert mine["case_count"] == 2 and set(mine["case_ids"]) == {c1, "CASE-0B0B0B0B0B0B"}
    assert f"🆔 **{a1}**" in body["answer"] and "(2 cases)" in body["answer"]
    rows = body["presentation"]["case_list"]
    assert [r["applicant_id"] for r in rows[:2]] == [a1, a1]                       # a group stays together
    assert ws(client, message="2 kholo")["case_id"] == rows[1]["case_id"]
    ws(client, message="mere cases")
    assert ws(client, message="1 wala case open karo")["case_id"] == rows[0]["case_id"]
    ws(client, message="mere cases")
    assert ws(client, message="doosra wala case kholo")["case_id"] == rows[1]["case_id"]


def test_two_people_with_the_same_name_get_one_question(client, on):
    make_case(client, "Amit Kumar")
    make_case(client, "Amit Kumar")
    body = ws(client, message="Amit ka case kholo")
    assert body["intent"] == "CASE_SELECTION" and body["clarification_required"]["reason"] == "CASE_AMBIGUOUS"
    assert len(body["clarification_required"]["options"]) == 2


def test_a_case_that_is_not_mine_is_refused(client, on, make_token):
    from tests.integration.test_fos_stage_boundary import FOS_SCOPES

    mine = client.headers.copy()
    client.headers.update({"Authorization": f"Bearer {make_token(subject='someone-else', scopes=FOS_SCOPES)}"})
    _, theirs = make_case(client, "Not Mine")
    client.headers.clear()
    client.headers.update(mine)
    ws(client, action="OPEN_CASE", case_id=theirs, status=403)
    body = ws(client, message=f"{theirs} kholo")
    assert body["intent"] == "CASE_SELECTION" and theirs not in str(body.get("presentation"))


# ---- inside a case ------------------------------------------------------------------------------
def test_questions_inside_the_open_case_use_it_and_carry_the_header(client, on):
    a, c = make_case(client, "Rahul Sharma")
    ws(client, action="OPEN_CASE", case_id=c)
    body = ws(client, message="kya baaki hai?")                         # no case id, no applicant id
    assert body["answer"].startswith(f"📍 {c}\n"), body["answer"]
    assert body.get("case_id") in (c, None) and body["intent"] not in ("CASE_LIST", "CASE_SELECTION")


def test_exit_and_switch_close_the_case(client, on):
    _, c = make_case(client, "Rahul Sharma")
    ws(client, action="OPEN_CASE", case_id=c)
    body = ws(client, message="bahar aao")
    assert body["answer"].startswith(f"🔒 **{c}** closed.") and body["intent"] == "CASE_LIST"
    after = ws(client, message="kya baaki hai?")
    assert after["intent"] == "CASE_SELECTION"                          # nothing open: asked to open one
    ws(client, action="OPEN_CASE", case_id=c)
    assert ws(client, action="EXIT_CASE")["answer"].startswith(f"🔒 **{c}** closed.")


def test_another_case_named_inside_one_is_answered_and_asked_never_switched(client, on):
    _, c1 = make_case(client, "Rahul Sharma")
    _, c2 = make_case(client, "Priya Verma")
    ws(client, action="OPEN_CASE", case_id=c1)
    body = ws(client, message=f"{c2} ka status kya hai?")
    assert f"Is case mein jaana hai? ({c2})" in body["answer"]
    still = ws(client, message="kya baaki hai?")
    assert still["answer"].startswith(f"📍 {c1}\n")                    # still in the first case


def test_the_list_is_never_bigger_than_the_page_and_aur_dikhao_pages(client, on, monkeypatch):
    from app.agents.applicant import config

    base = config.chatbot("case_workspace")
    monkeypatch.setattr(config, "chatbot", lambda name, _orig=config.chatbot: {**base, "page_size": 2}
                        if name == "case_workspace" else _orig(name))
    for name in ("A One", "B Two", "C Three"):
        make_case(client, name)
    first = ws(client, message="mere cases")
    assert len(first["presentation"]["case_list"]) == 2 and first["presentation"]["has_more"]
    second = ws(client, message="aur dikhao")
    assert second["presentation"]["page"] == 1 and len(second["presentation"]["case_list"]) >= 1
