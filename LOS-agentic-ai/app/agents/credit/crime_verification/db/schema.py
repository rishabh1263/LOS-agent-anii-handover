"""
The agent's database schema, as ordered migrations (append only; each runs once, in one transaction).

The tables live in the agent's OWN database -- never in the LOS case store.
"""

from __future__ import annotations

from app.agent_db import LOGS_MIGRATION, responses, rule_sets

_V1 = """
CREATE TABLE crime_verification_applications (
    application_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL,
    product TEXT NOT NULL,
    loan_amount NUMERIC(14, 2) NOT NULL CHECK (loan_amount >= 0),
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE crime_verification_parties (
    application_id TEXT NOT NULL REFERENCES crime_verification_applications(application_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('PRIMARY', 'CO_APPLICANT')),
    full_name TEXT NOT NULL DEFAULT '',
    father_name TEXT NOT NULL DEFAULT '',
    date_of_birth DATE,
    permanent_address TEXT NOT NULL DEFAULT '',
    pan TEXT NOT NULL,
    demo_scenario TEXT,
    PRIMARY KEY (application_id, role),
    UNIQUE (application_id, pan)
);

CREATE TABLE crime_verification_cases (
    case_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES crime_verification_applications(application_id) ON DELETE CASCADE,
    vendor TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('INITIATED', 'PROCESSING', 'COMPLETED', 'REVIEW_REQUIRED', 'FAILED',
                                           'WAIVED')),
    kind TEXT NOT NULL DEFAULT 'INITIAL' CHECK (kind IN ('INITIAL', 'RETRIGGER', 'RETRY', 'WAIVER')),
    idempotency_key TEXT UNIQUE,
    request_hash TEXT,
    initiated_by TEXT NOT NULL,
    initiated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    waived_reason TEXT,
    final_decision TEXT,
    final_remarks TEXT,
    finalized_by TEXT,
    finalized_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (case_id, application_id)
);
CREATE INDEX crime_verification_cases_app ON crime_verification_cases(application_id, created_at DESC);
CREATE INDEX crime_verification_cases_status ON crime_verification_cases(status);

-- a subject belongs to its case AND to the same application's party of that role: the two composite foreign keys make
-- it impossible to attach a person to the wrong application
CREATE TABLE crime_verification_subjects (
    subject_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    application_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('PRIMARY', 'CO_APPLICANT')),
    full_name TEXT NOT NULL,
    father_name TEXT NOT NULL,
    date_of_birth DATE NOT NULL,
    permanent_address TEXT NOT NULL,
    pan TEXT NOT NULL,
    pan_masked TEXT NOT NULL,
    snapshot JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('INITIATED', 'PROCESSING', 'COMPLETED', 'REVIEW_REQUIRED', 'FAILED',
                                           'WAIVED')),
    demo_scenario TEXT,
    provider_reference TEXT,
    provider_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (case_id, application_id) REFERENCES crime_verification_cases(case_id, application_id) ON DELETE CASCADE,
    FOREIGN KEY (application_id, role) REFERENCES crime_verification_parties(application_id, role) ON DELETE CASCADE,
    UNIQUE (case_id, role)
);
-- never two verifications of the same person in flight at once (duplicate initiation)
CREATE UNIQUE INDEX crime_verification_subjects_active ON crime_verification_subjects(application_id, role)
    WHERE status IN ('INITIATED', 'PROCESSING');
CREATE INDEX crime_verification_subjects_app ON crime_verification_subjects(application_id, role, created_at DESC);

CREATE TABLE crime_verification_results (
    result_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL UNIQUE REFERENCES crime_verification_subjects(subject_id) ON DELETE CASCADE,
    provider_reference TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('NO_MATCH', 'FINDINGS')),
    risk_level TEXT NOT NULL CHECK (risk_level IN ('NO_RISK', 'LOW_RISK', 'AVERAGE_RISK', 'HIGH_RISK',
                                                   'VERY_HIGH_RISK')),
    total_cases INTEGER NOT NULL CHECK (total_cases >= 0),
    category_counts JSONB NOT NULL,
    cluster_counts JSONB NOT NULL,
    summary TEXT NOT NULL,
    payload JSONB NOT NULL,
    result_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE crime_verification_reports (
    report_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES crime_verification_cases(case_id) ON DELETE CASCADE,
    subject_id TEXT NOT NULL REFERENCES crime_verification_subjects(subject_id) ON DELETE CASCADE,
    result_id TEXT NOT NULL REFERENCES crime_verification_results(result_id) ON DELETE CASCADE,
    report_type TEXT NOT NULL,
    version TEXT NOT NULL,
    file_ref TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    generated_by TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (result_id, version)
);

CREATE TABLE crime_verification_audit (
    event_id BIGSERIAL PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES crime_verification_applications(application_id) ON DELETE CASCADE,
    case_id TEXT REFERENCES crime_verification_cases(case_id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    event_type TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX crime_verification_audit_case ON crime_verification_audit(case_id, event_id);
CREATE INDEX crime_verification_audit_app ON crime_verification_audit(application_id, event_id);
"""

_V2 = """
-- the plain-words summary of a case's results (the AI's when it passed the check, else written by rules)
CREATE TABLE crime_verification_summaries (
    case_id TEXT PRIMARY KEY REFERENCES crime_verification_cases(case_id) ON DELETE CASCADE,
    facts_hash TEXT NOT NULL,
    text TEXT,
    source TEXT NOT NULL CHECK (source IN ('ai', 'refused', 'unavailable')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX crime_verification_summaries_facts ON crime_verification_summaries(facts_hash);
-- what the stored PDF was written from besides the result (its summary): a newer summary -> the report is rewritten
ALTER TABLE crime_verification_reports ADD COLUMN content_key TEXT;
"""

_V3 = """
-- MORE THAN ONE CO-APPLICANT (owner 2026-10-27: "co-applicant 10 bhi ho sakte hain"): a person on an application is
-- (role, seq) -- the applicant is (PRIMARY, 0), co-applicants are (CO_APPLICANT, 1..N). Existing rows keep their
-- meaning: the one co-applicant becomes seq 1. The wrong-application and in-flight guards move to the new key.
ALTER TABLE crime_verification_parties ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;
UPDATE crime_verification_parties SET seq = 1 WHERE role = 'CO_APPLICANT';
ALTER TABLE crime_verification_subjects ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;
UPDATE crime_verification_subjects SET seq = 1 WHERE role = 'CO_APPLICANT';

ALTER TABLE crime_verification_subjects DROP CONSTRAINT IF EXISTS crime_verification_subjects_application_id_role_fkey;
ALTER TABLE crime_verification_subjects DROP CONSTRAINT IF EXISTS crime_verification_subjects_case_id_role_key;
ALTER TABLE crime_verification_parties DROP CONSTRAINT IF EXISTS crime_verification_parties_pkey;

ALTER TABLE crime_verification_parties ADD CONSTRAINT crime_verification_parties_pkey PRIMARY KEY (application_id, role, seq);
ALTER TABLE crime_verification_parties ADD CONSTRAINT crime_verification_parties_seq
    CHECK ((role = 'PRIMARY' AND seq = 0) OR (role = 'CO_APPLICANT' AND seq >= 1));
ALTER TABLE crime_verification_subjects ADD CONSTRAINT crime_verification_subjects_seq
    CHECK ((role = 'PRIMARY' AND seq = 0) OR (role = 'CO_APPLICANT' AND seq >= 1));
ALTER TABLE crime_verification_subjects ADD CONSTRAINT crime_verification_subjects_party_fkey
    FOREIGN KEY (application_id, role, seq) REFERENCES crime_verification_parties(application_id, role, seq)
    ON DELETE CASCADE;
ALTER TABLE crime_verification_subjects ADD CONSTRAINT crime_verification_subjects_case_party_key
    UNIQUE (case_id, role, seq);

DROP INDEX IF EXISTS crime_verification_subjects_active;
CREATE UNIQUE INDEX crime_verification_subjects_active ON crime_verification_subjects(application_id, role, seq)
    WHERE status IN ('INITIATED', 'PROCESSING');
DROP INDEX IF EXISTS crime_verification_subjects_app;
CREATE INDEX crime_verification_subjects_app ON crime_verification_subjects(application_id, role, seq, created_at DESC);
"""

#: (version, description, sql) -- append only
_V4 = """
-- demo rows are marked, so production can remove exactly them (scripts/setup_agent_databases.py --purge-demo):
-- everything else of an application (people, cases, results, reports, audit, summaries) goes with it (ON DELETE CASCADE)
ALTER TABLE crime_verification_applications ADD COLUMN is_demo BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX crime_verification_applications_demo ON crime_verification_applications(is_demo) WHERE is_demo;
"""

MIGRATIONS: list[tuple[str, str, str]] = [
    ("0001", "crime verification: applications, parties, cases, subjects, results, reports, audit", _V1),
    ("0002", "crime verification: the AI summary of a case's results", _V2),
    ("0003", "crime verification: up to N co-applicants -- a person is (role, seq)", _V3),
    ("0004", "crime verification: is_demo on applications (the demo rows production removes)", _V4),
    ("0005", "agent_logs: WARNING and above application logs (shared, app/agent_db)", LOGS_MIGRATION[2]),
    # the policy each result was judged with (rule_sets, rule_set_hash on results); the audit trail append-only --
    # its demo rows (a demo application's, is_demo set from the application) may still be purged
    rule_sets.migration("0006", "crime verification: rule_sets; rule_set_hash on results; audit is_demo + "
                                "append-only",
                        before=rule_sets.demo_flag_sql("crime_verification_audit", "application_id",
                                                       "crime_verification_applications"),
                        decision_tables=["crime_verification_results"],
                        audit_tables=["crime_verification_audit"]),
    ("0007", "crime verification: api_responses (every API response, append-only)", responses.MIGRATION[2]),
]
