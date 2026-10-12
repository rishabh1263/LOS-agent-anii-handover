# LOS Copilot -- intelligence implementation and evidence (2026-10-11)

Scope: the confirmed gaps from `docs/AUDIT_2026-10-11.md` (Parts 1-2) and the Final Master Engineering Directive.
Built in an isolated git worktree (detached at 3d3bc2d + the 102-path working tree copied in) while full regression
Run 6 ran on the original tree; ported back after Run 6 finished. Nothing committed, pushed or deployed.
All tests use synthetic applicants on the isolated test PostgreSQL (port 49691); no customer data.

Status legend: IMPLEMENTED (code + passing focused tests), PARTIAL, MISSING, BLOCKED (needs an external decision or
resource), NOT VERIFIED (exists, not exercised here).

## 1. Requirements -> evidence

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | Bank-statement-period handbook answer beats the glossary abstention | IMPLEMENTED | `knowledge_answer._no_value_from_general_sources` (handbook hit >= threshold covering >= 60% of distinctive terms); `tests/core/test_rag_handbook_precedence.py` 4/4 |
| 1b | Off-topic abstention ("capital of France") -- REGRESSION from the 2026-10-10 calibration | IMPLEMENTED | `retriever.py` unseen-term weight 3 only for single-anchor questions, never for Hinglish function words; `tests/agents/test_fos_knowledge.py` 40/40 (3 failed before, also in Run 6) |
| 1c | general_llm never pre-empts a grounded answer; never passed off as the handbook | IMPLEMENTED | runs only after a knowledge miss and never with a case in scope (verified in code); answer labelled + `response_source=LLM_GENERAL`; `tests/core/test_general_answer_provenance.py` 3/3 |
| 1d | RAG metrics | IMPLEMENTED | `evals/copilot/rag_metrics` both COPILOT_TERMS_KNOWLEDGE states: retrieval 24/24, top-1 22/24, answers 26/26, citations 26/26, grounded 26/26, no-answer 10/10, false confidence 0/10 |
| 2 | Multi-case count / closest-to-CPA / most-pending / compare, authorised | IMPLEMENTED | `capabilities/case_insights.py`; `tests/integration/test_case_insights.py` 6/6 (another officer's case never counted, ranked or comparable) |
| 2b | "Show only cases pending with KYC" = the KYC filter, not "needs action" | IMPLEMENTED | `case_list.yaml` filter phrases; `test_directive_examples.py` |
| 3 | Routing: progress, CPA blockers, collected documents, DOB, compound | IMPLEMENTED | rules in `intents.py`, catalogue examples + `uploaded_documents` intent; compound never rewritten by meaning; `tests/core/test_routing_audit_rules.py`, `tests/integration/test_routing_audit_phrasings.py` |
| 4 | "why?", "back to the case", ordinal after open | IMPLEMENTED | exit phrase closes only as a command (`exit_rule`, `phrases.stay`); state follow-ups and ordinals never re-understood; `tests/integration/test_followups_audit.py` 6/6 (meaning ON via a spy) |
| 5 | Each document named once (draft, visit checklist, row count, readiness report) | IMPLEMENTED | `officer_tools.py`, `workspace._row_status`, `readiness_report.render`; `test_documents_named_once.py` 3/3, `test_directive_examples.py::test_readiness_names_the_missing_signature_once` |
| 6 | Bounded model-directed fallback planner | IMPLEMENTED, SHIPPED OFF (measured) | `capabilities/fallback_planner.py`; contract `tests/integration/test_fallback_planner.py` 25/25; live benchmark below -> `planner.enabled: false` |
| 7 | Directive section 3 required conversations | IMPLEMENTED | `tests/integration/test_directive_examples.py` 10/10 |
| 8 | Request-size guard before the body is read | IMPLEMENTED | `main.py` middleware (413 from Content-Length); `tests/integration/test_request_size_guard.py` |
| 9 | API-wide rate limit (application layer) | IMPLEMENTED (per process) / BLOCKED (cross-replica, per-IP: gateway) | `app/security/api_rate_limit.py`, `main.py`; `tests/integration/test_request_size_guard.py` (429 + Retry-After, per caller, health exempt, malformed setting keeps the default) |
| 10 | Regressions found by Run 6: "what happens after CPA" (no case) asked "which case?"; "what is foreclosure" got "Foreclosure letter" | IMPLEMENTED | `general._handbook_heading_answers`, `knowledge_answer._definitions_last`; `tests/integration/test_general_coverage.py` 127/127 |

| 11 | A field word never switches the case ("date of birth" vs an applicant named "Birth ...") | IMPLEMENTED | `workspace._by_name`; `test_directive_examples.py::test_a_field_word_never_switches...` + `test_a_real_name_still_switches` |
| 12 | No case-list read before a dialogue act / a message with no possible name | IMPLEMENTED | `vague.names_a_case`, `conversation/state.is_dialogue_act`; conversations eval 124/124 (was 115/124) |

### Meaning evaluations (2026-10-11/12, offline harness: the eval forces OLLAMA_HOST to a dead port)

| Eval | Now | Clean HEAD | New failures vs HEAD |
|---|---|---|---|
| semantic | 178/178 (unseen 123/123, p50 38 ms) | -- | -- |
| conversations | 124/124 | -- | 0 |
| families | 153/157 | 124/157 | 0 (all 4 remaining also fail at HEAD) |
| variants | 233/246 | 218/246 | 0 (9 missing_fields, 2 security, 1 hinglish, 1 referents -- all fail at HEAD) |

### Live planner benchmark (qwen2.5:3b Q4_K_M, CPU, num_ctx 2048; `runs/planner_benchmark_20261011.json`)

| Metric | Result |
|---|---|
| Unseen questions | 16 (7 not placed by any rule) |
| Correct tool chosen | 4/16 |
| Timed out at the 10 s budget | 12/16; latency p50 10.0 s, max 10.04 s |
| Calls over 3 | 0 |
| Tools outside the read-only allow-list run | 0 |
| Calls on a case other than the turn's | 0 (5 hostile prompts: injection, other case ids, bulk, approve/move, gibberish) |
| Budget enforcement (0.3 s) | timeout at 466 ms |
| Decision | `planner.enabled: false` -- safe but too slow / inaccurate on this host to justify the latency |

## 1c. Run 7 (after the port) -- every failure classified

`runs/final_regression7.log`, 10:27-11:31: **exit 1 -- 3 failed / 7609 passed / 34 skipped / 1 xfailed, 3859 s.**

| Failure | Class | Action |
|---|---|---|
| test_copilot_party_kyc_evals ("which fields did my KYC check compare?" -> CASE_INSIGHT) | REGRESSION (this work: compare shape too broad) | FIXED: compare needs a cases / applicants / case-id object |
| test_universal_copilot::test_the_semantic_fallback_needs_a_case_in_hand | REGRESSION (this work: "how far along" rule too broad) | FIXED: rule limited to this/the case / application / file |
| test_directive_examples::test_a_real_name_still_switches | TEST DATA COLLISION; exposed a PRE-EXISTING defect (clean HEAD too): an ambiguous own name got the other-customers refusal | FIXED: "Which one did you mean?"; tests for both |

Second live benchmark: **qwen3:4b 0/16** (every plan at the 10 s budget) -- the planner stays off.

## 1b. Run 6 (full regression on the pre-port tree) -- every failure classified

Command: `pytest -q -rf -p no:cacheprovider -n 2 -o faulthandler_timeout=900 tests --ignore=tests/e2e
--ignore=tests/integration/test_live_sprint2.py` (LOS_TEST_PG_DSN=port 49691), 20:24-21:22, log
`runs/final_regression6.log`. COMPLETE (not killed): **exit 1 -- 11 failed / 7507 passed / 33 skipped / 1 xfailed,
3467 s.**

| Failure | Class | Evidence | Action |
|---|---|---|---|
| test_fos_knowledge x3 (capital of France) | CONFIRMED REGRESSION (2026-10-10 calibration, unseen weight 3 -> 1) | answered from the glossary's Working capital | FIXED (single-anchor rule) |
| test_general_coverage (2 rows) | CONFIRMED REGRESSIONS (uncommitted 2026-10-10 work; both rows passed at HEAD) | "what happens after CPA" -> "which case?"; "what is foreclosure" -> Foreclosure letter | FIXED |
| test_question_rewrite x5 | ENVIRONMENT | FakeModel.calls == 0: the free-RAM guard (2 GB) skipped it under -n 2 with Qwen loaded; all pass alone | none (tests depend on free RAM) |
| test_one_pipeline | ORDER-DEPENDENT | two routes chose different wording variants ("You applied for" / "The loan amount"); passes alone | none |
| test_vague_messages (pick answers) | TEST ISOLATION + LIVE-MODEL NONDETERMINISM | fixed chat id "v1" in the persistent test store; with live Ollama the meaning layer sometimes answers "docs" directly; 3/3 pass with Ollama unreachable | test hardened (fresh chat id; asserts the picked fact, not a link count) -- ASSERTION CHANGED, review |

No security, authorisation or data-integrity failure.

## 2. Fallback planner -- contract

- Trigger: an open case, a typed question no rule places (`classify` UNKNOWN) and whose fuller reading is not
  high-confidence. A placed question never reaches the model (tested).
- Tools: read-only catalogue case intents only (`intent_catalogue.yaml planner.tools`); no upload, draft, hold,
  collect, write, approval or financial-decision action. Each call is answered by the deterministic engine via its
  canonical question -- the model never writes the answer and never reaches a tool directly.
- Validation (one invalid call rejects the whole plan, nothing runs): tool allow-listed; `case_id` non-empty, equal
  to the turn's case and authorised (`workspace.authorize`); `document` non-empty and known when the tool needs one;
  `party` APPLICANT/CO_APPLICANT; <= 3 calls; duplicates dropped.
- Budget: model + lookups share 10 s (`asyncio.wait_for`). Failure modes -> the deterministic reply stands:
  model unavailable / low memory, timeout, malformed JSON, rejected plan, every lookup failing. A failed lookup among
  successful ones is named ("I could not look up: ..."), never filled in.
- Trace: `understanding.fallback_planner` = {status, reason, calls, failed, ms}; the prompt carries the masked
  message and the case id, never the applicant's name.

## 3. Reconciliation findings (directive sections 5, 6, 11)

- Model: qwen2.5:3b Q4_K_M via Ollama; `OLLAMA_NUM_CTX` default 2048 (one value for every caller to avoid reloads).
  The app passes no `tools=`; the planner uses Ollama's JSON-schema `format` instead (same mechanism the meaning
  chooser uses). Context length NOT changed: no composition-fidelity measurement justifies it yet.
- MCP: a genuine protocol runtime exists -- FastMCP case server (`app/mcp/case_server.py`), `mcp.ClientSession`
  over memory / stdio / streamable HTTP, caller JWT re-validated server-side (`tests/integration/test_mcp_runtime.py`).
  The configured runtime mode is `in_process` (`applicant_agent.yaml mcp.mode`): the live chat does NOT cross the
  protocol. Switching to `protocol` is a deployment decision (server process, transport, URL) -- BLOCKED on that.
- ML: no trained classifier is in the chat path; the meaning bank is nomic-embed-text k-NN + a Qwen chooser. No
  labelled held-out set beyond the eval suites exists, so no supervised component was added (directive 5.3).
- CORS: no CORSMiddleware -- same-origin through the frontend proxy; browsers block cross-origin by default.
- Rate limiting: per-user chat flood guard (30/min) and login limiter only; NO API-wide rate limit -> gateway/infra,
  production blocker.
- Dev IdP: refused outside ENVIRONMENT development/dev/local/test; an unset ENVIRONMENT counts as production.

## 4. Risks / outstanding

- The planner ships OFF: on this CPU the live model timed out on 12/16 questions (section 1, benchmark).
- `in_process` MCP mode in the running app (see above).
- No API-wide rate limit; chunked uploads without Content-Length are bounded only by the route's own 25 MB check.
- Production IdP, multi-replica sessions, load testing, backup/restore: not verifiable in this environment.

## 5. Production blockers (real-environment gates -- NOT verified here; production readiness is NOT claimed)

| # | Blocker | Owner / dependency | Gate |
|---|---|---|---|
| B1 | Cross-replica and per-IP rate limiting | platform / API gateway | gateway policy in front of the service; the in-app limit is per process |
| B2 | Live chat uses `in_process` MCP | deployment decision | run the case MCP server (`app/mcp/case_server.py`), set `LOS_MCP_MODE=protocol`, `LOS_MCP_TRANSPORT=http`, `LOS_MCP_SERVER_URL`, `LOS_MCP_FALLBACK=fail`; then re-run tests/integration/test_mcp_runtime.py against that server |
| B3 | Production IdP, token lifetimes, logout across replicas | identity team | real OIDC issuer/JWKS; dev IdP must stay disabled (ENVIRONMENT unset = production) |
| B4 | Planner with the live model | owner decision on latency budget | tests/integration/test_planner_live_benchmark.py results (section 6); keep `planner.enabled` only if accuracy justifies the latency |
| B5 | Real-document OCR / verification | sanitized authorised fixtures | extraction-to-verification on real samples |
| B6 | Load, backup/restore, monitoring/alerts | SRE | load test on production-like infra; restore drill |
| B7 | Business rules marked DEMO / UNCONFIRMED | credit / policy owners | approval before activation |
| B8 | Hermetic test suite | QA | tests call the live Ollama when reachable (vague/meaning nondeterminism, free-RAM-dependent rewrite tests); run CI with OLLAMA_HOST unreachable or pin the model state |

## 6. Testing-team handoff

Environment: Windows, Python 3.12 venv, PostgreSQL test DSN `LOS_TEST_PG_DSN=postgresql://postgres:@127.0.0.1:<port>/postgres`
(never the dev DB), synthetic applicants only. Unload Qwen before suites (`ollama stop qwen2.5:3b` or keep_alive 0).

| Suite | Command |
|---|---|
| Focused (this work) | `OLLAMA_HOST=http://127.0.0.1:9 pytest -q tests/core/test_rag_handbook_precedence.py tests/core/test_routing_audit_rules.py tests/core/test_general_answer_provenance.py tests/integration/test_case_insights.py tests/integration/test_routing_audit_phrasings.py tests/integration/test_followups_audit.py tests/integration/test_documents_named_once.py tests/integration/test_fallback_planner.py tests/integration/test_directive_examples.py tests/integration/test_request_size_guard.py` |
| RAG metrics | `EMBEDDING_PROVIDER=hashing python -m evals.copilot.rag_metrics` (expect false_confidence 0/10) |
| Meaning evals | `python -m evals.copilot.semantic`, `conversations`, `families`, `variants` (nomic via Ollama) |
| Live planner benchmark | `LOS_LIVE_PLANNER=1 pytest -q -s tests/integration/test_planner_live_benchmark.py` (model loaded, nothing else running) -> `runs/planner_benchmark_20261011.json` |
| Full regression | `pytest -q -rf -p no:cacheprovider -n 2 -o faulthandler_timeout=900 tests --ignore=tests/e2e --ignore=tests/integration/test_live_sprint2.py` |
| UI E2E (stack up) | `scratchpad e2e_stack.sh` + `run_ui_e2e.py` / `run_logout_e2e.py` (officer `sprint3-officer`, test-only credentials) |

Manual checks for testers (chat, a case open unless noted): "how far along is this application?", "what exactly is
blocking CPA?", "which documents did we already collect?", "who is the applicant and what loan did they ask for?",
"what is pending?" then "why?", "what is the loan amount?" then "back to the case: what is pending?" (must NOT close),
"how close is this case to CPA?" (must NOT close), "close this case please" (closes), no case: "show my cases" ->
"open the second one" -> "and the first one?", "which of my cases is closest to CPA?", "compare CASE-A and CASE-B"
(with another officer's id: "can't find"), "Show only cases pending with KYC.", "Is this case ready for CPA?"
(Signature once), no case: "what happens after CPA", "what is foreclosure", "What is the capital of France?" (refused).

## 7. 2026-10-12 update -- generalisation, adapter, routes 2 and 3

| Measure (fresh / held-out unless marked) | Before | After |
|---|---|---|
| Meaning accuracy, set F (60 unseen, never tuned on) | 55% | **73%** (route 3 adapter) |
| Route 2 (Qwen 0.5B LoRA) on set F | -- | 65% (not deployed) |
| Conversation set C (fresh, offline) | 70% | 90% |
| LOS-only boundary, set O | -- | 17/17 |
| Live semantic eval: real understanding misses | 3 | 3 (adapter adds none) |
| Planner live (qwen2.5:3b) | 25%, 10 s | 50%, 6 s -- still OFF |

90% was the owner's target: not reached on fresh questions. Path to 90%+: 2-4 weeks of real (masked) officer
questions, labelled, then retrain the adapter (minutes) -- and route 2 only with that data and a GPU.
