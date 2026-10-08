# Chatbot (FOS copilot) -- operator notes

The rules for what the chatbot says live in [docs/CHATBOT_SPEC.md](docs/CHATBOT_SPEC.md).
What changed, step by step, with patches and test results, is in [CHANGES_LOG.md](CHANGES_LOG.md).
General setup (venv, `.env`, auth, tests) is in [README.md](README.md).

---

## Flags

All flags are environment variables (set in `.env`; restart the server after a change), **default OFF** unless
noted. Turn them on in this order, one at a time, watching the chatbot and `/ready` after each.

| # | Flag | Default | Effect |
|---|---|---|---|
| 1 | `COPILOT_SINGLE_CASE_RESOLVE` | off | No case id: one case -> answered ("For application <id>:"); several -> listed to pick from. Display only. |
| 2 | `COPILOT_TERMS_KNOWLEDGE` | off | "What is CIBIL / FOIR / EMI ..." answered from `knowledge/`; "my CIBIL" still routes to Credit. Display only. |
| 3 | `COPILOT_LOCALIZED_KYC_REASONS` | off | KYC mismatch reasons in the user's language; values quoted exactly. Display only. |
| 4 | `COPILOT_DOCUMENT_ACTIONS` | off | "What to upload" view: upload again / still pending / under review, grouped by party. Display only. |
| 5 | `COPILOT_EMPHASIS` | off | Adds `emphasis`, `answer_markdown` (bold key words), `answer_plain`. `answer` unchanged. |
| 6 | `LOS_DEMO_SEED_PROD_GUARD` | off | Demo seed refused when `ENVIRONMENT=production`. **Recommended ON for production.** |
| 7 | `LOS_STAGE_GATE_IN_SERVICE` | off | Every forward stage move is gated inside the service, fail closed; only an approved maker-checker OVERRIDE skips it. |
| 8 | `LOS_COAPP_MANDATORY_DOCS` | off | Co-applicant PAN / address proof / employment proof block FOS readiness (`applicant_agent.yaml` -> `readiness.co_applicant_documents`). |
| 9 | `LOS_SIGNATURE_MANDATORY` | off | Signature presence check for every party, new cases only. **Set `readiness.signature_mandatory.activation_date` the same day** -- without it every case is blocked, startup logs a CONFIGURATION ERROR and `/ready` is DEGRADED. |
| 10 | `LOS_FOS_CPA_KYC_RULE` | off | FOS -> CPA also needs every `cpa_gate` KYC check PASSED for every party. **Last**, after KYC policy sign-off; run `python -m scripts.report_cpa_kyc_gaps` first. |
| 11 | `LOS_COAPP_IDENTITY` | off | Co-applicant ids (`COAPP-<12 hex>`) in chat, upload and MCP. Only after migration 0004 (+ backfill); without the table the feature stays off and `/ready` is DEGRADED. |
| -- | `COPILOT_LLM_ROUTER` | **ON** (step 6b) | Qwen JSON router, consulted ONLY when the rules cannot read a question; picks one tool from `applicant_agent.yaml` -> `chatbot.router.tools`. `false` = emergency kill-switch. Free RAM < 1.5 GB, Ollama down, > 2.5 s or invalid output -> one clarifying question, never an error. `COPILOT_UNDERSTANDING_LLM` is a deprecated alias (startup warning; removed after go-live). Tests pin it OFF (`tests/conftest.py`). |

| 12 | `COPILOT_CASE_WORKSPACE` | off | 6-MVP: "mere cases", clickable cases, open / exit / switch (contract below). |
| 13 | `COPILOT_RESPONSE_STYLE` | off | 6e: status emoji, one 👉 next step, abbreviation rule (`app/config/glossary.yaml`). |
| 14 | `COPILOT_SESSION_MEMORY` | off | 6c: 24 h session, labels memory + summary for the router, "pehle wala case". Text history needs migration 0006 (proposal). |
| 15 | `COPILOT_VERIFY_DIAGNOSE` | off | 6f: "verify karna hai" -> diagnose every party (fix -> pending -> review). |
| 16 | `COPILOT_PARTY_RECOGNITION` | off | 6g: a name that fits two people -> one question (needs `LOS_COAPP_IDENTITY`). |
| 17 | `COPILOT_CASE_ACTIONS` | off | 6i: view document, raise / track queries, new case button (contract below). |
| 18 | `COPILOT_GUARDRAIL_HARDENING` | off | 6h: rate limit, self-harm care, threats, social engineering, abuse cooldown -- both endpoints. |
| 19 | `COPILOT_STREAMING` | off | 7: `/fos/copilot/stream` (SSE). |

### FOS E2E production plan (docs/FOS_E2E_PRODUCTION_PLAN.md) -- config default ON in dev, env overrides

| # | Flag | Dev | **Production value** | Effect |
|---|---|---|---|---|
| 20 | `COPILOT_LANGUAGE_LOCK` | on | **on** | The selected `reply_language` decides every text of the reply (section 2). |
| 21 | `COPILOT_PROFESSIONAL_FORMAT` | on | **on** (`emojis: false`) | No emojis, bullets, one "Next step:" line, `answer_plain` (section 3). |
| 22 | `COPILOT_KYC_TABLE` | on | **on** | KYC table per party + likely odd one out (section 4). |
| 23 | `COPILOT_READINESS_REPORT` | on | **on** | Grouped readiness from the gate config; READY = the live gate (sections 5, 7.1). |
| 24 | `COPILOT_SNAPSHOT_QA` | on | **on** (needs Ollama; off = clarification only) | Long-tail case questions from the masked fact sheet, fact-checked (6.4/6.5). |
| 25 | `COPILOT_COUNT_ANSWERS` | on | **on** | "kitne documents verified" counted from the store (6.6). |
| 26 | `COPILOT_HANDOFF_NOTE` | on | **on** | CPA handoff note for a ready case, md / html / pdf, audited (7.1). |
| 27 | `COPILOT_SMART_UPLOAD` | on | **on** | Upload without a type: what / whose / where, one question when unsure (7.2). |
| 28 | `COPILOT_CASE_TIMELINE` | on | **on** | Timeline, days in stage vs `timeline.targets_days`, past-target flag (7.3). |

**Recommended production set** (everything the FOS chat needs): flags 1-5, 11-28 on (11 and 16 only after migration
0004); 6 (`LOS_DEMO_SEED_PROD_GUARD`) on; `COPILOT_LLM_ROUTER` on; **7, 8, 9, 10 stay OFF until the owner decides**
(stage gate in service, co-app mandatory docs, signature mandatory, FOS -> CPA KYC rule). `/ready` -> `build.chatbot_flags`
shows what a running server has.

Tests: `tests/conftest.py` and `evals/copilot/harness.py` pin every Phase 3 / plan flag to "false" (a test turns on
its own); the golden runner `evals/golden/run.py` turns the plan flags on. Quality gate before a commit:
`python -m evals.golden.gate` (baseline `evals/golden/baseline.json`).

Tests: `tests/conftest.py` pins `LOS_STAGE_GATE_IN_SERVICE` and `LOS_FOS_CPA_KYC_RULE` to false unless a test
opts in, because `main.py` loads `.env` at import.

---

## Demo setup (recommended)

`.env` lines (add / change; restart the server after):
```
COPILOT_CASE_WORKSPACE=true
COPILOT_CASE_ACTIONS=true
COPILOT_RESPONSE_STYLE=true
COPILOT_VERIFY_DIAGNOSE=true
COPILOT_DOCUMENT_ACTIONS=true
COPILOT_TERMS_KNOWLEDGE=true
COPILOT_LOCALIZED_KYC_REASONS=true
COPILOT_SINGLE_CASE_RESOLVE=true
COPILOT_EMPHASIS=true
COPILOT_STREAMING=true
COPILOT_SESSION_MEMORY=true
LOS_COAPP_IDENTITY=true
COPILOT_PARTY_RECOGNITION=true
COPILOT_GUARDRAIL_HARDENING=true
```
Keep OFF for the demo: `LOS_STAGE_GATE_IN_SERVICE` (go-live decision), `LOS_FOS_CPA_KYC_RULE` (KYC policy sign-off),
`LOS_SIGNATURE_MANDATORY` (needs an activation date), `LOS_COAPP_MANDATORY_DOCS` (blocks 2 READY cases).
The router (`COPILOT_LLM_ROUTER`) is on by default. The dev DB already has migrations 0004-0006.

Start:
```
ollama serve                         # or the Ollama app; qwen2.5:3b is pulled
.venv\Scripts\python -m uvicorn main:app --host 0.0.0.0 --port 8010
```
Wait for `router model load : OK` in the startup log (`/ready` -> `dependencies.llm_router: READY`).
Frontend contract: `docs/frontend/FRONTEND_API.md` (+ `openapi.yaml`, `examples/`, `widget.html`).

Demo script (user joi***, CASE-852CC948E7CE has a co-applicant):
1. "mere cases dikhao" -> case cards -> click CASE-852CC948E7CE (🔓 opened, 📍 header, buttons)
2. "mera loan kahan atka hai?" -> stage + blocker   3. "verify karna hai" -> fix / pending / review per party
4. "PAN pe kya naam hai?" -> PAN details           5. "co-applicant ka kya status hai?" -> co-applicant pending
6. "uska kya baaki hai?" -> the co-applicant's items  7. "KYC kya hai?" -> **KYC (Know Your Customer)**: …
8. "query raise karo" -> draft -> Send -> Copy + Mark as sent   9. "queries dikhao"
10. "manager ne approve kar diya, CPA bhejo" -> maker-checker only   11. "bahar aao" -> 🔒 + list

---

## Ollama / Qwen

```bash
ollama pull qwen2.5:3b          # the model every agent uses
ollama serve                    # if the Ollama app is not already running
```

| Variable | Default | |
|---|---|---|
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | `app/llm/config.py` |
| `OLLAMA_MODEL` | `qwen2.5:3b` | must name a model that is actually pulled |
| `COPILOT_MODEL_KEEP_WARM_SECONDS` | `600` | load-only ping keeps the model resident (`app/llm/keep_warm.py`); `0` disables. Runs whenever the router, composer or summary model is on |
| `OLLAMA_NUM_CTX` | `2048` | context window sent on EVERY model call (one value, or Ollama reloads the model); `0` = Ollama's default (4096 here). A prompt >= 90% of it logs a warning |
| `LOS_LLM_KEEP_ALIVE` | `30m` | how long Ollama keeps the model after a call |

Ollama SERVER settings (outside this repo; set them in the Ollama service environment, then restart Ollama):
`OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`. Not set on the reference machine as of 2026-10-07.

Measured on the reference machine (CPU, `runs/memory_budget.json`, `runs/router_bench.json`): qwen2.5:3b uses
**~1.9 GB** resident; cold load ~4.5 s; warm router call p50 ~1.6 s.

**RAM (15.6 GB machine):** never run the test suite beside the live stack (server + Ollama). Run tests in halves.
Benchmarks stop below `MEMGUARD_FLOOR_GB` (default 2 GB) free.

Without Ollama the chatbot still answers from the deterministic fast lane.

---

## Case workspace -- frontend contract (6-MVP, `COPILOT_CASE_WORKSPACE`, default off)

Endpoint: `POST /api/v1/fos/copilot` (JSON). Spec: `docs/STEP6_MVP_CASE_WORKSPACE.md`. Settings, phrases and labels:
`applicant_agent.yaml` -> `chatbot.case_workspace`.

**Requests** (`applicant_id` is optional only while the workspace is on; echo `context` from the last response):

| Action | Body | What happens |
|---|---|---|
| `LIST_CASES` | `{"action": "LIST_CASES"}` | the caller's own cases (live grants), page 1; closes an open case |
| `OPEN_CASE` | `{"action": "OPEN_CASE", "case_id": "CASE-852CC948E7CE"}` | scope-checked (403 if not the caller's), opens it |
| `EXIT_CASE` | `{"action": "EXIT_CASE"}` | closes the open case, shows the list |
| `CUSTOM_QUERY` | `{"message": "mere cases dikhao"}` / `"2"` / `"doosra wala"` / `"CASE-852C kholo"` / `"APP-18E7 ka case"` / `"Priya ka case"` / `"bahar aao"` / `"aur dikhao"` | the same, by words |
| `CUSTOM_QUERY` inside a case | `{"message": "kya baaki hai?"}` (no case_id) | answered for the open case; the answer starts `📍 CASE-xxx` |

**Responses** (beside the usual fields):
```json
{"intent": "CASE_LIST",
 "presentation": {"case_list": [{"number": 1, "case_id": "CASE-852CC948E7CE", "applicant_id": "APP-18E7D65C5048",
                                 "applicant_name": "Rahul S.", "stage": "FOS", "status_label": "⏳ 2 docs pending",
                                 "status_kind": "DOCS", "emoji": "⏳",
                                 "action": {"type": "open_case", "case_id": "CASE-852CC948E7CE"}}],
                  "counts": {"total": 2, "pending": 1, "completed": 1}, "page": 0, "has_more": false},
 "context": {"workspace_id": "default"}}
```
```json
{"intent": "CASE_OPENED", "case_id": "CASE-852CC948E7CE",
 "presentation": {"workspace": {"case_id": "CASE-852CC948E7CE", "header": "📍 CASE-852CC948E7CE",
   "buttons": [{"type": "exit_case", "label": "🔒 Exit"}, {"type": "switch_case", "label": "🔁 Switch case"},
               {"type": "ask", "label": "Kya baaki hai?", "message": "Kya baaki hai?"}, ...]}}}
```
- Render each `case_list` row as a card; a click sends `OPEN_CASE` with its `case_id` (never free text).
- `exit_case` -> `EXIT_CASE`; `switch_case` -> `LIST_CASES`; `ask` -> `CUSTOM_QUERY` with its `message`.
- `status_kind`: `KYC` (⚠️ KYC issue), `DOCS` (⏳ N docs pending), `READY` (✅ ready for CPA), `OTHER`.
- Counts of 0 are omitted. Names are "Rahul S." in the list and in full inside an opened case.
- Two matches for a name -> `intent: CASE_SELECTION`, `clarification_required.reason: CASE_AMBIGUOUS` with options.
- A question naming ANOTHER case while one is open is answered for that case and ends
  `👉 Is case mein jaana hai? (CASE-…)`; the open case does not change.

### Case actions -- frontend contract (6i, `COPILOT_CASE_ACTIONS`, default off)

| Action | Body (besides applicant_id / case_id, or an open workspace case) | Response |
|---|---|---|
| `VIEW_DOCUMENT` | `{"document_id": "<id from documents / document_actions>"}` | `actions: [{"type": "OPEN_URL", "url": "/api/v1/fos/documents/view?token=…", "expires_in": 300}]` -- open it with the same JWT within 5 min |
| `RAISE_QUERY` (or "query raise karo") | none -> a DRAFT | `intent: RAISE_QUERY_DRAFT`, `query_draft`, `actions: [Send (confirm: true), Edit (editable)]` |
| `RAISE_QUERY` | `{"confirm": true, "query": {...the draft, text possibly edited}}` | `intent: RAISE_QUERY`, `query`; with no customer channel also `MARK_QUERY_SENT` + `copy_text` |
| `MARK_QUERY_SENT` | `{"query_id": "…"}` | recorded (audit); the query stays OPEN |
| `LIST_QUERIES` (or "queries dikhao") | none | `queries: [{query_id, reason, status, days_open, reply}]` |
| `NEW_CASE` (or "naya case") | none | `actions: [{"type": "OPEN_UI_NEW_CASE"}]` -- open the new-case form; nothing is created in chat |

Nothing is created or sent without `confirm: true` from the Send button.

---

## How a question is routed, and the learning loop

1. Input guardrail (refusals) -> 2. reply guard ("haan", "pehla wala": asked back) -> 3. FAST LANE (rules,
   follow-ups, small talk; ~5 ms) -> 4. EXAMPLE BANK (`app/config/router_bank.yaml`, char n-grams; ~30 ms) ->
   5. Qwen (only when the bank is unsure; JSON schema = only catalogue tools) -> bank and Qwen disagree -> a one-tap
   question with both options. Settings: `applicant_agent.yaml` -> `chatbot.router` (`embedding`, `constrained`,
   `agreement`).

**Learning loop.** When the router answers a phrasing wrongly (seen in testing, logs or user reports):
1. add it to `evals/router_misses.yaml` with the right tool (wording only -- never names, numbers or ids):
   `- { text: "kab tak hoga", tool: case_status, source: ..., added: YYYY-MM-DD }`
   (`state:` for a follow-up that only means something after a given answer, e.g. `kyu?` after APPLICATION_STATUS);
2. restart the server (the bank is built once per process) -- the phrasing is now answered from the bank, no model;
3. re-measure: `python -m evals.perf.router_tune2 --variants B8` (never beside the test suite; Qwen loads).
A phrasing the RULES get wrong (fast lane) is fixed in the rules / `semantic_concepts.yaml` instead -- the bank only
sees what the rules could not read.

Measurement scripts: `evals/perf/router_ab.py` (prompt formats), `evals/perf/router_tune2.py` (whole pipeline),
`evals/perf/router_latency.py` (latency + the 17-question script).

---

## Backups and migrations

Full commands: README.md -> "Backups before schema changes". In short:

1. **Backup first.** `pg_dump` (custom format). Gated migrations and the backfill refuse without a dump that
   exists, is not empty, is < 24 h old and is a pg_dump (`app/store/backup_check.py`). Pass it as
   `--backup-file`, or `LOS_MIGRATION_BACKUP_FILE` for the dev auto-apply.
2. **Gated migrations** (`postgres_repo.GATED_MIGRATIONS`: 0004, 0005; 0006 planned for 6c):
   - dev: applied at startup only with the feature flag on, a fresh backup, and the precondition met;
   - **production: never automatic.** A person runs `python -m scripts.apply_migration <version> --backup-file <dump>`.
3. **Co-applicant backfill:** dry-run writes a plan with a fingerprint ->
   review -> `python -m scripts.backfill_co_applicants --apply --plan-file <plan> --backup-file <dump>`.
   Revert with `--revert <run_id> --backup-file <dump>` (rollback SQL part 1 before, part 2 after:
   `scripts/sql/rollback_coapp_identity_*.sql`).
4. Real backfill run logs live in `runs/backfills/` (dev DB: only `BF-20261007T062255-901F`). Tests write theirs
   to a temp dir.

Production order: pg_dump -> `apply_migration 0004` -> backfill dry-run, reviewed -> `--apply` -> `apply_migration 0005`.

### Chat history (migration 0006, `COPILOT_SESSION_MEMORY`)
- Table `chat_turns`: every turn MASKED (identifiers + the case's party names / addresses) then ENCRYPTED
  (`crypto.seal_value`); the user is stored only as a salted hash; UNIQUE (user, conversation, turn, role) so a retry
  never duplicates a turn. Apply: pg_dump -> `python -m scripts.apply_migration 0006 --backup-file <dump>` (dev: may
  auto-apply only with `COPILOT_SESSION_MEMORY=true` + `LOS_MIGRATION_BACKUP_FILE`; production: by hand).
- RETENTION, AUTOMATIC: with session memory on, the server runs the cleanup once at startup and then every
  `chatbot.memory.cleanup_interval_hours` (24): turns past `retention_days` (90) are deleted and rollback copies
  `chat_turns_backup_*` older than that are dropped; each run is logged ("chat history cleanup: …").
- MANUAL: `python -m scripts.cleanup_chat_history [--dry-run]`; FORGET ME: `--forget-subject <subject>` (every row
  of that user).
- Rollback: `scripts/sql/rollback_chat_history.sql` (copies the table to `chat_turns_backup_<stamp>` first).

---

## Adding a chatbot (MCP) tool

Example to copy: `co_applicant.get` (step 5d).

1. **Contract** -- `app/mcp/contracts.py`: add a `ToolContract` (name, one-line summary the router reads,
   `input_schema`, `scope_key`, `never=DOWNSTREAM_CONCERNS`). **Every read tool must be scoped by `case_id` or an
   applicant id** -- `tests/integration/test_copilot_applicant_scope.py::test_no_mcp_read_tool_is_unscoped` fails
   otherwise.
2. **Provider** -- same file, the tool -> provider map (`case_store`, `workflow`, `policy_engine`, ...).
3. **Server** -- `app/mcp/case_server.py`: register the handler and **authorize through the case**
   (`access.authorize` / `access.authorize_co_applicant`). The router is never the security layer: the
   guardrail runs before it and every tool enforces ownership itself.
4. **Runtime** -- `app/mcp/runtime.py`: thread any new argument through.
5. **Tests** -- a contract test, a refusal test for another customer's case (403, zero downstream calls), and the
   parity test `tests/integration/test_copilot_provenance.py`.
6. Log it in `CHANGES_LOG.md` and, if it changes what the chatbot says, in `docs/CHATBOT_SPEC.md`.
