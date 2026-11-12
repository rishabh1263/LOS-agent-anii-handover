"""
CONCURRENCY-SAFE CASE CREATION (Phase A hardening, 2026-10-22) -- no schema change.

THE DEFECTS IT CLOSES (fos_api.create_case -> applicant_create -> application_create):
  - a caller-supplied applicant_id / case_id was CHECKED, then UPSERTED: two simultaneous requests both passed the
    check and the second silently OVERWROTE the first (save_* is ON CONFLICT DO UPDATE);
  - a double click / network retry opened TWO cases (UAT-10, 2026-10-22).

HOW, WITHOUT A MIGRATION:
  1. SERIALISED. A Postgres advisory lock per key -- the supplied ids, the declared mobile (keyed HMAC, never the plain
     number) + product, and the caller's Idempotency-Key -- held from the existence check to the last write. Advisory
     locks are database-wide, so this holds across worker processes and servers. Lock waits run in a thread: the event
     loop never blocks. A wait longer than FOS_CREATE_LOCK_TIMEOUT_S (default 10) is refused CREATE_BUSY, never hangs.
  2. IDEMPOTENT. With an Idempotency-Key header, the case created for (caller, key) is recorded as a case event
     (CASE_CREATE_IDEMPOTENCY, ref = SHA-256 of caller + key: the key itself is never stored). A retry finds it under
     the same lock and gets THAT case back -- never a second one. Durable across restarts and processes.
  3. SAME PERSON, SAME PRODUCT, NO KEY: NOT blocked -- whether that is a duplicate depends on the business duplicate
     window, which is not approved (dedupe_policy.yaml P05_DUPLICATE_WINDOW stays null). It is recorded as a
     POSSIBLE_DUPLICATE_AT_CREATE case event for the dedupe assessment and the officer.

LIMITS (stated): the replay lookup and the mobile recheck scan case_events / applicants (no index without migration
0010); a store without a connection pool (none in production) runs unserialised and says so in the log.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)
IDEMPOTENCY_EVENT = "CASE_CREATE_IDEMPOTENCY"
POSSIBLE_DUPLICATE_EVENT = "POSSIBLE_DUPLICATE_AT_CREATE"


class CreateBusy(RuntimeError):
    """Another create holding the same key did not finish within the lock timeout."""


def lock_timeout_s() -> float:
    try:
        return max(0.1, float(os.getenv("FOS_CREATE_LOCK_TIMEOUT_S") or 10))
    except ValueError:
        return 10.0


def idempotency_ref(subject: str | None, key: str | None) -> str | None:
    """SHA-256 of caller + key: what is stored and looked up (the key itself never is)."""
    key = (key or "").strip()
    if not key:
        return None
    return "idem:" + hashlib.sha256(f"{subject or ''}\x1f{key}".encode()).hexdigest()


def _mobile_token(mobile: str | None) -> str | None:
    from app.agents.dedupe import identity

    norm = identity.mobile(mobile)
    return identity.token("MOBILE", norm) if norm else None


def keys_for(*, applicant_id: str | None, case_id: str | None, mobile: str | None, product: str | None,
             idem_ref: str | None) -> list[str]:
    keys = []
    if applicant_id:
        keys.append(f"create:applicant:{applicant_id.strip()}")
    if case_id:
        keys.append(f"create:case:{case_id.strip()}")
    token = _mobile_token(mobile)
    if token:
        keys.append(f"create:mobile:{token}:{(product or '').strip().upper()}")
    if idem_ref:
        keys.append(f"create:{idem_ref}")
    return keys


@asynccontextmanager
async def serialised(keys: list[str]):
    """Hold a Postgres advisory lock per key (sorted: two callers never deadlock) for the body. Yields whether locks
    were taken. CreateBusy when a lock is not granted within lock_timeout_s()."""
    from app.store import get_repository

    repository = get_repository()
    pool_get = getattr(repository, "_pool_get", None)
    if pool_get is None or not keys:
        if keys:
            logger.warning("create_guard: store has no connection pool; case creation is not serialised")
        yield False
        return
    pool = pool_get()
    conn = await asyncio.to_thread(pool.getconn)
    taken: list[str] = []
    try:
        await asyncio.to_thread(conn.execute, f"SET lock_timeout = '{int(lock_timeout_s() * 1000)}ms'")
        for key in sorted(set(keys)):
            try:
                await asyncio.to_thread(conn.execute, "SELECT pg_advisory_lock(hashtext(%s))", (key,))
            except Exception as exc:  # noqa: BLE001 - lock_timeout (LockNotAvailable) or a lost connection
                raise CreateBusy(str(type(exc).__name__)) from None
            taken.append(key)
        yield True
    finally:
        for key in reversed(taken):
            try:
                await asyncio.to_thread(conn.execute, "SELECT pg_advisory_unlock(hashtext(%s))", (key,))
            except Exception:  # noqa: BLE001 - a session lock dies with its connection anyway
                pass
        try:
            await asyncio.to_thread(conn.execute, "RESET lock_timeout")
        except Exception:  # noqa: BLE001
            pass
        await asyncio.to_thread(pool.putconn, conn)


class KeyReused(ValueError):
    """The idempotency key was used before for a DIFFERENT request body: never replayed, never a new case."""


_FINGERPRINT = "payload:"


def fingerprint(body: dict[str, Any]) -> str:
    """SHA-256 of the request body, canonical (sorted keys, no nulls): what a replay must match."""
    import json

    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in sorted(value.items()) if v is not None}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    return hashlib.sha256(json.dumps(clean(body), sort_keys=True, default=str).encode()).hexdigest()


def find_replay(idem_ref: str | None, body_fingerprint: str | None = None) -> tuple[str, str] | None:
    """(case_id, applicant_id) already created for this caller + key, or None. KeyReused when the key was used for a
    different body (a silent replay would hand back the wrong case). A record without a fingerprint (none exist in
    production; kept for compatibility) replays."""
    if not idem_ref:
        return None
    from app.store import get_repository

    repository = get_repository()
    row = repository._one("SELECT case_id, summary FROM case_events WHERE event_type = ? AND ref_id = ? "
                          "ORDER BY sequence LIMIT 1", (IDEMPOTENCY_EVENT, idem_ref))
    if not row:
        return None
    stored = str(row["summary"] or "")
    if body_fingerprint and stored.startswith(_FINGERPRINT) and stored[len(_FINGERPRINT):] != body_fingerprint:
        raise KeyReused(idem_ref)
    application = repository.get_application(row["case_id"])
    return (application.case_id, application.applicant_id) if application else None


def remember(case_id: str, idem_ref: str | None, body_fingerprint: str | None = None) -> None:
    if not idem_ref:
        return
    from app.store import get_repository
    from app.store.models import CaseEvent

    get_repository().record_event(CaseEvent(event_id=uuid.uuid4().hex, case_id=case_id,
                                            event_type=IDEMPOTENCY_EVENT, ref_id=idem_ref,
                                            summary=_FINGERPRINT + (body_fingerprint or "")))


def possible_duplicates(*, mobile: str | None, product: str | None, exclude_case: str) -> list[str]:
    """Other cases of the same product whose applicant declared the same mobile (normalised). Read under the create
    lock, so a simultaneous create is always seen by the second request."""
    from app.agents.dedupe import identity
    from app.store import get_repository

    norm = identity.mobile(mobile)
    if not norm or not product:
        return []
    repository = get_repository()
    rows = repository._all("SELECT a.case_id, p.mobile FROM applications a JOIN applicants p "
                           "ON p.applicant_id = a.applicant_id WHERE a.product = ? AND a.case_id <> ? "
                           "AND p.mobile LIKE ?", (product, exclude_case, f"%{norm[-4:]}"))
    return [r["case_id"] for r in rows if identity.mobile(r["mobile"]) == norm]


def note_possible_duplicate(case_id: str, others: list[str]) -> None:
    if not others:
        return
    from app.store import get_repository
    from app.store.models import CaseEvent

    get_repository().record_event(CaseEvent(
        event_id=uuid.uuid4().hex, case_id=case_id, event_type=POSSIBLE_DUPLICATE_EVENT,
        summary=f"{len(others)} other case(s) of the same product share the declared mobile (not blocked: the "
                "duplicate window is not approved)"))


__all__ = ["CreateBusy", "IDEMPOTENCY_EVENT", "KeyReused", "POSSIBLE_DUPLICATE_EVENT", "find_replay", "fingerprint",
           "idempotency_ref",
           "keys_for", "lock_timeout_s", "note_possible_duplicate", "possible_duplicates", "remember", "serialised"]
