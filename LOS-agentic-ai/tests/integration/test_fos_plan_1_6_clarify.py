"""
FOS PLAN 1.6 -- an ambiguous or garbled message gets ONE clarifying question, never "not recorded" or "outside what
I can help with". On both endpoints.
"""

from __future__ import annotations

import pytest

from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

ENDPOINTS = ["/api/v1/fos/copilot", "/api/v1/copilot/query"]


@pytest.fixture
def opened(client, demo):
    _, c = make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": c})
    return c


def ask(client, endpoint, message):
    body = {"message": message} | ({"action": "CUSTOM_QUERY"} if "fos" in endpoint else {})
    return client.post(endpoint, json=body).json()


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_document_named_before_case_asks_which_details(client, opened, endpoint):
    reply = ask(client, endpoint, "mere Aadhar case ka details kaise dekhun")
    assert "not been recorded" not in reply["answer"] and "No Aadhaar" not in reply["answer"]
    assert "Aadhaar details, or this case's details?" in reply["answer"], reply["answer"]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("message", ["list of kaise sajao", "details ka kya karu yaar"])
def test_a_garbled_message_with_a_work_word_is_never_out_of_scope(client, opened, endpoint, message):
    reply = ask(client, endpoint, message)
    assert "outside what I can help" not in reply["answer"], reply["answer"]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_the_same_document_named_with_this_case_is_not_ambiguous(client, opened, endpoint):
    reply = ask(client, endpoint, "is case ka PAN detail")
    assert "or this case's details?" not in reply["answer"]
