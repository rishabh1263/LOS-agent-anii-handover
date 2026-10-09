# FOS Copilot — conversational foundation: audit, implementation, validation (2026-10-09)

Scope: the directive "Zero-hardcoding conversational foundation". Earlier detailed audit: `docs/CHATBOT_AUDIT.md`
(2026-10-08). Evidence files are named inline; every number below was produced by a command in section C.

---

## A. Audit

### A1. Architecture and request flow (verified in this checkout)

| Component | State in this checkout | Evidence |
|---|---|---|
| Chat entry | `POST /api/v1/fos/copilot` (+ `/stream`, `/action`); `/api/v1/copilot/query` is a thin wrapper | `app/api/routes/fos_api.py`, `copilot_api.py` |
| Python agent service | FastAPI `main.py`; the copilot agent `app/agents/applicant/copilot/agent.py` | — |
| Tools | through `app/mcp` (`applicant`, `runtime`): in-process, or PROTOCOL mode where the MCP server re-authorises the caller | `agent.py:_call_tools` |
| LangGraph | used by `app/orchestration/graph.py`, `app/agents/applicant/graph.py`, `app/agents/credit/graph.py`; **the chat path does not run through LangGraph** | grep |
| Qwen | `qwen2.5:3b` via Ollama `127.0.0.1:11434` (chooser, rewrite, general answer, router); `nomic-embed-text` for meaning + RAG | `.env`, `app/llm` |
| RAG | dense (nomic) with BM25 fallback, config `app/config/knowledge.yaml` | `app/knowledge` |
| Conversation state | `ConversationStore` → **PostgreSQL** (`RepositoryConversationStore`, default `store: repository`), keyed by (officer subject, conversation id), inactivity TTL 24 h | `conversation/state.py:_Store` |
| .NET LOS | **not in this checkout** (only .NET-style connection-string keys in `app/store/postgres_repo.py`; a separate `login-api` folder) | grep |

Message path (`fos_api._copilot_json`, CUSTOM_QUERY): action-link map → context recall → language lock → abuse guard →
input guardrail (`security/guardrails` + `request_policy`) → **vague pick / list-question drop / hold-while-draft**
→ chat case create / field edit (`case_form.chat_turn`, Confirm required) → **vague question** → **meaning**
(`semantics/meaning.py`: nomic k-NN over intent examples + paraphrases, entities, follow-ups, Qwen chooser for the
middle band) → general layer (definitions, calculators, policy values, knowledge, validated general answer) →
workspace (open / switch / list / name, `capabilities/workspace.py`) → agent (deterministic intents → MCP tools →
answer composer) → contract publish (`{request_id, markdown, tts}`).

### A2. Capability classification (before this pass → after)

| Capability | Before | After | Evidence |
|---|---|---|---|
| Multi-turn, follow-ups | 2 (implemented, under-tested in Hinglish) | 1 verified | scenario run4 turns 3–8, 13 |
| Applicant switching | 3 partial: "doosre applicant" **blocked as cross-customer**; "pehle wale par wapas" unknown; a name with a case open refused | 1 | run4 turns 9–12, 24; tests |
| Session isolation | 1 (store keyed by subject) | 1 | run4 turn 35; `test_the_conversation_survives_a_restart_and_never_reaches_another_officer` |
| Persistence / recovery | 2 (PostgreSQL store, untested across a cache drop) | 1 | run4 turn 33; test above |
| Clarification | 3: "usko process kar do" → generic menu (9 s) | 1: one question, the supported actions | turn 16 |
| Corrections | 5 missing ("nahi, mera matlab bank statement") | 1 | turn 15; `test_a_correction_...` |
| Hold / cancel | 5 missing ("ruko, abhi action mat lena" → "not recorded") | 1, verified against the store | turns 17, 23; `test_hold_drops_a_pending_draft...` |
| Action execution + verification | 1 for field edit (draft → Confirm → UI form route), stage move (gated transition, `expected_stage`, idempotency key, audit) | 1 | turns 20–21 (500000 → 600000 read back), 26 (refused, stage FOS) |
| Policy grounding | 3: a general-model answer in broken Hinglish shown | 1: readiness / checklist from config; echo / length validator | turn 28; `test_a_model_answer_that_echoes...` |
| Streaming | 1 (SSE contract test) | 1 | `test_api_contract.py` |
| Language | Hinglish understood by meaning examples; replies are English (reply_language) | same | — |

### A3. Defects found by the real conversation (root causes)

| # | Symptom (real transcript) | Root cause |
|---|---|---|
| 1 | "doosre applicant ka status dekhna hai" → "can't provide other customers' private data" | `request_policy` rule `cross_17` (correct for a customer) applied to an officer's own other case |
| 2 | "Priya Verma" → a vague question; "Priya ka pending" with Rahul open → refused | the vague step and meaning did not know case names; the workspace switched by name only with no case open |
| 3 | "pehle wale par wapas chalo", "galat applicant select kiya" → "can't find that applicant" | missing workspace phrases (config) |
| 4 | "ruko, abhi action mat lena" → "This is not recorded on the case." | no hold meaning; a pending draft made meaning skip the message |
| 5 | "usko process kar do" → generic menu after 9 s | no ambiguous-action meaning |
| 6 | "Policy mein exactly kya requirement hai?" → "Policy mein kya requirement hai, depend ke liye lender." | the general model answer was not checked for echo / length; the question had no meaning example |
| 7 | "Is applicant ka status" → the name; "pending kyun hai" → the list; "next stage par bhej sakte" → next step | no Hinglish examples for those intents |
| 8 | Next-step answers had no action | the step's document was not offered as its Upload link |
| 9 | One turn 71 min, one 258 s | embedding call timeout 60 s per call; cold-model loop (a timed-out call cancels Ollama's load, the next call is cold again); not reproduced after the fixes, source not proven (Ollama stall or host sleep) |
| 10 | Upload links `doc=Driving Licence` | `contract._labels` rewrote codes inside link targets |
| 11 | "yes" treated as a vague word | unquoted `yes` / `no` in YAML are booleans |

### A4. Hardcoding audit
See section D.

### A5. Baseline
* The directive's historical reference (3 273 non-integration passed / 4 skipped; 41/41 golden, zero model calls)
  was **not re-verified as a pre-change baseline**: the full suite takes > 1 h on this machine and the owner rule is
  "no models loaded during the suite"; the first baseline run was stopped at 9 % to run the model scenarios. The
  working tree already held the day's earlier (uncommitted) changes, so a "before" snapshot of the committed code
  was not run. What exists: the chatbot suites earlier today, 301 passed / 7 failed (6 fixed since, 1 pre-existing:
  `test_general_coverage`, knowledge-pack wording), and the final full run in section C.

---

## B. Implementation (this pass)

| File | Change |
|---|---|
| `app/api/routes/fos_api.py` | officer switch phrases pass `cross_17` (`_switches_case`); hold while a draft waits (before the form); `control` / `clarify_action` / `list` / `upload` meaning kinds; meaning never rewrites a message that names an own case; next-step Upload link (`_attach_next_step_upload`); co-applicant named on a case without one → as typed |
| `app/agents/applicant/copilot/capabilities/control.py` (new) | hold (drops `write` / `after_pick` / `vague` / `portfolio`, nothing written) and "which action?" (supported actions, config) |
| `app/agents/applicant/copilot/capabilities/workspace.py` | a name with a case open switches to that own case (one match) and answers there; bare name opens it |
| `app/agents/applicant/copilot/capabilities/vague.py` | `names_a_case`, `waits_for`, `is_command`, upload option/party/language from config |
| `app/agents/applicant/copilot/semantics/meaning.py` | strict follow-up (only the slot) + corrections; normaliser for short forms; no-case general preference; clear-lead accept; config cache by mtime; cold-model warm-up on a failed choice |
| `app/agents/applicant/copilot/capabilities/general.py` | `_poor_general_answer` (min words / echo) |
| `app/agents/applicant/copilot/answering/contract.py` | link targets keep codes |
| `app/agents/applicant/copilot/agent.py` | the old router never offers a tool name as an option |
| `app/agents/applicant/copilot/capabilities/product_flow.py` | the list's Yes / No question dropped when the next message is not its answer |
| `app/llm/availability.py` | `warm_in_background` (cold-model loop) |
| `app/knowledge/embeddings.py`, `app/config/knowledge.yaml` | `embed_timeout_seconds: 8` |
| `main.py` | startup warm-up: meaning bank + chat model (meaning on only) |
| config | `intent_catalogue.yaml` (Hinglish examples; intents `work_queue`, `hold_action`, `ambiguous_action`, `upload_document`; `decide.prefer_general_without_case`, `accept_with_lead`; correction fillers), `conversation_general.yaml` (`control`, `vague` additions, `general_llm.min_words/max_echo`), `applicant_agent.yaml` (switch / previous phrases), `case_list.yaml` (pending files) |
| tests | `tests/integration/test_fos_conversation_foundation.py` (17), `tests/integration/test_live_fos_scenarios.py` (opt-in live, hard gates asserted) |

Design notes
* **Conversation state** stays the existing PostgreSQL store (no new store): the open case, case history, pending
  draft / pick / options, last meaning — per (officer, conversation), 24 h TTL. On resume, every fact is re-read
  from the case store (nothing operational is taken from the conversation record).
* **Authorization** is unchanged and server-side: every case read is checked per case (`workspace.authorize`,
  MCP protocol re-authorisation); the switch exception only lets the workspace's *own-case picker* run.
* **Actions**: chat edits and creates go through the UI form's routes after Confirm; the stage move through the
  gated transition (`expected_stage`, idempotency key, audit). The LLM never executes or decides anything; it
  chooses among intents or writes a general explanation that is validated.
* No new dependency, model, database, or service.

---

## C. Tests and measurements

### C1. Commands
* Live scenario (real Qwen + nomic, production flags, in-process FastAPI app, test PostgreSQL):
  `LOS_LIVE_LLM_TESTS=1 python -m pytest tests/integration/test_live_fos_scenarios.py -q` (runs here as run1–run4
  from the same script).
* Foundation regression: `python -m pytest tests/integration/test_fos_conversation_foundation.py -q` → **17 passed**.
* Full regression: `python -m pytest -n 2 -m "not ocr" tests --ignore=tests/integration` and `... tests/integration`
  → see C4.

### C2. The 35-turn FOS conversation (one chat, real models) — `runs/fos_scenarios/run4.md` / `.json`
Synthetic applicants: Rahul Sharma (KYC name mismatch on the bank statement, signature missing; KYC result seeded
as the KYC agent records it), Priya Verma (nothing uploaded), Amit Rao. Test doubles on the chat path: none.

| Run | Wrong | Partial | p50 | p95 | max | Notes |
|---|---|---|---|---|---|---|
| run1 (before fixes) | 16 | — | 2.3 s | 5.1 s | 9.6 s | defects A3 |
| run2 | 4 | — | 2.6 s | 6.9 s | **258 s** | one stalled turn |
| run3 | 3 | — | 1.0 s | 4.9 s | 5.3 s | |
| run4 (final) | **0** | 6 | **0.96 s** | **5.0 s** | 5.8 s | 35 turns, 0 slow-turn dumps |

Rubric per turn: right applicant / scope; answers the question asked; facts from the case store or configured
rules only; no fabricated action; safe. Partial (run4): turn 7 (lists the documents but does not say whether a
clarification alone would do), 8 and 32 ("already uploaded" answered with the pending list), 14 ("did the status
change?" answered with the status, not whether it changed), 15 ("bank statement verified" without the KYC name
mismatch), 33 (restores the case and its pending list but does not name the applicant).

Hard gates (asserted in the live test, observed in run4):
* Unauthorized consequential actions: **0**. Fabricated successes: **0**.
* Field edit: Confirm → `loan_amount` 500000 → **600000** read back from the store; the checklist then required
  Income Proof (the amount-based rule, not scripted).
* Hold with a pending 7-lakh draft: amount still **600000**; a later "confirm" cannot revive it (unit test).
* Move to CPA on a not-ready case: refused with the gate's reasons; stage **FOS** read back.
* Cross-applicant leakage: **0** ("Priya ka pending" with Rahul open → Priya's case only).
* Session recovery after dropping the process caches: the case restored from PostgreSQL (turn 33).
* Another officer with the same chat id: no case, nothing of officer A's (turn 35).

### C3. Measurement areas (directive phase 7)
| Area | Result | Source |
|---|---|---|
| Intent understanding (scenario turns routed by meaning) | 23 decisions: 21 right, 2 debatable (8, 32); **biased**: several scenario sentences were added as catalogue examples after run1 — the unbiased number is the frozen held-out sets (C5) | run4.json `meaning` |
| Reference resolution / switching | 7 of 7 correct (turns 9–13, 24, 33) | run4 |
| Clarification | 2 of 2 one-question with options (16, vague) | run4 |
| Latency | p50 0.96 s, p95 5.0 s (Qwen warm) | run4 |
| Model calls | 0 chooser calls in run4 (all 23 decisions by embedding / follow-up); the old router / general answer may still call Qwen on unmatched turns | run4.json |
| Regression | see C4 | |

### C4. Full regression (final)
Owner choice (2026-10-09): the full suite (> 4 h here, mostly document / OCR / bank-statement tests this pass does
not touch) was replaced by the chatbot integration suites. Not re-run today: the non-integration suite and the
non-chatbot integration tests.

* `pytest -n 2 tests/integration/<chatbot suites: foundation, vague, meaning, general, case list, pipeline, sticky,
  raise query, contract, officer basics, edit values, coverage, workspace, master_*, step6b*, fos_*>`
  (models unloaded, `LOS_TEST_PG_DSN` = the session's test PostgreSQL) → **736 passed, 4 failed, 3 skipped, 1 xfailed**
  (`runs/final_2026-10-09/chatbot_integration.log`).
* The 4: `test_general_coverage` (pre-existing, knowledge-pack wording); three `test_fos_copilot_contract` cases this
  pass caused and then fixed: gibberish ("asdfghjkl") was asked as a vague word (now only known words are vague),
  and "what is the CIBIL score" with a case open was answered as a definition (now a case value, routed downstream).
* Re-run of those + vague + foundation + general + officer basics + selfcheck + nothing-hardcoded → **116 passed**.

### C5. Frozen held-out sets (never tuned on)
`EVAL_RUN=1 EVAL_MODEL=1 EVAL_SETS=heldout_v2_case,heldout_v2_messy,heldout_v2_general,heldout_v2_vague pytest
tests/integration/test_eval_gate.py` (real models) vs this morning's `current` (models off), `runs/eval/*_{current,new}.json`:

| Set | Correct (current → new) | WRONG (current → new) |
|---|---|---|
| case (100) | 66 → **76** | 32 → **23** |
| messy (50) | 32 → **33** | 14 → **14** |
| general (50) | 32 → **38** | 5 → **4** |
| vague (30) | 19 → **28** | — → **0** |

WRONG is not zero: 23 + 14 + 4 = 41 wrong answers remain on held-out text this pass never saw — the main open item.
Latency in this eval: p50 8.2–8.9 s, p95 9.3–18 s, recorded under memory pressure (`min_free_ram_gb` 0.02 during the
run). The same messages re-run in fresh chats right after: 0.1–2.0 s. The 8 s floor's cause is not proven; the
scenario runs (C2) measured p50 0.96 s. The golden set run was stopped (owner: faster); its result is not reported.

### C6. Contextual fragments (owner 2026-10-09)
`capabilities/vague.py contextual()`: the previous question of the chat when it is one of the fragment's readings
(topic from config `case_topics`, or the top-ranked case intents); one clear reading alone when it is not a
multi-reading topic word; else one question. Data always re-read from the case store for the case open now.
* Tests: `tests/integration/test_contextual_fragments.py` — 7 passed (docs: new chat / after pending / after
  verification / after uploaded / after switching applicant / no supporting context; upload with a document in
  context — this one pins the real model's recorded ranking as a test double).
* Real models, real chat path (`runs/fos_scenarios/fragments_real.txt`): new chat "docs" → asks; after "what is
  pending" "docs" → pending list; after "bank statement verified?" "docs" → its status, "upload" → its Upload link;
  case open "status" / "pending" → one question; "kyc" / "next" → answered; after switching to Priya "docs" → Priya's
  pending only. 0.1–0.3 s per fragment.

---

## D. Hardcoding audit (modified files and their call paths)

| Where | Finding | Introduced / existing | Now |
|---|---|---|---|
| `vague.py` upload link | party fixed to `applicant`, language `en` | introduced earlier today | party from the message (`vague.upload_party`), language from the chat |
| `vague.py` / `meaning.py` | intent names, the word "top", 0.78 fallback, filler list in code | introduced earlier today | config (`document_intents_also`, `document_intent`, case_list `first_n`, catalogue thresholds required, `followup_fillers`) |
| `meaning.py` chooser prompt | sentence in code | introduced | `intent_catalogue.yaml model.instructions` |
| `request_policy.cross_17` | blocks an officer's own other applicant | existing (correct for customers) | officer route lets only the workspace's own-case switch through |
| `control.py` | sentences / supported actions | new | config `control.*`; only actions that exist (collect, pending, readiness, the gated move) |
| Production paths | synthetic names (`Rahul`, `Priya`) appear only in docstrings / comments; `config/demo/bureau_demo.yaml` is the existing demo profile | existing | unchanged (demo config, not reachable by the chat path) |
| `test_master_14_nothing_hardcoded` | 7 passed | | |

Static values that remain, and why: link names (`action:upload` etc. — the API contract), flow keys (`write`,
`after_pick`, `vague`, `portfolio` — state schema), meaning kinds (`case`, `control`, … — the catalogue's schema).

---

## E. Known limitations
* "Bank statement is verified" does not add that KYC found its name different (the document-status path does not
  read the KYC finding); the KYC answer itself is correct.
* Replies are English (the chat's `reply_language`); Hinglish is understood, not written.
* Two-part questions ("kis applicant par the? uska pending kya tha?") answer the second part.
* LangGraph is not on the chat path; the directive's "LangGraph orchestration" for chat is not present.
* .NET LOS integration is not in this checkout; not exercised.
* The long stalls (71 min / 258 s) were not reproduced after the fixes; their source is not proven.
* The scenario runs used the in-process app (TestClient) with a dev identity, not a deployed server with real logins.
* `test_general_coverage` (knowledge-pack wording) fails, pre-existing.

## F. Readiness
**READY FOR CONTROLLED TESTING** — the hard gates hold in the real-model scenario and in the regression tests; the
partial answers above and the open limitations need owner review before staging.
