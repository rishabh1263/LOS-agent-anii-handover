# Migration 0010: dedupe identity index + document file hash. PROPOSAL: NOT APPROVED, NOT APPLIED

Status: proposal for owner review (2026-10-22). Nothing has run. The dedupe agent works without this migration in
**scan mode** (§5). The migration only makes matching indexed, complete and fast.

## Why

- PAN cannot be matched today:
  - the primary applicant's PAN exists only inside the encrypted `case_findings.payload` (EXTRACTION kind);
  - the co-applicant's PAN is Fernet-encrypted (`app/store/crypto.py:102`), and Fernet ciphertext differs every time.
- Nothing can be indexed.
- The file SHA-256 is computed on upload (`app/store/documents.py:72`) but never saved, so a file reused across cases
  cannot be found.

## SQL (gated version "0010", flag `LOS_DEDUPE_INDEX`, same process as 0004/0006)

```sql
-- One row per (case, party): keyed-HMAC identity tokens, never a plain or reversible value
CREATE TABLE IF NOT EXISTS party_identity (
    case_id        TEXT NOT NULL REFERENCES applications(case_id) ON DELETE CASCADE,
    party_id       TEXT NOT NULL,                      -- applicant_id or co_applicant_id
    applicant_id   TEXT NOT NULL,                      -- the case's customer record (APP-...)
    party_role     TEXT NOT NULL CHECK (party_role IN ('PRIMARY_APPLICANT', 'CO_APPLICANT')),
    pan_hmac       TEXT,                               -- "<key_id>:<hex HMAC-SHA256(key, normalised PAN)>"
    mobile_hmac    TEXT,                               -- same, normalised 10-digit Indian mobile
    pan_source     TEXT CHECK (pan_source IN ('DOCUMENT_EXTRACTION', 'DECLARED')),
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (case_id, party_id)
);
CREATE INDEX IF NOT EXISTS idx_party_identity_pan       ON party_identity (pan_hmac);
CREATE INDEX IF NOT EXISTS idx_party_identity_mobile    ON party_identity (mobile_hmac);
CREATE INDEX IF NOT EXISTS idx_party_identity_applicant ON party_identity (applicant_id);

-- The stored file's SHA-256 (hex): a file reused across cases
ALTER TABLE documents ADD COLUMN IF NOT EXISTS file_sha256 TEXT;
CREATE INDEX IF NOT EXISTS idx_documents_file_sha256 ON documents (file_sha256);
```

- Precondition: none. It adds one new table and one nullable column; no existing data changes.
- **No UNIQUE on `pan_hmac`, deliberately.** One person legitimately has many cases, and can be primary on one case
  and co-applicant on another.
- **Concurrency today (corrected 2026-10-22).** The dedupe agent takes a **session-level** Postgres advisory lock
  (`pg_advisory_lock(hashtext(key))`) on one pooled connection while it assesses: keys are the customer record and
  the PAN token. This serialises two **assessments** of the same person. It does **not** prevent two **cases being
  created** at the same moment: the FOS case creation (`fos_api.create_case`) is unchanged, and has no PAN at
  creation time anyway (the PAN arrives later, from the document). Simultaneous creates are **detected** afterwards
  (`applications.seconds_between_same_product_creates`, rule P15). They are not **prevented**.

## Rollback (`scripts/sql/rollback_dedupe_index.sql`, run by a person)

```sql
BEGIN;
DROP INDEX IF EXISTS idx_documents_file_sha256;
ALTER TABLE documents DROP COLUMN IF EXISTS file_sha256;
DROP TABLE IF EXISTS party_identity;          -- derived data only: rebuilt by the backfill; nothing original is lost
DELETE FROM schema_migrations WHERE version = '0010';
COMMIT;
```

## Data protection

1. **Keyed HMAC, not a hash.**
   - A PAN has a small keyspace (5 letters + 4 digits + 1 letter, about 3×10¹²). Anyone holding a database copy could
     brute-force a plain SHA-256 of a PAN.
   - HMAC-SHA256 with a secret key held **outside** the database (env `LOS_IDENTITY_HMAC_KEY`) makes the tokens
     useless without the key.
2. **A separate key from the encryption key** (`LOS_DATA_ENCRYPTION_KEY`): a leak of one does not expose the other.
3. **Key format** `key_id:base64key[,key_id2:key2]`:
   - the first key writes; every listed key is tried when matching;
   - rotation = add a new key first, then re-run the backfill.
4. **Startup rules:**
   - production refuses to start without the key (same rule as the encryption key);
   - development and tests use a fixed, public **development-only** key.
5. **What the table can and cannot reveal:**
   - it reveals equality ("these two cases share a PAN"), which is its purpose;
   - it holds no PAN, no name and no Aadhaar.
6. **Aadhaar is never indexed**, per the owner rule.
7. **Under DPDP**, the token is pseudonymous personal data:
   - the service account is the only reader;
   - rows are deleted with the case (`ON DELETE CASCADE`);
   - API responses never return a token.
8. **Mobile tokens are only a supporting signal**: family members share numbers. A mobile match alone never becomes an
   identity match or a fraud flag.

## Backfill (`scripts/backfill_party_identity.py`)

- **Order:** run after the migration and after a fresh `pg_dump`, which `scripts.apply_migration` already enforces.
- **Default `--dry-run`:** prints counts only.
- **Batches:** `--batch 500` cases, ordered by `case_id`, resumable with `--after <case_id>`.
- **Primary applicant PAN:**
  - source: the current gate-released EXTRACTION findings of the applicant's party (decrypted in memory only);
  - normalised, HMAC'd, and upserted (`ON CONFLICT (case_id, party_id) DO UPDATE`);
  - idempotent: a re-run writes the same values.
- **Co-applicant PAN:**
  - source: `co_applicants.pan` (decrypted) when migration 0004 is applied;
  - otherwise the co-applicant party's EXTRACTION findings.
- **Mobile:** `applicants.mobile` (primary only; co-applicant mobile is not stored today).
- **File hash:** for every `documents` row whose file is still in the document store, hash the stored bytes. A file
  that is gone stays NULL and is counted.
- **Output:** counts only (cases, parties with or without PAN, files hashed or missing). No value is ever printed or
  logged.
- **Cost estimate:** one decrypt per finding row plus one file read per document. At dev scale this takes seconds; in
  production it should run in an off-peak window.
- **New writes after the migration: NOT WRITTEN YET (corrected 2026-10-22).** An earlier version of this document said
  the write path was written but inactive. That was wrong: no code writes `party_identity` or `file_sha256` today, and
  neither does the backfill script (`scripts/backfill_party_identity.py` does not exist yet). After approval, these
  would be added and reviewed:
  - ingest (`app/store/ingest.py`, EXTRACTION findings) and co-applicant intake (`app/agents/los/co_applicants.py`)
    upsert the identity row;
  - the upload path (`los_api._retain_uploads`) saves `file_sha256`;
  - the dedupe agent reads the table instead of scanning, once `table_exists("party_identity")` is true.

## Review: risks and required decisions (2026-10-22)

| # | Area | Finding | Risk | Decision needed |
|---|---|---|---|---|
| 1 | Locking during apply | `apply_gated` runs the DDL inside `BEGIN … COMMIT` under the migration advisory lock (`postgres_repo.py:262-281`), so a failure rolls back everything (atomic). `CREATE INDEX` (non-concurrent) on `documents` takes a SHARE lock and blocks uploads while it builds. | Upload stall during the build, proportional to the `documents` row count. | Plain index inside the transaction (fine for a small table, in a planned window) **or** `CREATE INDEX CONCURRENTLY` as a separate, non-transactional step (no write block, but not atomic and needs its own failure handling). |
| 2 | `ALTER TABLE documents ADD COLUMN` | Nullable with no default: on PG 11+ this is a metadata-only change. | Low. Needs a short ACCESS EXCLUSIVE lock; it waits behind long transactions. | Set `lock_timeout` for the apply (for example `SET lock_timeout='5s'`), so the change fails fast instead of queueing. |
| 3 | FK `ON DELETE CASCADE` → `applications` | Identity rows disappear with their case. | Low; cases are not deleted today. | Confirm this retention rule: a token lives exactly as long as its case. |
| 4 | Key management | `LOS_IDENTITY_HMAC_KEY` is separate from the Fernet key. Each token carries its key id, and matching tries every key. | **Key loss:** every token becomes useless. That is recoverable, because the table is derived; rebuild with the backfill from the encrypted source, which needs the Fernet key. **Key leak:** with a database copy, PAN (and mobile, about 4×10⁹ values) tokens can be brute-forced. | Where the key lives (vault / secret manager), who may read it, rotation cadence, and a leak runbook (rotate, then rebuild). |
| 5 | Rotation | Writes use the first key and reads try all keys, so a lookup sends one token per key. | Until a full re-backfill under the new key, old-key rows stay matchable only while the old key is still configured. | Order: add the new key as **first**, keep the old one, run the backfill, then remove the old key. Who signs off each step. |
| 6 | Backfill | Not written yet. It must decrypt findings (Fernet key) and read every stored file. | Runtime grows with the document store size. It handles plaintext PAN in memory on the run host. | Run host, window, and approval of the script itself before it runs. Dry-run counts reviewed first. |
| 7 | Partial backfill | Upserts are idempotent and the run can resume with `--after`. | While the backfill is incomplete, table mode would miss un-backfilled cases. | The agent must stay in **scan mode** until the backfill finishes. Decide the switch (a flag, or a `schema_migrations`-style marker that records the backfill as complete). |
| 8 | Privacy | Tokens are pseudonymous personal data (DPDP). No PAN, name or Aadhaar is stored, and tokens never appear in responses. | The equality the table reveals is its purpose; access to the table must be limited. | The DB role that may read `party_identity`; whether audit logs may name matched case ids (today they name only the assessed case). |
| 9 | Rollback | Drops only derived data. It does not touch `schema_migrations` bookkeeping other than 0010's own row. | Low. | The rollback script is not created yet; approve it together with the migration. |
| 10 | Mobile | Supporting only: a family shares a number. | A mobile token match is never treated as identity (enforced in `match.py` and `fraud_dedupe.report_shared_mobile: false`). | None for the schema; confirm the policy stays as it is. |

**Approval blockers:** decisions 1, 2, 4, 5, 6 and 7. Until then: no DDL, no backfill, scan mode only.

## The four dedupe parameters (owner, 2026-10-22) and what 0010 covers

| Parameter | Meaning | Stored by this LOS | In 0010 |
|---|---|---|---|
| PAN | PAN | ✅ (encrypted) | `pan_hmac` |
| Mobile No | mobile | ✅ primary applicant only (`applicants.mobile`) | `mobile_hmac` |
| AP Code | customer ID: permanent, never changes | ❌ no column | **not added.** It needs the customer-master source; inventing a column would mean inventing data |
| PR Code | loan account number, created at disbursement | ❌ this LOS ends before disbursement | **not added.** It needs the LMS (phase C) |

When the core / LMS source is connected, AP Code and PR Code get their own proposal: a keyed-HMAC link table filled
from that source, never typed in by the LOS. Until then the agent answers `INSUFFICIENT_DATA` for them, with
`needs_source`. It never answers "no match".

## Without this migration (current state)

The dedupe agent runs in **scan mode**:

- It reads up to `limits.max_scan_cases` recent cases (config) and decrypts their PAN in memory.
- It hashes the case's own stored files and compares them with files of the scanned cases.
- Results are real but bounded:
  - `coverage.mode = "SCAN"`;
  - `PARTIAL_ANALYSIS` whenever the store holds more cases than the bound.
