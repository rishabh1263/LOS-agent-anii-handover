"""
MIGRATION 0006 -- chat history (step 6c; approved 2026-10-07 with: UNIQUE per turn, backup-table cleanup,
scheduled cleanup, forget-me). On a throwaway test database -- never the dev one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.store import chat_history, crypto, set_repository
from app.store.models import Applicant, Application
from app.store.postgres_repo import apply_gated
from app.store.testing import fresh_repository


@pytest.fixture
def repo(monkeypatch):
    monkeypatch.setenv("COPILOT_SESSION_MEMORY", "true")
    repository = fresh_repository()
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


@pytest.fixture
def ready(repo):
    assert apply_gated(repo, "0006") is True
    return repo


def _case(repo):
    repo.save_applicant(Applicant(applicant_id="APP-0006A", full_name="Rahul Sharma", address="12 MG Road, Pune"))
    repo.save_application(Application(case_id="CASE-0006A", applicant_id="APP-0006A", product="PERSONAL_LOAN"))
    return "CASE-0006A"


def test_without_the_table_nothing_is_stored_and_nothing_fails(repo):
    assert repo.chat_history_ready() is False and chat_history.enabled() is False
    assert chat_history.cleanup()["skipped"]


def test_a_turn_is_masked_then_encrypted_and_the_subject_hashed(ready):
    case_id = _case(ready)
    chat_history.record(subject="officer-1", conversation_id="conv1", case_id=case_id, turn_no=1,
                        question="Rahul Sharma ka PAN ABCDE1234F, ghar 12 MG Road, Pune", answer="ok", intent="X")
    rows = ready.chat_turns(chat_history.subject_hash("officer-1"), "conv1")
    assert [r["role"] for r in rows] == ["USER", "BOT"]
    stored = rows[0]["text_sealed"]
    assert "Rahul" not in stored and "ABCDE1234F" not in stored and "MG Road" not in stored   # encrypted at rest
    plain = crypto.open_value(stored)
    assert "Rahul" not in plain and "ABCDE1234F" not in plain and "[ADDRESS]" in plain and "[NAME]" in plain
    assert "officer-1" not in str(rows)                                                       # no login id


def test_a_retry_never_duplicates_a_turn(ready):
    for _ in range(2):
        chat_history.record(subject="s", conversation_id="c", case_id=None, turn_no=3, question="q", answer="a",
                            intent=None)
    assert len(ready.chat_turns(chat_history.subject_hash("s"), "c")) == 2                    # one USER + one BOT


def test_forget_me_deletes_every_row_of_that_subject_only(ready):
    for subject in ("me", "someone-else"):
        chat_history.record(subject=subject, conversation_id="c", case_id=None, turn_no=1, question="q", answer="a",
                            intent=None)
    assert chat_history.forget("me") == 2
    assert ready.chat_turns(chat_history.subject_hash("me"), "c") == []
    assert len(ready.chat_turns(chat_history.subject_hash("someone-else"), "c")) == 2


def test_retention_deletes_expired_turns_and_old_backup_tables(ready):
    chat_history.record(subject="s", conversation_id="c", case_id=None, turn_no=1, question="q", answer="a",
                        intent=None)
    future = datetime.now(timezone.utc) + timedelta(days=91)            # "now" for the cleanup
    old = (future - timedelta(days=120)).strftime("%Y%m%dT%H%M%S")      # older than retention -> dropped
    new = (future - timedelta(days=10)).strftime("%Y%m%dT%H%M%S")       # within retention -> kept
    for stamp in (old, new):
        ready._write(f'CREATE TABLE "chat_turns_backup_{stamp}" AS SELECT * FROM chat_turns', ())
    dry = chat_history.cleanup(dry_run=True, now=future)
    assert dry["expired_turns"] == 2 and dry["backup_tables_dropped"] == [f"chat_turns_backup_{old}"]
    assert len(ready.chat_turns(chat_history.subject_hash("s"), "c")) == 2                      # dry run: kept
    done = chat_history.cleanup(now=future)
    assert done["expired_turns"] == 2 and ready.chat_turns(chat_history.subject_hash("s"), "c") == []
    left = [r["table_name"] for r in ready._all(
        "SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'chat_turns_backup_%'", ())]
    assert left == [f"chat_turns_backup_{new}"]                                                 # the recent copy stays


def test_the_migration_is_gated_on_session_memory(monkeypatch):
    from app.store import postgres_repo

    monkeypatch.delenv("COPILOT_SESSION_MEMORY", raising=False)
    assert postgres_repo._gated_auto_apply_allowed("0006") == (False, "COPILOT_SESSION_MEMORY is off")
    assert postgres_repo.GATED_FLAGS["0004"] == "LOS_COAPP_IDENTITY"
