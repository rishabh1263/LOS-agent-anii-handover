"""
THE FRONTEND CONTRACT (frontend_handoff/API_CONTRACT.md, version in app/config/copilot_reply.yaml contract_version).

Fails when the shape the frontend depends on changes WITHOUT bumping the contract version: the reply keys, the error
body, the action result types, the SSE events, the chat endpoints and the request schemas are compared with
frontend_handoff/contract_snapshot.json (regenerate it with scripts/contract_snapshot.py when you bump the version).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = json.loads((ROOT / "frontend_handoff" / "contract_snapshot.json").read_text(encoding="utf-8"))
BUMP = ("the frontend contract changed: bump contract_version (app/config/copilot_reply.yaml), update "
        "frontend_handoff/API_CONTRACT.md + contract.ts, then run scripts/contract_snapshot.py")


def test_the_static_contract_matches_the_snapshot():
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import contract_snapshot

    now = contract_snapshot.current()
    if now["contract_version"] == SNAPSHOT["contract_version"]:
        assert now == SNAPSHOT, BUMP
    else:
        assert now != SNAPSHOT, "the version was bumped: regenerate frontend_handoff/contract_snapshot.json"


def test_a_chat_reply_has_exactly_the_contract_keys_and_the_version_header(client, prod):
    make_case(client, "Rahul Sharma")
    r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "show my cases",
                                                 "chat_id": "contract", "reply_language": "en"})
    assert r.status_code == 200
    assert sorted(r.json()) == SNAPSHOT["reply_keys"]
    assert r.headers["X-Contract-Version"] == SNAPSHOT["contract_version"]


def test_action_results_and_errors_keep_their_shape(client, prod):
    _, case_id = make_case(client, "Rahul Sharma")
    seen = set()
    for href in (f"action:show_in_ui?case={case_id}", "action:upload?doc=PAN&party=applicant", "action:copy?ref=draft-1",
                 f"action:open_case?id={case_id}"):
        body = client.post("/api/v1/fos/action", json={"href": href, "chat_id": "contract"}).json()
        seen.add(body["type"])
        if body["type"] == "reply":
            assert set(body) == {"type", *SNAPSHOT["reply_keys"]}
    assert sorted(seen) == SNAPSHOT["action_result_types"]
    bad = client.post("/api/v1/fos/copilot", json={"action": "custom_query", "message": "x", "chat_id": "contract"})
    assert bad.status_code == 422 and sorted(bad.json()["error"]) == SNAPSHOT["error_keys"]
    assert bad.headers["X-Contract-Version"] == SNAPSHOT["contract_version"]


def test_the_stream_sends_only_the_contract_events(client, prod):
    make_case(client, "Rahul Sharma")
    r = client.post("/api/v1/fos/copilot/stream", json={"action": "CUSTOM_QUERY", "message": "show my cases",
                                                        "chat_id": "contract-s", "reply_language": "en"})
    events = set(re.findall(r"^event: (\w+)", r.text, re.M))
    assert events and events <= set(SNAPSHOT["sse_events"]) and "final" in events
