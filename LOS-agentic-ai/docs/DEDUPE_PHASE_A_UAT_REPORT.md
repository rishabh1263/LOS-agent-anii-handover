# Dedupe Phase A: controlled UAT report (2026-10-22)

The owner approved a UAT trial only: synthetic data, no migration, no production change.

## Environment (checked by the suite itself before every test)

| Restriction | How it was met |
|---|---|
| Isolated database | A fresh `los_t_*` Postgres clone of the migrated test template per test, asserted empty (0 applicants, 0 applications) before seeding. It is never the dev store (`runtime/pgdata`). |
| Synthetic data only | Generated PANs (`UATP?####Z`), mobiles `90000xxxxx`, names `UAT PERSON …`, synthetic file bytes. No real customer data. |
| Isolated document store | An empty `LocalDocumentStore` in each test's own temp directory. |
| `DEDUPE_ENABLED` | `true` only inside the UAT process (monkeypatch). No config file or production setting changed. |
| Policy | `app/config/dedupe_policy.yaml` as committed: **conditionally approved for UAT, not signed off** (UAT-00 asserts `signed_off: false`). |
| Duplicate window | Left **unset**: P05_DUPLICATE_WINDOW and P15 are `NOT_EVALUATED` and `completeness.complete: false` (UAT-10, UAT-11). |
| No migration | UAT-00 asserts that `party_identity` and `documents.file_sha256` do not exist. |
| UAT HMAC secret | Random per run (`secrets.token_hex(32)`), env only, key id `uat1`. It is never written to disk. UAT-05, 08 and 12 assert it is absent from responses, audit lines and every log record. **Production key management is NOT done.** |
| Unavailable sources | AP / PR code, loan history, DPD, top-up and RCU are not mocked as data. Their explicit unavailable statuses are what is tested (UAT-11). |

## Commands and actual results

| Command | Result | Duration |
|---|---|---|
| `python -m pytest -p no:cacheprovider -v tests/uat/test_dedupe_phase_a_uat.py` | **15 passed**, 0 failed, 0 skipped | 36.1 s |
| `python -m pytest -q -p no:cacheprovider tests/uat/test_dedupe_phase_a_uat.py tests/agents/test_dedupe_agent.py tests/integration/test_dedupe_api.py` | **83 passed**, 0 failed, 0 skipped | 1 min 36 s |
| Earlier run: fraud / LOS flow / health / co-applicant / credit-flow suites (7 files, `-m "not ocr and not live_llm and not live_jev"`) | **175 passed**, 0 failed, 14 deselected | 10 min 36 s |
| Full suite `-m "not ocr and not live_llm and not live_jev and not live_pg" tests` | **NOT COMPLETED.** Stopped after 53 min at the owner's direction; **no result, not reported as passed** | — |

## UAT cases

| ID | Case | Result |
|---|---|---|
| UAT-00 | The environment is the approved one: no migration, UAT key in use, policy not signed off | PASS |
| UAT-01 | The same PAN (different case / spacing) on another customer record → MATCH, `IDENTITY_CONFLICT_REVIEW`, fraud `pan` | PASS |
| UAT-02 | False positives: same mobile with a different PAN (family), PAN one character apart, same name with a different PAN → NO_MATCH; mobile only → POSSIBLE_MATCH. None become a fraud match. | PASS (4) |
| UAT-03 | Role swaps in both directions → P02 TRIGGERED, never a fraud match | PASS |
| UAT-04 | Document reuse: across customers → P06; the same customer's resubmission is counted separately | PASS |
| UAT-05/06 | Through the real API: no PAN, mobile, other case id, other record id, token or secret in the body; an officer is refused another customer's case (403); search flags DECLARED_NEW_BUT_FOUND | PASS |
| UAT-07 | Partial analysis: scan bound reached → PARTIAL_ANALYSIS; document store down → failed source, P06 NOT_EVALUATED, incomplete | PASS |
| UAT-08 | Each assessment → one masked audit line + one `DEDUPE_ASSESSED` timeline event | PASS |
| UAT-09 | Fraud agent via the LOS flow hook (this run's PAN, before ingest) → `DEDUPE_MATCH` CRITICAL; dedupe outage → the old data gap | PASS |
| UAT-10 | Two cases created **concurrently through the FOS API** → both created, both then detected as an identity conflict; P15 NOT_EVALUATED | PASS (creation is not prevented: a limitation) |
| UAT-11 | AP code / PR code → INSUFFICIENT_DATA with `needs_source`, NOT_VERIFIABLE for Existing; loan history, top-up, RCU and duplicate window NOT_EVALUATED | PASS |
| UAT-12 | The UAT secret, token prefix, PAN and mobile appear in no log record (all loggers, DEBUG) | PASS |

## Known limitations (UAT-observed or by design)

1. **Concurrent case creation is not prevented.** It is only detected afterwards (UAT-10).
2. **Mobile is primary-only;** a co-applicant's mobile is not stored. A shared mobile alone is POSSIBLE_MATCH by design.
3. **Matching needs a released PAN.** An early-FOS case without a PAN document is INSUFFICIENT_DATA, or mobile-only.
4. **Scan mode:** bounded (2000 cases / 500 files); the cost grows with the store until migration 0010.
5. **Document reuse is byte-identical only:** a rescanned copy of the same paper is not detected.
6. **AP code / PR code:** no source, so an EXISTING customer's PR code cannot be verified.

## Production blockers (unchanged by this UAT)

1. Business / policy sign-off of `dedupe_policy.yaml` (currently `signed_off: false`; production refuses to start).
2. `LOS_IDENTITY_HMAC_KEY` in a vault, with access control and a rotation runbook. Not done.
3. Migration 0010 decisions (`docs/MIGRATION_0010_PROPOSAL.md`): not approved, not applied.
4. The duplicate window and concurrency window time limits from the business owner.
5. The core / LMS interface for AP code and PR code (`docs/DEDUPE_CORE_LMS_INTERFACE.md`).
6. A full regression run to completion. The stopped run has no result.

Nothing was deployed, no migration was applied, and no production setting was changed. Unrelated working-tree changes
were preserved.
