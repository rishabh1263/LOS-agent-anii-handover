# Dedupe Phase A: hardening report (2026-10-22)

Scope: hardening, no new features. No migration, no schema change, no production setting changed. The duplicate
window is still unset. No AP / PR source was invented.

## 1. Concurrency-safe case creation

**Inspected:** `fos_api.create_case` → `app/mcp/applicant.applicant_create` → `application_create`. The defects:

| # | Defect | Effect |
|---|---|---|
| D1 | A supplied `applicant_id` / `case_id` was **checked, then UPSERTED** (`ON CONFLICT DO UPDATE`) | Two simultaneous creates both passed the check; the second **silently overwrote** the first |
| D2 | No idempotency on create | A double click or network retry opened **two cases** (UAT-10) |
| D3 | The applicant is written before the case | A supplied `case_id` that already existed left an **orphan applicant** |
| D4 | The dedupe API ran its database work on the event loop | A lock wait could stall the server |

**Fix (no schema change; the existing architecture is kept):**
- New module `app/agents/applicant/create_guard.py`.
- `fos_api.create_case` is now a thin guarded wrapper. The original body is kept verbatim as `_create_case_unguarded`,
  so the UI form and both chat create paths all go through the guard.
- **Serialised:** a Postgres advisory lock per key (supplied ids, declared mobile as a keyed HMAC + product,
  idempotency key). It is held from the existence check to the last write.
  - Database-wide, so it holds across processes.
  - Waits run in a thread.
  - `FOS_CREATE_LOCK_TIMEOUT_S` (default 10) → `503 CREATE_BUSY`, never a hang.
- **Idempotent:** an optional `Idempotency-Key` header. A retry by the same caller returns the first case
  (`Idempotent-Replayed: true`). Only a SHA-256 of caller + key is stored, as a `case_events` row: durable, with no new
  table.
- **Orphan-free:** an existing supplied `case_id` is refused (same 400 `INVALID_INPUT` as before) **before** the
  applicant is written.
- **Same mobile + same product, no key:** **not blocked** (the duplicate window is not approved). It is recorded as a
  `POSSIBLE_DUPLICATE_AT_CREATE` case event.
- `dedupe_api` assess / search now run in a worker thread (D4).

**Limits (no schema):**
- The replay lookup scans `case_events` by `ref_id`, and the mobile recheck scans `applicants`. Neither is indexed;
  that fits within the migration 0010 discussion.
- Applicant + application are still two writes, not one transaction. Under the lock and with the pre-check, the known
  orphan path is closed; a crash between the two writes can still leave an applicant without a case.

## 2. Tests (exact commands, actual results)

| Command | Result | Time |
|---|---|---|
| `pytest -v tests/integration/test_fos_create_concurrency.py` | **6 passed** | 13 s |
| Mutation check: the same file with the guard switched off (`-p noguard`, scratchpad plugin) | **3 failed** (double submit, overwrite, overlap), as they must | 8 s |
| `pytest -q tests/uat/test_dedupe_phase_a_uat.py tests/agents/test_dedupe_agent.py tests/integration/test_dedupe_api.py tests/integration/test_fos_create_concurrency.py tests/integration/test_fos_api.py` | **153 passed, 4 failed** | 5 min 56 s |

**The failures are in `test_fos_api.py` Copilot intent routing and are not caused by this change:**
- Two fail the same way with the guard **off** and **on**: `downstream_questions[Should we approve this loan?]` and
  `downstream_questions[What is the applicant's credit score?]`. The Copilot answers `CLARIFICATION` /
  `NOT_IN_SCOPE` where the test expects `OUT_OF_SCOPE`.
- Two more (`[Do a complete KYC.]`, `[Run an RCU check.]`, and `test_no_approval_recommendation_is_ever_made`) failed
  only in the loaded run: they flip with machine load.
- This is the Copilot area with about 1,700 lines of uncommitted owner changes. They were not touched or "fixed" here.

**UAT + dedupe + concurrency alone: all passed** (part of the 153). The full regression **has not completed**; see §3.

## 3. Why the full regression takes 53+ minutes: measurements and plan (NOT implemented; for approval)

**Measured:**
- **9,055 tests** are collected (156 deselected as OCR / live). The earlier "~17 min" was a much smaller suite.
- A timing run reached 15 % (~1,360 tests) in ~22 min, about **1 s per test**, so ~2–2.5 h end to end on this laptop.
  It was stopped; no result is claimed.
- **Setup, not the tests, dominates:**

  | File | Setup avg | Call avg |
  |---|---|---|
  | `test_fos_api.py` | 2.75 s | 1.17 s |
  | `test_credit_slice3_assessment.py` | 1.85 s | 0.81 s |
  | `test_dedupe_agent.py` | 0.90 s | 0.14 s |

- **The shared fixtures cost 0.01 s per test** (the 3.7 s session start happens once), so the cost is in what each
  test builds itself.
- **`fresh_repository()`:**
  - clone 0.28–0.4 s, plus `initialise()` 0.2–0.3 s;
  - **every 7th clone also DROPs the oldest database (`WITH (FORCE)`) synchronously: spikes of 1.2–8.5 s.**
- The test cluster runs Postgres 16.2 with `fsync=on`, `synchronous_commit=on`.

**Plan** (every step keeps every test and assertion; the full suite stays the release gate):

| # | Change | Expected gain | Risk |
|---|---|---|---|
| 1 | Drop retired test databases on a background thread (not in the next test's setup); still bounded, still all dropped at exit | Removes the 1–8 s spikes (~1 in 7 DB tests) | Low |
| 2 | Skip `initialise()`'s migration check on a clone of the already-migrated template | ~0.25 s per DB test | Low |
| 3 | The **test cluster only** (`%TEMP%/los_pg_test`): `fsync=off`, `synchronous_commit=off`, `full_page_writes=off` | Faster clones and writes | Low (a disposable test cluster; never the dev or production store) |
| 4 | Run with `pytest-xdist` (already installed): `-n 2` on this laptop (memory-bound: ~1 GB per worker), `-n auto` in CI. A trial on a subset first, to find order-dependent tests | ~2–3× on wall time | Medium: order / shared-file assumptions surface |
| 5 | Two gates: a **fast gate** (`-m "not slow"`, marking the measured heaviest files) on every change; the **full suite** as the release gate, unchanged | Fast feedback; the gate is kept | Low; needs a `slow` marker in `pytest.ini` |
| 6 | Run the full gate on a quiet machine / CI, not beside the dev servers | The measured run competed with the 8010 / 8020 servers | — |

**Order proposed:** 1 + 2 + 3 (test infrastructure only), then measure, then 4 on a subset, then decide on 5.

## 4. Blockers (unchanged; NOT production-ready)

1. Policy sign-off (`signed_off: false`).
2. A production HMAC key in a vault, with rotation (not done; UAT used an ephemeral key).
3. Migration 0010 decisions (not approved, not applied).
4. The duplicate / concurrency windows from the business owner (P05 / P15 stay NOT_EVALUATED).
5. The core / LMS interface for AP / PR codes.
6. A completed full regression run (needs §3, or a quiet machine).
7. **New:** the existing Copilot intent-routing failures in `test_fos_api.py` (§2), to be triaged by the Copilot owner.

## 5. Next approval needed

- **(a)** Approve the test-suite optimisation steps 1–3 (test infrastructure only), then a measured full run.
- **(b)** Whether `Idempotency-Key` should become **required** for UI case creation (today it is optional; the
  frontend must send it to get double-submit protection).
- **(c)** The business duplicate window (to turn `POSSIBLE_DUPLICATE_AT_CREATE` into a decision).
