"""
The agent's database schema, as ordered migrations (append only; each runs once, in one transaction). The tables live
in the agent's OWN database -- never in the LOS case store.

    applications    what the LOS sent: applicant, loan, property (type, address, owners)
    cases           one legal verification of an application: status, agency, the CPA's decision
    documents       uploaded files (reference + hash) and what the agent read off the Sale Deed
    corrections     a CPA's correction of a field read off the Sale Deed (who, why, before, after)
    agency_reports  the agency's Verification Report answers (one row per submission)
    runs            one validation: the ten rule results and the recommendation
    summaries       the AI summary of a run (or why there is none)
    audit           everything that happened (ids, statuses, fields -- never a document's content)
"""

from __future__ import annotations

from app.agent_db import LOGS_MIGRATION, responses, rule_sets

_V1 = """
CREATE TABLE legal_verification_applications (
    application_id TEXT PRIMARY KEY,
    branch TEXT NOT NULL,
    product TEXT NOT NULL DEFAULT '',
    loan_amount NUMERIC(14, 2) NOT NULL DEFAULT 0 CHECK (loan_amount >= 0),
    applicant_name TEXT NOT NULL,
    property_type TEXT NOT NULL,
    property_address TEXT NOT NULL DEFAULT '',
    property_pin TEXT NOT NULL DEFAULT '',
    owner_names JSONB NOT NULL DEFAULT '[]',
    stage TEXT NOT NULL DEFAULT '',
    is_demo BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX legal_verification_applications_demo ON legal_verification_applications(is_demo) WHERE is_demo;

CREATE TABLE legal_verification_cases (
    case_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL UNIQUE REFERENCES legal_verification_applications(application_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('NEW', 'DOCUMENTS_UPLOADED', 'WITH_AGENCY', 'REPORT_RECEIVED', 'VALIDATED',
                                           'DECIDED')),
    agency TEXT,
    sent_by TEXT,
    sent_at TIMESTAMPTZ,
    idempotency_key TEXT UNIQUE,
    decision TEXT,
    decision_remarks TEXT,
    decided_by TEXT,
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE legal_verification_documents (
    document_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES legal_verification_cases(case_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_ref TEXT NOT NULL,
    content_type TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    pages INTEGER,
    metadata JSONB NOT NULL DEFAULT '{}',
    read_status TEXT NOT NULL CHECK (read_status IN ('READ', 'PARTIAL', 'NOT_READ', 'STORED')),
    extracted JSONB NOT NULL DEFAULT '{}',
    reader_notes JSONB NOT NULL DEFAULT '[]',
    uploaded_by TEXT NOT NULL,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX legal_verification_documents_case ON legal_verification_documents(case_id, uploaded_at);

CREATE TABLE legal_verification_corrections (
    correction_id BIGSERIAL PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES legal_verification_documents(document_id) ON DELETE CASCADE,
    field TEXT NOT NULL,
    before_value JSONB,
    after_value JSONB,
    reason TEXT NOT NULL,
    corrected_by TEXT NOT NULL,
    corrected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE legal_verification_agency_reports (
    report_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES legal_verification_cases(case_id) ON DELETE CASCADE,
    agency TEXT NOT NULL,
    answers JSONB NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX legal_verification_agency_reports_case ON legal_verification_agency_reports(case_id, submitted_at DESC);

CREATE TABLE legal_verification_runs (
    run_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES legal_verification_cases(case_id) ON DELETE CASCADE,
    recommendation TEXT NOT NULL CHECK (recommendation IN ('CLEAR_FOR_REVIEW', 'REVIEW_REQUIRED', 'ESCALATE')),
    rules JSONB NOT NULL,
    overview JSONB NOT NULL,
    facts_hash TEXT NOT NULL,
    run_by TEXT NOT NULL,
    run_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX legal_verification_runs_case ON legal_verification_runs(case_id, run_at DESC);

CREATE TABLE legal_verification_summaries (
    run_id TEXT PRIMARY KEY REFERENCES legal_verification_runs(run_id) ON DELETE CASCADE,
    facts_hash TEXT NOT NULL,
    text TEXT,
    source TEXT NOT NULL CHECK (source IN ('ai', 'refused', 'unavailable')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX legal_verification_summaries_hash ON legal_verification_summaries(facts_hash) WHERE source = 'ai';

CREATE TABLE legal_verification_audit (
    event_id BIGSERIAL PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES legal_verification_applications(application_id) ON DELETE CASCADE,
    case_id TEXT,
    actor TEXT NOT NULL,
    event_type TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX legal_verification_audit_case ON legal_verification_audit(case_id, event_id);
"""

# the application as Leviosa shows it (owner's screenshot 2026-10-29): property usage / nature / document, the owner by
# first / middle / last name, the address in its parts, PANs MASKED only, assigned to, the LOS status, collateral no.
_V3 = """
ALTER TABLE legal_verification_applications ADD COLUMN details JSONB NOT NULL DEFAULT '{}';
"""

# Waive and Save (owner 2026-10-29): a case can be WAIVED (with the reason, who, when); the legal vendor chosen in the
# Application Details is saved before the case is sent
_V4 = """
ALTER TABLE legal_verification_cases DROP CONSTRAINT legal_verification_cases_status_check;
ALTER TABLE legal_verification_cases ADD CONSTRAINT legal_verification_cases_status_check
    CHECK (status IN ('NEW', 'DOCUMENTS_UPLOADED', 'WITH_AGENCY', 'REPORT_RECEIVED', 'VALIDATED', 'DECIDED', 'WAIVED'));
ALTER TABLE legal_verification_cases ADD COLUMN selected_agency TEXT;
ALTER TABLE legal_verification_cases ADD COLUMN waive_reason TEXT;
ALTER TABLE legal_verification_cases ADD COLUMN waived_by TEXT;
ALTER TABLE legal_verification_cases ADD COLUMN waived_at TIMESTAMPTZ;
"""

# The agency's report as a document (owner 2026-10-29: "auto report generate karo, auto attach"): each submitted report
# keeps the PDF generated from its answers (a document of the case) and the analysis written into it
_V5 = """
ALTER TABLE legal_verification_agency_reports ADD COLUMN document_id TEXT REFERENCES legal_verification_documents(document_id);
ALTER TABLE legal_verification_agency_reports ADD COLUMN analysis JSONB NOT NULL DEFAULT '[]';
"""

MIGRATIONS: list[tuple[str, str, str]] = [
    ("0001", "legal verification: applications, cases, documents, corrections, agency reports, runs, summaries, audit",
     _V1),
    ("0002", "agent_logs: WARNING and above application logs (shared, app/agent_db)", LOGS_MIGRATION[2]),
    ("0003", "legal verification: the application's details as the LOS shows them (masked PANs only)", _V3),
    ("0004", "legal verification: waive a case; the legal vendor saved before sending", _V4),
    ("0005", "legal verification: the agency's generated report (document) and its analysis", _V5),
    # the policy each run was made with (rule_sets, rule_set_hash on runs); the audit trail append-only -- its demo
    # rows (a demo application's, is_demo set from the application) may still be purged
    rule_sets.migration("0006", "legal verification: rule_sets; rule_set_hash on runs; audit is_demo + append-only",
                        before=rule_sets.demo_flag_sql("legal_verification_audit", "application_id",
                                                       "legal_verification_applications"),
                        decision_tables=["legal_verification_runs"], audit_tables=["legal_verification_audit"]),
    ("0007", "legal verification: api_responses (every API response, append-only)", responses.MIGRATION[2]),
]
