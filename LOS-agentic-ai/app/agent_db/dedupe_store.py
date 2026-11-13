"""
THE DEDUPE AGENT'S OWN SCHEMA (owner 2026-11-03: "dedupe ka alag schema karo jaise saare agents ke hai, aur audit ko
common rakho saare agents ke liye"; config app/config/agent_databases.yaml `dedupe`, DSN DEDUPE_DSN or the shared
LOS_AGENTIC_DSN -> schema `dedupe`).

    audit_dedupe_decisions      every dedupe decision: case, policy version, status, recommendation, each rule's outcome
    audit_customer_history      every customer history check: parameter searched (its NAME), party, match, outcome
    rule_sets / rule_set_active / rule_set_changes
                                the dedupe and customer history policies: every version, the one in force, who changed it
    api_responses               every POST /api/v1/credit/dedupe response (identifiers masked)
    agent_logs                  WARNING+ logs of app.agents.dedupe
  THE CHECK ITSELF, IN COLUMNS (2026-11-03, owner: "haan chaaro tables bana do") -- one write per dedupe check, from
  the answer the officer got (customer_history._record): never a PAN / mobile / name, only field NAMES and results.
    dedupe_checks               one row per check: case, parameter NAME, party, declared + found customer type,
                                customer status, where found (MIFIN / LOS), the one outcome, reason codes, coverage
    dedupe_scenario_results     each check's S01..S18 scenarios: DETECTED / NOT_DETECTED / ..., the evidence counts
    dedupe_matches              each matched record per party: the other case (as the officer may see it) or the MIFIN
                                record ref, which fields matched (names), role change, name differs, confidence
    audit_events                the standard business audit trail (DEDUPE_CHECKED), as every agent has

The rows still arrive the way they always did: the dedupe agent appends its JSONL audit lines
(app/observability/audit_log.py append_line) and app/agent_db/audit_store.py routes each configured file to the schema
of the agent that owns it -- these two files to this schema. Same table names and columns as the ones they had in the
`audit` schema (until 2026-11-03), so a query only changes its schema.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

from app.agent_db import AUDIT_MIGRATION, LOGS_MIGRATION, Migration, core, demo_case, responses, rule_sets, rule_store

AGENT = "dedupe"

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
"""

TABLES = ("audit_dedupe_decisions", "audit_customer_history")

_V3 = """
-- one row per dedupe check, as the officer saw it (field NAMES and results only -- never an identifier's value)
CREATE TABLE dedupe_checks (
    check_id                BIGSERIAL   PRIMARY KEY,
    request_id              TEXT        NOT NULL UNIQUE,
    checked_at              TIMESTAMPTZ NOT NULL,
    case_id                 TEXT,
    actor                   TEXT,
    parameter               TEXT,
    checked_party           TEXT,
    customer_type_declared  TEXT,
    customer_type_result    TEXT,
    customer_status         TEXT,
    customer_found_in       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    los_search_status       TEXT,
    los_search_strength     TEXT,
    los_recommendation      TEXT,
    mifin_match             TEXT,
    history_status          TEXT,
    dedupe_status           TEXT,
    outcome                 TEXT,
    reason_codes            JSONB       NOT NULL DEFAULT '[]'::jsonb,
    next_actions            JSONB       NOT NULL DEFAULT '[]'::jsonb,
    policy_version          TEXT,
    los_policy_version      TEXT,
    cases_scanned           INTEGER,
    partial                 BOOLEAN,
    complete                BOOLEAN,
    duration_ms             NUMERIC(12, 1),
    is_demo                 BOOLEAN     NOT NULL DEFAULT false
);
CREATE INDEX dedupe_checks_case ON dedupe_checks (case_id, checked_at);
CREATE INDEX dedupe_checks_outcome ON dedupe_checks (outcome, checked_at);

-- each check's scenarios (S01..): DETECTED / NOT_DETECTED / NOT_EVALUATED, with the evidence COUNTS
CREATE TABLE dedupe_scenario_results (
    result_id    BIGSERIAL   PRIMARY KEY,
    request_id   TEXT        NOT NULL,
    checked_at   TIMESTAMPTZ NOT NULL,
    case_id      TEXT,
    scenario     TEXT        NOT NULL,
    status       TEXT,
    evidence     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    is_demo      BOOLEAN     NOT NULL DEFAULT false,
    UNIQUE (request_id, scenario)
);
CREATE INDEX dedupe_scenarios_scenario ON dedupe_scenario_results (scenario, status, checked_at);

-- each matched record per party: an LOS case (its id as the officer may see it) or a MIFIN record ref
CREATE TABLE dedupe_matches (
    match_id         BIGSERIAL   PRIMARY KEY,
    request_id       TEXT        NOT NULL,
    checked_at       TIMESTAMPTZ NOT NULL,
    case_id          TEXT,
    source           TEXT        NOT NULL,
    party_role       TEXT,
    party_id         TEXT,
    matched_ref      TEXT,
    matched_role     TEXT,
    matched_product  TEXT,
    record_type      TEXT,
    relation         TEXT,
    status           TEXT,
    confidence       TEXT,
    matched_fields   JSONB       NOT NULL DEFAULT '[]'::jsonb,
    reason_codes     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    role_change      BOOLEAN,
    name_differs     BOOLEAN,
    visible          BOOLEAN,
    is_demo          BOOLEAN     NOT NULL DEFAULT false
);
CREATE INDEX dedupe_matches_case ON dedupe_matches (case_id, checked_at);
CREATE INDEX dedupe_matches_ref ON dedupe_matches (matched_ref);
""" + rule_sets.audit_guard_sql(["dedupe_checks", "dedupe_scenario_results", "dedupe_matches"])

CHECK_TABLES = ("dedupe_checks", "dedupe_scenario_results", "dedupe_matches")

MIGRATIONS: list[Migration] = [
    ("dedupe-0001", "the dedupe agent's audit trails: dedupe decisions, customer history checks", _V1),
    LOGS_MIGRATION,
    rule_sets.migration("dedupe-0002", "rule_sets; rule_set_hash on dedupe decisions; both audit tables append-only",
                        decision_tables=["audit_dedupe_decisions"], audit_tables=list(TABLES)),
    rule_store.MIGRATION,                    # the dedupe and customer history policies' versions
    responses.MIGRATION,                     # the dedupe API's responses
    ("dedupe-0003", "the check in columns: dedupe_checks, dedupe_scenario_results, dedupe_matches", _V3),
    AUDIT_MIGRATION,                         # audit_events, as every agent has
]

PURGE_DEMO_SQL = [f"DELETE FROM {t} WHERE is_demo" for t in TABLES + CHECK_TABLES + ("audit_events",)]

CHECK_COLUMNS = ("request_id", "checked_at", "case_id", "actor", "parameter", "checked_party", "customer_type_declared",
                 "customer_type_result", "customer_status", "customer_found_in", "los_search_status",
                 "los_search_strength", "los_recommendation", "mifin_match", "history_status", "dedupe_status",
                 "outcome", "reason_codes", "next_actions", "policy_version", "los_policy_version", "cases_scanned",
                 "partial", "complete", "duration_ms", "is_demo")
SCENARIO_COLUMNS = ("request_id", "checked_at", "case_id", "scenario", "status", "evidence", "is_demo")
MATCH_COLUMNS = ("request_id", "checked_at", "case_id", "source", "party_role", "party_id", "matched_ref",
                 "matched_role", "matched_product", "record_type", "relation", "status", "confidence",
                 "matched_fields", "reason_codes", "role_change", "name_differs", "visible", "is_demo")
EVENT_COLUMNS = ("occurred_at", "actor", "event_type", "case_id", "entity_type", "entity_id", "request_id", "outcome",
                 "details", "is_demo")
CASTS = {"customer_found_in": "jsonb", "reason_codes": "jsonb", "next_actions": "jsonb", "evidence": "jsonb",
         "matched_fields": "jsonb", "details": "jsonb"}


def _j(value: Any, empty: Any) -> str:
    return json.dumps(value if value is not None else empty, default=str, sort_keys=True)


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _evidence(value: Any) -> dict[str, Any]:
    """A scenario's evidence, numbers and short codes only (counts, flags) -- never a free-text value."""
    out = {}
    for k, v in _d(value).items():
        if isinstance(v, bool) or isinstance(v, (int, float)) or v is None:
            out[k] = v
        elif isinstance(v, str) and len(v) <= 40 and v.replace("_", "").isalnum() and v.upper() == v:
            out[k] = v                                              # a code (e.g. searched: PAN), never a value
        elif isinstance(v, list):
            out[k] = len(v)
    return out


def rows_for(result: dict[str, Any], *, actor: str | None = None,
             at: datetime | None = None) -> dict[str, list[tuple]]:
    """The four tables' rows for ONE dedupe answer (the dict the API returned). Pure: no database."""
    at = at or datetime.now(timezone.utc)
    request_id, case_id = result.get("request_id"), result.get("case_id")
    demo = bool(result.get("is_demo")) or demo_case(AGENT, case_id)
    los = _d(result.get("los_dedupe"))
    case, search = _d(los.get("case")), _d(los.get("search"))
    history, dedupe = _d(result.get("history")), _d(result.get("dedupe"))
    type_check = _d(result.get("customer_type_check")) or _d(search.get("customer_type_check"))
    coverage = _d(case.get("coverage")) or _d(search.get("coverage"))
    check = (request_id, at, case_id, actor, result.get("searched_parameter"), result.get("checked_party"),
             type_check.get("declared"), type_check.get("result"), result.get("customer_status"),
             _j(result.get("customer_found_in"), []), search.get("status"), search.get("strength"),
             case.get("recommendation"), _d(history.get("customer")).get("match"), history.get("history_status"),
             dedupe.get("status"), result.get("outcome"), _j(result.get("reason_codes"), []),
             _j(result.get("next_actions"), []), _d(result.get("policy")).get("policy_version"),
             _d(case.get("policy") or search.get("policy")).get("policy_version"), coverage.get("cases_scanned"),
             coverage.get("partial"), _d(result.get("completeness")).get("complete"),
             _d(result.get("timings_ms")).get("total_ms"), demo)
    scenarios = [(request_id, at, case_id, str(sc.get("scenario")), sc.get("status"), _j(_evidence(sc.get("evidence")), {}),
                  demo) for sc in (case.get("scenarios") or []) if isinstance(sc, dict) and sc.get("scenario")]
    matches = []
    for party in case.get("parties") or []:
        for m in _d(party).get("matches") or []:
            m = _d(m)
            matches.append((request_id, at, case_id, "LOS", party.get("role"), party.get("party_id"), m.get("case_id"),
                            m.get("matched_role"), m.get("product"), m.get("customer_record"), m.get("relation"),
                            m.get("status"), m.get("confidence"), _j(m.get("matched_fields"), []),
                            _j(m.get("reason_codes"), []), m.get("role_change"), m.get("name_differs"),
                            m.get("visible"), demo))
    for rec in dedupe.get("matched_records") or []:
        rec = _d(rec)
        matches.append((request_id, at, case_id, str(rec.get("source") or "MIFIN"), rec.get("role"), None,
                        rec.get("record_ref"), None, None, rec.get("record_type"), None, None, None,
                        _j(rec.get("matched_fields"), []), _j([], []), None, None, None, demo))
    event = (at, actor or "system", "DEDUPE_CHECKED", case_id, "dedupe_check", request_id, request_id,
             result.get("outcome"), _j({"customer_status": result.get("customer_status"),
                                        "parameter": result.get("searched_parameter"),
                                        "checked_party": result.get("checked_party"),
                                        "reason_codes": result.get("reason_codes") or []}, {}), demo)
    return {"dedupe_checks": [check], "dedupe_scenario_results": scenarios, "dedupe_matches": matches,
            "audit_events": [event]}


_COLUMNS = {"dedupe_checks": CHECK_COLUMNS, "dedupe_scenario_results": SCENARIO_COLUMNS,
            "dedupe_matches": MATCH_COLUMNS, "audit_events": EVENT_COLUMNS}


def record_check(result: dict[str, Any], *, actor: str | None = None) -> bool:
    """Queue one dedupe answer's rows for the four tables (the background writer: the request never waits). Nothing
    configured: nothing happens. Never raises; says whether the rows were queued."""
    from app.agent_db.writer import writer

    if not core.dsn(AGENT):
        return False
    try:
        rows = rows_for(result, actor=actor)
        for table, values in rows.items():
            w = writer(AGENT, MIGRATIONS, table, _COLUMNS[table], casts=CASTS)
            if w is None:
                return False
            for r in values:
                w.put(r)
        return True
    except Exception:  # noqa: BLE001 - the answer is never failed by its own record
        return False


def insert_rows(conn: Any, rows: dict[str, list[tuple]]) -> dict[str, int]:
    """Write rows now on this connection (search_path = the dedupe schema), skipping a check already stored (its
    request_id): the backfill from api_responses uses it. Returns rows written per table."""
    done = {t: 0 for t in rows}
    request_ids = {r[0] for r in rows.get("dedupe_checks", [])}
    if any(conn.execute("SELECT 1 FROM dedupe_checks WHERE request_id = %s", (rid,)).fetchone() for rid in request_ids):
        return done
    for table, values in rows.items():
        cols = _COLUMNS[table]
        marks = ", ".join(f"%s::{CASTS[c]}" if c in CASTS else "%s" for c in cols)
        for r in values:
            conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})", r)
            done[table] += 1
    return done


def results_in(response: Any) -> Iterable[dict[str, Any]]:
    """The dedupe answers inside one stored API response: POST /dedupe is one; POST /dedupe/all holds one per row."""
    if not isinstance(response, dict):
        return []
    if "searched_parameter" in response and "request_id" in response:
        return [response]
    return [r["result"] for r in (response.get("rows") or [])
            if isinstance(r, dict) and isinstance(r.get("result"), dict) and "searched_parameter" in r["result"]]


__all__ = ["AGENT", "CHECK_TABLES", "MIGRATIONS", "PURGE_DEMO_SQL", "TABLES", "insert_rows", "record_check",
           "results_in", "rows_for"]
