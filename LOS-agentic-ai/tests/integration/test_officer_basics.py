"""
THE OFFICER'S BASICS IN ONE CHAT (owner 2026-10-08: "5 case + 5 document questions"): a case is opened and STAYS open
-- no question closes it ("case kahan atka hai" is not a "stuck cases" list), every answer is about that case.
"""

from __future__ import annotations

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

QUESTIONS = [
    ("is case ka status kya hai", ["pending"]),
    ("case kahan atka hua hai", ["pending"]),
    ("loan amount kitna hai", ["5,00,000"]),
    ("applicant ka naam batao", ["Rahul Sharma"]),
    ("kya ye case cpa mein bhej sakte hai", ["checks passed"]),
    ("PAN upload hua kya", ["PAN", "not been uploaded|hasn't been uploaded|missing"]),
    ("kaunse documents baaki hai", ["Still pending", "Bank Statement"]),
    ("bank statement verify hua?", ["Bank Statement"]),
    ("aadhaar kyu reject hua", ["Aadhaar"]),
    ("address proof mein kya de sakte hai", ["Passport", "Voter ID"]),
]


def test_ten_basics_in_one_chat(client, prod):
    import re

    from app.agents.applicant.copilot.capabilities import safety

    _, case_id = make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": f"open {case_id}", "chat_id": "b"})
    wrong = []
    for message, must in QUESTIONS:
        safety.reset()
        md = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": message,
                                                       "reply_language": "en", "chat_id": "b"}).json()["markdown"]
        if "Which case is this about" in md or "closed." in md or not all(re.search(m, md) for m in must):
            wrong.append(f"{message!r}: {md[:200]!r}")
    assert not wrong, "\n".join(wrong)
