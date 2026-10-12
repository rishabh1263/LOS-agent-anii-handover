# Sprint 4 Report — KYC name match, table on request, English conversation

Date: 2026-10-10 · Branch: dev/rishabh · Nothing committed, pushed or deployed.
All test data is synthetic. Tests used the isolated embedded Postgres (`LOS_TEST_PG_DSN`). The dev DB was not touched.

## 1. Application name vs the verified PAN (KYC)

**Where:** `app/agents/los/kyc_gate.py` → `application_name_match()`. The setting is `kyc_policies.yaml` → `application_name`.

| Outcome | When | KYC party status |
|---|---|---|
| MATCH | The comparator method is in `match_methods` (EXACT, TOKEN_SET) | PASS |
| PARTIAL | Similar but only FUZZY/INITIALS (e.g. "Arjun Mehra" vs "ARJUN MEHTA") | REVIEW |
| MISMATCH | Different names | BLOCKED |
| PENDING | No PAN on this party has passed verification yet | NOT_READY |
| UNVERIFIABLE | Name missing on one side (application or PAN extraction) | REVIEW |

Rules we kept:
- Only a **VERIFIED, non-superseded** PAN for **that party** counts. Each PAN is checked, and the worst result wins.
- The PAN name is read from the EXTRACTION findings (`pan_name_fields`). Nothing is guessed.
- The application name is **never overwritten** with the PAN name. A verified name is shown next to it, never in its place.
- No aggressive fuzzy matching: FUZZY and INITIALS can never produce a MATCH. They are downgraded to PARTIAL, which needs a reviewer.
- With `cpa_gate: true`, `APPLICATION_NAME` is a sixth required check for FOS→CPA. A MISMATCH blocks the move with `409 GATE_NOT_MET` (see `test_an_application_name_that_differs_from_the_verified_pan_blocks`).

**Explainability:** the KYC answer (`kyc_table.attach`) and the document-actions answer state the result in one configured sentence, `languages.yaml` `docact_app_name_<OUTCOME>`. For example: *Application name does not match the verified PAN: the application says "X", the PAN says "Y".* This is the same result the gate counts.

## 2. Cross-document consistency

This already exists and is unchanged. The KYC agent's NAME/DOB checks across PAN, Aadhaar and bank statement run through `profile_match` / `app/services/name_match`. A PASS on NAME now also saves the verified name (`store/ingest.py`), and the answer mentions it (`docact_kyc_name_verified*`). Sprint 4 adds the application-vs-PAN link described above. The synthetic seed case A (bank statement "KARAN SETHI" vs PAN "ARJUN MEHTA") still shows NAME FAIL.

## 3. Case table only on request

Config: `case_list.yaml` → `format` (`default: summary`, `table_phrases`, `no_table_phrases`, `show_table`).
- By default the reply is a short list, one line per case: `1. Name · CASE-… · Status · [Open]`. A **Show as a table** button follows it.
- "show me the table", "in a table please" and the button itself re-render the **same list just shown** as a table. "don't show the table, just summarize" gives the short list again.
- All the list's own features are unchanged: paging ("Showing 1-5 of 7"), top-N, downloads, and the "Which case is this about?" picker.

## 4. English conversation follow-ups

| Said | Behaviour |
|---|---|
| "show the top two cases with KYC issues" | Top-N list, short form |
| "why is the first one pending?" / "open the second one" / "the last one" | `case_list.ordinal_pick` takes that row of the list just shown, opens it, then answers the rest of the question |
| "show the table" | The same list as a table |
| "the other applicant" | When exactly one other case exists, it opens that case |
| "has the name been verified?" / "does the PAN match the application?" | KYC answer with the application-name line |

An ordinal beyond the list, or with no list shown, picks nothing, so no case is guessed. Ordinal picks skip the meaning rewrite and the general layer (`fos_api.py`).

## 5. Tests

New:
- `tests/core/test_application_name_vs_pan.py` (20)
- `tests/core/test_sprint4_conversation.py` (20)
- `test_an_application_name_that_differs_from_the_verified_pan_blocks` in `test_fos_cpa_kyc_rule.py`

Updated to the owner's new rules, with the same intent and no weaker checks:
- `test_fos_cpa_kyc_rule.py`: the fixture now has a named applicant plus a PAN extraction, and the required checks are ALL + `APPLICATION_NAME`.
- Two unit tests in `test_chatbot_fixes_20261009.py` cover document-level KYC only, so the name check is switched off there explicitly.
- The list tests (`test_case_list_topn`, `test_final_fix`, `test_master_15_product_flow`, `test_master_15b_stage_flow`) used to detect "a list was shown" via table rows `| CASE-`. They now accept the short-list row or a table row. Row counts, order ("newest first, five only") and paging are asserted exactly as before. In `extra_links`, the short list's per-row Open links are treated like table rows: they are the list itself.

Layered runs (fast first):

| Layer | Scope | Result | Time |
|---|---|---|---|
| L1 unit | Sprint 4 core tests | 40 passed | seconds |
| L2 affected integration | list + KYC rule + Sprint 4 files | 91 passed | 89 s |
| L3 regression subset | tests/core, tests/agents, tests/integration (copilot/kyc/case/fos/list/gate/guardrail…) | see §6 | see §6 |

## 6. Regression result

**L3 run** (`runs/sprint4_regression.log`, exit 0, completed): **20 failed, 3271 passed, 7 skipped in 2278 s (38 min)**, `-n 4`.

Every failure was reproduced in isolation (`runs/sprint4_isolate.log`: the same 18 failed deterministically, so they are not flakes) and classified with evidence:

| # | Test(s) | Class | Evidence / action |
|---|---|---|---|
| 5 | `test_master_3_case_list_scale` (4 tests, 5 cases) | **New (Sprint 4), intended rule** | They counted table rows `\| CASE-`; the default is now the short list. The `rows()` helper accepts both forms and `case_of()` reads the id from either. Same counts, order and caps. **Fixed, passing.** |
| 1 | `test_master_12_selfcheck` | **New (Sprint 4), intended rule** | The `LIST` pattern (and the two "must NOT list" patterns) now match short-list or table rows. **Fixed, passing.** |
| 1 | `test_fos_plan_1_2_case_list::test_the_bare_switch_still_lists` | **New regression (Sprint 4)** | Root cause: with exactly one other case, any switch phrase opened it, but "dusra case" must list (FOS plan 1.2). Fix: only the configured `the_other` phrases ("the other applicant/customer/borrower") open directly. New test `test_the_other_applicant_opens_the_only_other_case`. **Fixed.** |
| 1 | `test_contextual_fragments::test_docs_after_pending_documents…` | **New regression (Sprint 4)** | Root cause: the application-name PENDING line led the pending-documents answer, and glossary expansion made the two otherwise identical answers differ. Fix: `document_actions` says the name line only once a PAN is verified; the KYC answer still says PENDING. **Fixed, passing.** |
| 4 | `test_copilot_composition` ×2 (stage / pending), `test_copilot_quality_evals`, `test_copilot_party_kyc_evals` | **Environment** | `.env` sets `APPLICANT_AGENT_LLM_FOR_SIMPLE_INTENTS=true` (owner's all-flags-ON change of 2026-10-08; every `.env.bak-*` and `.env.example` say `false`), so simple intents are sent to the composer. With only that variable set to `false`, all 4 pass (`runs/sprint4_isolate_flagoff.log`). Not a code defect. **Decision for the owner:** keep the flag ON (the tests then need to pin it) or turn it OFF (deterministic answers, no model latency). |
| 5 | `test_copilot_process_questions`, `test_copilot_rag_metrics` ×2, `test_copilot_semantic_evals`, `test_copilot_conversation_evals`, `test_copilot_family_evals` | **Pre-existing** | They fail identically on a clean `git worktree` of HEAD `3d3bc2d` (the committed code without any of this session's changes) with the same `.env` (`runs/sprint4_head_baseline.log`; semantic 167/178 at HEAD vs 168/178 now). `family_evals` is already recorded as pre-existing (CHANGES_LOG failure #2). **Unresolved; not release-blocking for Sprint 4 scope, but they block "release candidate".** |
| 1 | `test_copilot_routing_contract::test_mixed…` | **Pre-existing** | Failed in the Sprint 3A focused run and on the pre-Sprint-2 snapshot. |
| 1 | `test_copilot_variant_evals` | **Pre-existing** | Fails since the Sprint 2 baseline (one-word owner decision). |

**Stop-gate re-run:** the first re-run of the 25 affected files (`runs/sprint4_regression_rerun.log`) **did not complete**: a worker hung and the 40-min `timeout` killed it ("node down: Not properly terminated"). That is **not a pass**. About 20 orphaned `multiprocessing` workers from killed earlier runs (their parent processes were gone) were cleaned up, and the subset was re-run with `faulthandler_timeout=300` (`runs/sprint4_regression_rerun2.log`).

**Stop-gate re-run, completed: 12 failed, 405 passed, 1 skipped in 413 s.** The 12 failures are exactly the classified ones: 4 environment (simple-intents flag), 6 pre-existing on HEAD, `test_mixed` and `variant_evals`. **0 new failures.** Every Sprint 4 fix passes in this combined run. No hang recurred (the earlier one coincided with the orphaned workers and memory pressure). Afterwards the memory reaper stopped the backend (8010), auth (8020) and frontend (5173); they were not restarted automatically.

**Owner decision 2026-10-10 ("off rakho"):** `.env` `APPLICANT_AGENT_LLM_FOR_SIMPLE_INTENTS=false` (only that line changed; backup `.env.bak-before-simple-intents-off`).
Re-run of the same 25 files with `-n 2` (`runs/sprint4_regression_flagoff.log`): **10 failed, 407 passed, 1 skipped in 1026 s.**
- The 4 environment failures: composition (stage), composition (pending) and party_kyc now **pass**. quality_evals failed in the parallel run on `cost` (read budget), as did composition `test_the_prompt_is_small…` (no model call). **Both pass when run alone** (`27 passed`, composition + quality files), so they are classified as **load-sensitive**: under `-n 2` the request budget skips the composer, and the read count varies. They are tracked, not hidden.
- The rest: the 6 pre-existing failures (same as on HEAD), `test_mixed` and `variant_evals`.
- **0 failures attributable to Sprint 4 or to the name-correction fix.**

**Stop-gate for Sprint 4: CLOSED.** Remaining to decide: the `.env` simple-intents flag (owner), and the 6 pre-existing eval failures (tracked, unresolved).

## 7. Not done / known

- The frontend is untouched in Sprint 4. The existing link/ask contract renders the short list and the table button.
- The full 4 h suite was not run. L3 covers the touched areas.
- Pre-existing failures carried over from Sprint 2/3A (not caused by Sprint 4): `test_copilot_variant_evals` (one-word owner decision), `test_copilot_routing_contract::test_mixed…` (also fails on the pre-Sprint-2 snapshot), and resource-sensitive or order-dependent flakes.
