# Chatbot audit — how it works today (read-only, 2026-10-08)

Scope: the FOS chatbot as it is in the working tree on 2026-10-08 (HEAD `69f7546` + one uncommitted, half-finished edit
to `app/agents/applicant/copilot/capabilities/case_form.py` — see §11). Nothing was changed for this audit; the 10-question
trace (§8) ran in-process against the test Postgres server with the model OFF.

---

## 1. Entry points

| Endpoint | File:function | Accepts | Notes |
|---|---|---|---|
| `POST /api/v1/fos/copilot` | `app/api/routes/fos_api.py:copilot` → `_copilot_json` / `_copilot_upload` | JSON `CopilotRequest` or multipart (upload) | The **full** pipeline (§2). JSON fields that change behaviour: `action` (enum, default `CUSTOM_QUERY`; `LIST_CASES`, `OPEN_CASE`, `EXIT_CASE`, `UPLOAD_DOCUMENT`, `RAISE_QUERY`, `NEW_CASE` …), `message`, `chat_id` (memory + one workspace per chat), `case_id` / `applicant_id` (optional: case in scope), `reply_language` / `response_language`, `context`, `new_chat`, `action_link` (a posted-back link), `co_applicant_id`. Idempotency-Key header honoured. |
| `POST /api/v1/fos/copilot/stream` | `fos_api.py:copilot_stream` | same JSON | SSE: `typing` → one `status` (generic after 700 ms, or a specific hint) → `delta` chunks → `final` `{request_id, markdown, tts}` (`answering/realtime.py:events`). |
| `POST /api/v1/fos/copilot/stop`, `GET /copilot/replay/{id}`, `GET /copilot/updates` | `fos_api.py` | — | cancel / reconnect / "what changed" pushes. |
| `POST /api/v1/fos/action` | `fos_api.py:run_action` | `{href, chat_id, reply_language}` | Every `[Label](action:…)` link: files (xlsx/docx/pdf/view), `{type: open_ui}`, `{type: upload}`, `{type: copy}`, or a chat reply. |
| `POST /api/v1/copilot/query` | `app/api/routes/copilot_api.py:query` → `_query` → `agent.answer_question` | JSON `CopilotQueryRequest` (`message`, `case_id`, `applicant_id`, `party_id`, `stage`, `chat_id`/`conversation_id`, `context`, `language`, `reply_language`, `new_chat`, `channel`) | The "universal" copilot. Shares abuse guard, safety, general layer, workspace and the agent with `/fos/copilot`, but NOT the case-form / stage-flow step or the long post-reply chain (only `faq.apply_unknown`, `why_fallback`, `general.after`, `stage_move_note`). |
| Supporting: `GET /fos/cases`, `/fos/quick-actions`, `/fos/form-schema`, `GET/PUT /fos/cases/{id}/form`, `/fos/exports*`, `/fos/imports*`, `/fos/chats*`, `/fos/cases/{id}/activity`, `/fos/notifications`, `POST /fos/documents` (upload), `/los/cases/{id}/queries*` | `fos_api.py`, `los_api.py` | — | UI + chat share them. |

**What the frontend actually calls** (`frontend/src/runtime/chatbot/hooks/useChatbot.ts`):
- general chat → `POST /api/v1/copilot/query` (`api/client.ts:queryChat`) with `message, case_id, applicant_id, party_id, stage, conversation_id` — **no `chat_id`, no `reply_language`**, and it reads **`res.answer`**, while both endpoints now return `{request_id, markdown, tts}` (contract ON) → the current frontend shows "No answer returned" unless updated (`docs/frontend/CHATLINKS_TSX_INTEGRATION.md` step 1).
- `POST /api/v1/fos/copilot` only when `isDocumentRelatedQuery(message)` **and** the UI has both `caseId` and `applicantId` (`useChatbot.ts:335`).
- uploads → `POST /api/v1/fos/documents`.
- `ChatLinks.tsx` / `chatActions.ts` (`/fos/action`) exist but are **not wired** into `MessageBubble` yet.

---

## 2. Pipeline (`POST /fos/copilot`, a typed message)

`fos_api.py:copilot` → `_copilot_json`:

1. Idempotency replay (`realtime.idempotent`).
2. Body validation → 422 `INVALID_REQUEST` (names the field) — `_copilot_json`.
3. `action_link` → mapped request (`answering/contract.py:request_for`).
4. Chat key, cancel older stream (`realtime.start`); `new_chat` → `contract.forget`; else **memory load** `contract.recall(subject, chat_id)`; `workspace_id = chat_id` (one workspace per chat).
5. **Language lock** (`answering/language_lock.py:set_for_request`).
6. **Abuse guard** (`capabilities/abuse_guard.py:screen`) — foul language → warning, nothing read.
7. **Safety screen** (`capabilities/safety.py:screen`) — rate limit, self-harm, threats, social engineering.
8. **Input guardrail** (`app/security/guardrails.py:check_input`) — injection, leaks, SQL, other people's data (own case list allowed).
9. **Case form / stage flow** (`capabilities/case_form.py:chat_turn` → `stage_flow.propose/step`, `_propose_edit`) — case draft, field edit, query draft / Confirm, move to CPA, collect-from-customer, query status. Writes only after Confirm; the confirmed move executes in the route (`fos_api.move_case_stage`).
10. **General layer** (`capabilities/general.py:answer` → `_fast`) — help / ok / frustration, role / login stage, name, process steps, product checklist, credit-decision decline, EMI/FOIR/LTV calculators, policy numbers, glossary definitions, general knowledge (FAQ → configured facts → handbook BM25), multi-part split; **slow path** `semantics/question_rewrite.py:rewrite` (Qwen) only when not understood (§5).
11. Verify-diagnose redirect (`answering/document_actions.py:asks_to_verify`).
12. **Workspace** (`capabilities/workspace.py:handle`) — list / open / exit / switch / pick by number-id-name, list filters & top-N (`case_list.understand`), "which case?" (`ask_which_case`), replay of the original question after a pick (re-runs step 9 for workflow requests).
13. No-case turn (`fos_api._no_case_turn`) — 1 case → answered for it; several → "Which case is this about?"; none → "no cases" + Create.
14. Case actions (6i) (`capabilities/case_actions.py`), co-applicant resolution (`_resolve_co_applicant`), single-case resolve (`case_pick.py`).
15. **Agent** (`app/agents/applicant/copilot/agent.py:answer_question` → `_conversational` → …) — see 2b.
16. Post-reply chain (in `copilot`): JEV decisions, attention (`answering/attention.py`), document actions, style + abbreviations (`style.py`), PII mask (`safety.mask_output`), in-case header, emphasis, co-applicant header, presentation, KYC table, readiness report, counts, timeline, handoff note, officer tools, professional format (`professional.py`), `faq.apply_unknown`, `case_brief.why_fallback`, `general.after` (did-you-mean / 2-miss fallback), `product_flow.stage_move_note`, sensitivity mask.
17. **Publish** (`contract.publish` → `_markdown`): markdown + links (budget 3, Close always, uploads exempt from no-repeat), tts, remember context per chat.

### 2b. Inside the agent (`agent.py`)
`answer_question` (last-check wrapper: `guardrails.published`, structured enrich, language contract, composer finish) → `_conversational` → party/subject resolution → `answer_question` body (592-):
1. input guardrail again (`guardrails.check_input`), authorised ids;
2. small talk / capability / own history (`conversation.classify`, `request_policy`);
3. queries / verify / stage-gate / pending-work capabilities;
4. **follow-up resolution** (`conversation/followup.py:resolve`);
5. **rules classification** (`_classify_typed` → `semantics/intents.py:classify/understand` + `semantic_frame.py`);
6. **short query** (`semantics/short_query.py`) — one-word questions asked back;
7. **Qwen router** (`semantics/llm_router.py:route`) only when intent is still `UNKNOWN` (example bank first, §3);
8. handoff ("talk to a human"), ownership, read path: MCP tools (§6), deterministic answer (`answering/answer.py`, `phrasing.py`, `status_facts.py`), knowledge (`_knowledge_reply` → `knowledge_answer.py`), snapshot QA;
9. grounding / fidelity validators, `composer.finish` (natural composition flag OFF).

---

## 3. Understanding layer — who decides "what is the user asking"

| Mechanism | Where | Size | Output | Precedence |
|---|---|---|---|---|
| Abuse lexicon | `app/config/abuse_lexicon.yaml`, `abuse_guard.py` | multi-language word lists | block | 1st |
| Safety patterns | `capabilities/safety.py` + config | patterns | block / care reply | 2nd |
| Input guardrail | `app/security/guardrails.py` | ~30 regex rules + decoded forms | allow / refuse category | 3rd |
| Workflow phrases | `product_flow.yaml stage_flow.phrases` (customer_query, free_query, query_status, move, query_to_stage, reply/resolve), `case_form` edit cues | ~120 phrases | draft / Confirm / status | 4th (step 9) |
| General layer | `conversation_general.yaml` (114 phrase strings, 16 question-shape regexes, 10 Hinglish search rewrites, policy subjects/bare words, fee patterns, decisions phrases) + `general.py` | — | a full reply or None | 5th (step 10) |
| Question rewrite (LLM) | `semantics/question_rewrite.py`, config `llm_rewrite` (10 examples) | — | one English question → step 10 again | inside 10, slow path only |
| Workspace / case list phrases | `applicant_agent.yaml chatbot.case_workspace` (exit/switch/list/open…), `case_list.yaml phrases` (top/last/oldest/filters/numbers/not_with) | part of 843 chatbot phrase strings | list / open / exit / pick | 6th (step 12) |
| FAQ | `app/config/faq.yaml` (16 items, 5 answers) | — | steps answer | inside general + agent |
| Normaliser (Hinglish → words) | `app/agents/applicant/normalize.py`, `chatbot.normalization.synonyms` (92) + fuzzy correction | — | rewritten message | before rules in agent |
| Language canonicaliser | `app/agents/applicant/language.py` (+ `languages.yaml`) | lexicons per language | English form (Devanagari etc.) | agent rules |
| Small talk / capability | `conversation.classify`, `request_policy` | phrases | intent | agent step 2 |
| Follow-up resolver | `conversation/followup.py` | rules | rewritten message ("aur LTV?") | agent step 4 |
| **Rules classifier** | `semantics/intents.py` (2 472 lines, 33 intents, 48 compiled regexes + phrase tables) + `semantic_frame.py` (887 lines; concepts in `semantic_concepts.yaml`: 31 concepts, 11 products, 23 protected, 24 conversation) | — | `Classification(intent, confidence)` | agent step 5 — **the main engine for case questions** |
| Short query | `semantics/short_query.py` | — | clarify / resolved question | agent step 6 |
| Example bank (k-NN) | `semantics/embedding_router.py` + `app/config/router_bank.yaml` (154 examples over 10 tools), **char n-gram vectoriser** (not nomic), threshold 0.45, margin 0.05 | — | tool (no model) | agent step 7a |
| Qwen router | `semantics/llm_router.py`, `applicant_agent.yaml chatbot.router` (tool catalogue, JSON schema) | — | tool → canonical question → rules again | agent step 7b, only for `UNKNOWN` |
| Did-you-mean / fallback | `general.after` (router top-2 within 0.05; 2nd miss → 3 closest FAQ + supervisor) | — | clarification | post-reply |

**When they disagree:** there is no voting — **the first layer that produces a reply wins**, in pipeline order (§2). The
agent's internal order is rules → short query → bank → Qwen; the bank and Qwen disagreeing → both offered as a one-tap
question (`agreement: true`). This "first match wins" across ~10 independent phrase systems is the main source of the
failures in §8/§9 (e.g. the case-list filter word "atka" used to close an open case).

---

## 4. RAG

- **Sources:** `knowledge/fos/*.md` — **14 files, ~6 400 words** (address_proof, cibil, cpa_readiness, document_policy,
  document_requirements (+ home / personal), document_statuses, eligibility_terms, faq, fos_workflow,
  verification_guidelines, loan_terms, officer_howto). Front-matter: id, type, `derived_from`, `requires_flag`.
  Plus `app/knowledge/process_knowledge.py` (generic DEMO stage descriptions) and the glossary (16 terms, `glossary.yaml`).
- **Chunking:** by markdown heading (`app/knowledge/markdown_repo.py`); short sections stay whole; word-count units
  (`chunking.py`). Stage-scoped repository (`repository.py`).
- **Retriever actually used:** `KNOWLEDGE_BACKEND` unset → **lexical BM25** with the heading weighted
  (`app/knowledge/__init__.py:get_retriever` → `retriever.LexicalRetriever`), threshold `KNOWLEDGE_MIN_SCORE` = **0.28**.
  The dense path (`EmbeddingRetriever`, nomic-embed-text, cosine ≥ 0.53, `FallbackRetriever` to BM25) exists but is **off**
  (`EMBEDDING_PROVIDER` unset → "hashing" → lexical). No fusion, no reranker (measured worse; `retriever.py` docstring).
- **Second look:** `knowledge_answer.py:_retrieve/_second_look` — canonical-English retry, distinctive-term coverage
  rerank (`knowledge_vocabulary.yaml`, 28 concepts), threshold-question guard (`_asks_threshold`).
- **Vector store:** `app/knowledge/vector_store.py` — Qdrant, two collections (`los_case_context` scoped by app/case,
  `los_process_knowledge`); `QDRANT_URL` unset → **in-memory**. Used by `app/knowledge/grounding.py` (case + process
  evidence for the `/copilot/query` composer). `compose.case_answers: false` → not used to answer case questions today.
- **Passage → answer:** (a) configured fact wins outright (`facts.authoritative_answer`, no model, no passage);
  (b) else retrieved passage → **deterministic extract**, or **Qwen phrasing** when `allow_model` (agent path default True;
  general layer `allow_model: false`), checked by `grounding.validate`, `output_validation`, `fidelity.check` — a rejected
  model sentence falls back to the passage text; (c) general layer adds a relevance guard (`general._about`) and a
  heading tie-break. Citations: `file.md#Heading` in `detail.citations`; user-facing "Source:" lines use plain names.
- **Case questions do NOT use RAG** — they are answered from the structured store via MCP tools (§6). RAG = knowledge/FAQ only.

---

## 5. LLM usage (model `qwen2.5:3b` via Ollama `127.0.0.1:11434`; `qwen3:4b`, `nomic-embed-text` also installed)

| Call | File | When | Settings | Writes user text? | State today |
|---|---|---|---|---|---|
| Router | `semantics/llm_router.py:route` | agent intent `UNKNOWN` after rules + example bank | temp 0, `num_predict` 40, timeout 2.5 s, RAM ≥ 1.5 GB, JSON schema, decision cache; prompt = fixed tool catalogue (2 006 chars) | No — picks a tool → canonical question | **ON** (config) |
| Question rewrite | `semantics/question_rewrite.py:rewrite` | general layer slow path (not understood / "not in KB") | temp 0, 48 tokens, 1.5 s, RAM ≥ 2 GB, JSON `{"q"}`, cache, validated (no new numbers / ids), logged `evals/rewrite_log.jsonl` | No — rewritten question only | **ON** (config); **never measured with the real model** (RAM) |
| Knowledge phrasing | `knowledge_answer.py` (`_SYSTEM`: "Answer ONLY from the reference material…") | agent knowledge path with `allow_model=True` | temp 0.1, max 160 tokens, timeout 2.5 s | **Yes** (validated; fallback to passage) | ON in agent path; OFF in general layer |
| Case answer phrasing | `answering/answer.py` (`_SYSTEM_PROMPT`: "data ALREADY decided… ≤2 sentences") | `use_llm = compose_with_model and llm_enabled and English and …` | temp 0.1, max 180, 2.5 s | Yes (validated) | effectively OFF for simple intents (`llm_for_simple_intents: false`) |
| Grounded composer | `app/knowledge/grounding.py` + `copilot_api._accept_composed` | `/copilot/query` downstream-stage / MIXED | timeout ≤ request budget 2.8 s | Yes (validated twice) | `compose.case_answers: false` |
| Natural composition | `answering/composer.py` ("reword… keep every number") | every reply | temp 0.2, 160 tokens | Yes | **OFF** (`CHATBOT_NATURAL_COMPOSITION` unset) |
| Snapshot QA | `capabilities/snapshot_qa.py` (JSON `{answer, keys, missing}` from document facts) | questions about a document's extracted fields | temp 0, 120 tokens, 3 s, RAM ≥ 1.5 GB | Yes (short, keys checked) | **ON** |
| Semantic frame LLM | `semantic_frame.py:llm_frame` | retired (replaced by router) | 1.5 s, 64 tokens | No | OFF (`COPILOT_UNDERSTANDING_LLM` unset) |
| Case summary | `app/agents/applicant/case_summary.py` / `los/summary.py` | `/fos` case summary endpoint | 1.5 s, 64 tokens | Yes | **OFF** (`LOS_LLM_SUMMARY_ENABLED` unset) |

In the test suite every model call is pinned OFF (`tests/conftest.py:llm_router_off_by_default`).

---

## 6. Case data

- **MCP tools** (`app/mcp/applicant.py` registry, contracts in `app/mcp/contracts.py`): read — `applicant.get`,
  `application.get`, `applications.list`, `co_applicant.get`, `documents.get`, `documents.checklist`,
  `documents.verification`, `workflow.pending_items`, `workflow.next_action`, `workflow.readiness`, `eligibility.get`;
  write — `applicant.create/update`, `application.create/update`, `documents.mark_for_reupload`.
  Plus direct services: queries (`app/agents/los/queries.py`), stage gate (`app/agents/los/stage_gate.py`), KYC gate
  (`app/agents/los/kyc_gate.py`), readiness report (`answering/readiness_report.py`), document actions
  (`answering/document_actions.py`), timeline, activity log, JEV decisions, case memory facts.
- **Facts per case:** application form fields (name, mobile, DOB, address, email, product, loan amount, employment,
  tenure), stage + history, documents (type, party, status, verification verdict, reasons), extracted fields (snapshot
  QA), KYC A (form vs documents) / B (documents vs each other) per party, gate checks (FOS readiness, KYC, signature,
  co-applicant docs), queries/deviations, eligibility verdict (demo policy), timeline / days in stage, activity log.
- **Answerable today (case open, §8 run C):** status, pending documents (+ upload links), why stuck, loan amount and form
  fields, stage / days in stage, CPA readiness report, KYC table, document status per type, queries status, counts,
  customer message / visit checklist, handoff note. Weak: "name match on all documents" (answered from the handbook,
  not the case's KYC B table), co-applicant follow-ups, one-word "docs".

---

## 7. Config and flags

**Config files that change chatbot behaviour** (`app/config/`): `applicant_agent.yaml` (chatbot.*: 46 sections, 843
phrase strings, normalisation 92 synonyms, router, compose, llm, readiness labels, case_workspace phrases, case_actions,
templates), `conversation_general.yaml` (general layer + rewrite), `product_flow.yaml` (case status, home table,
portfolio flow, case brief, stage flow, case form, exports, import, link policy, which-case), `case_list.yaml`,
`copilot_reply.yaml` (contract, links registry, realtime, tts), `faq.yaml`, `glossary.yaml`, `knowledge_vocabulary.yaml`,
`router_bank.yaml`, `languages.yaml`, `tts.yaml`, `abuse_lexicon.yaml`, `queries.yaml`, `stage_gates.yaml`,
`stage_lifecycle.yaml`, `kyc_policies.yaml`, `documents.yaml`, `eligibility_policy.yaml` (DEMO), `jev.yaml`;
`app/agents/applicant/copilot/semantics/semantic_concepts.yaml`; `knowledge/fos/*.md`.

**Flags — effective value now** (env, else config default):

| ON | |
|---|---|
| from config default | COPILOT_LLM_ROUTER, COPILOT_LLM_REWRITE, COPILOT_LANGUAGE_LOCK, COPILOT_PROFESSIONAL_FORMAT, COPILOT_KYC_TABLE, COPILOT_READINESS_REPORT, COPILOT_SNAPSHOT_QA, COPILOT_COUNT_ANSWERS, COPILOT_HANDOFF_NOTE, COPILOT_SMART_UPLOAD, COPILOT_CASE_TIMELINE, COPILOT_MD_TTS_CONTRACT, COPILOT_CASE_LIST_PAGING, COPILOT_FAQ, COPILOT_ABUSE_GUARD, COPILOT_PRODUCT_FLOW, COPILOT_CHAT_CASE_CREATE, general layer, example bank |
| from `.env` | COPILOT_CASE_WORKSPACE, COPILOT_GUARDRAIL_HARDENING, COPILOT_CASE_ACTIONS, COPILOT_DOCUMENT_ACTIONS, COPILOT_VERIFY_DIAGNOSE, COPILOT_RESPONSE_STYLE, COPILOT_EMPHASIS, COPILOT_STREAMING, COPILOT_SESSION_MEMORY, COPILOT_TERMS_KNOWLEDGE, COPILOT_SINGLE_CASE_RESOLVE, COPILOT_PARTY_RECOGNITION, COPILOT_LOCALIZED_KYC_REASONS, LOS_COAPP_IDENTITY, LOS_STAGE_GATE_IN_SERVICE, LOS_FOS_CPA_KYC_RULE, LOS_SIGNATURE_MANDATORY, LOS_COAPP_MANDATORY_DOCS, LOS_CASE_MEMORY_ENABLED, LOS_OCR_WORKER_ENABLED |
| **OFF** | CHATBOT_NATURAL_COMPOSITION, COPILOT_UNDERSTANDING_LLM (deprecated), LOS_LLM_SUMMARY_ENABLED, LOS_LOGIN_SELF_GRANT_LEGACY; `KNOWLEDGE_BACKEND` (→ lexical), `EMBEDDING_PROVIDER` (→ hashing), `QDRANT_URL` (→ in-memory) |

Other: `ENVIRONMENT=development`, `LOS_STORE_BACKEND=postgres`, `OLLAMA_MODEL=qwen2.5:3b`, `LOS_LLM_KEEP_ALIVE=30m`.

---

## 8. Trace — 10 questions (in-process, `/fos/copilot`, one chat per run, model OFF, 2 cases, no documents)

Layer = `response_source` / intent recorded at `contract.publish`.

### Run A — fresh chat, no case open (the questions in the order given)

| # | Question | Handled by → intent | Data | Reply (short) | Verdict |
|---|---|---|---|---|---|
| 1 | show my cases | workspace → `CASE_LIST` | `application.list` | "You have 2 cases… Showing 1-2 of 2" + table | ✅ |
| 2 | what is pending on this case | no-case turn → `CASE_SELECTION` | `application.list` | "Which case is this about?" | ✅ (no case open) |
| 3 | why is this case stuck | workspace list filter "stuck" → `CASE_LIST` | `application.list` | "None of your cases match (past the stage target)." | ❌ "this case" read as a "stuck cases" list |
| 4 | does the name match on all documents | agent → `NOT_IN_SCOPE` | — | "I can only help with your cases, documents, KYC and moving to CPA." | ❌ core KYC-B question lost |
| 5 | what is needed to move to CPA | general → `GENERAL_KNOWLEDGE` | handbook | "KYC runs once the identity documents are uploaded; a failed KYC must be fixed before…" | ⚠️ partial passage, not the move steps |
| 6 | how long till approval | general → `UNKNOWN_TERM` | — | "That isn't in the knowledge base yet…" | ❌ TAT phrase not matched ("not configured" expected) |
| 7 | what is KYC | general → `DEFINITION` | glossary | "KYC (Know Your Customer): the check that…" | ✅ |
| 8 | what's the loan amount | no-case → `CASE_SELECTION` | `application.list` | "Which case is this about?" | ✅ |
| 9 | and the co-applicant? | no-case → `CASE_SELECTION` | — | "Which case is this about?" | ✅ (no case) |
| 10 | docs | no-case → `CASE_SELECTION` | — | "Which case is this about?" | ✅ (no case) |

### Run B — case opened first, then the same 10
Q1 "show my cases" **closes the open case** by design ("CASE-… closed." + list); every later answer is then identical to
Run A. One list question wipes the case context.

### Run C — case open, Q2–Q10 (no list question)

| # | Question | Handled by → intent | Data | Reply (short) | Verdict |
|---|---|---|---|---|---|
| 2 | what is pending on this case | agent → `DOCUMENTS_PENDING` | checklist/documents | "Still pending: PAN, Address Proof, Bank Statement, Signature" + Upload links | ✅ |
| 3 | why is this case stuck | agent → `CASE_HISTORY` | records | "held because the PAN is still outstanding… next step: upload the PAN" | ✅ |
| 4 | does the name match on all documents | agent → `FOS_KNOWLEDGE` | handbook | generic "KYC is checked in two ways: A… B…" | ⚠️ general, not this case's KYC table |
| 5 | what is needed to move to CPA | stage flow → `MOVE_NOT_READY` | gate | "can't move to CPA yet: FOS requirements" | ⚠️ names the catch-all, not the items |
| 6 | how long till approval | agent → `CASE_TIMELINE` | timeline | "0 day(s) in FOS. Within the FOS target of 3 day(s)." | ⚠️ days in stage, not approval time |
| 7 | what is KYC | general → `DEFINITION` | glossary | definition | ✅ |
| 8 | what's the loan amount | agent → `APPLICANT_PROFILE` | `application.get` | "The loan amount on your application is ₹5,00,000." | ✅ |
| 9 | and the co-applicant? | post-reply → `DID_YOU_MEAN` | — | "Do you mean: What is the KYC status? · What is pending?" | ❌ follow-up lost |
| 10 | docs | agent short query → `UNKNOWN` | — | "which one do you mean: co-applicant documents…" + "Maybe one of these: how do i create a case…" | ❌ wrong carry-over + noisy fallback |

Score: Run A 6/10 acceptable, Run C 5/10 fully right + 3 partial.

---

## 9. Honest assessment

**Works well**
- Security & safety layers (abuse, safety, input/output guardrails, ownership on every tool) — 130-attack suite, leak
  refusals; write actions only after Confirm, executed by the same route logic as the UI (`queries.raise_query`,
  `move_case_stage`, `case_form.save`).
- Deterministic case facts with a case open (pending, status, stuck, loan amount, readiness, KYC table, timeline).
- Configured facts beat retrieval; model text is validated or replaced by the source; DEMO policy is labelled.
- Case list at scale (paging, top-N, filters), downloads/links through one action endpoint.

**Fragile**
- **~10 independent phrase systems, first match wins** (§3). A word belonging to one system steals a message meant for
  another ("atka" → stuck-cases list; "purana" → oldest list; "his loan" → case referent; "kaunse documents" → product
  list). Each fix adds phrases + guards, which adds new collisions. This is the most common failure path.
- Case context is fragile: a list request closes the open case (Run B); with no case open almost every case question
  becomes "Which case is this about?" (held-out: 44/100).
- Follow-ups on another party ("and the co-applicant?") and one-word questions ("docs").
- Knowledge corpus is small (14 files) and BM25-only; semantic retrieval (measured better P@1 0.805 vs 0.537) is off.
- The frontend is out of sync with the backend contract (`answer` vs `markdown`, no `chat_id`), and only uses
  `/fos/copilot` for document questions.

**Duplicated / dead**
- Two pipelines (`/fos/copilot` full, `/copilot/query` partial) with re-implemented steps (general layer placement,
  post-reply chains differ).
- Two "raise query" phrase sets (`applicant_agent.yaml case_actions.phrases.raise_query` and `product_flow.yaml
  stage_flow.phrases.free_query/customer_query`).
- `semantic_frame.llm_frame` (retired), `CHATBOT_NATURAL_COMPOSITION` composer (off), dense retriever (off),
  `case_summary` LLM (off), Qdrant case-context index (built, not used for answers while `compose.case_answers` is off).
- Several "unknown" texts ("I don't know that yet", "isn't in the knowledge base yet", "I can only help with…").

**Measured numbers (2026-10-08, model OFF)**

| Set | Result |
|---|---|
| General questions, no case (first probe) | 41/91 → 116/116 after fixes (rules tuned on it) |
| Golden set (127, tuned) | 126 correct, **0 wrong**, 1 "which case" |
| Held-out v1 (100, frozen 2026-10-08) | before **27 correct / 9 wrong** → after class fixes **35 correct / 1 wrong** / 15 not-in-KB / 44 which-case / 2 clarify / 3 other; p50 ~40 ms, p95 ~120 ms |
| 10 officer basics, case open | 2/10 → 10/10 (now a test) |
| This trace (§8) | Run A 6/10, Run C 5/10 + 3 partial |
| Qwen rewrite on held-out | **not measured** (needs ~2.2 GB RAM; free RAM ~3.7 GB, floor 2 GB) |

**Environment notes found during the audit:** hundreds of leftover test databases (`los_t_*`, `los_s_*`, `los_tpl_*`) on
the test Postgres server (`%TEMP%\los_pg_test`, port 56265) from interrupted test runs; `pgserver` refused a new session
while that server was running (`LOS_TEST_PG_DSN` works around it).

---

## 10. English-only: what could be dropped or simplified

- Language lock and reply-language plumbing (`answering/language_lock.py`, `language_gateway`, `reply_language` fields).
- `app/agents/applicant/language.py` canonicaliser + `languages.yaml` lexicons (Hindi, Marathi, Devanagari detection,
  other scripts) and `canonical_forms` for the security policy.
- Every per-language text map (`en / hi-Latn / hi / mr …`) in `applicant_agent.yaml`, `product_flow.yaml`,
  `copilot_reply.yaml`, `conversation_general.yaml`, `case_list.yaml`, `faq.yaml`, `glossary.yaml` meanings, `tts.yaml`.
- Hinglish phrase lists (normalisation synonyms 92, `kya hai / kaise / karo / dikhao / baaki…` across phrase systems),
  Hinglish question-shape rewrites (`general_question.retrieval_rewrites`), Hinglish router/rewrite prompt clauses.
- `COPILOT_LOCALIZED_KYC_REASONS`, translated KYC reasons, client translate path (`frontend …/utils/translate.ts`).
- Abuse lexicon reduced to English. Keep: the English rules, the guardrails (decoded/other-language attacks still matter
  for security even if replies are English-only).

---

## 11. Not part of the audit — state of the working tree

- Uncommitted, **half-finished** edit in `app/agents/applicant/copilot/capabilities/case_form.py` (chat edits: lakh
  amounts, "X ki jagah Y", stray ":"), started before this audit request; it parses but is untested and references a
  config list `value_fillers` that does not exist yet (treated as empty). Untracked: `tests/integration/test_zz_edit_probe.py`,
  `runs/edit_probe.md` (a probe).

---

## Options (no recommendation yet)

1. Keep the architecture; consolidate the phrase systems into one ordered intent table with tests per collision.
2. One understanding step up front: an LLM (or a trained classifier) maps every message to {intent, slots, case scope},
   rules only as a fast cache; answers stay deterministic from tools/KB.
3. Turn on semantic retrieval (nomic dense, measured better) and grow the knowledge base; keep configured facts first.
4. Make case scope sticky: a list never closes the open case; follow-ups inherit the case/party.
5. One endpoint for chat (retire the partial `/copilot/query` path) and align the frontend to the `{markdown, tts}` contract.
6. English-only simplification (§10) to cut the phrase surface roughly in half.
7. Measure before choosing: run the frozen held-out set with the real model (needs ~2.2 GB free RAM).
