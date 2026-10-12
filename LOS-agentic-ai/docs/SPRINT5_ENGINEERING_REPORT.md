# LOS Copilot Production Hardening: Engineering Report (Sprint 5)

Date: 2026-10-10 · Branch: dev/rishabh · **Nothing committed, pushed or deployed.**
Tests use synthetic applicants on an isolated embedded PostgreSQL (`LOS_TEST_PG_DSN`). The dev DB was never used.
Benchmarks use the repository's sample documents; the reports record only types, statuses and timings, never document content.

Architecture, verified: Browser → React (Vite :5173, `/api` proxy) → FastAPI `main.py` (:8010) → copilot pipeline (`fos_api._copilot_json`) → in-process tools → PostgreSQL. There is no .NET layer.

---

## 1. Sprint 4 regression and the stop-gate

Full detail is in `docs/SPRINT4_REPORT.md` §6.
- **L3 run:** 20 failed, 3271 passed.
- **Classification:**
  - 8 Sprint 4 failures, fixed.
  - 4 environment failures: `.env` `APPLICANT_AGENT_LLM_FOR_SIMPLE_INTENTS=true`. The owner then set it to `false`.
  - 6 pre-existing failures, which fail the same way on a clean `git worktree` of HEAD.
  - 2 already known (`test_mixed`, `variant_evals`).
- **Stop-gate re-run:** 0 new failures.
- **First attempt:** it hung and was killed by memory pressure. ~20 orphaned pytest workers were found and cleaned up; the re-run then completed.

## 2. Case-name correction (§9)

| Root cause (reproduced first) | Fix | Evidence |
|---|---|---|
| After a correction the DB held the right name, but the case list showed the misspelling for `cache_ttl_seconds` (30 s): the per-officer row cache was never invalidated on a write | `case_list.forget_case()`, called on every form save and every logged case write; `request_cache.invalidate()` after the save | `test_name_correction_persists.py`: failed before, passes after. Checks the DB row, the form re-fetch, the API list, the chat list and the KYC application-name check |
| `case_form.save` wrote **whole records read earlier**, so two officers editing different fields at once lost one edit, and an applicant write followed by a failed application write left half an edit | `Repository.update_case_fields` (SQL override): one transaction, a per-case `pg_advisory_xact_lock`, current values re-read inside the lock, only changed columns updated, all-or-nothing commit | `test_two_edits_of_different_fields_at_once_both_survive`, `test_a_failed_write_leaves_neither_half` |
| A form opened before another officer's correction silently overwrote it | Optional `expected` in `PUT /cases/{id}/form` (backward compatible) → **409 `STALE_FORM`** with the current values. The chat Confirm checks the value its proposal showed (`edit_stale`) | `test_a_stale_form_does_not_overwrite_a_newer_correction`, `test_a_chat_confirm_after_someone_else_changed_the_name_is_refused` |
| A duplicate request wrote again and logged again | Unchanged values are skipped: outcome UNCHANGED, no write, no audit, `changed: []` (an additive response field) | `test_a_duplicate_request_writes_and_logs_once` |

Audit and activity entries record exactly the changed fields. The PAN/OCR name never overwrites the application name. The application-name KYC check reads the **current** name on every call, with no cached result (`test_a_corrected_application_name_is_re_checked_with_no_cached_result`).

## 3. Document performance (§6, §9)

The tool is `scripts/bench_documents.py`, which calls the same `flow.process_application` as the upload route. OCR engines are warmed first, as at server start. Each set runs 3 times and the median is reported. CPU time and RSS come from psutil. The store is isolated, there are no LLM calls, and nothing is persisted. The host has 16 logical CPUs, 3 OCR workers and 4 document workers.

| Set | Docs | Before median | After median | Notes |
|---|---|---|---|---|
| one (PAN) | 1 | 1752 ms | 1259 ms | path unchanged: run-to-run variance |
| two | 2 | 3613 ms | 2751 ms | unchanged path |
| four | 4 | 4793 ms | 3695 ms | unchanged path; documents already run concurrently (summed per-document time > wall time) |
| five | 5 | 5002 ms | 3639 ms | unchanged path |
| home_loan_full | 6 | 5312 ms | 4399 ms | unchanged path |
| lap_full_default | 4 | 6015 ms | 3223 ms | unchanged path |
| **edge_scanned_pdf (4-page SBI scan)** | 1 | **23060 ms** | **18619 ms** | **the only changed path: −19 %, same output (REVIEW, 40 rows)** |
| edge_large_pdf (~40 text pages) | 1 | 2221 ms | 2059 ms | native text layer, no OCR |
| edge_malformed | 1 | 8 ms | 8 ms | FAIL, no crash |
| edge_unreadable_image | 1 | 1828 ms | 1671 ms | FAIL |

Only the scanned-PDF path changed. The other rows moved because the "before" run shared the machine with a regression run. They are reported, not claimed as gains. Peak RSS was 263–327 MB. p95 is not reported: 3 runs is too few.

**The bottleneck, measured with a step profiler:**
- Scanned-statement extraction: the grid strategy rasterised the file and OCR'd page 1 (its ruled summary block), then found no rows.
- The column fallback then rasterised the file again and OCR'd all 4 pages again, one after another.
- Total: 2 rasterisations and 5 page reads (19.7 s warm, 38 s cold).

**The change:** `bank_statement/extract._ScannedPages`, one reader per `extract_bank_statement` call. It rasterises once and OCRs each page at most once, with identical recogniser input. The result is 1 rasterisation and 4 reads, 15.4 s warm in isolation. A regression test (`tests/agents/test_bank_scanned_pages_once.py`) fails on the old code and passes on the new; the 139 bank-statement tests pass.

**Measured and rejected as the default:** reading the pages in parallel on the OCR pool gave 15.3 s warm, the same as sequential. OCR is CPU-bound and each pool engine owns only a share of the ONNX threads, so concurrent pages each ran about 3× slower. Parallel reads only helped a cold start (26.6 s → 20.2 s), and they would occupy every OCR worker the other documents in the request need. They stay available as an opt-in (`BANK_STATEMENT_PARALLEL_PAGES=true`).

**Not introduced: durable job infrastructure (Celery, Redis, RabbitMQ).** No measurement justifies it:
- Large scans are already routed to the existing background OCR queue (`inline_ocr_pages`, `ocr_queue`, with persisted job status).
- Upload sets of 1–6 documents complete in about 3–6 s.

## 4. HL/LAP FOS document matrix (§7)

This is generated from `applicant_agent.config.checklist_for` and confirmed on real cases through `GET /api/v1/fos/checklist/{case}`.

| Product | Required at FOS | Optional |
|---|---|---|
| HOME_LOAN | PAN · BANK_STATEMENT · ADDRESS_PROOF (DL / Passport / Voter ID) · SIGNATURE | ITR · SALARY_SLIP · EMPLOYMENT_PROOF |
| LOAN_AGAINST_PROPERTY | **no product entry → `default`**: PAN · ADDRESS_PROOF | none |
| PERSONAL_LOAN | PAN · ADDRESS_PROOF · BANK_STATEMENT · SIGNATURE | none |
| (no product) | PAN · ADDRESS_PROOF | none |

- **Signature:** enforced for **every** product by the readiness rule (`LOS_SIGNATURE_MANDATORY`, `workflow.signature_items`, cases created from 2026-10-08), even where the checklist does not list it.
- **Property documents:** sale deed / `PROPERTY_OWNERSHIP_PROOF` is a **CPA** requirement for HL and LAP (`stage_requirements.yaml`, status UNCONFIRMED). `SALE_DEED` is not an FOS upload type.
- **Existing checks, unchanged:**
  - Slot/type validation: `accepts`.
  - Wrong-type uploads: `DOCUMENT_TYPE_MISMATCH` → REQUEST_CORRECT_DOCUMENT.
  - Replacements: re-upload supersedes, with tests.
  - Missing / unreadable / unsupported outcomes: covered by the edge benchmarks above, where a malformed or blank file gives a clean FAIL.

**Open business questions (not invented, not encoded):**
1. Should LAP get its own FOS checklist? Today it has no bank statement and no signature slot on display, though the signature is still enforced at readiness.
2. Should property documents be collected at FOS for HL/LAP, or stay CPA-only?
3. Should `employment_type` decide EMPLOYMENT_PROOF / ITR at FOS? Today they are optional.
4. `stage_requirements.yaml` is marked `status: UNCONFIRMED` and needs business sign-off.

## 5. KYC and cross-document validation (§8)

What exists, verified:
- Per-field checks: NAME, DOB, PAN, FATHER_NAME and ADDRESS (component-weighted), with `blocking` and `cpa_gate` per check. Confidence is kept separate from match score. A mismatch routes to review and is never treated as fraud.
- **Application name vs verified PAN** (Sprint 4): MATCH / PARTIAL / MISMATCH / PENDING / UNVERIFIABLE → PASS / REVIEW / BLOCKED / NOT_READY / REVIEW. Only EXACT and TOKEN_SET make a MATCH.

Changed in this sprint:
- The name check is re-evaluated from the current values on every read, with a test.
- Re-uploads supersede the earlier document.

Not added, because no approved rule exists:
- Employment consistency and property consistency. There are no FOS documents for them, so they are NOT_APPLICABLE until the business defines them.

## 6. Conversation (§4) and safe actions (§5)

Live chat-route tests are in `tests/integration/test_conversation_followups.py` (13 tests):
- pending documents
- **"Show me the next two"**
- "why is the first one pending"
- "No, I meant the previous case"
- "what should I do next"
- **"Is it done?" before Confirm**
- "Continue" with a parked draft
- reload with an empty context
- ambiguity ("Which case?")
- an empty filter
- cross-officer isolation (ordinal and id)
- **a stale Confirm button**
- **Confirm after logout**

Three defects found and fixed:
1. "Show me the next two." fell through to "Which case is this about?", because paging only accepted messages of 3 words or fewer. Even when reached, "next N" paged by the *new* size (rows 3–4, not 6–7). Fixes:
   - `case_list.only_pages_the_list()`, built from config vocabulary.
   - `ListQuery.offset` with `next_page()`, so "next N" continues after the last row shown. Show-more uses the same helper.
2. "Is it done?" while an edit waited for Confirm silently **dropped** the proposal, and the old Confirm then answered with a generic menu. Now it answers "Not saved yet … waiting for your Confirm" and re-offers Confirm / Cancel (`status_words`, `edit_waiting`). A Confirm button whose change is gone answers "no longer pending, so nothing was written" (`write_gone`).
3. Logout left the user's pending Confirm executable for up to 30 min. `/api/v1/auth/logout` now ends the session (`case_form.end_session`), and a write proposed before it is void. In production the identity provider's logout must call the same hook; this is listed as a limitation.

Safe actions (`tests/integration/test_safe_action_outcomes.py`, 5 tests):
- Chat stage moves keep the existing idempotency key, `expected_stage` and gate re-evaluation. The change is that **success is said only after the stage is read back**.
- A timeout after COMMIT is reconciled and reported as moved.
- A timeout before the move is reported as not moved.
- An outcome that can't be read back is reported as UNKNOWN (`move_unknown`) and never as success. Every outcome is audited.
- A second click of the same Confirm never acts twice.
- Chat field edits say "Updated" only when the stored value reads back as the new one (`edit_unverified`).

## 7. NLP / retrieval (§10, §12)

`scripts/eval_retrieval.py`, run on the 24 labelled answerable questions plus 10 off-topic questions in the repository (`evals/copilot/assistant_corpus/rag.json`):

| Retriever | P@1 | MRR@5 | hit@5 | median / p95 latency | confident on off-topic |
|---|---|---|---|---|---|
| BM25 | 0.375 | 0.491 | 0.667 | 1.3 / 1.6 ms | 3/10 |
| dense nomic-embed-text | 0.500 | 0.612 | 0.792 | 10.3 / 84.6 ms (first query 10.7 s, which embeds the corpus) | **10/10** |

- **Correction (2026-10-11):** an earlier version of this line said runtime was BM25-only. That was wrong. With no `OLLAMA_URL`, the embedder falls back to `OLLAMA_HOST`, which `.env` sets, so the running server **did** answer from dense retrieval (`knowledge.yaml backend: vector` since 10-09). It is now `backend: lexical` for answers, based on the calibration in the closure section below; the meaning bank keeps nomic.
- **The quoted baseline is not reproduced here.** The 0.805 / 0.854 figures came from a different 41-question set that is not in the repo.
- **Dense ranks better** (+0.125 P@1), but at the configured `vector_threshold` it is confident on every off-topic question.
- **Recommendation:** calibrate the dense threshold on the labelled set first, then enable it with measured false-confidence. **Not changed** in this sprint: changing it unmeasured would trade ranking for false confidence.
- **No fine-tuning or new models:** there is no representative labelled data to justify them.
- **Reference resolution:** handled by deterministic rules; the new follow-up tests show where the rules hold.

## 7b. Closure work (2026-10-11), see docs/CLOSURE_TRACKER.md

**Legacy evals: all pass.** process, RAG (both flags), semantic 178/178, conversation 124/124, family, variant, and `test_mixed` (failing since before Sprint 2). The real defects behind them were fixed:
- the stage-guide routing;
- false confidence on company values (4/10 → 0/10);
- "passport is mandatory" (factually wrong);
- greetings reading the case list;
- generic options for every word;
- option picks by place;
- the which-document question after a one-document question;
- an internal note leaking into a user-facing answer.

The one-word contract was re-baselined per the owner's decision (2026-10-10): clarification or direct answer, never retrieval or a model call, and nothing read before a clarifying question.

**Dense calibration** (`scripts/eval_retrieval.py --calibrate`, `runs/eval/dense_calibration.json`):
- Genuinely off-topic questions score 0.47–0.56.
- In-domain value questions score up to 0.86 and overlap the answerable ones, so no threshold separates them.
- Result: answers use BM25 + abstention; the dense signal is used only for routing (0.80 floor); `vector_threshold` recalibrated 0.53 → 0.57.

**D1 HL/LAP (industry default, UNCONFIRMED until business sign-off):**
- LAP is a selectable product. FOS requires PAN, address proof, bank statement, signature and the property's sale deed; ITR, salary slip and employment proof are optional.
- HL offers the sale deed as optional at FOS (CPA still requires ownership proof).
- LAP demo eligibility policy (LTV 60 %).

**D2 cross-document (industry default, UNCONFIRMED):**
- PROPERTY_OWNERSHIP: a deed buyer must be the applicant or a co-applicant (strict match; MISMATCH → BLOCKED; gated where the deed is required).
- EMPLOYER_CONSISTENCY: one employer across the salary slip and employment proof (a difference is REVIEW, never a block).

**Sessions:** a pending Confirm is bound to the OIDC `sid`; the dev IdP logout voids it.

**Full lifecycle:** `test_full_lifecycle.py`.

**Access-violation dumps:** Windows `CreateProcess` noise, reproduced with no app code; no effect on results.

## 8. External research (§10)

| Topic | Source | Applied / why |
|---|---|---|
| ONNX Runtime threads per session | [onnxruntime.ai: Thread management](https://onnxruntime.ai/docs/performance/tune-performance/threading.html) | Confirms the existing `_default_ocr_threads` split (each session's intra-op pool defaults to the physical-core count; several sessions oversubscribe). It explains why parallel page OCR did not help |
| CPU-bound work in async apps | [Sentry: run_in_executor vs run_in_threadpool](https://sentry.io/answers/fastapi-difference-between-run-in-executor-and-run-in-threadpool/) (secondary) | Async alone does not parallelise CPU work. The existing bounded executors are kept; no process pool, since ONNX releases the GIL and the measurement showed CPU saturation |
| Idempotent writes and safe retries | [Stripe: Idempotent requests](https://docs.stripe.com/api/idempotent_requests) | Matches the existing `idempotency_key` on stage moves and queries, and the new no-op on duplicate form writes |
| Lost updates, advisory locks | [PostgreSQL BUG #19116 thread](https://hackorum.dev/topics/73395); [advisory lock notes](https://reintech.io/blog/exploring-postgresql-advisory-locks-capabilities) | Transaction-scoped advisory lock + re-read inside the transaction (the pattern `case_stage` already used) |
| KYC documents (India) | [RBI Master Direction – KYC](https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=11261) | PAN is not an OVD (address proof comes from the OVD list), consistent with the ADDRESS_PROOF slot. **Product-level FOS requirements are the lender's policy, not RBI's**, which is why §4 records them as business questions |

No YouTube or demo material was used as evidence. Secondary sources are marked as such.

## 9. Changed files (this sprint)

- `app/store/repository.py` (`same_value`, `update_case_fields` default)
- `app/store/sql_repo.py` (transactional `update_case_fields`)
- `app/agents/applicant/copilot/capabilities/case_form.py` (save via the transaction, stale and read-back checks, status words, gone buttons, logout epoch)
- `app/agents/applicant/copilot/capabilities/case_list.py` (`forget_case`, `offset`, `next_page`, `only_pages_the_list`)
- `app/agents/applicant/copilot/capabilities/workspace.py` (`the_other`, paging gate)
- `app/agents/applicant/copilot/capabilities/vague.py` (paging is a command)
- `app/agents/applicant/copilot/capabilities/stage_flow.py` (`move_unknown_text`)
- `app/agents/applicant/copilot/answering/document_actions.py` (no PENDING name line)
- `app/api/routes/fos_api.py` (`expected` / `STALE_FORM`, move reconciliation and read-back)
- `app/api/routes/auth_api.py`, `app/security/dev_idp.py` (logout ends pending writes)
- `app/agents/bank_statement/extract.py` (`_ScannedPages`)
- `app/config/product_flow.yaml` (`edit_stale`, `status_words`, `edit_waiting`, `write_gone`, `edit_unverified`, `move_unknown`)
- `app/config/applicant_agent.yaml` (`the_other`)
- `.env` (one line: `APPLICANT_AGENT_LLM_FOR_SIMPLE_INTENTS=false`, owner decision; backup `.env.bak-before-simple-intents-off`)
- New scripts: `scripts/bench_documents.py`, `scripts/eval_retrieval.py`
- New tests:
  - `tests/integration/test_name_correction_persists.py` (7)
  - `tests/integration/test_conversation_followups.py` (13)
  - `tests/integration/test_safe_action_outcomes.py` (5)
  - `tests/agents/test_bank_scanned_pages_once.py` (2)
  - plus additions to `test_application_name_vs_pan.py`, `test_fos_plan_1_2_case_list.py` and `test_encryption_at_rest.py`

No DB migration was needed. The API changes are additive (`expected` and `changed`). The frontend is untouched.

**Rollback:**
- revert these files; or
- set `BANK_STATEMENT_PARALLEL_PAGES` aside (it is off by default) and restore `.env` from its backup.

## 10. Final regression

_See §11; filled in from `runs/final_regression.log`._

## 11. Release decision

_Pending the final regression._

Reproducible commands:

```
LOS_TEST_PG_DSN=postgresql://postgres:@127.0.0.1:49691/postgres python -m pytest -n 3 tests --ignore=tests/e2e --ignore=tests/integration/test_live_sprint2.py
LOS_TEST_PG_DSN=... python scripts/bench_documents.py --runs 3 --out runs/bench/documents_after.json
OLLAMA_URL=http://127.0.0.1:11434 EMBEDDING_PROVIDER=ollama python scripts/eval_retrieval.py
```
