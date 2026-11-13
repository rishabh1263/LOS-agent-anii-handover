"""
P1-A (2026-10-18): THE QWEN AGENT'S HISTORY IS SHARED BY EVERY WORKER.

The history is a row of the conversation store's own table (conversation_state, "agent:" + chat id, under the
caller's subject), masked then encrypted. "Another worker" here is ANOTHER OS PROCESS on the same PostgreSQL test
database -- its own empty process memory, the way a second uvicorn worker or a restarted one starts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.agents.applicant.copilot.capabilities import qwen_agent
from app.store import set_repository
from app.store.testing import fresh_repository

ROOT = Path(__file__).resolve().parents[2]
OFFICER, OTHER = "officer-a", "officer-b"


@pytest.fixture
def repo(monkeypatch):
    repository = fresh_repository()
    repository.initialise()
    set_repository(repository)
    qwen_agent._MEMORY.clear()
    yield repository
    qwen_agent._MEMORY.clear()
    set_repository(None)


def _worker(repository, body: str) -> dict:
    """Run `body` in a NEW Python process against the same database; it prints one JSON line."""
    script = "import json, sys\nfrom app.agents.applicant.copilot.capabilities import qwen_agent\n" + textwrap.dedent(body)
    env = {**os.environ, "LOS_STORE_DSN": repository._dsn, "LOS_DEV_EMBEDDED_PG": "false"}
    env.pop("PYTEST_CURRENT_TEST", None)
    done = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def _row(repository, subject: str, chat: str):
    return repository.get_conversation(subject, "agent:" + chat)


# ---- across workers / restarts --------------------------------------------------------------------------------------

def test_a_follow_up_on_another_worker_gets_the_same_history(repo):
    qwen_agent.remember((OFFICER, "chat-1"), "what is pending for this case", "Salary slip is pending.", "fos_1")
    seen = _worker(repo, f"""
        print(json.dumps(qwen_agent.history(({OFFICER!r}, "chat-1"))))
    """)
    assert seen == [{"role": "user", "content": "what is pending for this case"},
                    {"role": "assistant", "content": "Salary slip is pending."}]


def test_what_another_worker_stored_reaches_this_one(repo):
    _worker(repo, f"""
        qwen_agent.remember(({OFFICER!r}, "chat-1"), "kyc status?", "KYC is verified.", "fos_9")
        print(json.dumps({{"ok": True}}))
    """)
    assert qwen_agent._MEMORY == {}                                     # nothing of it in this process
    assert qwen_agent.history((OFFICER, "chat-1"))[-1] == {"role": "assistant", "content": "KYC is verified."}


def test_a_restarted_worker_continues_the_conversation(repo):
    qwen_agent.remember((OFFICER, "chat-1"), "q1", "a1", "fos_1")
    _worker(repo, f"""
        qwen_agent.remember(({OFFICER!r}, "chat-1"), "q2", "a2", "fos_2")
        print(json.dumps({{"ok": True}}))
    """)
    after = _worker(repo, f"""
        print(json.dumps(qwen_agent.history(({OFFICER!r}, "chat-1"))))
    """)
    assert [t["content"] for t in after] == ["q1", "a1", "q2", "a2"]


# ---- isolation ------------------------------------------------------------------------------------------------------

def test_two_chats_of_one_officer_never_share_history(repo):
    qwen_agent.remember((OFFICER, "chat-1"), "about case A", "Case A is in review.", "fos_1")
    assert qwen_agent.history((OFFICER, "chat-2")) == []
    assert _worker(repo, f"print(json.dumps(qwen_agent.history(({OFFICER!r}, 'chat-2'))))") == []


def test_another_officer_with_the_same_chat_id_never_reads_it(repo):
    qwen_agent.remember((OFFICER, "chat-1"), "applicant Rahul's PAN status", "PAN is verified.", "fos_1")
    assert qwen_agent.history((OTHER, "chat-1")) == []
    assert _worker(repo, f"print(json.dumps(qwen_agent.history(({OTHER!r}, 'chat-1'))))") == []


def test_a_chat_id_with_separators_cannot_reach_another_subject(repo):
    qwen_agent.remember(("a|b", "c"), "secret question", "secret answer", "fos_1")
    assert qwen_agent.history(("a", "b|c")) == []                       # the old joined key would have collided


# ---- order, window, retries -----------------------------------------------------------------------------------------

def test_turns_keep_their_order_and_the_window(repo, monkeypatch):
    monkeypatch.setitem(qwen_agent.cfg(), "history_exchanges", 3)
    for i in range(1, 5):
        qwen_agent.remember((OFFICER, "chat-1"), f"q{i}", f"a{i}", f"fos_{i}")
    assert [t["content"] for t in qwen_agent.history((OFFICER, "chat-1"))] == ["q2", "a2", "q3", "a3", "q4", "a4"]


def test_a_retried_request_is_stored_once_even_from_another_worker(repo):
    qwen_agent.remember((OFFICER, "chat-1"), "q1", "a1", "fos_same")
    qwen_agent.remember((OFFICER, "chat-1"), "q1", "a1", "fos_same")
    _worker(repo, f"""
        qwen_agent.remember(({OFFICER!r}, "chat-1"), "q1", "a1", "fos_same")
        print(json.dumps({{"ok": True}}))
    """)
    assert [t["content"] for t in qwen_agent.history((OFFICER, "chat-1"))] == ["q1", "a1"]


def test_concurrent_turns_in_one_process_neither_lose_nor_corrupt_a_turn(repo):
    import threading

    threads = [threading.Thread(target=qwen_agent.remember, args=((OFFICER, "chat-1"), f"q{i}", f"a{i}", f"fos_{i}"))
               for i in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    turns = qwen_agent.history((OFFICER, "chat-1"))
    assert len(turns) == 6 and all(turns[i]["role"] == ("user", "assistant")[i % 2] for i in range(6))
    assert sorted(t["content"] for t in turns[::2]) == ["q0", "q1", "q2"]


# ---- masking, encryption, expiry ------------------------------------------------------------------------------------

def test_the_row_is_masked_then_encrypted(repo):
    qwen_agent.remember((OFFICER, "chat-1"), "is PAN ABCDE1234F verified for Rahul", "Yes, it is verified.", "fos_1")
    stored, _ = _row(repo, OFFICER, "chat-1")
    assert "ABCDE1234F" not in stored and "Rahul" not in stored and "verified" not in stored   # sealed at rest
    said = qwen_agent.history((OFFICER, "chat-1"))[0]["content"]
    assert "ABCDE1234F" not in said and "verified" in said                                    # masked, readable


def test_an_expired_conversation_gives_no_history(repo, monkeypatch):
    from app.agents.applicant.copilot.conversation import state as conv

    qwen_agent.remember((OFFICER, "chat-1"), "q1", "a1", "fos_1")
    monkeypatch.setattr(conv, "ttl_seconds", lambda: -1)
    assert qwen_agent.history((OFFICER, "chat-1")) == []


# ---- failing safe ---------------------------------------------------------------------------------------------------

class _Down:
    def __getattr__(self, name):
        def fail(*_a, **_k):
            raise ConnectionError("store down")
        return fail


def test_a_store_that_is_down_falls_back_to_this_process_and_never_raises(repo):
    set_repository(_Down())
    qwen_agent.remember((OFFICER, "chat-1"), "q1", "a1", "fos_1")          # no exception
    assert [t["content"] for t in qwen_agent.history((OFFICER, "chat-1"))] == ["q1", "a1"]


def test_no_chat_ref_remembers_nothing(repo):
    qwen_agent.remember(None, "q", "a", "fos_1")
    assert qwen_agent.history(None) == [] and qwen_agent._MEMORY == {}


def test_shared_history_off_is_this_process_only(repo, monkeypatch):
    monkeypatch.setitem(qwen_agent.cfg(), "shared_history", False)
    qwen_agent.remember((OFFICER, "chat-1"), "q1", "a1", "fos_1")
    assert _row(repo, OFFICER, "chat-1") is None
    assert [t["content"] for t in qwen_agent.history((OFFICER, "chat-1"))] == ["q1", "a1"]


def test_only_role_and_content_reach_the_model(repo):
    qwen_agent.remember((OFFICER, "chat-1"), "q1", "a1", "fos_1")
    assert all(set(t) == {"role", "content"} for t in qwen_agent.history((OFFICER, "chat-1")))
