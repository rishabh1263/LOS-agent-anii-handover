# LOS Chatbot — Frontend API Contract (version 1.0)

**One rule above all:** the chat has ONE endpoint, `POST /api/v1/fos/copilot`, and every reply is
`{ request_id, markdown, tts }`. Do **not** call `/api/v1/copilot/query` and do **not** read `answer`. Every response carries
the header **`X-Contract-Version: 1.0`**; if it differs from the version the app was built for, show a "please refresh"
banner and log it. The contract is pinned by `tests/integration/test_api_contract.py` (backend) — it fails if any shape
below changes without a version bump (`frontend_handoff/contract_snapshot.json`).

Base URL: same origin via the Vite proxy (`/api` → `http://127.0.0.1:8010`). All paths below are under `/api/v1`.
Generated artifacts in this folder: `openapi.json` (live schema), `contract.ts` (types), `chatClient.ts` (typed client),
`examples/*.json` (captured request/response pairs).

---

## 1. The chat endpoint

### `POST /api/v1/fos/copilot` (JSON) — a typed message, a button press, a confirm

| Field | Type | Required | Notes |
|---|---|---|---|
| `action` | `"CUSTOM_QUERY"` (typed text) or a button action (`LIST_CASES`, `OPEN_CASE`, `EXIT_CASE`, `NEW_CASE`, `RAISE_QUERY`, `LIST_QUERIES`, `VIEW_DOCUMENT`, …) | yes | exact upper-case value; anything else → 422 |
| `message` | string ≤ 1000 | for `CUSTOM_QUERY` | non-empty after trimming, else 422 `MESSAGE_REQUIRED` |
| `chat_id` | string ≤ 128 | **yes (always send it)** | one id per chat window, **persisted** (localStorage) and sent on every message of that chat. The server keeps the open case, the pending question and the last list per chat. A new chat = a new id. |
| `reply_language` | `"en"` | recommended | English only for now |
| `new_chat` | boolean | no | `true` once, to start the chat fresh |
| `case_id`, `applicant_id` | string | **only** when the UI has a case selected (e.g. a case page) | otherwise omit — the server resolves the case from the chat |
| `action_link` | string | no | alternative to `/fos/action`: post a link's `href` here (`{"action_link": "action:open_case?id=…", "chat_id": …}`) |
| `context` | object | no | never a string; usually omit |

**Upload** — same endpoint, `multipart/form-data`: `action=UPLOAD_DOCUMENT`, `files` (one or more; `file` for a single
one), `chat_id`, optional `document_types` (one per file, same order — from the upload link's `document_type`; empty =
auto-detect), `co_applicant_id` for a co-applicant's document, and `applicant_id` + `case_id` only when the chat has no open
case. Do not set the `Content-Type` header yourself (the browser sets the boundary).

**Response (always)**

```json
{ "request_id": "fos_…", "markdown": "…", "tts": "…" }
```

- `markdown`: render it (see section 2 for links). Tables, bold, lists, `>` quotes. No HTML.
- `tts`: plain text for read-aloud (already short; never read the markdown).
- `request_id`: keep it for replay (section 3) and support tickets.

**Do not** call `/api/v1/copilot/query` (a legacy wrapper) and do not read `answer`, `presentation`, `intent` — they are
not part of the contract.

---

## 2. Links inside `markdown`

| Link | Meaning | What the UI does |
|---|---|---|
| `[Label](ask:<url-encoded text>)` | a suggested question / option | send the decoded text as the next `CUSTOM_QUERY` message in the same chat |
| `[Label](action:<name>?k=v…)` | a server action | `POST /api/v1/fos/action` (below) |
| `[Label](https://…)` | an ordinary link | open in a new tab (`rel="noopener noreferrer"`); any other scheme: show as text |

Action names you will see: `open_case`, `exit_case` (Close), `list_more`, `download` / `download_list` (`format=xlsx|docx|pdf`),
`handoff_note`, `view_document`, `upload`, `show_in_ui`, `show_list_in_ui`, `new_case`, `copy`, `confirm_write`,
`cancel_write`, `raise_query`, `mark_query_sent`.

### `POST /api/v1/fos/action`

Request: `{ "href": "<the link exactly as in the markdown>", "chat_id": "<same chat>", "reply_language": "en" }`

Response — one of:

| Response | Meaning | UI |
|---|---|---|
| a **file** (`Content-Type` not JSON, `Content-Disposition: attachment; filename="…"`) | download / handoff note | save with the server's filename |
| a file with `Content-Disposition: inline` | `view_document` | open in a new tab (object URL) |
| `{"type":"open_ui","route":"/cases/CASE-…"}` | show a screen | navigate to `route` |
| `{"type":"upload","document_type":"PAN","party":"applicant","post_to":"/api/v1/fos/copilot?chat_id=…"}` | upload | open the file picker, then multipart POST to `post_to` with `files` + `document_types=<document_type>` |
| `{"type":"copy","ref":"draft-1"}` | copy | copy the Nth `>` quote of that reply to the clipboard |
| `{"type":"reply","request_id","markdown","tts"}` | a chat reply | append it to the chat |

---

## 3. Streaming — `POST /api/v1/fos/copilot/stream`

Same request body as section 1. `text/event-stream`, events in this order:

| Event | Payload | When |
|---|---|---|
| `typing` | `{request_id, ms}` | at once |
| `status` | `{request_id, text, ms}` | at most once: "Understanding your question…" while the model works, or a generic line after 700 ms |
| `delta` | `{request_id, markdown}` | the reply in chunks — append them |
| `final` | `{request_id, markdown, tts}` | the complete reply (replace the deltas with it) |
| `cancelled` | `{request_id, ms}` | a newer message in the same chat, or `stop` |
| `error` | `{request_id, status, detail}` | see section 4 |

- **Stop:** `POST /api/v1/fos/copilot/stop` `{ "chat_id": "…" }`.
- **Reconnect:** `GET /api/v1/fos/copilot/replay/{request_id}` → the final `{request_id, markdown, tts}` (own requests only).
- **Idempotency:** send `Idempotency-Key: <uuid>` on retries; the same key returns the stored reply, never a rerun.
- **Updates:** `GET /api/v1/fos/copilot/updates?chat_id=…&wait=25` (long-poll) → changes on cases this chat looked at.

---

## 4. Auth and errors

- `POST /api/v1/auth/login` `{username, password, stage?}` → `{access_token, refresh_token, expires_in, token_type, stage}`.
- Every call: `Authorization: Bearer <access_token>`. Access token ≈ 15 min; `POST /api/v1/auth/refresh`
  `{refresh_token}` → a new pair (refresh ≈ 7 days). `POST /api/v1/auth/logout` `{refresh_token}`.
- On **401**: refresh once, retry once; if it fails again → sign-in screen.

Every error body is `{ "detail": …, "error": { "code", "message", "retryable", "request_id" } }`. **Show `error.message`**
(written for a person; never show codes). Retry automatically only when `retryable` is true.

| Status | Code (examples) | Show the user |
|---|---|---|
| 401 | `AUTHENTICATION_REQUIRED` | "Your session is missing or has expired. Please sign in again." (after one refresh attempt) |
| 403 | `ACCESS_DENIED` | the message ("You are not authorized to do this.") |
| 404 | `NOT_FOUND` | "That was not found." (a case of another officer is also 404) |
| 409 | `CONFLICT` / not ready | the message; refresh the view |
| 422 | `INVALID_REQUEST`, `MESSAGE_REQUIRED`, `INVALID_ACTION_LINK` | the message — `detail.fields` names the bad field for developers |
| 429 | `RATE_LIMITED` | "Too many requests right now. Please wait a moment and try again." (retryable) |
| 503 | `SERVICE_UNAVAILABLE` | "The service is temporarily unavailable. Please try again shortly." (retryable) |

Abuse: a message with foul language returns **200** with a warning in `markdown` (the word masked) — show it like any
reply. Rate limit on chat: 429.

---

## 5. Other endpoints the UI uses (all GET unless noted; Bearer token; own cases only)

| Path | Params | Returns |
|---|---|---|
| `/fos/cases` | `filter, search, sort, page (0-based), size (≤ 20), stage, product, status, group (pending/done), created_from, created_to` | the home table: rows (case_id, app_id, applicant name, stage, status, created date), total, page info |
| `/fos/cases/{case_id}/form` | `lang` | the saved form (row click) |
| `PUT /fos/cases/{case_id}/form` | body `{changes: {field: value}}` (changed fields only, after the user confirms) | saved; same rules as create and chat |
| `/fos/form-schema` | `lang` | fields, order, required, rules |
| `/fos/quick-actions` | `lang` | buttons `{id, label, send}` — post `send` as a `CUSTOM_QUERY` |
| `/fos/exports` | `format=xlsx|docx|pdf`, `case_id` or `q` | `{url}` short-lived signed link → `GET` it (file) |
| `POST /fos/imports` | multipart `file` (.xlsx) | row-by-row validation; nothing written |
| `POST /fos/imports/{import_id}/confirm` | — | creates the valid rows |
| `/fos/notifications` | `lang` | cases pending longer than the configured days |
| `/fos/chats`, `/fos/chats/{chat_id}` | — | the caller's past chats (masked) |
| `/fos/cases/{case_id}/activity` | — | who changed what, when |
| `POST /fos/documents` | multipart | upload outside the chat (the chat upload is preferred) |
| `/los/cases/{case_id}/queries` (GET / POST), `/los/cases/{case_id}/queries/{query_id}/status` (POST) | see `openapi.json` | queries (raise / list / respond / resolve) |

---

## 6. Types and client

- `frontend_handoff/contract.ts` — hand-written TypeScript types for this contract (chat request/response, links, action
  results, SSE events, errors). `frontend_handoff/chatClient.ts` — a typed client (fetch, refresh-once on 401, SSE reader).
- **Generated** types for every endpoint: `frontend_handoff/api-types.d.ts` (openapi-typescript 7.13.0, from
  `openapi.json`). In the frontend: `openapi-typescript` is a devDependency (approved 2026-10-09; installed with
  `--legacy-peer-deps` because it declares a TypeScript 5 peer and the app uses TypeScript 6 — generation and strict
  `tsc` pass). Regenerate after a backend change: export `openapi.json`, then `npm run gen:api` (writes
  `src/runtime/chatbot/api/api-types.d.ts`). Use `paths["/api/v1/fos/cases"]["get"]["responses"]["200"]...` for the
  non-chat endpoints; keep `contract.ts` for the chat reply (its `markdown` links are a text contract openapi cannot
  describe).
- Vague / one-word messages (`docs`, `case`, `kyc`, `top 2`): the reply is ONE question with 2–4 numbered options —
  `ask:` links, plus an `action:upload` link when the word names a document. Send the option's text (tap) or its
  number; a second vague message answers the most likely option directly.

## 7. Contract tests

`tests/integration/test_api_contract.py`: the reply keys, the action result types, the error shape, the
`X-Contract-Version` header, and the OpenAPI request schemas of the chat endpoints are compared with
`contract_snapshot.json`. A change without bumping `contract_version` (`app/config/copilot_reply.yaml`) fails the build.

## 8. Examples

`frontend_handoff/examples/*.json` — real request/response pairs (masked), captured in-process from the running app:
chat, follow-up with chat_id, vague message + pick, case list + open, link click, download, upload, stream, error, abuse.

## 9. Integration checklist (Vite + React + TS app)

1. `runtime/chatbot/api/client.ts`: call `POST /api/v1/fos/copilot` (not `/copilot/query`); body
   `{action: "CUSTOM_QUERY", message, chat_id, reply_language: "en"}`; read **`markdown`** and **`tts`**.
2. `runtime/chatbot/hooks/useChatbot.ts`: create one `chat_id` per conversation (uuid), store it, send it on every
   message; drop the `isDocumentRelatedQuery` split (one endpoint answers everything); send `case_id` only from a case page.
3. `MessageBubble.tsx`: render assistant messages with `ChatLinks.tsx` (`<ChatMarkdown …/>`); `ask:` → send as a message,
   `action:` → `POST /fos/action`; uploads → the picker + multipart to `post_to`.
4. Read-aloud: use `tts`, not the markdown.
5. Check `X-Contract-Version` once per session.
6. Verify: send "show my cases" → a table with Open links; click Open → the case brief with Upload / Close; "docs" →
   one question with options; pick one → the answer; "download excel" → a file saves; 401 after 15 min → silent refresh.
