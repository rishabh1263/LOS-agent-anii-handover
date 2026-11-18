"""Anything pending -> its Upload buttons under the answer (owner, 2026-10-14: the KYC status said "upload the
documents" with no button). Only the case's own pending documents become buttons."""
from __future__ import annotations

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401
from tests.integration.test_vague_messages import say


def test_kyc_status_on_a_case_with_pending_documents_carries_upload_buttons(client, prod):
    _, case_id = make_case(client, "Rahul Sharma")
    say(client, f"open {case_id}", "up-kyc")
    md = say(client, "what is the kyc status", "up-kyc")
    assert "(action:upload?doc=" in md, md


def test_a_greeting_never_carries_upload_buttons(client, prod):
    _, case_id = make_case(client, "Rahul Sharma")
    say(client, f"open {case_id}", "up-hi")
    md = say(client, "hi", "up-hi")
    assert "(action:upload?" not in md, md
