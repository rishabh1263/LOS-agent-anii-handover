"""
THE CASE STORE'S SQL, WRITTEN ONCE -- run on PostgreSQL (app/store/postgres_repo.py).

SqlRepository holds every query and every row <-> domain mapping of the
repository contract. It owns no engine: the concrete backend supplies the
connection (`_connect`) and the schema migration (`initialise`). PostgreSQL is
the only backend -- in development (an embedded local server, app/store/__init__.py),
in tests and in production -- so what the tests prove is what production runs.

Statements are plain SQL with `?` placeholders; the backend's translation turns
them into its own dialect (postgres_repo.translate). Driver errors arrive as
StoreDbError / StoreIntegrityError, so every fail-closed path below is engine-free.

Times are stored as ISO-8601 UTC strings: readable in any SQL console, and
ordered correctly as text.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.store.models import (
    Applicant,
    Application,
    ApplicationStatus,
    CaseDecision,
    CaseEvent,
    CaseFinding,
    CaseStage,
    Document,
    DocumentStatus,
    DocumentVersion,
    FindingKind,
    StageTransition,
    utcnow,
)
from app.store.repository import Repository, RepositoryError
from app.store import crypto as _crypto


class StoreDbError(Exception):
    """A database error, raised by the backend's connection adapter."""


class StoreIntegrityError(StoreDbError):
    """A constraint refused the write (unique, foreign key, check)."""

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS applicants (
    applicant_id   TEXT PRIMARY KEY,
    full_name      TEXT,
    mobile         TEXT,
    email          TEXT,
    date_of_birth  TEXT,
    address        TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    case_id          TEXT PRIMARY KEY,
    applicant_id     TEXT NOT NULL,
    status           TEXT NOT NULL,
    product          TEXT,
    loan_amount      TEXT,
    tenure_months    TEXT,
    interest_rate_pct TEXT,
    declared_monthly_obligations TEXT,
    declared_monthly_income TEXT,
    property_value   TEXT,
    employment_type  TEXT,
    policy_id        TEXT,
    policy_version   TEXT,
    policy_pinned_at TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    FOREIGN KEY (applicant_id) REFERENCES applicants (applicant_id)
);
CREATE INDEX IF NOT EXISTS idx_applications_applicant
    ON applications (applicant_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS documents (
    document_id         TEXT PRIMARY KEY,
    case_id             TEXT NOT NULL,
    applicant_id        TEXT NOT NULL,
    party_id            TEXT,
    party_role          TEXT NOT NULL DEFAULT 'PRIMARY_APPLICANT',
    document_type       TEXT NOT NULL,
    status              TEXT NOT NULL,
    source_id           TEXT,
    verification_status TEXT,
    reason_codes        TEXT NOT NULL DEFAULT '[]',
    extracted_fields    TEXT NOT NULL DEFAULT '{}',
    uploaded_at         TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (case_id) REFERENCES applications (case_id)
);
CREATE INDEX IF NOT EXISTS idx_documents_case
    ON documents (case_id, uploaded_at);

-- ======================================================================
-- CASE MEMORY
--
-- Additive. Every statement is CREATE ... IF NOT EXISTS, so opening a
-- store written before these tables existed adds them and touches
-- nothing that was already there.
-- ======================================================================

-- One conclusion the pipeline reached. VERIFICATION, KYC, FINANCIAL,
-- RISK and the rest share this shape, discriminated by finding_kind --
-- see models.CaseFinding for why that is one table and not five.
CREATE TABLE IF NOT EXISTS case_findings (
    finding_id    TEXT PRIMARY KEY,
    case_id       TEXT NOT NULL,
    party_id      TEXT,
    finding_kind  TEXT NOT NULL,
    stage         TEXT,
    status        TEXT,
    score         INTEGER,
    confidence    INTEGER,
    reason_codes  TEXT NOT NULL DEFAULT '[]',
    payload       TEXT NOT NULL DEFAULT '{}',
    source_type   TEXT,
    source_id     TEXT,
    document_id   TEXT,
    created_at    TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    content_hash  TEXT,
    FOREIGN KEY (case_id) REFERENCES applications (case_id)
);
CREATE INDEX IF NOT EXISTS idx_findings_case
    ON case_findings (case_id, created_at);
CREATE INDEX IF NOT EXISTS idx_findings_party
    ON case_findings (case_id, party_id);
CREATE INDEX IF NOT EXISTS idx_findings_kind
    ON case_findings (case_id, finding_kind);
CREATE INDEX IF NOT EXISTS idx_findings_document
    ON case_findings (document_id);
-- One row per logical finding per content. A re-run that concluded the
-- same thing collides here instead of appending a duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS idx_findings_identity
    ON case_findings (case_id, finding_kind, IFNULL(party_id, ''),
                      IFNULL(source_id, ''), IFNULL(content_hash, ''));

-- One upload of one document. A re-upload is a new row, not an edit.
CREATE TABLE IF NOT EXISTS document_versions (
    document_version_id TEXT PRIMARY KEY,
    document_id         TEXT NOT NULL,
    case_id             TEXT NOT NULL,
    party_id            TEXT,
    version             INTEGER NOT NULL DEFAULT 1,
    source_id           TEXT,
    content_hash        TEXT,
    created_at          TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents (document_id),
    FOREIGN KEY (case_id) REFERENCES applications (case_id)
);
CREATE INDEX IF NOT EXISTS idx_docversions_document
    ON document_versions (document_id, version);
CREATE INDEX IF NOT EXISTS idx_docversions_case
    ON document_versions (case_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_docversions_identity
    ON document_versions (document_id, version);

-- A decision the pipeline RECORDED, with the policy it was taken under.
CREATE TABLE IF NOT EXISTS case_decisions (
    decision_id    TEXT PRIMARY KEY,
    case_id        TEXT NOT NULL,
    decision       TEXT,
    next_action    TEXT,
    status         TEXT,
    reason_codes   TEXT NOT NULL DEFAULT '[]',
    policy_id      TEXT,
    policy_version TEXT,
    created_at     TEXT NOT NULL,
    FOREIGN KEY (case_id) REFERENCES applications (case_id)
);
CREATE INDEX IF NOT EXISTS idx_decisions_case
    ON case_decisions (case_id, created_at);

-- What happened on a case, in order. `sequence` and not the timestamp
-- is the ordering, because a request writes several events inside one
-- millisecond.
CREATE TABLE IF NOT EXISTS case_events (
    event_id   TEXT PRIMARY KEY,
    case_id    TEXT NOT NULL,
    party_id   TEXT,
    event_type TEXT NOT NULL,
    stage      TEXT,
    summary    TEXT,
    ref_id     TEXT,
    created_at TEXT NOT NULL,
    sequence   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (case_id) REFERENCES applications (case_id)
);
CREATE INDEX IF NOT EXISTS idx_events_case
    ON case_events (case_id, sequence);
CREATE INDEX IF NOT EXISTS idx_events_created
    ON case_events (case_id, created_at);

-- WORK THAT OUTLIVES THE REQUEST THAT ASKED FOR IT.
--
-- A scanned statement too large to read inside an upload used to be
-- answered with "route it to the asynchronous extraction queue", and
-- there was no queue: nothing was recorded, nothing ran, and the
-- document stayed REVIEW for ever. This table IS the queue. A row
-- here is a promise that something will happen, which is why it is
-- durable rather than an in-memory list -- a restart must not lose
-- work a caller was told would be done.
CREATE TABLE IF NOT EXISTS ocr_jobs (
    job_id        TEXT PRIMARY KEY,
    document_id   TEXT NOT NULL,
    case_id       TEXT NOT NULL,
    applicant_id  TEXT NOT NULL,
    party_id      TEXT,
    document_type TEXT,
    status        TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    detail        TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ocr_jobs_document
    ON ocr_jobs (document_id);
CREATE INDEX IF NOT EXISTS idx_ocr_jobs_status
    ON ocr_jobs (status, created_at);
CREATE INDEX IF NOT EXISTS idx_ocr_jobs_case
    ON ocr_jobs (case_id);

-- WHO MAY ACCESS WHAT. One row per (subject, resource). Written when an
-- authenticated subject creates an applicant or a case; read by
-- app/security/access.py before any case data is served. No row, no
-- access -- service principals (los.read / los.write) aside.
CREATE TABLE IF NOT EXISTS access_grants (
    subject       TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id   TEXT NOT NULL,
    granted_at    TEXT NOT NULL,
    PRIMARY KEY (subject, resource_type, resource_id)
);
CREATE INDEX IF NOT EXISTS idx_access_grants_resource
    ON access_grants (resource_type, resource_id);

-- WHERE A CASE IS IN THE LOS LIFECYCLE. One row per case, written only by
-- the stage transition service (app/agents/los/stage_lifecycle.py) and
-- only together with the stage_transitions row that explains it. No row:
-- the case has never been transitioned and resolves as it always did.
-- `version` is the compare-and-set guard against a stale writer.
CREATE TABLE IF NOT EXISTS case_stage (
    case_id          TEXT PRIMARY KEY,
    stage            TEXT NOT NULL,
    stage_status     TEXT NOT NULL,
    stage_started_at TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    version          INTEGER NOT NULL,
    FOREIGN KEY (case_id) REFERENCES applications (case_id)
);

-- THE STAGE HISTORY. Append-only: the triggers below refuse UPDATE and
-- DELETE, so a recorded transition can never be rewritten. UNIQUE
-- (case_id, version) means two writers can never both record version N.
CREATE TABLE IF NOT EXISTS stage_transitions (
    transition_id             TEXT PRIMARY KEY,
    case_id                   TEXT NOT NULL,
    version                   INTEGER NOT NULL,
    kind                      TEXT NOT NULL,
    from_stage                TEXT,
    to_stage                  TEXT NOT NULL,
    from_status               TEXT,
    to_status                 TEXT NOT NULL,
    previous_stage_started_at TEXT,
    source                    TEXT NOT NULL,
    actor                     TEXT,
    reason                    TEXT,
    request_id                TEXT,
    correlation_id            TEXT,
    created_at                TEXT NOT NULL,
    UNIQUE (case_id, version),
    FOREIGN KEY (case_id) REFERENCES applications (case_id)
);
CREATE TRIGGER IF NOT EXISTS stage_transitions_no_update
    BEFORE UPDATE ON stage_transitions
    BEGIN SELECT RAISE(ABORT, 'stage history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS stage_transitions_no_delete
    BEFORE DELETE ON stage_transitions
    BEGIN SELECT RAISE(ABORT, 'stage history is append-only'); END;

-- JEV SEMANTIC DECISION RUNS (app/jev). One row per evaluation: the typed
-- decisions, their probabilities and confidence, the evidence version they
-- were made on, and what the orchestrator did with them. APPEND-ONLY -- a
-- re-evaluation is a new run, never an overwrite -- and keyed for
-- idempotency: the same evidence + scope + question-set version is
-- evaluated once (UNIQUE evaluation_key).
CREATE TABLE IF NOT EXISTS jev_runs (
    jev_run_id           TEXT PRIMARY KEY,
    evaluation_key       TEXT NOT NULL UNIQUE,
    case_id              TEXT NOT NULL,
    party_id             TEXT,
    stage                TEXT,
    evaluation_scope     TEXT NOT NULL,
    question_set_version TEXT NOT NULL,
    evidence_version     TEXT NOT NULL,
    trigger              TEXT,
    provider             TEXT NOT NULL,
    model                TEXT,
    status               TEXT NOT NULL,
    decisions            TEXT NOT NULL DEFAULT '[]',
    actions              TEXT NOT NULL DEFAULT '[]',
    error_code           TEXT,
    latency_ms           REAL,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jev_runs_case ON jev_runs (case_id, created_at);
CREATE TRIGGER IF NOT EXISTS jev_runs_no_update
    BEFORE UPDATE ON jev_runs
    BEGIN SELECT RAISE(ABORT, 'jev runs are append-only'); END;

-- MAKER / CHECKER APPROVALS (app/approvals). One generic row per controlled
-- action. `version` is bumped on every change and every change is a
-- compare-and-set on it, so two checkers can never both decide one request.
CREATE TABLE IF NOT EXISTS approvals (
    approval_id       TEXT PRIMARY KEY,
    case_id           TEXT NOT NULL,
    action_type       TEXT NOT NULL,
    resource_type     TEXT NOT NULL,
    resource_id       TEXT NOT NULL,
    maker_id          TEXT NOT NULL,
    checker_id        TEXT,
    status            TEXT NOT NULL,
    reason            TEXT NOT NULL,
    comments          TEXT,
    payload           TEXT NOT NULL DEFAULT '{}',
    result            TEXT,
    policy_reference  TEXT,
    evidence_version  TEXT NOT NULL,
    version           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    expires_at        TEXT NOT NULL,
    CHECK (checker_id IS NULL OR checker_id <> maker_id)
);
CREATE INDEX IF NOT EXISTS idx_approvals_case ON approvals (case_id, created_at);

-- COPILOT CONVERSATION STATE (copilot/conversation/state.py, store
-- "repository"): labels only, never values; TTL-pruned. In the case store so
-- every worker and instance -- and a restart -- sees the same conversation.
CREATE TABLE IF NOT EXISTS conversation_state (
    subject_key       TEXT NOT NULL,
    conversation_id   TEXT NOT NULL,
    state             TEXT NOT NULL,
    last_activity_at  REAL NOT NULL,
    PRIMARY KEY (subject_key, conversation_id)
);
CREATE INDEX IF NOT EXISTS idx_conversation_activity ON conversation_state (last_activity_at);
"""


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _column(row: "Any", name: str) -> str | None:
    """
    One column, or None when this database predates it.

    A store file written before a column existed still opens; `initialise`
    adds the column, but a connection opened against a read-only copy or an
    unmigrated file must not raise IndexError on a SELECT *.
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


#: Columns added after the first release, and the migration that adds them.
#: Each is nullable with no default, so adding one to a populated table is a
#: metadata-only change and cannot rewrite or lose a row.
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "applications": [
        ("employment_type", "TEXT"),
        ("policy_id", "TEXT"),
        ("policy_version", "TEXT"),
        ("policy_pinned_at", "TEXT"),
        ("co_applicant_id", "TEXT"),
        # Affordability inputs. Nullable with no default: an application
        # written before these existed keeps them NULL, which the
        # eligibility check reports as not captured -- the truth about
        # that row.
        ("tenure_months", "TEXT"),
        ("interest_rate_pct", "TEXT"),
        ("declared_monthly_obligations", "TEXT"),
        ("property_value", "TEXT"),
        # Declared income (credit underwriting). Nullable: existing rows keep
        # NULL, which underwriting reports as "not declared".
        ("declared_monthly_income", "TEXT"),
    ],
    "case_findings": [
        # When the row was LAST written. Nullable: an existing row keeps
        # NULL and is ordered by `created_at`, which is the truth about
        # it -- it has only ever been written once as far as it knows.
        ("updated_at", "TEXT"),
    ],
    "documents": [
        # Nullable with no default: an existing row keeps party_id NULL and
        # `Document.owner_id` falls back to applicant_id, which is exactly
        # what that row meant when it was written.
        ("party_id", "TEXT"),
        ("party_role", "TEXT"),
    ],
}


#: Indexes created AFTER the column migration, not inside _SCHEMA.
#:
#: CREATE TABLE IF NOT EXISTS does nothing to a table that already exists,
#: so on an upgraded database the new column does not exist yet when the
#: schema script runs. An index over it inside _SCHEMA therefore failed
#: with "no such column: party_id" and took the whole `initialise` down --
#: every existing deployment would have refused to start. Indexes over
#: added columns belong here, after `_add_missing_columns` has run.
_ADDED_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_documents_party "
    "ON documents (case_id, party_id)",
)


def _parse(value: str | None) -> datetime:
    if not value:
        return utcnow()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return utcnow()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class SqlRepository(Repository):
    """Every query of the repository contract; the backend supplies the connection."""

    #: The backend's name, reported by health() and at startup.
    backend = "sql"

    def __init__(self) -> None:
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialised = False

    # -- connection: supplied by the backend ---------------------------------

    def _connect(self) -> Any:
        """One connection per thread, created on first use in that thread."""
        raise NotImplementedError

    def initialise(self) -> None:
        """Bring the schema to the current migration (the backend's own)."""
        raise NotImplementedError

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except StoreDbError:
                pass
            self._local.conn = None

    def health(self) -> dict[str, Any]:
        try:
            self._connect().execute("SELECT 1").fetchone()
            return {"backend": self.backend, "available": True}
        except Exception as exc:  # noqa: BLE001
            return {"backend": self.backend, "available": False, "error": type(exc).__name__}

    # -- applicants --------------------------------------------------------

    def get_applicant(self, applicant_id: str) -> Applicant | None:
        row = self._one("SELECT * FROM applicants WHERE applicant_id = ?",
                        (applicant_id,))
        return self._applicant(row) if row else None

    def save_applicant(self, applicant: Applicant) -> Applicant:
        applicant.updated_at = utcnow()
        self._write(
            """
            INSERT INTO applicants (applicant_id, full_name, mobile, email,
                                    date_of_birth, address, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(applicant_id) DO UPDATE SET
                full_name     = excluded.full_name,
                mobile        = excluded.mobile,
                email         = excluded.email,
                date_of_birth = excluded.date_of_birth,
                address       = excluded.address,
                updated_at    = excluded.updated_at
            """,
            (applicant.applicant_id, applicant.full_name, applicant.mobile,
             applicant.email, applicant.date_of_birth, applicant.address,
             _iso(applicant.created_at), _iso(applicant.updated_at)),
        )
        return applicant

    def list_applicants(self, limit: int = 50) -> list[Applicant]:
        rows = self._all(
            "SELECT * FROM applicants ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        )
        return [self._applicant(r) for r in rows]

    # -- applications ------------------------------------------------------

    def get_application(self, case_id: str) -> Application | None:
        row = self._one("SELECT * FROM applications WHERE case_id = ?", (case_id,))
        return self._application(row) if row else None

    def save_application(self, application: Application) -> Application:
        application.updated_at = utcnow()
        self._write(
            """
            INSERT INTO applications (case_id, applicant_id, status, product,
                                      loan_amount, tenure_months,
                                      interest_rate_pct,
                                      declared_monthly_obligations,
                                      declared_monthly_income,
                                      property_value,
                                      employment_type,
                                      co_applicant_id,
                                      policy_id, policy_version,
                                      policy_pinned_at,
                                      created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(case_id) DO UPDATE SET
                status           = excluded.status,
                product          = excluded.product,
                loan_amount      = excluded.loan_amount,
                tenure_months    = excluded.tenure_months,
                interest_rate_pct = excluded.interest_rate_pct,
                declared_monthly_obligations =
                    excluded.declared_monthly_obligations,
                declared_monthly_income = excluded.declared_monthly_income,
                property_value   = excluded.property_value,
                employment_type  = excluded.employment_type,
                co_applicant_id  = excluded.co_applicant_id,
                policy_id        = excluded.policy_id,
                policy_version   = excluded.policy_version,
                policy_pinned_at = excluded.policy_pinned_at,
                updated_at       = excluded.updated_at
            """,
            (application.case_id, application.applicant_id,
             application.status.value, application.product,
             application.loan_amount, application.tenure_months,
             application.interest_rate_pct,
             application.declared_monthly_obligations,
             getattr(application, "declared_monthly_income", None),
             application.property_value,
             application.employment_type,
             application.co_applicant_id,
             application.policy_id, application.policy_version,
             (_iso(application.policy_pinned_at)
              if application.policy_pinned_at else None),
             _iso(application.created_at),
             _iso(application.updated_at)),
        )
        return application

    def list_applications(self, applicant_id: str) -> list[Application]:
        rows = self._all(
            "SELECT * FROM applications WHERE applicant_id = ? "
            "ORDER BY updated_at DESC",
            (applicant_id,),
        )
        return [self._application(r) for r in rows]

    # -- documents ---------------------------------------------------------

    def get_document(self, document_id: str) -> Document | None:
        row = self._one("SELECT * FROM documents WHERE document_id = ?",
                        (document_id,))
        return self._document(row) if row else None

    def save_document(self, document: Document) -> Document:
        document.updated_at = utcnow()
        self._write(
            """
            INSERT INTO documents (document_id, case_id, applicant_id,
                                   party_id, party_role,
                                   document_type, status, source_id,
                                   verification_status, reason_codes,
                                   extracted_fields, uploaded_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                document_type       = excluded.document_type,
                status              = excluded.status,
                source_id           = excluded.source_id,
                verification_status = excluded.verification_status,
                reason_codes        = excluded.reason_codes,
                extracted_fields    = excluded.extracted_fields,
                updated_at          = excluded.updated_at
            -- OWNERSHIP IS NOT UPDATED ON CONFLICT, DELIBERATELY.
            -- The document key already contains the party, so a conflict
            -- means the SAME party re-uploaded the SAME file. An UPDATE
            -- that could rewrite party_id would be a path for one
            -- person's document to change hands, which is precisely what
            -- the party model exists to prevent.
            """,
            (document.document_id, document.case_id, document.applicant_id,
             document.party_id, document.party_role,
             document.document_type, document.status.value, document.source_id,
             document.verification_status, json.dumps(document.reason_codes),
             json.dumps(document.extracted_fields, default=str),
             _iso(document.uploaded_at), _iso(document.updated_at)),
        )
        return document

    def list_documents(self, case_id: str, *, include_superseded: bool = False) -> list[Document]:
        # A SUPERSEDED row is a replaced upload kept for audit: not a case
        # document, so not listed unless asked for (models.DocumentStatus).
        rows = self._all(
            "SELECT * FROM documents WHERE case_id = ?"
            + ("" if include_superseded else " AND status != 'SUPERSEDED'")
            + " ORDER BY uploaded_at",
            (case_id,),
        )
        return [self._document(r) for r in rows]

    def list_documents_for_cases(self, case_ids: list[str]) -> dict[str, list[Document]]:
        """ONE query for every case's documents (no N+1 over a portfolio)."""
        wanted = [c for c in dict.fromkeys(case_ids) if c]
        out: dict[str, list[Document]] = {c: [] for c in wanted}
        if not wanted:
            return out
        marks = ",".join("?" for _ in wanted)
        for row in self._all(f"SELECT * FROM documents WHERE case_id IN ({marks}) "
                             "AND status != 'SUPERSEDED' ORDER BY uploaded_at",
                             tuple(wanted)):
            document = self._document(row)
            out.setdefault(document.case_id, []).append(document)
        return out

    def get_current_findings_for_cases(self, case_ids: list[str],
                                       kind: "FindingKind | str | None" = None
                                       ) -> dict[str, list[CaseFinding]]:
        """ONE query for every case's findings, then the current row of each."""
        from app.store.repository import current_findings

        wanted = [c for c in dict.fromkeys(case_ids) if c]
        grouped: dict[str, list[CaseFinding]] = {c: [] for c in wanted}
        if not wanted:
            return grouped
        marks = ",".join("?" for _ in wanted)
        sql = f"SELECT * FROM case_findings WHERE case_id IN ({marks})"
        args: list[object] = list(wanted)
        if kind:
            sql += " AND finding_kind = ?"
            args.append(_kind_value(kind))
        for row in self._all(sql + " ORDER BY created_at, rowid", tuple(args)):
            finding = self._finding(row)
            grouped.setdefault(finding.case_id, []).append(finding)
        return {c: current_findings(f) for c, f in grouped.items()}

    # -- plumbing ----------------------------------------------------------

    # -- access grants -------------------------------------------------------

    def grant_access(self, subject: str, resource_type: str,
                     resource_id: str) -> None:
        if not subject or not resource_id:
            return
        self._write(
            "INSERT OR IGNORE INTO access_grants (subject, resource_type, "
            "resource_id, granted_at) VALUES (?, ?, ?, ?)",
            (str(subject), str(resource_type).upper(), str(resource_id),
             _iso(utcnow())),
        )

    def has_access(self, subject: str, resource_type: str,
                   resource_id: str) -> bool:
        if not subject or not resource_id:
            return False
        return self._one(
            "SELECT 1 FROM access_grants WHERE subject = ? AND "
            "resource_type = ? AND resource_id = ?",
            (str(subject), str(resource_type).upper(), str(resource_id)),
        ) is not None

    def _one(self, sql: str, args: tuple) -> Any | None:
        self.initialise()
        try:
            return self._connect().execute(sql, args).fetchone()
        except StoreDbError as exc:
            raise RepositoryError(f"Case store read failed: {exc}") from exc

    def _all(self, sql: str, args: tuple) -> list[Any]:
        self.initialise()
        try:
            return list(self._connect().execute(sql, args).fetchall())
        except StoreDbError as exc:
            raise RepositoryError(f"Case store read failed: {exc}") from exc

    def _write(self, sql: str, args: tuple) -> None:
        self.initialise()
        conn = self._connect()
        try:
            conn.execute(sql, args)
            conn.commit()
        except StoreDbError as exc:
            conn.rollback()
            raise RepositoryError(f"Case store write failed: {exc}") from exc

    # -- row -> model ------------------------------------------------------

    @staticmethod
    def _applicant(row: Any) -> Applicant:
        return Applicant(
            applicant_id=row["applicant_id"],
            full_name=row["full_name"],
            mobile=row["mobile"],
            email=row["email"],
            date_of_birth=row["date_of_birth"],
            address=row["address"],
            created_at=_parse(row["created_at"]),
            updated_at=_parse(row["updated_at"]),
        )

    @staticmethod
    def _application(row: Any) -> Application:
        try:
            status = ApplicationStatus(row["status"])
        except ValueError:
            status = ApplicationStatus.APPLICATION_CREATED
        return Application(
            case_id=row["case_id"],
            applicant_id=row["applicant_id"],
            status=status,
            product=row["product"],
            loan_amount=row["loan_amount"],
            employment_type=_column(row, "employment_type"),
            co_applicant_id=_column(row, "co_applicant_id"),
            tenure_months=_column(row, "tenure_months"),
            interest_rate_pct=_column(row, "interest_rate_pct"),
            declared_monthly_obligations=_column(
                row, "declared_monthly_obligations"),
            declared_monthly_income=_column(row, "declared_monthly_income"),
            property_value=_column(row, "property_value"),
            policy_id=_column(row, "policy_id"),
            policy_version=_column(row, "policy_version"),
            policy_pinned_at=(_parse(pinned)
                              if (pinned := _column(row, "policy_pinned_at"))
                              else None),
            created_at=_parse(row["created_at"]),
            updated_at=_parse(row["updated_at"]),
        )

    @staticmethod
    def _document(row: Any) -> Document:
        try:
            status = DocumentStatus(row["status"])
        except ValueError:
            status = DocumentStatus.UPLOADED

        def _load(raw: str, fallback):
            try:
                return json.loads(raw)
            except (TypeError, ValueError):
                return fallback

        return Document(
            document_id=row["document_id"],
            case_id=row["case_id"],
            applicant_id=row["applicant_id"],
            party_id=_column(row, "party_id"),
            party_role=(_column(row, "party_role") or "PRIMARY_APPLICANT"),
            document_type=row["document_type"],
            status=status,
            source_id=row["source_id"],
            verification_status=row["verification_status"],
            reason_codes=_load(row["reason_codes"], []),
            extracted_fields=_load(row["extracted_fields"], {}),
            uploaded_at=_parse(row["uploaded_at"]),
            updated_at=_parse(row["updated_at"]),
        )


    # ==================================================================
    # CASE MEMORY
    #
    # Reads are CASE-SCOPED BY CONSTRUCTION: every SELECT below starts
    # from a case_id and no method can return a row from another case.
    # That is a data-access property, not an authorisation check -- the
    # caller still has to have passed ownership before asking.
    # ==================================================================

    def save_finding(self, finding: CaseFinding) -> CaseFinding:
        """
        Record one conclusion.

        IDEMPOTENT ON CONTENT. The unique index over
        (case_id, kind, party, source, content_hash) means a re-run that
        concluded the same thing updates the existing row instead of
        appending a second one -- a case processed twice should not read
        as a case that changed its mind.
        """
        self._write(
            """
            INSERT INTO case_findings (
                finding_id, case_id, party_id, finding_kind, stage, status,
                score, confidence, reason_codes, payload, source_type,
                source_id, document_id, created_at, version, content_hash,
                updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(case_id, finding_kind, IFNULL(party_id, ''),
                        IFNULL(source_id, ''), IFNULL(content_hash, ''))
            DO UPDATE SET
                status       = excluded.status,
                score        = excluded.score,
                confidence   = excluded.confidence,
                reason_codes = excluded.reason_codes,
                payload      = excluded.payload,
                stage        = excluded.stage,
                document_id  = excluded.document_id,
                version      = case_findings.version + 1,
                updated_at   = excluded.updated_at
            """,
            (finding.finding_id, finding.case_id, finding.party_id,
             _kind_value(finding.finding_kind), finding.stage, finding.status,
             finding.score, finding.confidence,
             json.dumps(list(finding.reason_codes or [])),
             # document values encrypted at rest (app/store/crypto.py)
             _crypto.seal(_kind_value(finding.finding_kind), finding.payload or {}),
             finding.source_type, finding.source_id, finding.document_id,
             _iso(finding.created_at), finding.version, finding.content_hash,
             _iso(utcnow())),
        )
        return finding

    def get_case_findings(
        self,
        case_id: str,
        party_id: str | None = None,
        kind: "FindingKind | str | None" = None,
    ) -> list[CaseFinding]:
        """
        Findings for one case, oldest first.

        `party_id` narrows to that party AND to case-level findings that
        belong to no single party -- a cross-document check is the case's,
        not one person's, and hiding it when a party is named would lose
        it. It never widens past the case.
        """
        sql = "SELECT * FROM case_findings WHERE case_id = ?"
        args: list[object] = [case_id]

        if party_id:
            sql += " AND (party_id = ? OR party_id IS NULL)"
            args.append(party_id)
        if kind:
            sql += " AND finding_kind = ?"
            args.append(_kind_value(kind))

        sql += " ORDER BY created_at, rowid"
        return [self._finding(row) for row in self._all(sql, tuple(args))]

    # ======================================================================
    # THE OCR QUEUE
    #
    # Work that outlives the request that asked for it. See
    # app/store/ocr_queue.py for why it is durable rather than a list.
    # ======================================================================

    def save_ocr_job(self, job: "OcrJob") -> "OcrJob":
        """Insert or update one job. Keyed by document, not by attempt."""
        job.updated_at = utcnow()
        self._write(
            """
            INSERT INTO ocr_jobs (
                job_id, document_id, case_id, applicant_id, party_id,
                document_type, status, attempts, detail, created_at,
                updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                job_id     = excluded.job_id,
                status     = excluded.status,
                attempts   = excluded.attempts,
                detail     = excluded.detail,
                updated_at = excluded.updated_at
            """,
            (job.job_id, job.document_id, job.case_id, job.applicant_id,
             job.party_id, job.document_type, job.status.value, job.attempts,
             job.detail, _iso(job.created_at), _iso(job.updated_at)),
        )
        return job

    def get_ocr_job(self, document_id: str) -> "OcrJob | None":
        row = self._one("SELECT * FROM ocr_jobs WHERE document_id = ?",
                        (document_id,))
        return self._ocr_job(row) if row else None

    def find_ocr_job(self, job_id: str) -> "OcrJob | None":
        """One job by its own id (GET /jobs/{job_id}); the caller authorizes its case."""
        row = self._one("SELECT * FROM ocr_jobs WHERE job_id = ?", (job_id,))
        return self._ocr_job(row) if row else None

    def get_ocr_jobs(self, case_id: str) -> list["OcrJob"]:
        rows = self._all(
            "SELECT * FROM ocr_jobs WHERE case_id = ? ORDER BY created_at",
            (case_id,),
        )
        return [self._ocr_job(row) for row in rows]

    def reclaim_stale_ocr_jobs(self, older_than_seconds: float,
                               max_attempts: int) -> int:
        """
        Put ORPHANED jobs back to work.

        A job is claimed by moving it to PROCESSING; a worker that dies
        mid-job (crash, kill, deploy) leaves it there, and `claim_ocr_job`
        never looks at PROCESSING rows -- so the document was "being read"
        for ever. A PROCESSING row untouched for longer than the worker's
        lease is orphaned: it goes back to QUEUED for another attempt, or
        to FAILED when its attempts are spent. Returns how many moved.
        """
        from datetime import timedelta

        from app.store.ocr_queue import OcrJobStatus

        cutoff = _iso(utcnow() - timedelta(seconds=older_than_seconds))
        now = _iso(utcnow())
        failed = self._write_count(
            "UPDATE ocr_jobs SET status = ?, detail = ?, updated_at = ? "
            "WHERE status = ? AND updated_at < ? AND attempts >= ?",
            (OcrJobStatus.FAILED.value,
             "The background read was interrupted and its attempts are spent; "
             "the document needs manual review.",
             now, OcrJobStatus.PROCESSING.value, cutoff, max_attempts),
        )
        requeued = self._write_count(
            "UPDATE ocr_jobs SET status = ?, detail = ?, updated_at = ? "
            "WHERE status = ? AND updated_at < ?",
            (OcrJobStatus.QUEUED.value,
             "The background read was interrupted; it has been queued again.",
             now, OcrJobStatus.PROCESSING.value, cutoff),
        )
        return failed + requeued

    def claim_ocr_job(self) -> "OcrJob | None":
        """
        Take the oldest queued job, marking it PROCESSING.

        THE CLAIM IS A SINGLE WRITE, so two workers cannot take the
        same job: the UPDATE names the status it expects to replace,
        and only one of them changes a row. One worker runs today,
        which is exactly when a claim like this is cheap to get right
        and expensive to retrofit.
        """
        from app.store.ocr_queue import OcrJobStatus

        while True:
            row = self._one(
                "SELECT * FROM ocr_jobs WHERE status = ? "
                "ORDER BY created_at LIMIT 1",
                (OcrJobStatus.QUEUED.value,),
            )
            if row is None:
                return None

            job = self._ocr_job(row)
            taken = self._write_count(
                "UPDATE ocr_jobs SET status = ?, attempts = ?, "
                "updated_at = ? WHERE job_id = ? AND status = ?",
                (OcrJobStatus.PROCESSING.value, job.attempts + 1,
                 _iso(utcnow()), job.job_id, OcrJobStatus.QUEUED.value),
            )
            if taken:
                job.status = OcrJobStatus.PROCESSING
                job.attempts += 1
                return job
            # Somebody else took it between the read and the write.

    def _write_count(self, sql: str, args: tuple) -> int:
        """A write that reports how many rows it changed."""
        self.initialise()
        conn = self._connect()
        try:
            cursor = conn.execute(sql, args)
            conn.commit()
            return int(cursor.rowcount or 0)
        except StoreDbError as exc:
            raise RepositoryError(f"Case store write failed: {exc}") from exc

    def _ocr_job(self, row) -> "OcrJob":
        from app.store.ocr_queue import OcrJob, OcrJobStatus

        try:
            status = OcrJobStatus(str(row["status"]))
        except ValueError:                             # pragma: no cover
            status = OcrJobStatus.FAILED

        return OcrJob(
            job_id=row["job_id"],
            document_id=row["document_id"],
            case_id=row["case_id"],
            applicant_id=row["applicant_id"],
            party_id=row["party_id"],
            document_type=row["document_type"],
            status=status,
            attempts=int(row["attempts"] or 0),
            detail=row["detail"],
            created_at=_parse(row["created_at"]),
            updated_at=_parse(row["updated_at"]),
        )

    def save_document_version(
        self, version: DocumentVersion
    ) -> DocumentVersion:
        """Record one upload. Re-recording the same version is a no-op."""
        self._write(
            """
            INSERT INTO document_versions (
                document_version_id, document_id, case_id, party_id,
                version, source_id, content_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id, version) DO UPDATE SET
                source_id    = excluded.source_id,
                content_hash = excluded.content_hash
            """,
            (version.document_version_id, version.document_id, version.case_id,
             version.party_id, version.version, version.source_id,
             version.content_hash, _iso(version.created_at)),
        )
        return version

    def get_document_versions(self, document_id: str) -> list[DocumentVersion]:
        """Every version of one document, oldest first."""
        return [
            self._document_version(row)
            for row in self._all(
                "SELECT * FROM document_versions WHERE document_id = ? "
                "ORDER BY version, rowid",
                (document_id,),
            )
        ]

    def save_decision(self, decision: CaseDecision) -> CaseDecision:
        """Record a decision the pipeline reached."""
        self._write(
            """
            INSERT INTO case_decisions (
                decision_id, case_id, decision, next_action, status,
                reason_codes, policy_id, policy_version, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(decision_id) DO UPDATE SET
                decision     = excluded.decision,
                next_action  = excluded.next_action,
                status       = excluded.status,
                reason_codes = excluded.reason_codes
            """,
            (decision.decision_id, decision.case_id, decision.decision,
             decision.next_action, decision.status,
             json.dumps(list(decision.reason_codes or [])),
             decision.policy_id, decision.policy_version,
             _iso(decision.created_at)),
        )
        return decision

    def get_case_decisions(self, case_id: str) -> list[CaseDecision]:
        """Decisions recorded for one case, oldest first."""
        return [
            self._decision(row)
            for row in self._all(
                "SELECT * FROM case_decisions WHERE case_id = ? "
                "ORDER BY created_at, rowid",
                (case_id,),
            )
        ]

    # -- Copilot conversation state (labels only) ---------------------------

    def get_conversation(self, subject_key: str, conversation_id: str) -> tuple[str, float] | None:
        row = self._one("SELECT state, last_activity_at FROM conversation_state "
                        "WHERE subject_key = ? AND conversation_id = ?", (subject_key, conversation_id))
        return (row["state"], float(row["last_activity_at"])) if row else None

    def put_conversation(self, subject_key: str, conversation_id: str, state: str, at: float) -> None:
        self._write("""INSERT INTO conversation_state (subject_key, conversation_id, state, last_activity_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(subject_key, conversation_id) DO UPDATE SET
                           state = excluded.state, last_activity_at = excluded.last_activity_at""",
                    (subject_key, conversation_id, state, at))

    def delete_conversations(self, *, older_than: float | None = None,
                             subject_key: str | None = None, conversation_id: str | None = None) -> int:
        if conversation_id is not None:
            return self._write_count("DELETE FROM conversation_state WHERE subject_key = ? AND conversation_id = ?",
                                     (subject_key, conversation_id))
        if older_than is not None:
            return self._write_count("DELETE FROM conversation_state WHERE last_activity_at < ?", (older_than,))
        return self._write_count("DELETE FROM conversation_state", ())

    # -- Maker / Checker approvals ----------------------------------------

    _APPROVAL_COLUMNS = ("approval_id", "case_id", "action_type", "resource_type", "resource_id", "maker_id",
                         "checker_id", "status", "reason", "comments", "payload", "result", "policy_reference",
                         "evidence_version", "version", "created_at", "updated_at", "expires_at")

    def save_approval(self, approval: dict) -> None:
        row = {**approval, "payload": json.dumps(approval.get("payload") or {}),
               "result": json.dumps(approval["result"]) if approval.get("result") is not None else None}
        cols = self._APPROVAL_COLUMNS
        self._write(f"INSERT INTO approvals ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                    tuple(row.get(c) for c in cols))

    def update_approval(self, approval: dict, *, expected_version: int) -> bool:
        """Compare-and-set on `version`: False when another writer changed it first."""
        changed = self._write_count(
            """UPDATE approvals SET checker_id = ?, status = ?, comments = ?, result = ?,
                   version = ?, updated_at = ? WHERE approval_id = ? AND version = ?""",
            (approval.get("checker_id"), approval["status"], approval.get("comments"),
             json.dumps(approval["result"]) if approval.get("result") is not None else None,
             expected_version + 1, approval["updated_at"], approval["approval_id"], expected_version))
        return changed > 0

    def _approval(self, row) -> dict:
        out = {k: row[k] for k in row.keys()}
        out["payload"] = json.loads(out.get("payload") or "{}")
        out["result"] = json.loads(out["result"]) if out.get("result") else None
        return out

    def get_approval(self, approval_id: str) -> dict | None:
        row = self._one("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,))
        return self._approval(row) if row else None

    def list_approvals(self, case_id: str) -> list[dict]:
        return [self._approval(r) for r in
                self._all("SELECT * FROM approvals WHERE case_id = ? ORDER BY created_at", (case_id,))]

    # -- JEV decision runs (append-only) ---------------------------------

    def save_jev_run(self, run: dict) -> bool:
        """Append one run. False when its evaluation_key was already recorded."""
        changed = self._write_count(
            """
            INSERT INTO jev_runs (
                jev_run_id, evaluation_key, case_id, party_id, stage,
                evaluation_scope, question_set_version, evidence_version,
                trigger, provider, model, status, decisions, actions,
                error_code, latency_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(evaluation_key) DO NOTHING
            """,
            (run["jev_run_id"], run["evaluation_key"], run["case_id"], run.get("party_id"),
             run.get("stage"), run["evaluation_scope"], run["question_set_version"],
             run["evidence_version"], run.get("trigger"), run["provider"], run.get("model"),
             run["status"], json.dumps(run.get("decisions") or []), json.dumps(run.get("actions") or []),
             run.get("error_code"), run.get("latency_ms"), run["created_at"]),
        )
        return changed > 0

    def _jev_run(self, row) -> dict:
        out = {k: row[k] for k in row.keys()}
        out["decisions"] = json.loads(out.get("decisions") or "[]")
        out["actions"] = json.loads(out.get("actions") or "[]")
        return out

    def find_jev_run(self, evaluation_key: str) -> dict | None:
        row = self._one("SELECT * FROM jev_runs WHERE evaluation_key = ?", (evaluation_key,))
        return self._jev_run(row) if row else None

    def list_jev_runs(self, case_id: str, party_id: str | None = None) -> list[dict]:
        """Every run on a case (one party's when given), oldest first."""
        if party_id is None:
            rows = self._all("SELECT * FROM jev_runs WHERE case_id = ? ORDER BY created_at", (case_id,))
        else:
            rows = self._all("SELECT * FROM jev_runs WHERE case_id = ? AND IFNULL(party_id, '') = ? "
                             "ORDER BY created_at", (case_id, party_id))
        return [self._jev_run(r) for r in rows]

    def record_event(self, event: CaseEvent) -> CaseEvent:
        """
        Append one event to the timeline.

        `sequence` is assigned here when the caller did not set one, so
        two events written in the same millisecond still have an order.
        """
        if not event.sequence:
            row = self._one(
                "SELECT COALESCE(MAX(sequence), 0) AS s FROM case_events "
                "WHERE case_id = ?",
                (event.case_id,),
            )
            event.sequence = int((row["s"] if row else 0) or 0) + 1

        self._write(
            """
            INSERT INTO case_events (
                event_id, case_id, party_id, event_type, stage, summary,
                ref_id, created_at, sequence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (event.event_id, event.case_id, event.party_id, event.event_type,
             event.stage, event.summary, event.ref_id,
             _iso(event.created_at), event.sequence),
        )
        return event

    def get_case_timeline(self, case_id: str) -> list[CaseEvent]:
        """The case timeline, in the order things happened."""
        return [
            self._event(row)
            for row in self._all(
                "SELECT * FROM case_events WHERE case_id = ? "
                "ORDER BY sequence, rowid",
                (case_id,),
            )
        ]

    # -- stage lifecycle -----------------------------------------------------

    def get_case_stage(self, case_id: str) -> CaseStage | None:
        row = self._one("SELECT * FROM case_stage WHERE case_id = ?",
                        (case_id,))
        if row is None:
            return None
        return CaseStage(
            case_id=row["case_id"], stage=row["stage"],
            stage_status=row["stage_status"],
            stage_started_at=_parse(row["stage_started_at"]),
            updated_at=_parse(row["updated_at"]),
            version=int(row["version"]),
        )

    def get_stage_transitions(self, case_id: str) -> list[StageTransition]:
        return [self._transition(row) for row in self._all(
            "SELECT * FROM stage_transitions WHERE case_id = ? "
            "ORDER BY version", (case_id,))]

    def get_stage_transition(self, transition_id: str) -> StageTransition | None:
        row = self._one("SELECT * FROM stage_transitions "
                        "WHERE transition_id = ?", (transition_id,))
        return self._transition(row) if row is not None else None

    def apply_stage_transition(self, expected_version: int, state: CaseStage,
                               transition: StageTransition,
                               event: CaseEvent) -> bool:
        """
        One IMMEDIATE transaction: the write lock is taken before the
        version is read, so no other writer can slip between the check and
        the write. Everything or nothing.
        """
        self.initialise()
        conn = self._connect()
        try:
            if conn.in_transaction:
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            # SERIALISED PER CASE, ON POSTGRES. SQLite's IMMEDIATE took a write
            # lock before the version was read; translated to a plain BEGIN it
            # takes none. Correctness never depended on it -- the unique
            # (case, version) constraint lets exactly one concurrent move commit
            # -- but without a lock the losers did all their work and then
            # rolled back. A per-case transaction-scoped advisory lock makes them
            # wait and see the new version instead (other cases untouched;
            # released at COMMIT/ROLLBACK; covers the first move too).
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"case_stage:{state.case_id}",))
            row = conn.execute(
                "SELECT version FROM case_stage WHERE case_id = ?",
                (state.case_id,)).fetchone()
            current = int(row["version"]) if row is not None else 0
            if current != expected_version:
                conn.rollback()
                return False

            conn.execute(
                """
                INSERT INTO case_stage (case_id, stage, stage_status,
                    stage_started_at, updated_at, version)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    stage = excluded.stage,
                    stage_status = excluded.stage_status,
                    stage_started_at = excluded.stage_started_at,
                    updated_at = excluded.updated_at,
                    version = excluded.version
                """,
                (state.case_id, state.stage, state.stage_status,
                 _iso(state.stage_started_at), _iso(state.updated_at),
                 state.version),
            )
            conn.execute(
                """
                INSERT INTO stage_transitions (transition_id, case_id,
                    version, kind, from_stage, to_stage, from_status,
                    to_status, previous_stage_started_at, source, actor,
                    reason, request_id, correlation_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (transition.transition_id, transition.case_id,
                 transition.version, transition.kind, transition.from_stage,
                 transition.to_stage, transition.from_status,
                 transition.to_status,
                 _iso(transition.previous_stage_started_at)
                 if transition.previous_stage_started_at else None,
                 transition.source, transition.actor, transition.reason,
                 transition.request_id, transition.correlation_id,
                 _iso(transition.created_at)),
            )
            # THE SAME TRANSITION ON THE CASE TIMELINE, so every existing
            # case-history view shows it. Sequenced inside the lock.
            seq = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS s FROM case_events "
                "WHERE case_id = ?", (event.case_id,)).fetchone()
            event.sequence = int((seq["s"] if seq else 0) or 0) + 1
            conn.execute(
                """
                INSERT INTO case_events (event_id, case_id, party_id,
                    event_type, stage, summary, ref_id, created_at, sequence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event.event_id, event.case_id, event.party_id,
                 event.event_type, event.stage, event.summary, event.ref_id,
                 _iso(event.created_at), event.sequence),
            )
            conn.commit()
            return True
        except StoreIntegrityError:
            # Another writer recorded this version (or this transition id)
            # first. Nothing of ours was written.
            conn.rollback()
            return False
        except StoreDbError as exc:
            conn.rollback()
            raise RepositoryError(f"Case store write failed: {exc}") from exc

    @staticmethod
    def _transition(row: "Any") -> StageTransition:
        previous = _column(row, "previous_stage_started_at")
        return StageTransition(
            transition_id=row["transition_id"], case_id=row["case_id"],
            version=int(row["version"]), kind=row["kind"],
            from_stage=_column(row, "from_stage"), to_stage=row["to_stage"],
            from_status=_column(row, "from_status"),
            to_status=row["to_status"],
            previous_stage_started_at=_parse(previous) if previous else None,
            source=row["source"], actor=_column(row, "actor"),
            reason=_column(row, "reason"),
            request_id=_column(row, "request_id"),
            correlation_id=_column(row, "correlation_id"),
            created_at=_parse(row["created_at"]),
        )

    # -- row mappers -------------------------------------------------------

    @staticmethod
    def _finding(row: "Any") -> CaseFinding:
        return CaseFinding(
            finding_id=row["finding_id"],
            case_id=row["case_id"],
            finding_kind=_kind(row["finding_kind"]),
            party_id=_column(row, "party_id"),
            stage=_column(row, "stage"),
            status=_column(row, "status"),
            score=_column(row, "score"),
            confidence=_column(row, "confidence"),
            reason_codes=_json_list(_column(row, "reason_codes")),
            payload=_json_dict(_crypto.open_(_column(row, "payload"))),
            source_type=_column(row, "source_type"),
            source_id=_column(row, "source_id"),
            document_id=_column(row, "document_id"),
            created_at=_parse(_column(row, "created_at")),
            version=int(_column(row, "version") or 1),
            content_hash=_column(row, "content_hash"),
            updated_at=(_parse(_column(row, "updated_at"))
                        if _column(row, "updated_at") else None),
        )

    @staticmethod
    def _document_version(row: "Any") -> DocumentVersion:
        return DocumentVersion(
            document_version_id=row["document_version_id"],
            document_id=row["document_id"],
            case_id=row["case_id"],
            party_id=_column(row, "party_id"),
            version=int(_column(row, "version") or 1),
            source_id=_column(row, "source_id"),
            content_hash=_column(row, "content_hash"),
            created_at=_parse(_column(row, "created_at")),
        )

    @staticmethod
    def _decision(row: "Any") -> CaseDecision:
        return CaseDecision(
            decision_id=row["decision_id"],
            case_id=row["case_id"],
            decision=_column(row, "decision"),
            next_action=_column(row, "next_action"),
            status=_column(row, "status"),
            reason_codes=_json_list(_column(row, "reason_codes")),
            policy_id=_column(row, "policy_id"),
            policy_version=_column(row, "policy_version"),
            created_at=_parse(_column(row, "created_at")),
        )

    @staticmethod
    def _event(row: "Any") -> CaseEvent:
        return CaseEvent(
            event_id=row["event_id"],
            case_id=row["case_id"],
            party_id=_column(row, "party_id"),
            event_type=row["event_type"],
            stage=_column(row, "stage"),
            summary=_column(row, "summary"),
            ref_id=_column(row, "ref_id"),
            created_at=_parse(_column(row, "created_at")),
            sequence=int(_column(row, "sequence") or 0),
        )


def _kind_value(kind) -> str:
    """A FindingKind or its string, as stored."""
    return kind.value if isinstance(kind, FindingKind) else str(kind)


def _kind(value: str | None) -> FindingKind:
    """
    Stored kind back to the enum.

    An unrecognised kind is reported as VERIFICATION rather than raising:
    a row written by a newer version must not make an older reader fall
    over on an ordinary SELECT.
    """
    try:
        return FindingKind(str(value))
    except ValueError:
        return FindingKind.VERIFICATION


def _json_list(value: str | None) -> list:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _json_dict(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = ["SqlRepository", "StoreDbError", "StoreIntegrityError"]
