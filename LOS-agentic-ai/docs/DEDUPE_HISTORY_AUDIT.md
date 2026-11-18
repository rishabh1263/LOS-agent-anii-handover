# Dedupe & Historical Loan Intelligence Agent: Repository Audit

Date: 2026-10-22. Scope: read-only audit, no code changed. Every claim is backed by a file:line reference in this repository.

## 1. Summary

This LOS works **before sanction**. It holds no loan book, no customer master and no repayment ledger. Dedupe exists only as an input slot in the fraud-risk agent, and nothing fills it.

As a result, about half of the 18 requested scenarios need data from systems outside this repository: the loan management system (LMS / core lending) and a real credit bureau. Section 7 lists the decisions needed before those scenarios can be built.

| Requirement | What exists in the repo | Evidence |
|---|---|---|
| AP Code customer master | **Not found.** The closest reusable person ID is `applicants.applicant_id` (`APP-<12 hex>`). There is no AP Code, CIF or UCIC. | `app/mcp/applicant.py:553`, `app/agents/los/parties.py:115` |
| PAN lookup / dedupe index | **Not found.** The primary applicant's PAN has no column; it is stored only inside the encrypted `case_findings` JSON. The co-applicant's PAN is encrypted with Fernet, which produces different ciphertext each time, so it cannot be matched or indexed. | `app/store/crypto.py:36,81-86,102-110`, `app/agents/los/co_applicants.py:156` |
| Person across roles (applicant ↔ co-applicant) | **Not found.** The same person gets two unrelated IDs. A co-applicant ID belongs to one case only. | `parties.py:43-47`, `co_applicants.py:170`, `postgres_repo.py:190-201` |
| Application lifecycle (rejected / cancelled / sanctioned / disbursed) | **Not found.** `ApplicationStatus` has 4 values, all inside the FOS stage. Stages run FOS → CPA → CREDIT → RCU → BOPS → HOPS → DISBURSEMENT. A case cannot be rejected or cancelled. | `app/store/models.py:39-45`, `app/agents/los/stages.py:35-44`, `app/config/stage_lifecycle.yaml:17-75` |
| Loan account (disbursed, outstanding, closure, settled, written off) | **Not found.** `DISBURSEMENT` is a stage name only; the stage registry marks disbursement details as `not_available`. | `app/agents/los/stage_registry.py:122-123` |
| EMI schedule, ledger, DPD, overdue | **Not found inside the LOS.** It exists only in two places: (a) a **demo** bureau ("There is no real bureau integration"), and (b) EMIs detected from bank-statement vendor analysis. | `app/agents/credit/bureau/__init__.py:4-8`, `bureau/base.py:30-67`, `app/agents/credit/repayment/obligations.py:107-143` |
| Top-up / balance transfer | Officer-typed inputs only (POS, MOB, previous sanction, LAN). Nothing is looked up from a loan book. | `app/agents/eligibility/calc/topup.py:1-31`, `app/config/eligibility_config.yaml:187-200,326-345` |
| Repeat loan, seasoning, exposure limits | **Not found.** `EXISTING_LOAN_COUNT`, `CUSTOMER_EXPOSURE_LIMIT` and `DPD_RISK` are listed as `not_implemented`. | `app/config/risk_policy.yaml:454-468` |
| RCU / fraud case with outcomes | **Not found.** The code itself says "there is no RCU producer"; `rcu_agent` is `enabled: false`. | `stage_registry.py:12-16`, `app/config/agents.yaml:397-401` |
| Document reuse across cases | The file's SHA-256 is computed but **not saved**. `document_versions.content_hash` is a hash of source ID + document type, not of the file. | `app/store/documents.py:72-74`, `app/store/ingest.py:577` |
| Collateral reuse, lien, CERSAI | **Not found.** Sale-deed fields (document number, khasra number, plot number…) are extracted but not compared across cases. | `app/agents/sale_deed/schemas.py:85-141` |
| Dedupe | Only as an input contract: `DedupeResult{matched_parameters, matched_customer_ids}`, rule `DEDUPE_MATCH` (PAN = CRITICAL). The LOS flow calls fraud risk without it, so dedupe is always reported as a data gap. | `app/agents/fraud_risk/schemas.py:87-100`, `rules.py:92-139`, `app/agents/los/flow.py:1535-1545`, `engine.py:115` |
| Aadhaar matching | Excluded. Owner rule: Aadhaar is never processed. (Aadhaar is still OCR-classified and its Secure QR is decoded; no number is extracted.) | `app/agents/document_agent/workflow.py:192-197`, `app/agents/verification/authenticity_providers.py:172-279` |
| Idempotency on case/applicant creation | Weak. Creation checks first and then upserts, which is not atomic: two concurrent creates overwrite each other instead of failing. There is no unique constraint on PAN, mobile or name+DOB. | `app/mcp/applicant.py:555,654`, `sql_repo.py:506,551` |

## 2. Reusable infrastructure

- **Rule engine (best one to reuse):** credit underwriting.
  - Rules are in YAML, validated at startup (unique ID, closed set of signals, operators, a threshold is required) and versioned.
  - Each rule ends as FIRED / NOT_MET / UNAVAILABLE / DISABLED. UNAVAILABLE becomes a `DATA_GAP` finding.
  - A sign-off gate stops an unsigned policy in production.
  - These outcomes map directly to TRIGGERED / NOT_TRIGGERED / NOT_EVALUATED / EVALUATION_FAILED.
  - Evidence: `app/agents/credit/policy.py:41-47,80-182`, `findings.py:56-79,149-167`, `assessment.py:6-10,59-71`, `provenance.py`.
- **Persistence:** Postgres only, raw SQL repository (`app/store/sql_repo.py`, `postgres_repo.py`).
  - Versioned migrations 0001–0009 (`postgres_repo.py:122-137`). Risky changes are `GATED_MIGRATIONS`, applied by hand with a backup (`scripts/apply_migration.py`). The proposal template is `docs/MIGRATION_0006_PROPOSAL.md`.
  - Transactions take an advisory lock (`sql_repo.py:599-659`). Pagination is `LIMIT/OFFSET` with an allow-list of sort columns (`:880-903`).
- **API conventions:**
  - Routers mount at `/api/v1` with `require_jwt` (`main.py:807+`). Routes add `require_any_scope(...)` (`app/security/access.py:300`) and a case-ownership check (`access.authorize`).
  - Pydantic models use `extra="forbid"`. Every response carries a `request_id`. Errors follow `{code, message, request_id}` (`app/api/errors.py`).
- **Masking:** `app/security/sensitivity.py` (`mask`, `mask_identifiers`, `mask_payload`). Gap: there is no masker for a mobile number held as its own field.
- **Audit:**
  - JSONL writer `app/observability/audit_log.py`.
  - Database tables `case_events`, `case_findings`, `case_decisions` (records policy version) and `stage_transitions`.
  - Maker-checker approvals in `app/approvals/service.py`: `RISK_OVERRIDE` is configured but nothing executes it.
- **Agent registration:** `app/orchestration/registry.py:38` plus `app/config/agents.yaml`.
- **Tests:**
  - 381 test files.
  - Each test gets a cloned Postgres database (`app/store/testing.py`); auth fixtures live in `tests/conftest.py`.
  - Markers: `ocr`, `slow`, `live_llm`, `live_pg`, `live_jev`.

## 3. Hook points

1. `app/api/routes/fos_api.py:913` `create_case`: dedupe check before the case is created (PAN, mobile).
2. `app/agents/los/flow.py:1535`: pass `DedupeResult` to fraud risk. This closes an existing gap, and fraud risk itself needs no change.
3. The stage move into CREDIT (`los_api.py:945-970`): a history refresh can run there, the same way underwriting is triggered.

## 4. Scenario coverage

| # | Scenario | Possible today | Blocker |
|---|---|---|---|
| 1 | New customer | ✅ Within LOS records only; the sources searched are reported | PAN index (§6) |
| 9 | Applicant/co-applicant role swap | ✅ | PAN index across both tables |
| 10 | Duplicate / simultaneous application | ✅ | PAN index |
| 11 | Identity conflict (one PAN, several applicant IDs) | ✅ | PAN index |
| 12 | Document reuse | ✅ | Save the file SHA (§6) |
| 15 | Missing / stale data | ✅ | none |
| 16 | Concurrency | ✅ | Unique constraint + advisory lock |
| 6, 7, 13 | Multiple loans, past default, borrowing pattern | ⚠️ Demo bureau only | Real bureau |
| 5 | Balance transfer | ⚠️ Officer input + EMIs detected in the bank statement | External loan statement |
| 2, 3, 4, 8, 17 | Portfolio, repeat loan, top-up, closure conflict, restructuring | ❌ Returns `DATA_UNAVAILABLE` | **LMS integration** |
| 14 | Collateral reuse | ❌ | Property IDs saved per case; lien/legal source |
| 18 | Fraud case later cleared | ❌ Returns `NOT_EVALUATED` | RCU case system |

## 5. Proposed structure (follows existing conventions)

```
app/agents/dedupe/
  config.py      loads dedupe_policy.yaml; fails closed and names the missing keys
  schemas.py     request/response models; enums for scenario IDs and P01–P20 outcomes
  identity.py    PAN / mobile keyed-hash match across applicants + co_applicants
  sources/       los_cases.py (real), bureau.py (existing adapter), lms.py (interface → SOURCE_UNAVAILABLE)
  lifecycle.py   application status and loan status kept separate
  behaviour.py   metrics computed only from authoritative sources
  scenarios.py   the 18 scenario IDs
  policy.py      reuses the credit policy pattern
  recommend.py   documented precedence between recommendations
  service.py
app/config/dedupe_policy.yaml  P01–P20, version, signed_off: false; every SBFC threshold null (UNCONFIGURED → NOT_EVALUATED)
app/api/routes/dedupe_api.py   POST /api/v1/dedupe/assess, GET /api/v1/cases/{case_id}/dedupe
tests/agents/test_dedupe_*.py, tests/integration/test_dedupe_api.py   synthetic data only
```

No business threshold is invented. A decision that depends on an unconfigured value comes back `NOT_EVALUATED` (fail closed).

## 6. Schema change: proposal only (needs approval; not run)

Draft migration 0010. It is **gated**, because existing cases need a backfill.

```sql
-- PAN / mobile stored as a keyed HMAC (secret from env): cannot be reversed, but can be matched; never plain text
CREATE TABLE IF NOT EXISTS party_identity (
  party_id TEXT NOT NULL, case_id TEXT NOT NULL, role TEXT NOT NULL,   -- PRIMARY_APPLICANT | CO_APPLICANT
  pan_hmac TEXT, mobile_hmac TEXT, created_at TEXT NOT NULL,
  PRIMARY KEY (party_id, case_id));
CREATE INDEX IF NOT EXISTS ix_party_identity_pan ON party_identity(pan_hmac);
CREATE INDEX IF NOT EXISTS ix_party_identity_mobile ON party_identity(mobile_hmac);
-- detecting a file reused across cases
ALTER TABLE documents ADD COLUMN IF NOT EXISTS file_sha256 TEXT;
CREATE INDEX IF NOT EXISTS ix_documents_sha ON documents(file_sha256);
```

## 7. Phased plan

| Phase | Content | Data |
|---|---|---|
| A | Identity resolution; scenarios 1, 9, 10, 11, 12, 15, 16; policies P01, P02, P05, P06, P08, P13, P14, P15; API; `DedupeResult` fed into fraud risk; tests | LOS (real) |
| B | Scenarios 6, 7, 13; policies P03, P04, P18; every result marked `is_demo` | Demo bureau adapter |
| C | Scenarios 2, 3, 4, 8, 17; policies P11, P12, P16, P19 | LMS API / table spec needed |
| D | Scenarios 14 and 18; policies P10, P20 | Collateral / RCU systems |

## 8. Decisions needed

1. **AP Code and the loan book:** which system holds them (core / LMS)? Its API or table spec is needed. Without it, phase C can only report `DATA_UNAVAILABLE`.
2. **Migration 0010** (§6): approve, or start phase A without a schema change. Without it, PAN matching means decrypting findings case by case, which is slow and cannot use an index.
3. **Bureau:** is a real CIBIL/Experian/CRIF integration planned? Today only the demo bureau exists.
4. **Policy values:** P03/P04/P11/P16/P18 thresholds (exposure limit, DPD, seasoning, lookback) must come from approved SBFC policy.
