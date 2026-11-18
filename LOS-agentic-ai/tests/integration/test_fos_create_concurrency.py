"""
Concurrent case creation through POST /api/v1/fos/applicants (Phase A hardening, 2026-10-22).

DETERMINISTIC: application_create is slowed by HOLD seconds, so two unserialised creates ALWAYS overlap; each pair of
requests starts behind a barrier on its own TestClient (its own event loop). With serialisation the second create
starts only after the first has written everything -- the assertions hold whichever request wins the race.
Synthetic data only.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import create_guard
from app.store import set_repository
from app.store.testing import fresh_repository

FOS_SCOPES = ["read_applicant", "read_application", "read_documents", "read_verification", "read_pending_items",
              "read_next_action", "create_applicant", "update_applicant", "create_application", "upload_document"]
HOLD = 0.6


@pytest.fixture
def repo(monkeypatch):
    from app.agents.applicant import config as agent_config

    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    agent_config.reload()
    r = fresh_repository()
    r.initialise()
    set_repository(r)
    yield r
    set_repository(None)
    agent_config.reload()


@pytest.fixture
def windows(monkeypatch):
    """Slow application_create down and record when each call ran."""
    from app.mcp import applicant as tools

    original = tools.application_create
    spans: list[tuple[float, float]] = []

    async def slow(*args, **kwargs):
        start = time.perf_counter()
        await asyncio.sleep(HOLD)
        try:
            return await original(*args, **kwargs)
        finally:
            spans.append((start, time.perf_counter()))

    monkeypatch.setattr(tools, "application_create", slow)
    return spans


def _body(n: int, *, mobile: str = "9000012345", case_id: str | None = None) -> dict:
    body = {"applicant": {"full_name": f"UAT PERSON {n}", "mobile": mobile, "date_of_birth": "1990-01-01",
                          "address": "UAT Street, Pune 411001"},
            "application": {"product": "PERSONAL_LOAN", "loan_amount": 300000}}
    if case_id:
        body["case_id"] = case_id
    return body


def _together(make_token, bodies, headers=None, subjects=None):
    """POST each body at the same instant, each from its own client; returns the responses in order."""
    from main import app

    barrier = threading.Barrier(len(bodies))
    results: list = [None] * len(bodies)

    def run(i):
        client = TestClient(app)
        token = make_token(subject=(subjects or ["uat-fos"] * len(bodies))[i], scopes=FOS_SCOPES)
        h = {"Authorization": f"Bearer {token}", **((headers or [{}] * len(bodies))[i])}
        barrier.wait()
        results[i] = client.post("/api/v1/fos/applicants", json=bodies[i], headers=h)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(bodies))]
    [t.start() for t in threads]
    [t.join(120) for t in threads]
    return results


def _count(repo, sql, args=()):
    return repo._one(sql, args)["n"]


def test_a_double_submit_with_one_idempotency_key_opens_exactly_one_case(repo, make_token, windows):
    key = {"Idempotency-Key": "uat-submit-0001"}
    a, b = _together(make_token, [_body(1), _body(1)], headers=[key, key])
    assert (a.status_code, b.status_code) == (201, 201)
    assert a.json()["case_id"] == b.json()["case_id"]
    assert sorted(r.headers.get("Idempotent-Replayed", "") for r in (a, b)) == ["", "true"]
    assert _count(repo, "SELECT COUNT(*) AS n FROM applications") == 1
    assert _count(repo, "SELECT COUNT(*) AS n FROM applicants") == 1
    assert len(windows) == 1                                   # the replay never reached the create tool


def test_the_same_key_from_two_different_callers_is_two_cases(repo, make_token, windows):
    key = {"Idempotency-Key": "uat-shared-key"}
    a, b = _together(make_token, [_body(1), _body(2, mobile="9000099999")], headers=[key, key],
                     subjects=["uat-fos-a", "uat-fos-b"])
    assert (a.status_code, b.status_code) == (201, 201) and a.json()["case_id"] != b.json()["case_id"]


def test_a_supplied_case_id_is_never_overwritten_by_a_simultaneous_create(repo, make_token, windows):
    a, b = _together(make_token, [_body(1, case_id="CASE-UATRACE0001"),
                                  _body(2, mobile="9000077777", case_id="CASE-UATRACE0001")])
    codes = sorted(r.status_code for r in (a, b))
    assert codes == [201, 400]
    winner = a if a.status_code == 201 else b
    loser = b if winner is a else a
    assert loser.json()["detail"]["error"] == "INVALID_INPUT"
    stored = repo.get_application("CASE-UATRACE0001")
    assert stored.applicant_id == winner.json()["applicant_id"]           # the winner's case, untouched
    assert _count(repo, "SELECT COUNT(*) AS n FROM applicants") == 1         # no orphan applicant from the loser


def test_same_mobile_same_product_is_serialised_and_noted_not_blocked(repo, make_token, windows):
    a, b = _together(make_token, [_body(1), _body(2)])
    assert (a.status_code, b.status_code) == (201, 201)                     # not blocked: the window is not approved
    (s1, e1), (s2, e2) = sorted(windows)
    assert s2 >= e1 - 0.01, "the two creates overlapped"                     # serialised
    noted = [e for r in (a, b) for e in repo.get_case_timeline(r.json()["case_id"])
             if e.event_type == create_guard.POSSIBLE_DUPLICATE_EVENT]
    assert len(noted) == 1                                                   # the second create saw the first


def test_a_create_waiting_too_long_is_refused_busy_never_hangs(repo, make_token, monkeypatch):
    from main import app

    monkeypatch.setenv("FOS_CREATE_LOCK_TIMEOUT_S", "0.3")
    body = _body(1)
    keys = create_guard.keys_for(applicant_id=None, case_id=None, mobile=body["applicant"]["mobile"],
                                 product="PERSONAL_LOAN", idem_ref=None)
    held, release = threading.Event(), threading.Event()

    def holder():
        async def hold():
            async with create_guard.serialised(keys):
                held.set()
                while not release.is_set():
                    await asyncio.sleep(0.05)
        asyncio.run(hold())

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(10)
    try:
        client = TestClient(app)
        r = client.post("/api/v1/fos/applicants", json=body,
                        headers={"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES)}"})
    finally:
        release.set()
        t.join(10)
    assert r.status_code == 503 and r.json()["detail"]["error"] == "CREATE_BUSY"
    assert _count(repo, "SELECT COUNT(*) AS n FROM applications") == 0


def test_a_sequential_retry_with_the_key_replays_and_without_it_creates(repo, make_token):
    from main import app

    client = TestClient(app)
    h = {"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES)}", "Idempotency-Key": "uat-retry-1"}
    first = client.post("/api/v1/fos/applicants", json=_body(1), headers=h)
    again = client.post("/api/v1/fos/applicants", json=_body(1), headers=h)
    assert first.json()["case_id"] == again.json()["case_id"] and again.headers["Idempotent-Replayed"] == "true"
    plain = client.post("/api/v1/fos/applicants", json=_body(1),
                        headers={"Authorization": h["Authorization"]})
    assert plain.status_code == 201 and plain.json()["case_id"] != first.json()["case_id"]
    stored_keys = repo._all("SELECT ref_id FROM case_events WHERE event_type = ?", (create_guard.IDEMPOTENCY_EVENT,))
    assert stored_keys and all("uat-retry-1" not in (r["ref_id"] or "") for r in stored_keys)   # only a hash stored


# ---- the key's payload is checked (2026-10-22) ------------------------------------------------------------------
def test_the_same_key_with_a_different_body_is_refused_never_a_wrong_replay(repo, make_token):
    from main import app

    client = TestClient(app)
    h = {"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES)}", "Idempotency-Key": "uat-reuse-1"}
    first = client.post("/api/v1/fos/applicants", json=_body(1), headers=h)
    other = client.post("/api/v1/fos/applicants", json=_body(2, mobile="9000055555"), headers=h)
    assert first.status_code == 201
    assert other.status_code == 409 and other.json()["detail"]["error"] == "IDEMPOTENCY_KEY_REUSED"
    assert _count(repo, "SELECT COUNT(*) AS n FROM applications") == 1          # no second case, no wrong case


def test_the_same_body_in_another_field_order_is_the_same_request(repo, make_token):
    from main import app

    client = TestClient(app)
    h = {"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES)}", "Idempotency-Key": "uat-order-1"}
    body = _body(1)
    reordered = {"application": dict(reversed(list(body["application"].items()))),
                 "applicant": dict(reversed(list(body["applicant"].items())))}
    first = client.post("/api/v1/fos/applicants", json=body, headers=h)
    again = client.post("/api/v1/fos/applicants", json=reordered, headers=h)
    assert again.status_code == 201 and again.headers.get("Idempotent-Replayed") == "true"
    assert again.json()["case_id"] == first.json()["case_id"]


async def test_the_chat_drafts_key_reaches_the_guard_and_a_new_draft_is_a_new_case(repo, make_token):
    from app.api.routes import fos_api

    claims = {"sub": "uat-chat", "scope": " ".join(FOS_SCOPES)}
    request = fos_api.CreateCaseRequest.model_validate(_body(1))
    one = await fos_api.create_case(request, claims, channel="chat", idempotency_key="chat-draft:abc")
    again = await fos_api.create_case(request, claims, channel="chat", idempotency_key="chat-draft:abc")
    fresh = await fos_api.create_case(request, claims, channel="chat", idempotency_key="chat-draft:def")
    assert one["case_id"] == again["case_id"] != fresh["case_id"]
