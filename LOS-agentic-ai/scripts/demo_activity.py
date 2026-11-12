"""
THE DEMO ACTIVITY (config app/config/agent_databases_demo.yaml): after each agent's demo rows, the agent's OWN code
paths are run on them so every table holds realistic demo data -- eligibility checks and saved entries, processed and
finalized bank cases, crime verifications with their PDF reports, Document Desk evaluations, chatbot conversations.
Everything is a demo row (is_demo, directly or through its demo case). Used by scripts/setup_agent_databases.py
--seed-demo; each function takes the agent's DSN and returns a one-line summary.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "app" / "config" / "agent_databases_demo.yaml"


@lru_cache(maxsize=1)
def config() -> dict[str, Any]:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}


def section(agent: str) -> dict[str, Any]:
    return dict(config().get(agent) or {})


def actor() -> str:
    return str(config().get("actor") or "demo-seed")


@contextmanager
def env(**values: str) -> Iterator[None]:
    """Variables set for a block, then put back (a caller's environment is never changed for good)."""
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ------------------------------------------------------------------------------------------------ eligibility
def eligibility(dsn: str) -> str:
    s = section("eligibility")
    if not s.get("enabled", True):
        return "off"
    import psycopg

    from app.agents.eligibility.agent import assess_payload
    from app.agents.eligibility.data import inputs_store, runs

    with env(ELIGIBILITY_DSN=dsn, ELIGIBILITY_DATA_PROVIDER="postgres"):
        with psycopg.connect(dsn) as conn:
            cases = [r[0] for r in conn.execute("SELECT case_id FROM eligibility_cases WHERE is_demo ORDER BY case_id")]
        checked = saved = 0
        for entry in s.get("saved_entries") or []:
            inputs_store.save(str(entry["case_id"]), dict(entry.get("entries") or {}), saved_by=actor())
            saved += 1
        if s.get("check_demo_cases", True):
            for case_id in cases:
                overrides = inputs_store.overrides_for(case_id)
                out = assess_payload({"case_id": case_id, "overrides": overrides})
                if out.get("eligibility"):
                    runs.save(out["eligibility"], requested_by=actor())
                    checked += 1
    return f"{checked} eligibility checks, {saved} saved entries"


# ------------------------------------------------------------------------------------------------ bank
def bank(dsn: str) -> str:
    s = section("bank_verification")
    from app.agents.credit.bank_verification.demo_data import reports, scenarios
    from app.agents.credit.bank_verification.demo_data import seed as bank_seed
    from app.agents.credit.bank_verification.repository import Repository
    from app.agents.credit.bank_verification.services import decision_service

    on = s.get("enabled", True)
    with env(BANK_VERIFICATION_DSN=dsn, BANK_VERIFICATION_AI_SUMMARY="off"):
        repo = Repository()
        done = bank_seed.reset(repo, start_state=not (on and s.get("process_every_case", True)))
        finalized = 0
        if on:
            for code in s.get("finalize_scenarios") or []:
                spec = next((x for x in scenarios.SCENARIOS if x["code"] == code), None)
                if spec is None:
                    raise ValueError(f"unknown bank demo scenario {code!r} in {CONFIG.name}")
                payload = {k: v for k, v in dict(spec["agency"]).items() if k != "with_report"}
                case_id = spec["case"]["case_id"]
                report = reports.for_case(case_id, ownership=payload.get("ownership", "CONFIRMED"),
                                          agency_id=payload.get("agency_id"))
                decision_service.finalize(repo, case_id, payload, report=(report, "agency_report.pdf"),
                                          actor=f"agency:{actor()}")
                finalized += 1
    summaries = _bank_summaries(dsn, repo) if on and s.get("ai_summaries", True) else None
    return (f"{done.get('cases', 0)} demo cases, {done.get('historical', 0)} historical, "
            f"{done.get('processed', 0)} processed, {finalized} finalized"
            + ("" if summaries is None else f", {summaries} AI summaries"
               + ("" if summaries else " (the model did not answer -- the screen writes them when asked)")))


def _bank_summaries(dsn: str, repo: Any) -> int:
    """The AI summary of every processed demo case, through the agent's own precompute worker (the local model). A
    summary is never invented: the model down -> none stored, the screen shows its rules-written text as always."""
    from app.agents.credit.bank_verification.services import summary_service

    with env(BANK_VERIFICATION_DSN=dsn, BANK_VERIFICATION_AI_SUMMARY="on"):
        for case in repo.cases(include_historical=False):
            if repo.latest_run(case["case_id"]):
                summary_service.enqueue(repo, case["case_id"])
        summary_service._QUEUE.join()
        import psycopg

        with psycopg.connect(dsn) as conn:
            return conn.execute("SELECT count(*) FROM summaries WHERE source = 'ai'").fetchone()[0]


# ------------------------------------------------------------------------------------------------ crime
def crime(dsn: str) -> str:
    s = section("crime_verification")
    from app.agents.credit.crime_verification import db as crime_db
    from app.agents.credit.crime_verification.demo_data import seed as crime_seed

    db = crime_db.Database(dsn, min_size=0, max_size=4)
    crime_db.use(db)
    try:
        # the AI summary through the agent's own worker (config ai_summaries; the model down -> it records that)
        with env(CRIME_VERIFICATION_AI_SUMMARY="on" if s.get("ai_summaries", True) else "off"):
            counts = crime_seed.reset(db)
            if not s.get("enabled", True):
                return f"{counts.get('crime_verification_applications', 0)} demo applications"
            verified, reports_made, skipped = _crime_verify(s)
    finally:
        crime_db.use(None)
        db.close()
    return (f"{counts.get('crime_verification_applications', 0)} demo applications, {verified} verified, "
            f"{reports_made} reports" + (f", {skipped} skipped (incomplete / waived)" if skipped else ""))


def _crime_verify(s: dict[str, Any]) -> tuple[int, int, int]:
    from app.agents.credit.crime_verification.domain.models import InitiateRequest, SubjectInput
    from app.agents.credit.crime_verification.services import queries, report_service, verification_service
    from app.agents.credit.crime_verification.services.access import Caller
    from app.agents.credit.crime_verification.services.errors import ServiceError

    who = Caller(user=actor(), permissions=frozenset({"view", "initiate", "retrigger", "refresh", "finalize",
                                                      "download_report", "manage_applications"}),
                 branches=frozenset({"*"}))
    wanted = [str(a) for a in s.get("verify_applications") or ["*"]]
    apps = [a["application_id"] for a in queries.list_applications(who, "")] if "*" in wanted else wanted
    from app.agents.credit.crime_verification.services import summary_service

    verified = made = skipped = 0
    cases: list[dict[str, Any]] = []
    for app_id in apps:
        view = queries.application_view(who, app_id)
        people = [p for p in view["subjects"] if not p["missing"] and not p["errors"]]
        if not people or view.get("latest_case"):
            skipped += 1
            continue
        req = InitiateRequest(application_id=app_id, confirmation_accepted=True, subjects=[
            SubjectInput(role=p["role"], seq=p["seq"], full_name=p["full_name"], father_name=p["father_name"],
                         date_of_birth=p["date_of_birth"], permanent_address=p["permanent_address"]) for p in people])
        try:
            case, _ = verification_service.initiate(who, req)
        except ServiceError:
            skipped += 1
            continue
        verified += 1
        cases.append(case)
    summary_service._QUEUE.join()                         # the summaries first: each report carries its case's
    if s.get("download_reports", True):
        for case in cases:
            for subject in case["subjects"]:
                if subject.get("result"):
                    report_service.download(who, subject["subject_id"])
                    made += 1
    return verified, made, skipped


# ------------------------------------------------------------------------------------------------ docs policy
def docs(dsn: str) -> str:
    s = section("docs_policy")
    if not s.get("enabled", True):
        return "off"
    from app.agent_db import core
    from app.agents.docs_verify_credit_agent.services import readiness_service, store_service
    from app.store import get_repository

    repo = get_repository()
    stored = missing = failed = 0
    with env(DOCS_POLICY_DSN=dsn):
        core.forget("docs_policy")
        for case_id in s.get("cases") or []:
            if repo.get_application(case_id) is None:
                missing += 1
                continue
            try:
                payload, _ = readiness_service._compute(case_id, repo, None)   # evaluated; nothing written to LOS
            except Exception:  # noqa: BLE001 - a case the desk cannot evaluate is counted, not fatal
                failed += 1
                continue
            store_service.save(case_id, {**payload, "audit": {**payload["audit"], "recorded": True}})
            stored += 1
        core.forget("docs_policy")
    out = f"{stored} evaluations"
    if missing:
        out += f", {missing} demo case(s) not in the LOS case store (start the LOS server once with its demo seed)"
    if failed:
        out += f", {failed} could not be evaluated"
    return out


# ------------------------------------------------------------------------------------------------ chatbot
def chatbot(dsn: str) -> str:
    s = section("chatbot")
    if not s.get("enabled", True):
        return "off"
    import hashlib

    import psycopg

    from app.agents.applicant import chat_store
    from app.agents.applicant.copilot.semantics import entities, meaning

    def h(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()[:16]

    now = datetime.now(timezone.utc)
    turns = list(s.get("turns") or [])
    counts = {"turns": 0, "audit": 0, "feedback": 0, "labelled": 0}
    with psycopg.connect(dsn) as conn:
        from app.agent_db import apply_migrations

        apply_migrations(conn, chat_store.AGENT, chat_store.MIGRATIONS)
        with conn.transaction():
            for n, t in enumerate(turns):
                at = now - timedelta(minutes=float(t.get("minutes_ago", 0)))
                rid = f"demo-{h(str(t.get('chat')) + str(n))}"
                q = meaning._masked(str(t["q"]))
                row = chat_store._turn_row({"ts": at.timestamp(), "rid": rid, "by": h(actor()),
                                            "chat": h(str(t.get("chat") or "")), "q": q,
                                            "frame": entities.extract(str(t["q"])).as_dict(), "layer": t.get("layer"),
                                            "intent": t.get("intent"), "label": t.get("intent"),
                                            "outcome": t.get("outcome", "answered"), "ms": t.get("ms")})
                conn.execute(f"INSERT INTO chat_turns_log ({', '.join(chat_store._TURN_COLUMNS)}, is_demo) VALUES "
                             f"(%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s::jsonb, true)", row)
                counts["turns"] += 1
                audit = chat_store._audit_row({"ts": at.isoformat(), "request_id": rid, "subject": actor(),
                                               "case_id": t.get("case_id"), "intent": t.get("intent"),
                                               "tools": [], "status": "OK", "channel": "web",
                                               "message_excerpt": q[:120]})
                conn.execute(f"INSERT INTO chat_audit ({', '.join(chat_store._AUDIT_COLUMNS[:-1])}, is_demo) VALUES "
                             f"({', '.join(['%s'] * 6)}, %s::jsonb, {', '.join(['%s'] * 10)}, true)", audit[:-1])
                counts["audit"] += 1
                if t.get("rating"):
                    fb = chat_store._feedback_row({"ts": (at + timedelta(seconds=20)).timestamp(), "by": h(actor()),
                                                   "rid": rid, "rating": t["rating"], "reason": t.get("reason")})
                    conn.execute(f"INSERT INTO chat_feedback ({', '.join(chat_store._FEEDBACK_COLUMNS)}, is_demo) "
                                 "VALUES (%s, %s, %s, %s, %s, true)", fb)
                    counts["feedback"] += 1
                if t.get("resolves"):
                    lb = chat_store._labelled_row({"ts": at.timestamp(), "q": meaning._masked(str(t["resolves"])),
                                                   "intent": t.get("intent"), "source": "resolved_clarification"})
                    conn.execute(f"INSERT INTO chat_labelled_turns ({', '.join(chat_store._LABELLED_COLUMNS)}, "
                                 "is_demo) VALUES (%s, %s, %s, %s, true)", lb)
                    counts["labelled"] += 1
    return ", ".join(f"{v} {k}" for k, v in counts.items())


__all__ = ["bank", "chatbot", "config", "crime", "docs", "eligibility"]
