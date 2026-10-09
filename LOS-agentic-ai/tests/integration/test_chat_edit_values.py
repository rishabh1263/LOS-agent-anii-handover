"""
CHANGING A CASE FIELD FROM CHAT (case_form._propose_edit): the value is read cleanly from natural wording -- amounts
in lakh, command words around the value, "X ki jagah Y" / "instead of X make it Y" -- then Confirm saves it.
"""

from __future__ import annotations

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

CASES = [
    ("loan amount 6 lakh kar do", "loan_amount", "600000"),
    ("change loan amount to 750000", "loan_amount", "750000"),
    ("mobile number change karo 9123456780", "mobile", "9123456780"),
    ("address update karo: 45 FC Road, Pune", "address", "45 FC Road, Pune"),
    ("Rahul Sharma ki jagah Rahul Kumar Sharma karo", "full_name", "Rahul Kumar Sharma"),
    ("instead of Rahul Sharma make it Rahul K Sharma", "full_name", "Rahul K Sharma"),
]


@pytest.mark.parametrize("message,field,value", CASES)
def test_edit_from_chat(client, prod, message, field, value):
    _, case_id = make_case(client, "Rahul Sharma")
    chat = f"edit-{field}-{value}"
    say = lambda m: client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": m,  # noqa: E731
                                                              "reply_language": "en", "chat_id": chat}).json()["markdown"]
    say(f"open {case_id}")
    proposal = say(message)
    assert "Confirm" in proposal, proposal
    assert "Updated" in say("confirm")
    stored = client.get(f"/api/v1/fos/cases/{case_id}/form").json()
    values = stored.get("values") or stored
    assert str(values[field]) == value
