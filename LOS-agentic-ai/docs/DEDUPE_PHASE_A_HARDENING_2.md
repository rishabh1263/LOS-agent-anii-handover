# Dedupe Phase A: hardening round 2 (2026-10-22)

No migration, no schema change, no production or development database setting changed. The duplicate window is still
unset. No AP / PR integration was added.

## 1. Test-infrastructure optimisations a–c (`app/store/testing.py`, test-only)

- **(a)** Retired test databases are dropped on a background thread: a bounded queue of 12; a full queue makes the next
  setup wait; everything is still dropped at exit.
- **(b)** A clone of the **validated** template skips `initialise()`, but only when it would do nothing:
  - every regular migration is in the template (checked once, at setup);
  - and no gated migration would be applied under the **current** flags. A test that switches `LOS_COAPP_IDENTITY` on
    still gets the full, locked path;
  - the flags are looked up at call time. The infra test found and forced this fix: an import bound at class creation
    had hidden a patched flag check.
- **(c)** The **disposable pgserver test cluster only** runs with `fsync` / `synchronous_commit` / `full_page_writes`
  off.
  - It is verified by its data directory under `%TEMP%/los_pg_test`.
  - It never applies with `LOS_TEST_PG_DSN`, and is never the dev store or production.
  - Opt-out: `LOS_TEST_PG_FAST=false`.
- New `tests/store/test_testing_infra.py` covers: clone usable, isolation, the gated path, background drops, cluster
  settings → **5 passed**.

### Measured before / after

Same 297 tests, same machine, nothing else running:
`test_fos_api.py`, `test_dedupe_agent.py`, `test_credit_slice3_assessment.py`, `test_dedupe_api.py`, `tests/store`
(the new infra file excluded from "after").

| | Before | After |
|---|---|---|
| Wall time | 517 s | **314 s (−39 %)** |
| Fixture setup (sum) | 384.8 s | **168.5 s (−56 %)** |
| Test call (sum) | 113.5 s | 129.2 s (+14 %, not explained; possibly the drop thread running concurrently) |
| Peak RSS (pytest + children) | 282 MB | 262 MB |
| CPU | 109 s | 107 s |
| Result | 295 passed, 2 failed, 4 skipped | **identical:** 295 passed, the same 2 failed, 4 skipped |

## 2. Idempotency-Key on every create path

| Path | Key |
|---|---|
| UI form (frontend handoff) | `chatClient.createCase(caseSubmission(previous, body))`: the key is reused while the body is unchanged (retry) and renewed for an edited form or a new case. **Not type-checked:** no TypeScript compiler is installed locally. |
| Chat draft confirm | `chat-draft:<draft_id>`, persisted by the guard. A new draft gets a new `draft_id`, so a new key. The existing in-memory per-process claim is kept. |
| Chat sheet-import confirm | `import:<import_id>:<row>` |
| `/fos/imports/{id}/confirm` route | `import:<import_id>:<row>` (the same format as chat) |

**Server behaviour:**
- **Same key + same body** (field order ignored) → the first case (`Idempotent-Replayed: true`).
- **Same key + different body** → **409 `IDEMPOTENCY_KEY_REUSED`**: no wrong replay, no new case. The body's SHA-256
  is stored with the key's hash.
- **No key** → still accepted (rollout compatibility): serialised and noted as a possible duplicate, but **not
  protected against double submits**.

**Remaining duplicate-submit risks:**
- clients that do not send a key yet;
- a chat draft that has no `draft_id` (an expiry-only draft gets no key);
- two different officers creating the same person (different callers have different keys; only noted as a possible
  duplicate);
- a crash between the applicant write and the case write.

`tests/integration/test_fos_create_concurrency.py` → **9 passed:**
- double submit, different callers, overwrite race, serialisation, busy, retry;
- key reuse with a different body → 409;
- reordered body → replay;
- chat-draft key → replay, and a new draft → a new case.

## 3. Copilot routing failures: root cause isolated (no Copilot code changed)

- **Clean HEAD** (`git archive` export, no working-tree changes): the 6 routing tests **all pass**.
- **Working tree, on a quiet machine, twice:** the same 2 fail **deterministically**:
  - "Should we approve this loan?" → `CLARIFICATION` (expected `OUT_OF_SCOPE` + `DECISION_AGENT`);
  - "What is the applicant's credit score?" → `NOT_IN_SCOPE` (expected `OUT_OF_SCOPE` + `CREDIT_AGENT`).
- KYC / RCU / `no_approval` fail **only under machine load**.
- **The bisect** (laying groups of modified files over the HEAD export):
  - config only passes;
  - the other agents / store / main pass;
  - **`app/agents/applicant/` reproduces it.**
- **Cause:** the uncommitted **general layer** (`copilot/capabilities/general.py`, about lines 805–818: the
  `product_flow.out_of_scope(...)` → `NOT_IN_SCOPE` and the `_decision(...) / meaning_kind == "decision"` branches),
  which `fos_api` runs **before** the out-of-scope router that sets `route_to`. The load-dependent ones go through
  `meaning_kind` (the semantic classifier).
- **Decision for the Copilot owner:** either the general layer defers questions naming a downstream agent to the
  router (keeping the `OUT_OF_SCOPE` + `route_to` contract), or the contract and tests change. Nothing was modified.

## 4. Test runs and the full regression

| Command | Result |
|---|---|
| `pytest -v tests/store/test_testing_infra.py` | 5 passed (after the fix above; 1 failed before it) |
| `pytest -q tests/integration/test_fos_create_concurrency.py` | 9 passed |
| Before / after measurement (297 tests, §1) | 295 passed, 2 failed (Copilot, §3), 4 skipped, in both |
| Targeted suite: UAT + dedupe + API + concurrency + FOS + chat case-form files | **NOT COMPLETED.** Stopped by Claude Code because the laptop ran low on memory; no result claimed |
| Full regression | **NOT RUN** (it waits on the targeted suite; memory) |

## 5. Production readiness: NOT READY

The blockers are unchanged:
- policy sign-off;
- the HMAC key in a vault;
- migration 0010;
- the duplicate window;
- the AP / PR source;
- a completed full regression;
- the Copilot routing contract decision (§3);
- the frontend adopting the Idempotency-Key helper.
