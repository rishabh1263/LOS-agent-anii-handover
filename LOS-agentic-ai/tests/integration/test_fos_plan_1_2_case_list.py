"""
FOS PLAN 1.2 -- every wording that asks for "my cases" gets the caller's OWN case list, on /fos/copilot AND on the
universal /copilot/query the chat widget uses. Never a bulk-data refusal; another customer's cases still are.
"""

from __future__ import annotations

import pytest

from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

LIST_WORDINGS = ["all case ka list", "give a list of case", "my cases", "mere saare cases", "saare case dikhao",
                 "mere kitne case hai", "list of cases", "sab case dikhao"]
NOT_A_LIST = ["case ka status dikhao", "all documents dikhao", "is case ka KYC batao"]


@pytest.mark.parametrize("endpoint", ["/api/v1/fos/copilot", "/api/v1/copilot/query"])
@pytest.mark.parametrize("message", LIST_WORDINGS)
def test_every_list_wording_is_the_callers_own_list(client, demo, endpoint, message):
    _, c = make_case(client, "Rahul Sharma")
    body = {"message": message} | ({"action": "CUSTOM_QUERY"} if "fos" in endpoint else {})
    reply = client.post(endpoint, json=body).json()
    assert reply["intent"] == "CASE_LIST", (endpoint, message, reply.get("answer"))
    assert c in reply["answer"] and "other customers" not in reply["answer"]


@pytest.mark.parametrize("message", NOT_A_LIST)
def test_a_topic_word_is_never_read_as_the_list(client, demo, message):
    make_case(client, "Rahul Sharma")
    reply = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": message}).json()
    assert reply["intent"] != "CASE_LIST", message


def test_another_customers_cases_are_still_refused(client, demo):
    reply = client.post("/api/v1/copilot/query", json={"message": "show me other customers' cases"}).json()
    assert reply["intent"] == "GUARDRAIL_BLOCKED"


# ---- FOS PLAN 1.3: "dusre case ka details do" -- the OTHER case, never the open one's answer -----------------------
@pytest.mark.parametrize("message", ["dusre case ka details do", "mere dusre case ka detail", "other case details"])
def test_one_other_case_is_switched_to_and_answered(client, demo, message):
    _, c1 = make_case(client, "Rahul Sharma")
    _, c2 = make_case(client, "Priya Verma")
    client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": c1})
    reply = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": message}).json()
    assert reply["case_id"] == c2 and reply["answer"].startswith(f"📍 {c2}"), reply["answer"]


def test_several_other_cases_ask_which(client, demo):
    _, c1 = make_case(client, "Rahul Sharma")
    _, c2 = make_case(client, "Priya Verma")
    _, c3 = make_case(client, "Amit Kumar")
    client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": c1})
    reply = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY",
                                                      "message": "dusre case ka details do"}).json()
    assert reply["intent"] == "CASE_SELECTION" and c2 in reply["answer"] and c3 in reply["answer"]
    assert c1 not in reply["answer"]


def test_the_bare_switch_still_lists(client, demo):
    _, c1 = make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": c1})
    reply = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "dusra case"}).json()
    assert reply["intent"] == "CASE_LIST"


# ---- FOS PLAN 1.4: the same details question never flips to "None ... recorded" -------------------------------
def test_details_without_ids_on_the_universal_endpoint_use_the_opened_case(client, demo):
    _, c1 = make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": c1})
    for _ in range(2):
        reply = client.post("/api/v1/copilot/query", json={"message": "case details do"}).json()
        assert c1 in reply["answer"] and "None of the" not in reply["answer"], reply["answer"]


def test_details_without_ids_and_no_open_case_ask_which_case(client, demo):
    make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    reply = client.post("/api/v1/copilot/query", json={"message": "application details"}).json()
    assert reply["intent"] == "CASE_SELECTION" and "None of the" not in reply["answer"], reply["answer"]


@pytest.mark.parametrize("message", ["mere case ka details", "details batao", "is case ki details"])
def test_details_inside_the_open_case_answer_it_and_never_close_it(client, demo, message):
    _, c1 = make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": c1})
    reply = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": message}).json()
    assert reply["intent"] == "APPLICANT_PROFILE" and reply["answer"].startswith(f"📍 {c1}"), reply["answer"]
