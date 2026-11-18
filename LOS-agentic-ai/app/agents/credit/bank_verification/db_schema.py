"""
THE BANK VERIFICATION DATABASE IN POSTGRESQL -- typed columns, keys and checks, in versioned migrations (app/agent_db).

    cases          one row per verification case (is_demo = a demo case; every other table hangs off a case and goes
                   with it: ON DELETE CASCADE)
    sources        the original inputs (the LOS entry, the AA data) as JSONB
    documents      stored files (statements, agency reports): size, sha256, the account read from it; the file itself
                   is on disk at stored_path
    extractions    each reading of a stored statement: status, the data read (JSONB), the facts (JSONB)
    runs           each verification run: the idempotency key, the input hash, the full response (JSONB)
    rule_results   the eight checks of a run, one row each
    decisions      the agency's finalization: final status, what was asked, the checklist payload, the outcome
    summaries      the plain-words summary of a run (AI or rules)
    audit_events   the case history (who did what, when) -- the bank's own shape, kept from the first version
    agent_logs     WARNING+ application logs (shared, app/agent_db)

Column ORDER matters: the repository inserts some rows positionally, so `row_no` (the insertion order the SQLite demo
store keeps as `rowid`) is always the last column. A released migration is never edited -- add the next one.
"""

from __future__ import annotations

from app.agent_db import LOGS_MIGRATION, Migration, responses, rule_sets, rule_store

AGENT = "bank_verification"

_V1 = """
CREATE TABLE cases (
    case_id          TEXT PRIMARY KEY,
    application_ref  TEXT          NOT NULL,
    applicant_name   TEXT          NOT NULL,
    loan_amount      NUMERIC(16,2) NOT NULL CHECK (loan_amount >= 0),
    product          TEXT          NOT NULL,
    stage            TEXT          NOT NULL,
    mode             TEXT          NOT NULL,
    status           TEXT          NOT NULL,
    risk_level       TEXT,
    assigned_agency  TEXT,
    scenario         TEXT,
    historical       BOOLEAN       NOT NULL DEFAULT false,
    created_at       TIMESTAMPTZ   NOT NULL,
    updated_at       TIMESTAMPTZ   NOT NULL,
    is_demo          BOOLEAN       NOT NULL DEFAULT false
);
CREATE INDEX cases_application ON cases (application_ref);
CREATE INDEX cases_status ON cases (status);

CREATE TABLE sources (
    source_id   TEXT PRIMARY KEY,
    case_id     TEXT        NOT NULL REFERENCES cases (case_id) ON DELETE CASCADE,
    kind        TEXT        NOT NULL,
    payload     JSONB       NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    row_no      BIGSERIAL
);
CREATE INDEX sources_case ON sources (case_id, kind, row_no);

CREATE TABLE documents (
    document_id       TEXT PRIMARY KEY,
    case_id           TEXT        NOT NULL REFERENCES cases (case_id) ON DELETE CASCADE,
    role              TEXT        NOT NULL,
    file_name         TEXT,
    kind              TEXT,
    size_bytes        BIGINT      NOT NULL CHECK (size_bytes >= 0),
    sha256            TEXT        NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    fingerprint       TEXT,
    account_number    TEXT,
    transaction_keys  JSONB,
    stored_path       TEXT        NOT NULL,
    seeded            BOOLEAN     NOT NULL DEFAULT false,
    created_at        TIMESTAMPTZ NOT NULL,
    row_no            BIGSERIAL
);
CREATE INDEX documents_case ON documents (case_id, role, row_no);

CREATE TABLE extractions (
    extraction_id  TEXT PRIMARY KEY,
    case_id        TEXT        NOT NULL REFERENCES cases (case_id) ON DELETE CASCADE,
    document_id    TEXT        NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
    status         TEXT        NOT NULL,
    data           JSONB,
    facts          JSONB       NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL,
    row_no         BIGSERIAL
);
CREATE INDEX extractions_document ON extractions (document_id, row_no);

CREATE TABLE runs (
    run_id           TEXT PRIMARY KEY,
    case_id          TEXT        NOT NULL REFERENCES cases (case_id) ON DELETE CASCADE,
    idempotency_key  TEXT,
    input_hash       TEXT        NOT NULL,
    response         JSONB       NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL,
    row_no           BIGSERIAL
);
CREATE UNIQUE INDEX runs_idem ON runs (case_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX runs_case ON runs (case_id, row_no);

CREATE TABLE rule_results (
    run_id   TEXT  NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    rule_id  TEXT  NOT NULL,
    status   TEXT  NOT NULL,
    reason   TEXT  NOT NULL,
    result   JSONB NOT NULL,
    PRIMARY KEY (run_id, rule_id)
);
CREATE INDEX rule_results_status ON rule_results (rule_id, status);

CREATE TABLE decisions (
    decision_id         TEXT PRIMARY KEY,
    case_id             TEXT        NOT NULL REFERENCES cases (case_id) ON DELETE CASCADE,
    run_id              TEXT        NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    final_status        TEXT        NOT NULL,
    requested           TEXT        NOT NULL,
    payload             JSONB       NOT NULL,
    outcome             JSONB       NOT NULL,
    report_document_id  TEXT        REFERENCES documents (document_id) ON DELETE SET NULL,
    decided_by          TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL,
    row_no              BIGSERIAL
);
CREATE INDEX decisions_case ON decisions (case_id, row_no);

CREATE TABLE summaries (
    run_id      TEXT PRIMARY KEY REFERENCES runs (run_id) ON DELETE CASCADE,
    text        TEXT,
    source      TEXT        NOT NULL,
    facts_hash  TEXT,
    created_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX summaries_facts ON summaries (facts_hash);

-- the case history; not a foreign key: a system event (a demo reset) has no case
CREATE TABLE audit_events (
    event_id  BIGSERIAL PRIMARY KEY,
    case_id   TEXT        NOT NULL,
    at        TIMESTAMPTZ NOT NULL,
    actor     TEXT        NOT NULL,
    event     TEXT        NOT NULL,
    detail    JSONB       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX audit_case ON audit_events (case_id, event_id);
"""

MIGRATIONS: list[Migration] = [
    ("bank-0001", "typed tables: cases, sources, documents, extractions, runs, rule_results, decisions, summaries, "
                  "audit_events", _V1),
    LOGS_MIGRATION,
    # the policy each run was made with (rule_sets, rule_set_hash on runs); audit_events append-only -- its demo rows
    # (a demo case's, is_demo set from the case) may still be purged
    rule_sets.migration("bank-0002", "rule_sets; rule_set_hash on runs; audit_events is_demo + append-only",
                        before=rule_sets.demo_flag_sql("audit_events", "case_id", "cases"),
                        decision_tables=["runs"], audit_tables=["audit_events"]),
    rule_store.MIGRATION,
    responses.MIGRATION,
]

#: what scripts/setup_agent_databases.py --purge-demo deletes (children go by ON DELETE CASCADE)
PURGE_DEMO_SQL = [
    "DELETE FROM audit_events WHERE case_id IN (SELECT case_id FROM cases WHERE is_demo)",
    "DELETE FROM cases WHERE is_demo",
]


def old_untyped_schema(conn) -> bool:
    """A database made by the first PostgreSQL store (every column TEXT, no schema_migrations) -- never migrated
    silently: its data must be exported and the database recreated."""
    row = conn.execute("SELECT data_type FROM information_schema.columns WHERE table_schema = current_schema() "
                       "AND table_name = 'cases' AND column_name = 'loan_amount'").fetchone()
    found = (row["data_type"] if isinstance(row, dict) else row[0]) if row else None
    return found == "text"
