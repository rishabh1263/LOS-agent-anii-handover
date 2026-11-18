"""
THE AUDIT DATABASE (LOS_AUDIT_DSN; config app/config/agent_databases.yaml `audit`): the COMMON audit schema of every
agent -- agent runs, issuer checks, field corrections, risk decisions -- one clean table per source.

DEDUPE HAS ITS OWN SCHEMA (owner 2026-11-03): its two sources (dedupe_decisions, customer_history) are configured under
`databases.dedupe.sources` and land in schema `dedupe` (app/agent_db/dedupe_store.py). This module still ROUTES every
audit file line: each source goes to the schema of the agent whose `sources` list it. The audit schema's own
audit_dedupe_decisions / audit_customer_history tables stay (migrations are never edited) with the rows written
before the move; scripts/move_dedupe_to_own_schema.py copies them over.

    audit_dedupe_decisions      (until 2026-11-03; now schema dedupe) dedupe decisions
    audit_customer_history      (until 2026-11-03; now schema dedupe) customer history checks
    audit_risk_decisions        fraud / risk: score, category, outcome, flags and data gaps (already scrubbed)
    audit_agent_runs            every agent run (harness): agent, status, error, violations, tools, model calls, time
    audit_issuer_verifications  issuer checks: provider, status, verified / mismatched FIELD NAMES, verdict, latency
    audit_field_corrections     who corrected which field of which document, and why -- never the values
    rule_sets                   rule sets recorded before 2026-11-03 (the dedupe policy's now live in schema dedupe)
Every audit table is append-only (only a demo row may be deleted: the demo purge).
    agent_logs                  WARNING+ logs of those agents (shared)

HOW ROWS ARRIVE. Each agent keeps writing its JSONL audit file exactly as before (app/observability/audit_log.py
append_line); append_line hands the same line to `from_file_line`, which knows the file by its configured path and
queues the row for the background writer (app/agent_db/writer.py) -- a request never waits for it. Nothing configured
(no LOS_AUDIT_DSN): nothing happens here. A row about a DEMO- case / application is is_demo.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import threading

from app.agent_db import LOGS_MIGRATION, Migration, common_audit, core, demo_case, responses, rule_sets, rule_store
from app.agent_db.writer import writer

AGENT = "audit"

_V1 = """
CREATE TABLE audit_dedupe_decisions (
    audit_id        BIGSERIAL PRIMARY KEY,
    occurred_at     TIMESTAMPTZ NOT NULL,
    request_id      TEXT,
    case_id         TEXT,
    policy_version  TEXT,
    status          TEXT,
    recommendation  TEXT,
    rules           JSONB NOT NULL DEFAULT '{}'::jsonb,
    coverage_mode   TEXT,
    cases_scanned   INTEGER,
    partial         BOOLEAN,
    is_demo         BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX audit_dedupe_case ON audit_dedupe_decisions (case_id, occurred_at);

CREATE TABLE audit_customer_history (
    audit_id            BIGSERIAL PRIMARY KEY,
    occurred_at         TIMESTAMPTZ NOT NULL,
    request_id          TEXT,
    case_id             TEXT,
    parameter           TEXT,
    party_role          TEXT,
    match               TEXT,
    history_status      TEXT,
    outcome             TEXT,
    reason_codes        JSONB NOT NULL DEFAULT '[]'::jsonb,
    policy_version      TEXT,
    explanation_source  TEXT,
    is_demo             BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX audit_history_case ON audit_customer_history (case_id, occurred_at);

CREATE TABLE audit_risk_decisions (
    audit_id           BIGSERIAL PRIMARY KEY,
    occurred_at        TIMESTAMPTZ NOT NULL,
    request_id         TEXT,
    application_id     TEXT,
    policy_version     TEXT,
    policy_signed_off  BOOLEAN,
    risk_score         INTEGER,
    risk_category      TEXT,
    final_outcome      TEXT,
    flags              JSONB NOT NULL DEFAULT '[]'::jsonb,
    data_gaps          JSONB NOT NULL DEFAULT '[]'::jsonb,
    summary_source     TEXT,
    duration_ms        NUMERIC(12, 2),
    is_demo            BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX audit_risk_application ON audit_risk_decisions (application_id, occurred_at);

CREATE TABLE audit_agent_runs (
    audit_id        BIGSERIAL PRIMARY KEY,
    occurred_at     TIMESTAMPTZ NOT NULL,
    run_id          TEXT,
    agent_id        TEXT,
    agent_version   TEXT,
    request_id      TEXT,
    correlation_id  TEXT,
    status          TEXT,
    error           JSONB,
    violations      JSONB NOT NULL DEFAULT '[]'::jsonb,
    started_at      TEXT,
    duration_ms     NUMERIC(12, 2),
    usage           JSONB,
    tools           JSONB NOT NULL DEFAULT '[]'::jsonb,
    model_calls     JSONB NOT NULL DEFAULT '[]'::jsonb,
    replay_digest   TEXT,
    is_demo         BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX audit_runs_agent ON audit_agent_runs (agent_id, occurred_at);
CREATE INDEX audit_runs_request ON audit_agent_runs (request_id);

CREATE TABLE audit_issuer_verifications (
    audit_id           BIGSERIAL PRIMARY KEY,
    occurred_at        TIMESTAMPTZ NOT NULL,
    event_id           TEXT UNIQUE,
    request_id         TEXT,
    case_id            TEXT,
    applicant_id       TEXT,
    document_id        TEXT,
    document_type      TEXT,
    attempted          BOOLEAN,
    provider           TEXT,
    mode               TEXT,
    provider_status    TEXT,
    reference_id       TEXT,
    consent_id         TEXT,
    verified_at        TEXT,
    verified_fields    JSONB NOT NULL DEFAULT '[]'::jsonb,
    mismatched_fields  JSONB NOT NULL DEFAULT '[]'::jsonb,
    reason_codes       JSONB NOT NULL DEFAULT '[]'::jsonb,
    latency_ms         NUMERIC(12, 2),
    document_verdict   TEXT,
    source             TEXT,
    is_demo            BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX audit_issuer_case ON audit_issuer_verifications (case_id, occurred_at);

CREATE TABLE audit_field_corrections (
    audit_id       BIGSERIAL PRIMARY KEY,
    occurred_at    TIMESTAMPTZ NOT NULL,
    request_id     TEXT,
    case_id        TEXT,
    document_id    TEXT,
    document_type  TEXT,
    source_id      TEXT,
    field          TEXT NOT NULL,
    reason         TEXT,
    corrected_by   TEXT,
    is_demo        BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX audit_corrections_case ON audit_field_corrections (case_id, occurred_at);
"""

# every audit table of THIS schema's migrations (kept as released: audit-0002's guard is built from it)
TABLES = ("audit_dedupe_decisions", "audit_customer_history", "audit_risk_decisions", "audit_agent_runs",
          "audit_issuer_verifications", "audit_field_corrections")

MIGRATIONS: list[Migration] = [
    ("audit-0001", "the audit trails: dedupe, customer history, risk, agent runs, issuer checks, field corrections", _V1),
    LOGS_MIGRATION,
    rule_sets.migration("audit-0002", "rule_sets; rule_set_hash on dedupe decisions; every audit table append-only",
                        decision_tables=["audit_dedupe_decisions"], audit_tables=list(TABLES)),
    rule_store.MIGRATION,                    # (the dedupe policy's versions until 2026-11-03; now schema dedupe)
    responses.MIGRATION,                     # (the dedupe API's responses until 2026-11-03; now schema dedupe)
    common_audit.MIGRATION,                  # audit_events: every agent's audit rows, one common table (2026-11-03)
]
# the tables this schema is written to now (the dedupe ones moved to schema dedupe)
OWN_TABLES = tuple(t for t in TABLES if t not in ("audit_dedupe_decisions", "audit_customer_history"))

# the rule sets this process registered (hash -> what to store), recorded by the writer with the rows that use them
_RULE_SETS: dict[str, dict[str, Any]] = {}
_RULE_SETS_LOCK = threading.Lock()


def register_rule_set(name: str, content: Any, *, version_label: str | None = None, status: str | None = None,
                      source_file: str | Path | None = None, is_demo: bool = False) -> str:
    """A rule set a decision is about to be made with: its hash (put it on the audit line as rule_set_hash). It is
    stored in rule_sets by the background writer, in the transaction that stores the decision."""
    digest = rule_sets.fingerprint(content)
    with _RULE_SETS_LOCK:
        _RULE_SETS.setdefault(digest, {"name": name, "content": content, "version_label": version_label,
                                       "status": status, "source_files": [source_file] if source_file else [],
                                       "is_demo": is_demo})
    return digest


def _with_rule_sets(index: int):
    """prepare() for a table whose rows carry rule_set_hash at `index`: each hash recorded first (same transaction);
    a hash this process never registered becomes NULL (a failing foreign key would drop the whole batch)."""
    def prepare(cur: Any, rows: list[tuple]) -> list[tuple]:
        out, done = [], set()
        for r in rows:
            digest = r[index]
            with _RULE_SETS_LOCK:
                spec = _RULE_SETS.get(digest) if digest else None
            if digest and spec is None:
                r = r[:index] + (None,) + r[index + 1:]
            elif spec is not None and digest not in done:
                rule_sets.record(cur, spec["name"], spec["content"], version_label=spec["version_label"],
                                 status=spec["status"], source_files=spec["source_files"], is_demo=spec["is_demo"])
                done.add(digest)
            out.append(r)
        return out
    return prepare
PURGE_DEMO_SQL = [f"DELETE FROM {t} WHERE is_demo" for t in TABLES] + ["DELETE FROM audit_events WHERE is_demo"]


def _when(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _j(value: Any, empty: Any) -> str:
    return json.dumps(value if value is not None else empty, default=str)


def _demo(*ids: Any) -> bool:
    return any(demo_case(AGENT, i) for i in ids if i)


# one row builder per source: (table, columns, casts, row from the JSON line)
def _dedupe(d: dict[str, Any]) -> tuple:
    cov = d.get("coverage") or {}
    return (_when(d.get("at")), d.get("request_id"), d.get("case_id"), d.get("policy_version"), d.get("status"),
            d.get("recommendation"), _j(d.get("rules"), {}), cov.get("mode"), cov.get("cases_scanned"),
            cov.get("partial"), _demo(d.get("case_id")), d.get("rule_set_hash"))


def _history(d: dict[str, Any]) -> tuple:
    return (_when(d.get("at")), d.get("request_id"), d.get("case_id"), d.get("parameter"), d.get("party_role"),
            None if d.get("match") is None else str(d.get("match")), d.get("history_status"), d.get("outcome"),
            _j(d.get("reason_codes"), []), d.get("policy_version"), d.get("explanation_source"),
            bool(d.get("is_demo")) or _demo(d.get("case_id")))


def _risk(d: dict[str, Any]) -> tuple:
    return (_when(d.get("timestamp")), d.get("request_id"), d.get("application_id"), d.get("policy_version"),
            d.get("policy_signed_off"), d.get("risk_score"), d.get("risk_category"), d.get("final_outcome"),
            _j(d.get("flags"), []), _j(d.get("data_gaps"), []), d.get("summary_source"), d.get("duration_ms"),
            _demo(d.get("application_id")))


def _run(d: dict[str, Any]) -> tuple:
    return (_when(d.get("timestamp")), d.get("run_id"), d.get("agent_id"), d.get("agent_version"), d.get("request_id"),
            d.get("correlation_id"), d.get("status"), _j(d.get("error"), None) if d.get("error") else None,
            _j(d.get("violations"), []), None if d.get("started_at") is None else str(d.get("started_at")),
            d.get("duration_ms"), _j(d.get("usage"), None) if d.get("usage") is not None else None,
            _j(d.get("tools"), []), _j(d.get("model_calls"), []), d.get("replay_digest"), False)


def _issuer(d: dict[str, Any]) -> tuple:
    return (_when(d.get("timestamp")), d.get("event_id"), d.get("request_id"), d.get("case_id"), d.get("applicant_id"),
            d.get("document_id"), d.get("document_type"), d.get("attempted"), d.get("provider"), d.get("mode"),
            d.get("provider_status"), d.get("reference_id"), d.get("consent_id"),
            None if d.get("verified_at") is None else str(d.get("verified_at")), _j(d.get("verified_fields"), []),
            _j(d.get("mismatched_fields"), []), _j(d.get("reason_codes"), []), d.get("latency_ms"),
            None if d.get("document_verdict") is None else str(d.get("document_verdict")), d.get("source"),
            _demo(d.get("case_id"), d.get("applicant_id")))


SOURCES: dict[str, tuple[str, tuple[str, ...], dict[str, str], Callable[[dict[str, Any]], tuple]]] = {
    "dedupe_decisions": ("audit_dedupe_decisions",
                         ("occurred_at", "request_id", "case_id", "policy_version", "status", "recommendation", "rules",
                          "coverage_mode", "cases_scanned", "partial", "is_demo", "rule_set_hash"),
                         {"rules": "jsonb"}, _dedupe),
    "customer_history": ("audit_customer_history",
                         ("occurred_at", "request_id", "case_id", "parameter", "party_role", "match", "history_status",
                          "outcome", "reason_codes", "policy_version", "explanation_source", "is_demo"),
                         {"reason_codes": "jsonb"}, _history),
    "risk_decisions": ("audit_risk_decisions",
                       ("occurred_at", "request_id", "application_id", "policy_version", "policy_signed_off",
                        "risk_score", "risk_category", "final_outcome", "flags", "data_gaps", "summary_source",
                        "duration_ms", "is_demo"), {"flags": "jsonb", "data_gaps": "jsonb"}, _risk),
    "agent_runs": ("audit_agent_runs",
                   ("occurred_at", "run_id", "agent_id", "agent_version", "request_id", "correlation_id", "status",
                    "error", "violations", "started_at", "duration_ms", "usage", "tools", "model_calls",
                    "replay_digest", "is_demo"),
                   {"error": "jsonb", "violations": "jsonb", "usage": "jsonb", "tools": "jsonb",
                    "model_calls": "jsonb"}, _run),
    "issuer_verifications": ("audit_issuer_verifications",
                             ("occurred_at", "event_id", "request_id", "case_id", "applicant_id", "document_id",
                              "document_type", "attempted", "provider", "mode", "provider_status", "reference_id",
                              "consent_id", "verified_at", "verified_fields", "mismatched_fields", "reason_codes",
                              "latency_ms", "document_verdict", "source", "is_demo"),
                             {"verified_fields": "jsonb", "mismatched_fields": "jsonb", "reason_codes": "jsonb"},
                             _issuer),
}
_CORRECTION_COLUMNS = ("occurred_at", "request_id", "case_id", "document_id", "document_type", "source_id", "field",
                       "reason", "corrected_by", "is_demo")


def _source_specs() -> dict[str, tuple[str, dict[str, Any]]]:
    """source -> (the agent whose schema keeps it, its file spec): every `sources` entry of every configured agent."""
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for agent, spec in (core.config().get("databases") or {}).items():
        for source, file_spec in ((spec or {}).get("sources") or {}).items():
            if source in SOURCES:
                out[source] = (str(agent), dict(file_spec))
    return out


def agent_of(source: str) -> str:
    """The agent schema a source's rows land in (dedupe's two sources: `dedupe`; the rest: `audit`)."""
    return _source_specs()[source][0]


def migrations_of(agent: str) -> list[Migration]:
    if agent == AGENT:
        return MIGRATIONS
    from app.agent_db import dedupe_store

    if agent == dedupe_store.AGENT:
        return dedupe_store.MIGRATIONS
    raise KeyError(f"no audit-file migrations for agent {agent!r}")


def source_path(source: str) -> Path:
    """The JSONL file a source writes (its env variable, else its default), absolute."""
    spec = _source_specs()[source][1]
    return Path(os.getenv(str(spec["env"])) or str(spec["default"])).resolve()


def sources_of(agent: str) -> list[str]:
    """The sources whose rows land in this agent's schema."""
    return [s for s, (a, _) in _source_specs().items() if a == agent]


def source_of(path: Path) -> str | None:
    target = Path(path).resolve()
    for source in _source_specs():
        if source_path(source) == target:
            return source
    return None


def row(source: str, line: str | dict[str, Any]) -> tuple:
    data = json.loads(line) if isinstance(line, str) else line
    return SOURCES[source][3](data)


def from_file_line(path: Path, line: str) -> None:
    """Called by append_line for EVERY audit line written anywhere: queue it when its file is a configured source --
    for the schema of the agent that owns the source (dedupe -> `dedupe`, the rest -> `audit`)."""
    source = source_of(path)
    if source is None:
        return
    agent = agent_of(source)
    if not core.dsn(agent):
        return
    table, columns, casts, _ = SOURCES[source]
    prepare = _with_rule_sets(columns.index("rule_set_hash")) if "rule_set_hash" in columns else None
    w = writer(agent, migrations_of(agent), table, columns, casts=casts, prepare=prepare)
    if w is not None:
        w.put(row(source, line))


def field_correction(*, request_id: str | None, case_id: str | None, document_id: str | None,
                     document_type: str | None, source_id: str | None, field: str, reason: str | None,
                     corrected_by: str | None) -> None:
    """A document field corrected by an officer: who, which field, why -- NEVER the old or the new value."""
    from app.agent_db.redact import redact

    w = writer(AGENT, MIGRATIONS, "audit_field_corrections", _CORRECTION_COLUMNS)
    if w is not None:
        w.put((datetime.now(timezone.utc), request_id, case_id, document_id, document_type, source_id, field,
               redact(str(reason or ""))[:500] or None, corrected_by, _demo(case_id)))


__all__ = ["AGENT", "MIGRATIONS", "OWN_TABLES", "PURGE_DEMO_SQL", "SOURCES", "TABLES", "agent_of", "field_correction",
           "from_file_line", "migrations_of", "register_rule_set", "row", "source_of", "source_path", "sources_of"]
