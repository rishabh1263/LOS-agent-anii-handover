"""
The DEV FLAG SET for the master-spec tests: every chatbot flag ON (as in .env), the owner-gated ones OFF.
Import the `prod` fixture plus `client` / `_store` from test_reupload_supersedes.
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import _PHASE3_FLAGS

#: 2026-10-08 the owner turned the gate / KYC flags ON too (.env); only the legacy login self-grant stays OFF --
#: it is a scope bypass, not a feature (MASTER SPEC section 2)
OFF = {"LOS_LOGIN_SELF_GRANT_LEGACY"}
ON = tuple(f for f in _PHASE3_FLAGS if f not in OFF)


@pytest.fixture
def prod(monkeypatch):
    for flag in ON:
        monkeypatch.setenv(flag, "true")
    from app.agents.applicant.copilot.capabilities import abuse_guard, case_list, exports, importer, safety

    safety.reset()
    abuse_guard.reset()
    exports.reset()
    importer.reset()
    case_list.clear_cache()


def say(client, message, *, lang="en", **extra):
    """One chat turn on /fos/copilot under the reply contract: the markdown."""
    r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": message,
                                                 "reply_language": lang, **extra})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"request_id", "markdown", "tts"}, body
    if os.getenv("DUMP_CONTRACT"):
        print(f"\n>>> {message}\n{body['markdown']}\n--- tts: {body['tts']}")
    return body["markdown"]


def make_case(client, name, **application):
    r = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": name, "mobile": "9876543210", "date_of_birth": "1990-04-12",
                      "address": "12 MG Road, Pune"},
        "application": {"product": "PERSONAL_LOAN", "loan_amount": 500000, **application}})
    assert r.status_code < 300, r.text
    return r.json()["applicant_id"], r.json()["case_id"]
