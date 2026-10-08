"""
FOS PLAN 1.5 -- one count of documents. The transcript said "3 pending" in chat and showed 7 upload buttons: both
were true (3 required + 4 optional), neither said so. `case_state` is the one count (required only, plus the optional
ones counted apart), the chat's pending list agrees with it, and every upload button says REQUIRED or OPTIONAL.
"""

from __future__ import annotations

from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


def test_required_and_optional_are_counted_apart_and_agree_with_the_chat(client, demo):
    a, c = make_case(client, "Rahul Sharma")
    body = client.post("/api/v1/fos/copilot", json={"applicant_id": a, "case_id": c,
                                                     "action": "GET_DOCUMENT_CHECKLIST"}).json()
    state = body["case_state"]
    required = [r for r in body["checklist"] if r.get("mandatory", True)]
    optional = [r for r in body["checklist"] if not r.get("mandatory", True)]
    assert state["documents_required"] == len(required) and state["documents_missing"] == len(required)
    assert state["documents_optional"] == len(optional) and state["documents_optional_missing"] == len(optional)

    uploads = [x for r in body["checklist"] for x in r.get("actions") or [] if x["action"] == "UPLOAD_DOCUMENT"]
    assert sum(1 for x in uploads if x["requirement"] == "REQUIRED") == state["documents_missing"]
    assert all(x["label"].endswith("(optional)") for x in uploads if x["requirement"] == "OPTIONAL")

    pending = client.post("/api/v1/fos/copilot", json={"applicant_id": a, "case_id": c, "action": "CUSTOM_QUERY",
                                                        "message": "kya baaki hai?"}).json()
    rows = [line for line in pending["answer"].splitlines() if line.startswith("📄")]
    assert len(rows) == state["documents_missing"], pending["answer"]          # the chat says the same count
