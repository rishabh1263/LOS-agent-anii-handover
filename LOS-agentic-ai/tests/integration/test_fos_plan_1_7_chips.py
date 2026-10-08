"""
FOS PLAN 1.7 -- the suggestion chips follow the conversation: never the same chips on every turn, and never the
question that was just answered.
"""

from __future__ import annotations

from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


def chips(client, message):
    return client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": message}).json() \
        .get("suggested_questions") or []


def test_chips_change_with_the_turn_and_skip_what_was_just_answered(client, demo):
    _, c = make_case(client, "Rahul Sharma")
    client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": c})
    after_pending = chips(client, "kya baaki hai?")
    after_status = chips(client, "status kya hai?")
    after_kyc = chips(client, "KYC ka kya status hai?")
    assert after_pending and after_status and after_kyc
    assert not any("pending" in s.lower() for s in after_pending), after_pending
    assert "What is the KYC status?" not in after_kyc, after_kyc                  # the question just answered
    assert len({tuple(after_pending), tuple(after_status), tuple(after_kyc)}) == 3   # never the same every turn


def test_the_rule_is_config():
    from app.agents.applicant import frontend

    assert frontend.contextual_suggestions(["What is pending on this case?", "Why is PAN required?"],
                                           "DOCUMENTS_PENDING")[0] == "Is this case ready for CPA?"
