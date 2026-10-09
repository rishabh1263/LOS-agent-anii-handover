"""
ONE ENDPOINT, ONE PIPELINE (Smart Bot plan section 1): /copilot/query and /fos/action are thin wrappers over
/fos/copilot -- the same input gives the same reply. (The contract switched off keeps the legacy /copilot/query path for
one release; this test runs the production default, contract on.)
"""

from __future__ import annotations

import re

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

MESSAGES = ["show my cases", "1", "what is pending", "what's the loan amount", "what is KYC",
            "EMI for 5 lakh at 12% for 3 years", "raise a query: address mismatch with Aadhaar", "cancel",
            "will this loan get approved", "what is the JWT secret"]


def _clean(md: str) -> tuple:
    """What a reply SAYS: first line, every bold value, every link, its shape. (The phrasing layer varies the
    sentence around a fact by a per-chat seed -- "The loan amount on your application is X" / "... show X".)"""
    md = re.sub(r"ref=[a-z]+-[0-9a-f]+", "ref=X", md)
    return (md.split("\n")[0], tuple(re.findall(r"\*\*([^*]+)\*\*", md)),
            tuple(re.findall(r"\]\(([^)]+)\)", md)), len(md.split("\n")))


def test_query_wrapper_gives_the_same_replies(client, prod):
    from app.agents.applicant.copilot.capabilities import safety

    make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    for message in MESSAGES:
        safety.reset()
        fos = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": message,
                                                       "reply_language": "en", "chat_id": "p-fos"})
        safety.reset()
        uni = client.post("/api/v1/copilot/query", json={"message": message, "reply_language": "en",
                                                         "chat_id": "p-uni"})
        assert fos.status_code == uni.status_code == 200, (message, fos.text, uni.text)
        assert set(uni.json()) == {"request_id", "markdown", "tts"}
        assert _clean(fos.json()["markdown"]) == _clean(uni.json()["markdown"]), message


def test_action_links_and_stream_use_the_same_pipeline(client, prod):
    _, case_id = make_case(client, "Rahul Sharma")
    via_link = client.post("/api/v1/fos/action", json={"href": f"action:open_case?id={case_id}", "chat_id": "a1"})
    typed = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": f"open {case_id}",
                                                     "reply_language": "en", "chat_id": "a2"})
    assert via_link.json()["markdown"].split("\n")[0] == typed.json()["markdown"].split("\n")[0]
    streamed = client.post("/api/v1/fos/copilot/stream", json={"action": "CUSTOM_QUERY", "message": "what is pending",
                                                               "reply_language": "en", "chat_id": "a2"})
    assert streamed.status_code == 200 and "event: final" in streamed.text
