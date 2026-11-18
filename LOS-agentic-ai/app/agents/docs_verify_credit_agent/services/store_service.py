"""
THE DOCUMENT DESK'S OWN DATABASE (DOCS_POLICY_DSN; config app/config/agent_databases.yaml `docs_policy`).

Every evaluation is ALSO written here, in clean columns, besides the LOS case store (audit_service.record keeps the LOS
case history as it was):

    docs_policy_evaluations          one row per evaluation: case, stage, overall status, recommended action, the
                                     policy (id / version / status / file hash), counts, the input hash, is_demo
    docs_policy_requirement_results  every requirement-level outcome of it (satisfied ones too), one row each
    audit_events                     DOCUMENT_READINESS_EVALUATED per stored evaluation (shared table, append-only)
    rule_sets                        the policy each evaluation was made with, stored once (rule_set_hash on the
                                     evaluation; app/agent_db/rule_sets.py)
    agent_logs                       WARNING+ logs (shared table)

IDEMPOTENT: (case_id, input_hash) is unique -- the same facts + policy + date write nothing new (a replay, or a retry
after the LOS store failed, is safe). Not configured (no DSN): nothing is written here and `enabled()` says so; in
production the setting docs_policy store is expected (scripts/setup_agent_databases.py --check reports it).
"""

from __future__ import annotations

import json
from typing import Any

from app.agent_db import AUDIT_MIGRATION, LOGS_MIGRATION, Migration, database, demo_case, dsn, responses, rule_sets, rule_store
from app.agents.docs_verify_credit_agent.models.requirement import DONE   # an outcome is open unless DONE

AGENT = "docs_policy"

_V1 = """
CREATE TABLE docs_policy_evaluations (
    evaluation_id           TEXT PRIMARY KEY,
    case_id                 TEXT        NOT NULL,
    stage                   TEXT,
    overall_status          TEXT        NOT NULL,
    recommended_action      TEXT,
    policy_id               TEXT        NOT NULL,
    policy_version          TEXT        NOT NULL,
    policy_status           TEXT,
    policy_file_sha256      TEXT        NOT NULL,
    input_hash              TEXT        NOT NULL,
    rules_evaluated         INTEGER     NOT NULL CHECK (rules_evaluated >= 0),
    rules_disabled          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    requirements_satisfied  INTEGER     NOT NULL CHECK (requirements_satisfied >= 0),
    requirements_open       INTEGER     NOT NULL CHECK (requirements_open >= 0),
    reused_documents        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    waivers_applied         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    base_policy             JSONB,
    agent_version           TEXT,
    evaluated_at            TIMESTAMPTZ NOT NULL,
    recorded_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_demo                 BOOLEAN     NOT NULL DEFAULT false,
    UNIQUE (case_id, input_hash)
);
CREATE INDEX docs_policy_evaluations_case ON docs_policy_evaluations (case_id, evaluated_at DESC);
CREATE INDEX docs_policy_evaluations_status ON docs_policy_evaluations (overall_status, evaluated_at DESC);

CREATE TABLE docs_policy_requirement_results (
    evaluation_id       TEXT    NOT NULL REFERENCES docs_policy_evaluations (evaluation_id) ON DELETE CASCADE,
    seq                 INTEGER NOT NULL CHECK (seq >= 0),
    rule_id             TEXT    NOT NULL,
    rule_name           TEXT,
    check_id            TEXT    NOT NULL,
    status              TEXT    NOT NULL,
    severity            TEXT,
    is_open             BOOLEAN NOT NULL,
    reason              TEXT,
    recommended_action  TEXT,
    party_id            TEXT,
    party_role          TEXT,
    slot                TEXT,
    missing_documents   JSONB   NOT NULL DEFAULT '[]'::jsonb,
    evidence            JSONB   NOT NULL DEFAULT '[]'::jsonb,
    deviation_codes     JSONB   NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (evaluation_id, seq)
);
CREATE INDEX docs_policy_requirements_rule ON docs_policy_requirement_results (rule_id, status);
CREATE INDEX docs_policy_requirements_open ON docs_policy_requirement_results (evaluation_id) WHERE is_open;
"""

MIGRATIONS: list[Migration] = [
    ("docs-0001", "docs_policy_evaluations and docs_policy_requirement_results", _V1),
    AUDIT_MIGRATION,
    LOGS_MIGRATION,
    rule_sets.migration("docs-0002", "rule_sets; rule_set_hash on evaluations; audit_events append-only",
                        decision_tables=["docs_policy_evaluations"], audit_tables=["audit_events"]),
    rule_store.MIGRATION,
    responses.MIGRATION,
]

#: what scripts/setup_agent_databases.py --purge-demo deletes (requirement rows go by ON DELETE CASCADE)
PURGE_DEMO_SQL = [
    "DELETE FROM audit_events WHERE is_demo",
    "DELETE FROM docs_policy_evaluations WHERE is_demo",
]


def enabled() -> bool:
    return bool(dsn(AGENT))


def _list(value: Any) -> str:
    return json.dumps(list(value or []), default=str)


def _record_policy(conn: Any, audit: dict[str, Any]) -> str | None:
    """The policy file this evaluation used (found by its file hash) stored once in rule_sets; its hash. None when
    that file is no longer there (an older evaluation replayed after the policy changed): its file hash stays stored."""
    from app.agents.docs_verify_credit_agent.config import policy_loader

    for loaded in policy_loader.load_in_force():
        if loaded.sha256 == audit.get("policy_file_sha256"):
            with conn.cursor() as cur:
                # the version in force as stored by the rule store (the same content -> its row, its hash)
                from app.agent_db import rule_store

                content = rule_store.load(f"docs_policy:{loaded.file}")
                info = rule_store.describe("docs_policy", content)
                return rule_sets.record(cur, f"docs_policy:{loaded.file}", content, version_label=info["version_label"],
                                        status=info["status"], source_files=[loaded.file], is_demo=info["is_demo"])
    return None


def save(case_id: str, payload: dict[str, Any]) -> bool:
    """Write one evaluation (and its outcomes and audit row) in one transaction. True when it was new."""
    if not enabled():
        return False
    audit = dict(payload.get("audit") or {})
    evaluation_id = str(audit.get("evaluation_id") or "")
    demo = demo_case(AGENT, case_id)
    db = database(AGENT, MIGRATIONS)
    with db.tx() as conn:
        rule_set_hash = _record_policy(conn, audit)
        row = conn.execute(
            "INSERT INTO docs_policy_evaluations (evaluation_id, case_id, stage, overall_status, recommended_action, "
            "policy_id, policy_version, policy_status, policy_file_sha256, input_hash, rules_evaluated, rules_disabled, "
            "requirements_satisfied, requirements_open, reused_documents, waivers_applied, base_policy, agent_version, "
            "evaluated_at, is_demo, rule_set_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, "
            "%s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING evaluation_id",
            (evaluation_id, case_id, payload.get("stage"), payload.get("overall_status"),
             payload.get("recommended_action"), audit.get("policy_id"), str(audit.get("policy_version") or ""),
             audit.get("policy_status"), audit.get("policy_file_sha256"), audit.get("input_hash"),
             int(audit.get("rules_evaluated") or 0), _list(audit.get("rules_disabled")),
             int(audit.get("requirements_satisfied") or 0), int(audit.get("requirements_open") or 0),
             _list(audit.get("reused_documents")), _list(audit.get("waivers_applied")),
             json.dumps(audit.get("base_policy"), default=str), payload.get("agent_version"),
             audit.get("evaluated_at"), demo, rule_set_hash)).fetchone()
        if row is None:
            return False                                       # already stored (a replay or a retry)
        outcomes = list(payload.get("outcomes") or [])
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO docs_policy_requirement_results (evaluation_id, seq, rule_id, rule_name, check_id, status, "
                "severity, is_open, reason, recommended_action, party_id, party_role, slot, missing_documents, "
                "evidence, deviation_codes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, "
                "%s::jsonb, %s::jsonb)",
                [(evaluation_id, n, o.get("rule_id"), o.get("rule_name"), o.get("check_id"), o.get("status"),
                  o.get("severity"), o.get("status") not in DONE, o.get("reason"),
                  o.get("recommended_action"), o.get("party_id"), o.get("party_role"), o.get("slot"),
                  _list(o.get("missing_documents")), _list(o.get("evidence")), _list(o.get("deviation_codes")))
                 for n, o in enumerate(outcomes)])
        conn.execute(
            "INSERT INTO audit_events (actor, event_type, case_id, entity_type, entity_id, outcome, details, is_demo) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
            ("docs-policy-agent", "DOCUMENT_READINESS_EVALUATED", case_id, "EVALUATION", evaluation_id,
             payload.get("overall_status"),
             json.dumps({"policy_id": audit.get("policy_id"), "policy_version": audit.get("policy_version"),
                         "requirements_open": audit.get("requirements_open"), "rule_set_hash": rule_set_hash},
                        default=str), demo))
    return True


__all__ = ["AGENT", "MIGRATIONS", "PURGE_DEMO_SQL", "enabled", "save"]
