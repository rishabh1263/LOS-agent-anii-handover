# FOS Copilot -- frontend API (Phase 3)

Everything the chat UI needs: endpoints, request fields, every action, the response fields to render, and the
buttons. Example payloads: `docs/frontend/examples/*.json`. Machine-readable: `docs/frontend/openapi.yaml`.
A working reference widget (plain HTML + JS, no build): `docs/frontend/widget.html`.

All features below are behind server flags (default OFF); the "demo set" turns them on (README_CHATBOT.md).

## 1. Auth
Every call: `Authorization: Bearer <JWT>` (the same token the app already uses). A case the user does not own is
**403** -- render the `detail.message`, never retry.

## 2. Endpoints

| Method | Path | Use |
|---|---|---|
| POST | `/api/v1/fos/copilot` | every chat turn and every button (JSON) |
| POST | `/api/v1/fos/copilot/stream` | the same request, as Server-Sent Events (status lines, then the answer) -- `COPILOT_STREAMING` |
| GET | `/api/v1/fos/documents/view?token=…` | open a document from a VIEW_DOCUMENT link (5 min, same user) -- `COPILOT_CASE_ACTIONS` |
| POST | `/api/v1/fos/copilot` (multipart) | document upload (unchanged) |

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
