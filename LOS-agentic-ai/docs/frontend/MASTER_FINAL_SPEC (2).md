# FOS Copilot: Master Final Spec v2 (single source of truth)

This is the ONE complete instruction for finishing the FOS copilot. Build everything in it, end to end, without waiting for more prompts.
It supersedes every earlier spec and prompt where they conflict. Work already done that matches it stays; do not redo it.

---

## 0. Your role and mindset

You are a senior conversational-AI engineer who has shipped banking copilots used by thousands of loan officers. In lending, one wrong number destroys trust, and officers judge a bot in the first five minutes.

Think like the officer in the field, customer sitting in front of them: they type fast, in Hinglish, with typos, half sentences and pronouns; they want the answer first, then what to do; they never want to repeat context; they ask things nobody planned for.

Make the bot behave like a sharp, careful colleague: understands anything, never makes things up, says clearly when something is not known, always moves the case forward. Before building each part ask: "What would a smart, experienced FOS colleague say here?" Build that from data and config, not hardcoded sentences.

## 1. Golden rules

1. Never a wrong answer. Every fact (number, name, ID, status, date, document, count) comes from the database or recorded findings; every generated sentence is fact-checked. Unverifiable means unsaid.
2. Unsure: one short clarifying question with 2 to 3 options. Never guess.
3. Data missing: say exactly what is missing and where it would come from.
4. Never change data or stages from chat. Writes are proposed and need explicit confirmation; stage moves are done by a person.
5. Scope enforced in code on every read and action.
6. Nothing hardcoded: phrases, synonyms, FAQ, templates, thresholds, checklists, readiness rules, labels live in config or the example bank.
7. One answer, said once. No duplicated text anywhere.
8. Same question, same facts, every time.

## 2. Login and scope (no applicant ID at login)

- Login is username/password (JWT) only. Remove any requirement to enter an applicant or application ID at login (backend and the reference widget). If the current frontend sends one, ignore it for scoping and document that it is no longer needed.
- The logged-in user sees ONLY the cases under them: the cases they created or were granted (access_grants by JWT subject). Inspect read-only how ownership is recorded today, report it, and make this the single scope rule.
- "my cases", "list all case", "mere cases", "saare cases" always return the caller's own list with a natural follow-up, never a refusal.
- Applicant ID typed in chat ("APP-B597637EF37D ke cases", "is applicant ke case dikhao"): list that applicant's cases, only those within the caller's scope. One case: open it. Several: list and ask which. Not in scope or unknown: the same neutral reply ("I can't find that applicant in your cases."), never confirming it exists.
- Same for case ID, co-applicant ID and applicant name typed in chat.

## 3. Case list at scale (200+ cases)

Backend:
- Paginated query: server-side limit and offset/cursor, indexed filters, separate cheap count, about 30 s per-user cache.
- A chat reply never contains more than one page (default 5, max 20).
- Filters: stage, needs action, KYC issue, documents pending, ready for CPA, stuck over target, created date range, product.
- Search: applicant name (fuzzy), case ID, applicant ID, co-applicant ID.
- Sort: needs action first (default), most recent, oldest, longest in stage.

Natural language to query (config driven):
- "top 5", "pehle 3", "first 10" → first N by default priority.
- "last 3", "latest 3", "recent cases" → newest N.
- "sabse purane", "oldest" → oldest N.
- "KYC wale", "ready wale", "atke hue", "pending docs wale" → filters.
- "aur dikhao", "next", "agle 5" → next page (remembered in state).
- "list all case", "saare cases" → summary line with counts, first page, "Showing 5 of 200", then options (more, filter, search).

Frontend for scale: in chat at most one page; a "full list" panel uses a separate paginated list endpoint (same scope) with search, filters, sort and virtual scrolling. Clicking a row opens that case in chat.

## 4. Case workspace and follow-ups

- Open a case by click, list number, case ID, applicant ID, co-applicant ID or name. Same name twice → ask which.
- Once open, every question uses that case; the reply starts with a short case line so the officer knows which case is active.
- On open: an automatic review (all checks, issues with fixes) and the FAQ for that state.
- Switch, exit, "the other case" work. A question about another case while one is open: answer if in scope, then ask whether to switch.
- Dialogue state each turn: active case, party, topic, last entities (document, field, check), last answer type, pending question and offered options, list page and filters.
- Rewrite every follow-up into a standalone question from the state before routing ("uska", "aur?", "kyu?", "kab?", "pehla wala", "2", "next"). Log original and rewritten.
- Yes/no and option picks apply to the pending question. Corrections drop the last interpretation and redo it. Full-session memory; a new chat starts fresh.

## 5. Ask anything without wrong answers

Pipeline: guardrail → state update → follow-up rewrite → known question (fast lane, example bank, router) → otherwise case snapshot QA → fact check → format.

- Case snapshot QA: compact masked fact sheet of the active case (application fields, parties, documents with status and reasons, extracted document fields, KYC results, stage and history with timestamps, queries). The model answers only from it and cites fact keys.
- Fact check: every number, name, ID, status, date and document must exist in the fact sheet, else discard and say what is not recorded or ask one question.
- Coverage: case fields, people, stage and CPA, time, counts, document contents (masked), why-questions (reason codes in plain language from config), process knowledge (knowledge base with citations), multi-question messages (up to 3).
- Config aids: synonym and spelling lexicon; glossary both ways (full form on first mention; "X ka matlab" gives full form, meaning and this case's value).
- Out of scope: one polite line and a relevant next option.

## 6. KYC and CPA readiness (two separate checks)

A. Application form vs documents: each document vs what the officer entered on the application form for that party (name, DOB, father's name, address as configured).
B. Cross-document: documents vs each other (e.g. PAN vs Aadhaar vs bank statement).

Per party, show A and B as separate markdown tables:
- A: field | form value | document | document value | result
- B: field | document 1 | value | document 2 | value | result
Then the likely odd one out (when others agree) and what to upload.

When KYC is failed or in review, say plainly: "Not ready for CPA. KYC is not complete." then reasons (A, then B), then "Next step: Ask the customer to upload correct documents that match the application form." naming the exact documents per party, and offer a customer message draft.

CPA readiness from the gate config: "CPA Readiness: X of Y checks passed", grouped; fix-it path ordered by fastest unblock; what-if simulation; all passed → "Ready for CPA. A person must confirm the move." plus the handoff note. Always equal to the real gate result.

## 7. FAQ (industry style, config)

- Catalogue in config: categories (My cases, Case, Documents, KYC, Co-applicant, Moving to CPA, General) with questions per language, the message each sends, conditions (case open, has co-applicant, KYC failed, documents pending, ready, stuck) and priority.
- Context-aware: built from state; hide what does not apply; boost what matters now.
- Shown on a new chat, on case open, on "help/FAQ/kya pooch sakta hoon". Rendered inside the markdown reply (section 8) as a short categorised list the user can click or type.
- Every FAQ item is a golden case and must be answered correctly.

## 8. Response contract: ONLY markdown + tts

Every chat reply (normal and stream) has exactly two content fields, nothing else:

    { "markdown": "...", "tts": "..." }

plus a minimal envelope that is not content: request_id and, for streaming, the event type. Stop sending every other content field (answer, answer_plain, answer_markdown, emphasis, presentation.*, chips, actions, suggestions, blocks). Keep a compatibility flag for one release only if the current widget breaks; the default is the new contract.

markdown (what the screen shows):
- Clean production format, no emojis: first line is the direct answer; then short numbered or bulleted points; markdown tables for comparisons (KYC A/B, documents, case lists); bold only key words; at most one "Next step:" line; about 8 lines except lists and tables.
- Interactive items live inside the markdown as standard links with an action scheme the frontend turns into buttons, e.g. `[Open](action:open_case?id=CASE-852C)`, `[Show more](action:list_more)`, `[Upload PAN](action:upload?doc=PAN&party=applicant)`, `[Copy message](action:copy?ref=draft-1)`, `[Download handoff note](action:handoff_note?case=CASE-852C)`. Suggestions and FAQ questions are links like `[What is pending?](ask:What is pending?)`. Each item appears once. The backend re-checks scope on every action.
- Selected language only (language lock).

tts (what the voice reads):
- Plain spoken sentences in the selected language: no markdown, symbols, tables, links or IDs read character by character.
- Short: the key answer and the next step, at most about 3 sentences. Lists summarised ("Three documents are pending: PAN, bank statement and address proof.").
- Numbers and amounts spoken naturally ("five lakh rupees"), dates spoken naturally, IDs shortened ("case ending 852C").
- Same facts as the markdown, never extra facts.

Tests: only these fields; no sentence repeated in markdown; tts has no markdown or symbols; tts facts are a subset of markdown facts; action links valid and scoped; language lock holds for both.

## 9. Features (deterministic, CPU)

Autopilot review on case open; fix-it path and what-if; smart upload (identify type and party, file it, confirm, report verification in the same chat, ask when unsure); case timeline and days in stage with target flag; customer message draft (selected language, exact items, copy link, never auto-sent); visit checklist per party; CPA handoff note (download link); "checks passed X of Y". All expressed through markdown + tts.

## 10. Frontend (final look)

Update frontend_handoff/ (FRONTEND_API.md, openapi.yaml, example JSONs) and the reference widget so it looks production-grade:
- Clean professional chat UI: readable typography, proper markdown rendering (tables, lists, bold), action links rendered as buttons/chips, consistent spacing, light and dark mode, mobile friendly.
- Login screen with username/password only; after login the greeting shows the user's case summary.
- Full-list side panel (paginated endpoint) for large portfolios.
- TTS: a speaker button per message and an auto-read toggle using browser speech synthesis with the tts field (en-IN / hi-IN voice by selected language). Mic button for voice input.
- Streaming: typing indicator, status line, text appearing live, stop button, retry, timestamps.

## 11. Real-time conversation feel

- Typing event within 300 ms; one generic status event for slow work, only after authorization.
- Stream the markdown progressively (answer line first, then points, then tables/links); tts sent once complete.
- First visible text under 1 s (fast lane) / 1.5 s (router or snapshot); p95 total under 3 s on this CPU.
- Long work (verification, OCR, handoff note): immediate reply, then a pushed follow-up message in the same chat when done.
- A new message or stop cancels the old reply. Reconnect with request_id replays the final message; an idempotency key prevents duplicates.
- Style: short natural acknowledgements where they help, refers back to what was said, uses names, varies phrasing (same facts), proactive next step, notices changes since the last check, short greeting with what needs attention.

## 12. Definition of done and final report

- Everything above built, flags ON in dev, related tests plus core suite passing after each part, golden set updated for every section (login scope, applicant ID in chat, list scale phrases, KYC A/B, follow-ups, FAQ items, markdown/tts contract, no duplication).
- Self-check before stopping: 30 messy multi-turn conversations of your own design (Hinglish, typos, pronouns, paging, case switching, applicant ID in chat, KYC, out of scope). Fix everything wrong. Report wrong answers (must be 0) and clarify rate.
- Do NOT run docs/HARD_TEST_PLAN.md; the owner will ask.

Final report, then STOP:
1. What is built, one line each, with its flag.
2. Exact .env lines to turn everything on.
3. Login/scope finding and how "my cases" and applicant ID in chat work now.
4. A 5-minute demo script.
5. Open items for the owner.

## 13. Working rules

- Order: 14 (applied throughout) → 2 → 8 → 3 → 4 → 5 → 6 → 7 → 11 → 9 → 10 → 12.
- After each part: related tests + core suite (fake model, models unloaded, RAM at least 2 GB), patch in runs/patches/, short CHANGES_LOG entry, two-line progress note, continue.
- STOP only for: a schema change (show SQL and rollback), a new dependency, a test failing twice, RAM below 2 GB.
- No git changes, no .env or secret edits, no killing processes you did not start.

## 14. Nothing hardcoded: what goes in config, what stays in code

Every literal value in this spec is a DEFAULT to put in config, not code:
- All reply sentences and templates, per language (including "Not ready for CPA. KYC is not complete.", "I can't find that applicant in your cases.", greetings, acknowledgements, status-event texts, out-of-scope lines), with 2 to 4 variants each.
- Page sizes (default 5, max 20), list cache TTL (30 s), list filters, sorts and their phrases ("top 5", "last 3", "sabse purane", "aur dikhao", "KYC wale"…).
- FAQ categories, questions, conditions and priority.
- KYC fields per check (from kyc_policies.yaml) and readiness groups/requirements (from stage_gates.yaml and checklists).
- Synonyms, spelling variants, glossary.
- TTS rules: sentence limit, how numbers, amounts, dates and IDs are spoken, per language.
- Latency budgets and the typing-event timing.
- Action-link names: one registry (name, parameters, scope rule), documented for the frontend.
- Plausibility minimums, stuck-in-stage targets.

These stay in code on purpose and are never configurable:
- Scope enforcement on every read and action.
- Guardrail before everything else.
- The fact check on every generated sentence.
- No data or stage change from chat without confirmation; stage moves only by a person.
- The markdown + tts response contract.
- The pipeline order.

Proof:
- A test scans the copilot code for user-facing sentences outside the config/template files and fails if it finds any (allow-list only for developer logs and errors).
- A test loads each config with a changed value (e.g. page size 7, a new FAQ item, a new synonym) and checks the behaviour follows without code changes.
- The final report lists every config file the officer-facing behaviour comes from.

---

## 15. The approved product flow (sir's flow): everything above must match this

Where this section and earlier sections differ, THIS section wins.

### 15.1 UI flow
1. Login: username, password and stage. Failed login → a clear message. Success → Home.
2. Home: one table of the user's own cases. Columns: Case ID, App ID, Applicant name, Stage, Status (Pending/Done, mapping from real statuses in config), Created date, Action. Search (Case ID, App ID, name), filter (status, stage), sort, server pagination, "No cases yet" empty state, loading state, mobile friendly (horizontal scroll, large buttons).
3. "New Case" button at the top → the blank form, same flow as today.
4. Click an existing row → the same form with all saved data filled, continue where the user stopped.
5. Same backend and database as the chatbot; a case created or changed anywhere shows everywhere after refresh.

### 15.2 Chatbot flow
1. Same login as the UI (same JWT). No second login, no applicant ID.
2. Two kinds of questions, both free text:
   - Case questions ("give me details of my cases", anything about a case or its documents) → answered from data (sections 3 to 6).
   - FAQ questions ("What is KYC?", "How do I upload a document?") → short clear reply; numbered steps for "how to" questions; from the knowledge base/FAQ config with citations. Not in the FAQ or data → "I don't know that yet" style reply from config; never guess.
3. Case answers contain: a small summary, plus links rendered as buttons: Download Excel, Download Doc, Show in UI.
4. After a case-portfolio answer, a follow-up from config: "Do you want to know about a particular case?" with Yes / No.
   - No → chat continues normally.
   - Yes → "Pending or Done?" with both as buttons → the list of that type (paged, section 3) → the user picks one → the case opens (section 4) and the bot asks what they want to know (status, pending items, documents, KYC, CPA readiness).
   - The user can also skip this and type anything at any point.
5. Quick buttons always available: My pending cases, New case, How to upload, FAQ.
6. Every reply about a case or an action includes "Show in UI" (opens the same case on the main screen).

### 15.3 Actions from chat (UI parity)
- Check status, list, filter, search, export: available in chat.
- Export: Excel (single case and filtered lists) and Doc (case summary / handoff note). Use existing libraries; if none can write xlsx or docx, STOP and ask before adding a dependency. Downloads are scope-checked, short-lived links, audit-logged.
- New case from chat: default is a "New Case" button that opens the UI form. Full creation in chat is behind a flag (off) until confirmed; if enabled, the bot collects the fields, shows a summary and asks "Confirm?" before creating.
- Import: Excel import in the UI (and from chat via upload) with validation first and row-by-row errors in plain English; nothing written until the user confirms.
- Every write (create, import, send, query) shows a summary and asks "Confirm?" first.

### 15.4 Supporting features
- Chat history: the user can see their old chats (masked, retention from config).
- Activity log: who created or changed what and when (UI and chat).
- Notifications: e.g. "CASE-123 has been pending for 5 days" (threshold from config), shown in the chat greeting and the home screen.
- Plain-English messages for errors, empty states and loading everywhere.

### 15.5 Owner decisions (confirmed)
- The button and wording is "Create New Case" everywhere (UI and chat), not "New PR".
- Downloads: Excel, Doc and PDF (single case and filtered lists). Existing libraries first; STOP and ask before adding a dependency.
- Chat can do EVERYTHING the frontend can do, fully, including creating a case: the bot collects the same fields as the UI form (same validation rules, from the same config/schema), shows a summary and asks "Confirm?" before creating; also continuing a partly filled case, uploading documents and editing fields, each with Confirm. Inspect the frontend/API to list every action it supports and give the chat the same set (report the list). Turn the "chat case creation" flag ON.
- Statuses: Created, Review, Disbursal (plus any others found in the data). Status mapping is config; the UI table, chat lists, filters ("pending", "review wale", "disbursal wale"), counts and the Pending/Done follow-up all use it. Default grouping: Pending = Created + Review, Done = Disbursal (config).

## 16. Abusive / foul language

- Detection from a config lexicon per language (English, Hindi, Hinglish, Marathi; Roman and Devanagari) with variants: spellings, leetspeak, spaced or dotted letters, masked forms (f***), repeated letters, mixed case. Normalise first; whole-token match; allow-list to avoid false positives on names and places. Severity levels in config (mild, strong, threat).
- If a message contains abusive language, DO NOT answer any part of it, even if it also has a real question. No case data, no tool call, no model call; nothing stored as a pending intent.
- Reply (config templates per language, variants): "Please don't use foul language." + the flagged word(s) in bold (masked by default, e.g. **m*******d**; display mode in config) + "Please rephrase your question respectfully, and I'll help."
- TTS: the warning only, never saying or spelling the word.
- Repeated abuse: cooldown (count and duration in config). Threats and self-harm keep their own handling (security event / caring reply with configured helpline text).
- Logs and stored history: the word masked; the event recorded (user, severity, count), never the raw text.
- Tests: "madarchod what is my kyc status" → warning only, zero tool calls, no KYC data in markdown or tts; variants detected; clean look-alike words not flagged; Hindi/Marathi script; cooldown; tts never contains the word.

### 16.1 Same approach everywhere
The abuse rule applies identically on every channel and surface: typed chat, voice input (speech-to-text output is checked the same way), uploaded/imported text shown back to users, FAQ search, case form fields entered through chat, chat history, notifications, exports (Excel/Doc/PDF), logs and audit. Flagged words are never repeated in plain form anywhere; tts never speaks or spells them; displays use the masked bold form. One shared module and one lexicon serve all of these.

## 17. Production-ready guardrails

Layered, all configurable lists/templates, enforcement in code:
1. Input: size limit, encoding normalisation, the abuse rule (16), prompt-injection and role-play detection (direct, encoded: Base64/hex/URL/leetspeak/other language, multi-turn split), system-prompt/tool leakage requests, SQL/code patterns, authority and social-engineering claims ("manager approved", "bypass KYC", "already verified offline"), requests to change data or stages.
2. Scope: every read, list, download and action checks the logged-in user's scope at the tool boundary; the same neutral reply for "not yours" and "does not exist"; bulk/enumeration requests outside scope refused; guessed or incremented IDs refused.
3. Untrusted content: OCR/document/knowledge text is data, never instructions; instruction-like text is stripped before any model call.
4. Model: the model never sees more than the masked fact sheet; output schema-validated; every fact checked (section 5).
5. Output: no code, paths, URLs (other than registered action links), SQL, tokens, stack traces or internal names; PII masked per policy (PAN, Aadhaar, account numbers, other parties' names and addresses); language lock; no emojis.
6. Rate limits: per-user chat and download limits (config), friendly message when hit.
7. Audit: every refusal, write, export, download and abuse event logged with request_id, masked.
8. Failure: DB/model/tool down → clean specific message, no invented data.
9. Tests: an attack suite of at least 120 cases across these categories (OWASP LLM01-style) must pass before the final report: 100 percent of cross-user, PII and stage-change attacks blocked, zero system prompt or tool leakage.

## 18. Order update

Build order is now: 14 throughout → 2 → 8 → 16 → 17 → 3 → 4 → 15 → 5 → 6 → 7 → 11 → 9 → 10 → 12.

## 19. One approach everywhere (consistency rule)

Every rule in this spec applies identically to every output channel and entry point: markdown, tts, the stream (every chunk and status event), stored chat history, logs and audit, notifications, downloads (Excel/Doc/handoff note), the FAQ, quick buttons, both endpoints (/fos/copilot and /copilot/query) and every language.
- Abusive language (16): the same decision everywhere. If the message is blocked, no channel answers any part of it: markdown shows only the warning, tts speaks only the warning (never the word), the stream sends no status or partial answer, nothing is stored as a pending intent, history and logs keep the word masked.
- Guardrails (17), scope (2), fact check (5), language lock, no emojis, no duplication: the same in markdown and tts and every other channel. tts never carries a fact, word or ID that the markdown would not show.
- One shared decision layer produces the outcome once; every channel only renders it. No channel has its own separate logic.
- Test: for every guardrail and abuse case, assert the same outcome across markdown, tts, stream events, stored history and both endpoints.

## 19. Abuse rule applies everywhere (one shared module)

The section 16 rule is ONE shared function used on every path, so behaviour is identical everywhere:
- Inputs: typed chat (both endpoints), voice input (speech-to-text text), quick-button and FAQ text, action parameters, query/customer-message drafts the officer edits, file names and free-text form fields submitted via chat, import file text fields.
- Outputs: markdown and tts are both built from the same warning template; markdown shows the word bold and masked, tts speaks only the warning.
- Generated text: drafts, summaries, handoff notes and exports are scanned too; a flagged word is masked before anything is shown, spoken, downloaded or stored.
- Storage and logs: chat history, audit, activity log and analytics store the masked form only.
- Streaming: the warning is sent as one complete message (never streamed word by word).
- Tests: the same abusive input via typed chat, voice text, edited draft and an import field gets the same handling; no path returns or stores the raw word.
