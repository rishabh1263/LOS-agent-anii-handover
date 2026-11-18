"""
ELIGIBILITY IS NOT THE CHATBOT'S (owner, 2026-10-13: "eligibility ko chatbot se alag rakhna hai").

The decision belongs to the Eligibility service (POST /api/v1/eligibility/assess). The chatbot imports no eligibility
module, reads no eligibility result and states no eligibility limit: an eligibility / FOIR / policy-limit question gets
the configured redirect line.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import client  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
CHATBOT = ROOT / "app" / "agents" / "applicant" / "copilot"


def test_no_chatbot_module_imports_eligibility():
    found = []
    for path in CHATBOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any(n.startswith("app.agents.eligibility") or n.endswith("eligibility_facts") for n in names):
                found.append(str(path.relative_to(ROOT)))
    assert not found, found


def _say(client, text: str, chat: str, case_id: str | None = None, applicant_id: str | None = None) -> str:
    body = {"action": "CUSTOM_QUERY", "message": text, "chat_id": chat, "reply_language": "en"}
    if case_id:
        body.update(case_id=case_id, applicant_id=applicant_id)
    r = client.post("/api/v1/fos/copilot", json=body)
    assert r.status_code == 200, r.text
    return str(r.json().get("markdown") or r.json().get("answer") or "")


@pytest.mark.parametrize("question", ["is this applicant eligible?", "what is the FOIR for this case?"])
def test_a_case_eligibility_question_is_redirected(client, prod, question):
    applicant_id, case_id = make_case(client, "Sep Elig Case")[:2]
    md = _say(client, question, "sep-elig", case_id, applicant_id)
    assert "Eligibility service" in md, md
    assert "%" not in md.split("\n")[0], md                                   # no FOIR figure stated


def test_a_policy_limit_question_is_redirected(client, prod):
    md = _say(client, "what FOIR is allowed?", "sep-limit")
    assert "Eligibility service" in md and "50%" not in md, md


def test_no_api_path_or_file_name_reaches_a_chat_reply():
    """Owner, 2026-10-13: "koi bhi API ka naam nahi aana chahiye, ya koi file name" -- the redirect names none, and
    the last check on every published reply strips one (screen and voice), leaving the action / ask links alone."""
    from app.agents.applicant import config
    from app.agents.applicant.copilot.answering.contract import _no_api_paths

    redirect = str((config.chatbot("eligibility") or {}).get("redirect") or "")
    assert redirect and "/api/" not in redirect and "POST" not in redirect
    shown = _no_api_paths("Checked in the Eligibility service (POST /api/v1/eligibility/check), not here. "
                          "[Open](action:open_case?id=CASE-1)")
    assert "/api/" not in shown and "POST" not in shown and "(action:open_case?id=CASE-1)" in shown
