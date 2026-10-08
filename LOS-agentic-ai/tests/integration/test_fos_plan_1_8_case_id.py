"""
FOS PLAN 1.8 -- a frontend that sends the same case_id on every request (the one from login) never overrides the
case the officer opened in the workspace. On both endpoints. Without an opened case the sent case_id is used as
always (and ownership-checked as always).
"""

from __future__ import annotations

import pytest

from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


@pytest.mark.parametrize("endpoint", ["/api/v1/fos/copilot", "/api/v1/copilot/query"])
def test_the_opened_case_wins_over_a_stale_case_id(client, demo, endpoint):
    a1, c1 = make_case(client, "Rahul Sharma")
    a2, c2 = make_case(client, "Priya Verma")
    client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": c2})
    body = {"message": "case details do", "case_id": c1, "applicant_id": a1} \
        | ({"action": "CUSTOM_QUERY"} if "fos" in endpoint else {})
    reply = client.post(endpoint, json=body).json()
    assert c2 in reply["answer"] and c1 not in reply["answer"], reply["answer"]


def test_without_an_opened_case_the_sent_case_id_is_used(client, demo):
    a1, c1 = make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    reply = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "case details do",
                                                      "case_id": c1, "applicant_id": a1}).json()
    assert c1 in reply["answer"]


def test_a_stale_case_id_of_someone_else_is_never_read(client, demo, make_token):
    from tests.integration.test_fos_stage_boundary import FOS_SCOPES

    _, mine = make_case(client, "Rahul Sharma")
    own = dict(client.headers)
    client.headers.update({"Authorization": f"Bearer {make_token(subject='someone-else', scopes=FOS_SCOPES)}"})
    _, theirs = make_case(client, "Not Mine")
    client.headers.clear()
    client.headers.update(own)
    client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": mine})
    reply = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "case details do",
                                                      "case_id": theirs}).json()
    assert theirs not in str(reply) and mine in reply["answer"]
