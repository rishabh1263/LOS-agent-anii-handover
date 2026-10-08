# FOS Copilot -- frontend API (Phase 3)

> **Start with `FRONTEND_GUIDE.md`** (same folder): the plain-language guide for the master spec v2 flow
> (login with stage, home table, form, chat replies as `{request_id, markdown, tts}`, action links, downloads,
> import, history, activity, notifications). Where this file describes the older reply envelope (`answer`,
> `presentation`, `chips`, `suggested_questions` ...), that envelope is only returned with
> `COPILOT_MD_TTS_CONTRACT=false` (a one-release compatibility switch); the default is the markdown + tts contract.

Everything the chat UI needs: endpoints, request fields, every action, the response fields to render, and the
buttons. Example payloads: `docs/frontend/examples/*.json`. Machine-readable: `docs/frontend/openapi.yaml`.
A working reference widget (plain HTML + JS, no build): `docs/frontend/widget.html`.

All features below are behind server flags (default OFF); the "demo set" turns them on (README_CHATBOT.md).

## 0. Which backend -- check this first
- **Point the frontend at THIS backend for testing:** base URL `http://127.0.0.1:8010` (the Vite dev proxy
  `/api` -> `http://127.0.0.1:8010`). Tokens come from the same server (`POST /api/v1/auth/login`; its JWKS is
  `http://127.0.0.1:8010/.well-known/jwks.json`).
- **Verify:** `GET /ready` -> `build`:
  ```json
  "build": {"phase": "Phase 3", "step": "FOS E2E plan, section 1", "commit": "b8dc73…",
            "chatbot_flags": {"COPILOT_CASE_WORKSPACE": true, "COPILOT_CASE_ACTIONS": true, "...": true},
            "chatbot_flags_on": 15, "chatbot_flags_total": 15}
  ```
  If `build` is missing, or a flag you need is `false`, the frontend is talking to an older or differently
  configured server (a typical symptom: 422 `WORKSPACE_DISABLED` on "mere cases"). Restart that server after any
  `.env` change -- flags are read at start.

## 0b. Which endpoint the chat widget uses -- ONE: `POST /api/v1/fos/copilot`
- Send **every** chat turn (typed questions and buttons) to `/api/v1/fos/copilot` (or `/copilot/stream`). It carries
  the whole Phase 3 contract: workspace (`presentation.case_list`, `presentation.workspace`), the `📍 CASE` header,
  response style, context-aware `suggested_questions`, `document_actions`, `case_state` counts, guardrail replies,
  and (sections 2-3 of the FOS plan) the language lock and the professional format.
- `/api/v1/copilot/query` (the Universal Copilot) stays for other stages and existing clients. It answers the same
  case questions from the same agent, and with the workspace on it also lists "my cases", answers in the opened case
  and asks "which case?" when none is chosen -- but it has no FOS envelope (no `presentation`, no chips). Do not
  split one chat between the two endpoints by guessing whether a message is "about documents".

## 0c. `case_id` on requests -- the opened case wins
- Inside the workspace send **no** `case_id` / `applicant_id` with chat turns: send back the `context` of the last
  response. The case the officer opened (`OPEN_CASE`, "2 kholo") answers every question.
- If the frontend sends a `case_id` on every request anyway (e.g. the one from login), it is **ignored while a case is
  open** -- the opened case wins, on both endpoints. With no case open, a sent `case_id` is used (ownership-checked;
  someone else's case is 403).
- `OPEN_CASE` always uses its `case_id` (the case to open).

## 0d. Language lock -- send the selected language on every turn
- Send `reply_language` (`en`, `hi`, `hi-Latn`, `mr`; aliases `english`, `hinglish` ...) on every request. It is
  AUTHORITATIVE: `en` selected -> every text of the reply is English (answer, clarifying question, options, buttons,
  chips, refusals, document reasons) even when the user typed Hinglish / Hindi / Marathi. Understanding still works
  in any language. `/fos/copilot` also accepts `response_language`; `/copilot/query` also accepts `language`.
- Nothing sent -> the language is detected from the message, as before.
- Server switch: `COPILOT_LANGUAGE_LOCK` (on in dev; `/ready` -> `build.chatbot_flags`).

## 0e. Format -- professional, no emojis
- With `COPILOT_PROFESSIONAL_FORMAT` on (production default) no reply field or button carries an emoji; lists are
  `- ` bullets; the reply ends with one `**Next step:** ...` line when there is one; `answer_plain` is the answer with
  no `**` markers (render it where markdown is not supported); `format` = `{"style": "PROFESSIONAL", "emojis": false}`.
- Examples in `docs/frontend/examples/` are generated in this format.

## 0a. Amounts: ALL AMOUNTS ARE IN RUPEES, NOT LAKHS
`loan_amount`, `property_value`, `declared_monthly_income` (and every other amount) are sent as full rupees:
`500000`, not `5` and not `"5 lakh"`. Create / update refuses an implausible figure or a unit word
(`lakh`, `cr`, `k`, ...) with **422**:
```json
{"detail": {"request_id": "fos_…", "error": "AMOUNT_IMPLAUSIBLE", "field": "loan_amount",
            "message": "Loan amount looks too small; enter the full amount in rupees, e.g. 500000."}}
```
Show `detail.message` next to the field. If the form collects lakhs, multiply by 100000 before sending. Minimums
are server config (`applicant_agent.yaml` -> `chatbot.plausibility`).

## 1. Auth
Every call: `Authorization: Bearer <JWT>` (the same token the app already uses). A case the user does not own is
**403** -- render the `detail.message`, never retry.

## 2. Endpoints

| Method | Path | Use |
|---|---|---|
| POST | `/api/v1/fos/copilot` | every chat turn and every button (JSON) |
| POST | `/api/v1/fos/copilot/stream` | the same request, as Server-Sent Events (status lines, then the answer) -- `COPILOT_STREAMING` |
| GET | `/api/v1/fos/documents/view?token=…` | open a document from a VIEW_DOCUMENT link (5 min, same user) -- `COPILOT_CASE_ACTIONS` |
| POST | `/api/v1/fos/copilot` (multipart) | document upload; inside an opened case no ids are needed and no `document_types` either (smart upload, 11e) |
| GET | `/api/v1/fos/handoff-note?case_id=…&format=pdf\|md\|html` | the CPA handoff note of a READY case (409 when not ready, 403 when not the caller's) -- `COPILOT_HANDOFF_NOTE` |
| GET | `/ready` | `build` -- which backend and which chatbot flags (section 0) |

### 2a. Build the UI from the server -- no hard-coding
- `GET /api/v1/fos/config` -> `features` (`case_workspace`, `case_actions`, `streaming`, `response_style`,
  `verify_diagnose`, `document_actions`, `guardrail_hardening`, `session_memory`, `party_recognition`,
  `co_applicant_identity`, `llm_router`, `emphasis`: true/false), `endpoints` (`copilot`, `stream` only when
  streaming is on, `view_document` only when case actions are on), and `workspace` (`page_size`,
  `quick_questions`, `list_message`) when the workspace is on. Show a feature's UI only when its flag is true.
- `GET /api/v1/fos/actions` -> only the actions this deployment accepts right now, each with `group`
  (`case` / `workspace` / `case_action`) and `extra_fields` (e.g. `OPEN_CASE` -> `["case_id"]`). A button built from
  this list never returns 422.
Read both once at app start (and after login); a feature switched on or off needs no frontend release.

## 3. Request (`POST /api/v1/fos/copilot`)

```json
{
  "action": "CUSTOM_QUERY",
  "message": "kaunse documents pending hain?",
  "applicant_id": "APP-18E7D65C5048",
  "case_id": "CASE-852CC948E7CE",
  "context": { "...": "echo the `context` object from the PREVIOUS response, unchanged" },
  "response_language": "hi-Latn"
}
```

- **Always echo `context`** from the last response (conversation memory, workspace, abbreviation memory live there).
- `applicant_id` is required, EXCEPT while the case workspace is on (an opened case supplies it).
- Extra fields per action: `document_id` (VIEW_DOCUMENT), `query` + `confirm` (RAISE_QUERY), `query_id`
  (MARK_QUERY_SENT), `co_applicant_id` (optional: ask about one co-applicant).

## 4. Actions

| `action` | Extra fields | What it does |
|---|---|---|
| `CUSTOM_QUERY` | `message` | a typed question (or a quick-question button's `message`) |
| `LIST_CASES` | -- | the user's own cases (case list cards) |
| `OPEN_CASE` | `case_id` | enter a case (a click on a case card) |
| `EXIT_CASE` | -- | close the open case, show the list |
| `VIEW_DOCUMENT` | `document_id` | a 5-minute signed link (`actions[].type = OPEN_URL`) |
| `RAISE_QUERY` | -- / `confirm: true`, `query` | a DRAFT first; created ONLY with `confirm: true` (the Send button) |
| `MARK_QUERY_SENT` | `query_id` | the user sent the copied message to the customer |
| `LIST_QUERIES` | -- | the case's queries |
| `NEW_CASE` | -- | returns an `OPEN_UI_NEW_CASE` button -- open your new-case form |
| `GET_PENDING_ITEMS`, `GET_APPLICATION_STATUS`, … | -- | the existing dropdown actions (unchanged) |

## 5. Response -- what to render

| Field | Render |
|---|---|
| `answer` | the reply text (markdown: `**bold**`, `- ` bullets, emoji) |
| `answer_markdown` / `answer_plain` | when present: markdown for the bubble / plain text for TTS & copy |
| `intent` | for analytics / special views (`CASE_LIST`, `CASE_OPENED`, `RAISE_QUERY_DRAFT`, …) |
| `clarification_required` | `{reason, question, options[]}` -- show `options` as tappable chips; a tap sends the option text (or its number) as `CUSTOM_QUERY` |
| `suggested_questions` | chips; a tap sends the text as `CUSTOM_QUERY` |
| `presentation.case_list[]` | **case cards** (section 6) |
| `presentation.counts` | the header counts (`total`, `pending`, `completed`, `rejected` -- a 0 is omitted) |
| `presentation.workspace` | inside a case: header + buttons (section 7) |
| `document_actions` | grouped "upload again / pending / under review" rows (each may carry `document_id`) |
| `actions[]` | buttons: `OPEN_URL`, `RAISE_QUERY` (Send / Edit), `MARK_QUERY_SENT` (+ `copy_text`), `OPEN_UI_NEW_CASE` |
| `context` | store it; send it back on the next request |
| `errors[]` / HTTP 4xx `detail` | show `message` |

## 6. Case list -> click -> inside the case (`COPILOT_CASE_WORKSPACE`)
1. "mere cases dikhao" / "my cases" or `LIST_CASES` -> `intent: CASE_LIST`,
   `presentation.case_list = [{number, case_id, applicant_id, applicant_name ("Rahul S."), stage, status_label,
   status_kind (KYC|DOCS|READY|OTHER), emoji, action: {type: "open_case", case_id}}]`, `presentation.has_more`.
2. Render each row as a **clickable card**. On click send `{"action": "OPEN_CASE", "case_id": row.case_id}`.
3. Typed selection works too: "2", "doosra wala", "CASE-852C kholo", "APP-18E7 ka case", "Priya ka case".
   Two people with the same name -> `clarification_required.reason = CASE_AMBIGUOUS` with options.
4. `has_more` -> show "aur dikhao" (send it as CUSTOM_QUERY).

## 7. Inside a case
- `intent: CASE_OPENED`: "🔓 **CASE-… (Full Name)** opened." + stage + main blocker + a suggestion.
- `presentation.workspace = {case_id, header: "📍 CASE-…", buttons: [{type: "exit_case"}, {type: "switch_case"},
  {type: "ask", label, message} ×4]}` -- `exit_case` -> `EXIT_CASE`; `switch_case` -> `LIST_CASES`; `ask` ->
  `CUSTOM_QUERY` with its `message`.
- Every later answer starts with `📍 CASE-…`; questions need no `case_id` / `applicant_id`.
- A question about ANOTHER case is answered and ends "👉 Is case mein jaana hai? (CASE-…)" -- the open case does not change.

## 8. Case actions (`COPILOT_CASE_ACTIONS`)
- **View document**: send VIEW_DOCUMENT with `document_id` -> `actions[0] = {type: "OPEN_URL", url, expires_in: 300}`;
  open `url` with the same JWT (a new tab / an inline viewer). 403 after 5 minutes or for another user.
- **Raise query**: "query raise karo" or RAISE_QUERY -> `RAISE_QUERY_DRAFT` + `query_draft` + buttons Send
  (`confirm: true, query`) / Edit (`editable: true` -> let the user edit `query.text`, then Send). Nothing is created
  before Send.
- **No customer channel** exists yet: after Send the reply carries `MARK_QUERY_SENT` with `copy_text` -> show a Copy
  button and a "Mark as sent" button (sends MARK_QUERY_SENT with `query_id`).
- **Track**: "queries dikhao" / LIST_QUERIES -> `queries[] = {query_id, reason, status, days_open, reply}`.
- **New case**: "naya case" / NEW_CASE -> `actions[0].type = OPEN_UI_NEW_CASE` -> open your new-case form.

## 9. Streaming (`COPILOT_STREAMING`)
`POST /api/v1/fos/copilot/stream` with the SAME body. Read `text/event-stream`:
```
event: status   data: {"text": "📄 Checking your documents…", "ms": 3}
event: status   data: {"text": "⏳ Still working…", "ms": 1004}
event: answer   data: { ...exactly the /fos/copilot response..., "latency": {"first_event_ms": 3, "total_ms": 1480} }
event: error    data: {"status": 403, "detail": {...}}
```
Show each `status` as a typing line; replace it with the `answer`. Status lines never contain case data.

## 10. Safety replies (`COPILOT_GUARDRAIL_HARDENING`)
`intent` `RATE_LIMITED` / `COOLDOWN` carry `retry_after_seconds` -- disable the input for that long.
`SELF_HARM_SUPPORT`, `SECURITY_EVENT`, `SOCIAL_ENGINEERING`: render `answer` as is (no buttons).

## 11. FOS production features (FOS E2E plan sections 4-7) -- what to render

All of them work the same on `/api/v1/fos/copilot` and `/api/v1/copilot/query`, follow `reply_language` and the
professional format, and each has its own server flag (all on in dev; `/ready` -> `build.chatbot_flags`).

### 11a. KYC table (`COPILOT_KYC_TABLE`) -- on every KYC answer (`intent: KYC_RESULT`)
`presentation.kyc_table.parties[]`: `{party_role, party_label, status, rows[], odd_one_out[]}`;
`rows[]`: `{check, check_label, document_type, document_label, value_on_document, value_on_application, result
(PASS / PARTIAL / FAIL / NOT_COMPARED), reason}` -- values masked per policy; `odd_one_out[]`: `{check, document_type,
fix: {action: "UPLOAD_DOCUMENT", document_type}}`. The answer text carries the same table in markdown.

### 11b. Readiness report (`COPILOT_READINESS_REPORT`) -- "ready hai kya", "CPA ke liye kya chahiye" (`intent: READINESS`)
`presentation.progress`: `{passed, total, ready}` -> a "checks passed X / Y" bar. `presentation.readiness_report`:
`{ready, gate, passed, total, groups[], unblock_order[], next_fix}`; `groups[]`: `{group (APPLICATION /
APPLICANT_DOCUMENTS / CO_APPLICANT_DOCUMENTS / SIGNATURE / KYC / OTHER), label, counted, note, items[]}`; `items[]`:
`{id, label, status (PASS / PENDING / FAILED / REVIEW), reason, fix}`. `ready` is the live gate's verdict only. A ready
case answers "Case is ready for CPA. A person must confirm the move." -- the chatbot never moves a stage.

### 11c. CPA handoff note (`COPILOT_HANDOFF_NOTE`) -- "handoff note banao" (`intent: HANDOFF_NOTE`)
Ready: the note in `answer` (markdown) + `actions[]` `{type: "OPEN_URL", label, url: "/api/v1/fos/handoff-note?...
&format=pdf"}` (call it with the same Bearer token). Not ready: the refusal with X / Y and the next fix.

### 11d. Long-tail answers (`COPILOT_SNAPSHOT_QA`) -- `intent: CASE_SNAPSHOT`
`snapshot`: `{status: ANSWERED | NOT_RECORDED, keys[], ms}`. ANSWERED = the model answered from the case's facts and
every value was checked against them; NOT_RECORDED = "This is not recorded on the case." + where it would come from.
Render as an ordinary answer.

### 11e. Smart upload (`COPILOT_SMART_UPLOAD`) -- multipart upload with no `document_types`
The answer starts with "This looks like the applicant's PAN. Filed under Applicant > PAN. <verdict>". When unsure:
`clarification_required` `{reason: UPLOAD_TYPE_UNKNOWN | UPLOAD_PARTY_UNSURE, question, options[]}` and, for the
party case, `actions[]` `{type: "UPLOAD_DOCUMENT", document_type, label, co_applicant_id?}` -> offer "upload again as
the co-applicant's". Nothing is re-filed silently.

### 11f. Timeline (`COPILOT_CASE_TIMELINE`) -- "case ka timeline", "kitne din se FOS mein hai", "kal wala verify hua?"
`intent: CASE_TIMELINE`; dated lines in `answer`. A case past its stage's turnaround target shows
"⏰ 6d in FOS (target 3)" in its case-list row and a "Past the FOS target ... Main blocker: ..." line on open.

### 11g. Counts and recovery
- "kitne documents verified hain" -> `intent: DOCUMENT_COUNT`, the number counted from the store (`COPILOT_COUNT_ANSWERS`).
- "ye nahi poocha" -> `clarification_required.reason: MISUNDERSTOOD` with options -> render the options as buttons.

### 11h. Officer tools (FOS plan 9b) -- each its own flag
- **What-if** (`COPILOT_WHAT_IF`): "agar bank statement upload karu toh ready ho jayega?" -> `intent: WHAT_IF`; yes / not yet
  + the remaining items, fastest first. A "yes" still says a person confirms the move.
- **Review on open** (`COPILOT_AUTOPILOT_REVIEW`): `OPEN_CASE` replies carry `review_card` `{passed, total, ready,
  issues[] {label, status, fix}, more, text}` -> render it as one card under the snapshot.
- **Status tables** (`COPILOT_STATUS_TABLES`): `presentation.document_table[]` `{party, document, document_type, status,
  document_id}` and `presentation.progress` `{passed, total, ready}` on case answers.
- **Customer message** (`COPILOT_CUSTOMER_MESSAGE`): "customer ko bata do kya lana hai" -> `intent:
  CUSTOMER_MESSAGE_DRAFT`, `customer_message` `{text, sent: false}`, `actions[]` `COPY_TEXT` / `EDIT_TEXT` (with `text`).
  It is NEVER sent by the server: copy it into the channel you use.
- **Visit checklist** (`COPILOT_VISIT_CHECKLIST`): "visit pe kya le jaun" -> `intent: VISIT_CHECKLIST`, `printable: true`;
  the answer is a per-party `- [ ]` list -> offer Print.

### 11i. Voice input -- frontend only (no API)
A mic button that fills the text box with the browser's speech recognition (`SpeechRecognition` /
`webkitSpeechRecognition`), locale from the selected language (`en` -> `en-IN`, `hi` / `hi-Latn` -> `hi-IN`, `mr` ->
`mr-IN`). The officer reviews the text and presses Send -- nothing is sent automatically, and no audio goes to the
server. Hide the button where the browser has no recognition. Reference: `docs/frontend/widget.html` (`setupMic`).
