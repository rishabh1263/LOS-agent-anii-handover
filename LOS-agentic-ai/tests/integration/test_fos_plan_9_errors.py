"""
FOS PLAN 9.4 -- every failure path gives a clean, specific message: the case store down (503), a timeout (504),
no access (403), out of scope, the model down -- never a stack trace or an internal name. Both endpoints.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

_INTERNAL = re.compile(r"traceback|\.py\b|psycopg|sqlite|postgres|127\.0\.0\.1|port=|ollama|qdrant|app\.agents|"
                       r"connection refused", re.I)


@pytest.fixture
def lenient(client):
    """A client that returns the 500 / 503 response instead of re-raising, as a real server does."""
    import main

    c = TestClient(main.app, raise_server_exceptions=False)
    c.headers.update(client.headers)
    return c


@pytest.mark.parametrize("failure, status, code", [
    (ConnectionError("could not connect to server: Connection refused (host=127.0.0.1 port=5432)"), 503,
     "CASE_STORE_UNAVAILABLE"),
    (TimeoutError("read timed out after 30s in app.agents.applicant"), 504, "TIMEOUT"),
])
@pytest.mark.parametrize("endpoint", ["/api/v1/fos/copilot", "/api/v1/copilot/query"])
def test_a_dependency_failure_is_specific_and_leaks_nothing(client, lenient, demo, _store, monkeypatch, failure,
                                                            status, code, endpoint):
    a, c = make_case(client, "Rahul Sharma")

    def boom(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(type(_store), "get_application", boom)
    body = {"message": "status kya hai?", "case_id": c, "applicant_id": a} \
        | ({"action": "CUSTOM_QUERY"} if "fos" in endpoint else {})
    r = lenient.post(endpoint, json=body)
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code
    assert not _INTERNAL.search(r.text), r.text


def test_no_access_and_out_of_scope_are_clean(client, demo, make_token):
    from tests.integration.test_fos_stage_boundary import FOS_SCOPES

    own = dict(client.headers)
    client.headers.update({"Authorization": f"Bearer {make_token(subject='someone-else', scopes=FOS_SCOPES)}"})
    a, theirs = make_case(client, "Not Mine")
    client.headers.clear()
    client.headers.update(own)
    denied = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "status kya hai?",
                                                       "case_id": theirs, "applicant_id": a})
    assert denied.status_code == 403 and not _INTERNAL.search(denied.text)
    _, mine = make_case(client, "Rahul Sharma")
    client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": mine})
    off = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY",
                                                    "message": "what is the weather in Mumbai today"}).json()
    assert off["answer"] and not _INTERNAL.search(off["answer"])
