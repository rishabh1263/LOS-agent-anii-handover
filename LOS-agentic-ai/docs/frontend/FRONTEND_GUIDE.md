# FOS Copilot: guide for the frontend team

This is the **one page to read first**. It explains, in plain words, what the system does, which API to call for
each screen, and what to show. The details (every field, every example) are in `FRONTEND_API.md`,
`openapi.yaml` and `examples/`. A working reference screen (plain HTML, no build) is `widget.html`. Open it in a
browser and you can try everything below.

> **Short version (Hinglish):** Login same rahega (username + password + stage). Home par ek table: user ke apne
> cases, `GET /api/v1/fos/cases` se. Row click = `GET /cases/{id}/form` se bhara hua form. Chat ke liye sirf ek API:
> `POST /api/v1/fos/copilot`. Har reply mein sirf do cheezein aati hain: `markdown` (screen par dikhao) aur `tts`
> (bolne ke liye). Markdown ke andar ke links buttons hain: `ask:` wala link = woh text chat mein bhejo,
> `action:` wala link = neeche diye table ke hisaab se karo. Downloads, import, history, notifications ke liye alag
> chhote APIs hain (section 6).

---

## 1. What is happening in this project (the chatbot)

- Field officers (FOS) create loan cases, upload documents, and move cases towards CPA.
- The **main UI** (your app) shows the officer's cases and the case form.
- The **chatbot** is an assistant on the same data. It answers questions about the officer's own cases ("kya pending
  hai?", "KYC status?", "CPA ready hai?"), answers FAQ ("How do I upload a document?"), creates or edits a case
  after asking **Confirm?**, gives Excel / Doc / PDF downloads, and never shows another officer's data.
- The same backend and database serve the UI and the chat. A case created or changed in one shows in the other
  after a refresh.
- The bot **never changes a stage**. A person does that. Every change from chat (create a case, edit a field,
  import) is shown as a summary first and saved only after the officer taps **Confirm**.
- Foul language gets a warning only. The message is not answered, the bad word is shown masked
  (`**m*******d**`), and the voice never says it.

## 2. Login

`POST /api/v1/auth/login`

```json
{ "username": "officer1", "password": "…", "stage": "FOS" }
```

- `stage` is one of `FOS, CPA, CREDIT, RCU, BOPS, HOPS, DISBURSEMENT`. An unknown stage gives **422** with a plain
  message.
- Wrong username or password gives **401**. Show "Wrong username or password. Please try again."
- **No applicant ID or case ID at login.** If your old screen sends `app_id` / `case_id`, the server ignores them
  for scope (they only preload a case the user already owns). You can remove them.
- The response has `access_token` (send it as `Authorization: Bearer <token>` on every call), `refresh_token`
  (`POST /api/v1/auth/refresh`) and `stage` (echoed).
- The **same token** works for the main UI and the chat. There is no second login.
- The user only ever sees **their own cases** (the cases they created or were given). The server checks this on
  every call. You never filter for security yourself.

## 3. Home screen: the case table

`GET /api/v1/fos/cases?page=0&size=20&search=&group=&status=&stage=&sort=&lang=en`

| Query param | Meaning |
|---|---|
| `search` | name (typos are fine), case ID, App ID, co-applicant ID |
| `group` | `pending` or `done` |
| `status` | `Created`, `Review`, `Disbursal` |
| `stage` | `FOS`, `CPA`, … |
| `sort` | `needs_action` (default), `created_date` (oldest first), `created_date_desc`, `updated`, `oldest`, `recent`, `longest_in_stage` |
| `page`, `size` | server pagination (size is capped at 20) |
| `lang` | `en`, `hi`, `hi-Latn`, `mr`: the language of the labels |

The response gives you everything to draw the table:

```json
{
  "columns": [{"key": "case_id", "label": "Case ID"}, {"key": "app_id", "label": "App ID"},
              {"key": "applicant_name", "label": "Applicant name"}, {"key": "stage", "label": "Stage"},
              {"key": "status", "label": "Status"}, {"key": "created_date", "label": "Created date"},
              {"key": "action", "label": "Action"}],
  "rows": [{"case_id": "CASE-852C…", "app_id": "APP-B597…", "applicant_name": "Rahul Sharma", "stage": "FOS",
            "status": "Created", "status_text": "Created", "group": "pending", "group_label": "Pending",
            "created_date": "2026-10-08", "status_label": "3 docs pending",
            "action": {"label": "Open", "open_form": "/api/v1/fos/cases/CASE-852C…/form",
                       "open_chat": "[Open](action:open_case?id=CASE-852C…)"}}],
  "total": 42, "page": 0, "size": 20, "has_more": true,
  "empty_text": null,
  "texts": {"loading": "Loading your cases...", "error": "We couldn't load your cases. Please try again.",
            "new_case_label": "Create New Case"},
  "groups": {"pending": "Pending", "done": "Done"},
  "statuses": {"Created": "Created", "Review": "Review", "Disbursal": "Disbursal"}
}
```

- Show the **Status** column from `status_text` and the Pending/Done badge from `group_label`. The mapping
  (stage → Created / Review / Disbursal → Pending / Done) is server config, so do not hardcode it.
- `status_label` is the short "what needs action" text ("3 docs pending"). It's useful as a second line.
- `empty_text` is filled when there are no cases ("No cases yet.") or no match ("No cases match your search.").
  Show it instead of an empty table. Use `texts.loading` / `texts.error` for those states.
- Mobile: let the table scroll sideways and keep buttons large.
- The **Create New Case** button (label from `texts.new_case_label`) opens your blank form, same as today.

## 4. The case form (create, continue, edit)

| Call | Use |
|---|---|
| `GET /api/v1/fos/form-schema?lang=en` | the fields in order: name, label, required, max length, choices, rules |
| `POST /api/v1/fos/applicants` | create (unchanged): `{"applicant": {...}, "application": {...}}` |
| `GET /api/v1/fos/cases/{case_id}/form` | **row click**: the saved values, `missing` (required fields still empty), `editable` |
| `PUT /api/v1/fos/cases/{case_id}/form` | save changed fields: `{"changes": {"loan_amount": 600000}}` |

- Build the form from `form-schema` so the UI and the chat use the **same fields and the same rules**.
- Before `PUT`, show the user a summary of the changes and ask **Confirm?**. Send only after they confirm.
- `PUT` errors: **422** `INVALID_FIELDS` with `detail.fields.<name>.message` (show it under the field), **409**
  `FORM_LOCKED` (the case is past FOS), **404** (not the user's case, or it does not exist; both give the same
  message).
- Amounts are always **full rupees** (`500000`, not `5` or "5 lakh").

## 5. The chat

### 5.1 One endpoint
`POST /api/v1/fos/copilot` for every typed message and every button. Use `POST /api/v1/fos/copilot/stream` for
live typing (same body, Server-Sent Events).

```json
{ "action": "CUSTOM_QUERY", "message": "kya pending hai?", "reply_language": "en", "chat_id": "<uuid per chat>" }
```

- Make a new `chat_id` for each new chat and send `"new_chat": true` on its first message. One chat is one
  conversation: the open case, the last list and any draft stay inside that chat only.
- Send `reply_language` on every turn (`en`, `hi`, `hi-Latn`, `mr`). The reply is always in that language, even
  if the user types Hinglish.
- You do **not** send `case_id` or `applicant_id`. The chat remembers the case the user opened.

### 5.2 Every reply is exactly this

```json
{ "request_id": "fos_…", "markdown": "…", "tts": "…" }
```

- `markdown` is what you show. Render tables, lists and bold. No emojis are sent.
- `tts` is what the speaker reads: plain sentences, no symbols, IDs shortened ("case ending 852C"). Use browser
  speech (`speechSynthesis`, voice `en-IN` / `hi-IN` / `mr-IN`) for a speaker button on each message and an
  auto-read toggle.
- Nothing else is in the reply. Older fields (`answer`, `chips`, `presentation`, `actions`, …) are gone.

### 5.3 Buttons are links inside the markdown

Render every markdown link as a button (or chip).

- **`[Text](ask:Some text)`**: send `Some text` as the next chat message (decode `%20` etc.).
- **`[Label](action:name?x=y)`**: look up `name` here:

| `name` | What the frontend does |
|---|---|
| `open_case`, `list_more`, `list_cases`, `exit_case`, `switch_case`, `raise_query`, `mark_query_sent`, `confirm_write`, `cancel_write` | post `{"action_link": "<the whole href>", "chat_id": …, "reply_language": …}` to `/api/v1/fos/copilot`. The server maps it and re-checks access. |
| `upload` (`doc`, `party`) | open a file picker, then post multipart to `/api/v1/fos/copilot?chat_id=…` with `file`, `document_type=<doc>`, `party=<party>` |
| `view_document` (`id`) | post the `action_link` as above; the reply has a short-lived view link |
| `download` (`format`, `case`) | `GET /api/v1/fos/exports?case_id=<case>&format=<format>` → `{url}` → `GET <url>` with the same token → save the file |
| `download_list` (`format`, `q`) | `GET /api/v1/fos/exports?q=<q>&format=<format>` (same as above, for the filtered list) |
| `show_in_ui` (`case`) | open that case's form on the main screen (`GET /cases/{case}/form`) |
| `show_list_in_ui` (`q`) | open the home table with those filters (`q` = `group:pending,search:rahul` or `all`) |
| `new_case` | open your **Create New Case** form |
| `handoff_note` (`case`) | `GET /api/v1/fos/handoff-note?case_id=<case>&format=pdf` |
| `copy` (`ref`) | copy the Nth quoted block (`> …`) of that message to the clipboard (nothing is sent) |

An unknown `name` is a bug. Report it; don't guess. The server has the full list (`copilot_reply.yaml`
`action_links`).

### 5.4 Quick buttons (always visible under the chat)
`GET /api/v1/fos/quick-actions?lang=en` → `[{id, label, send}]` (My pending cases, Create New Case, How to upload,
FAQ). On tap, send `send` as a chat message.

### 5.5 What the chat flow looks like (so you know what to expect)
1. Chat opens: send `"hi"` (new chat). The bot greets with what needs attention and any notifications
   ("CASE-123 has been pending for 5 days").
2. "my cases": a table (one page), downloads, and "Do you want to know about a particular case?" [Yes] [No].
3. Yes → "Pending or Done?" [Pending] [Done] → that list → the user taps **Open** or types the number → the case
   opens with a short review and "What do you want to know?" with options.
4. Inside a case every question is about that case. Every case answer has **Download Excel / Doc / PDF** and
   **Show in UI**.
5. "Create new case" in chat: the bot asks the same fields as the form one by one, shows a summary, and creates the
   case only after **Confirm**. "cancel" stops it. If the user asks something else in between, the draft waits;
   "Create new case" again continues it.
6. "loan amount 600000 karo" inside a case: the bot shows the change and saves only after **Confirm**.
7. An Excel sheet sent in chat is checked row by row; nothing is created until **Confirm**.
8. "How do I upload a document?": numbered steps with a source. Unknown FAQ: "I don't know that yet…".
9. "my cases" shows the officer's **5 most recent** cases ("Show more" pages on).

### 5.5b The FOS ⇄ CPA workflow (what the officer does in chat)
- **At FOS, something wrong** (KYC mismatch, rejected or missing document): case answers show
  **Send query to customer**. The bot drafts ONE message listing every problem → **Confirm** → it is recorded as a
  query to the customer, and the message is shown with **Copy message** (the app sends it by SMS/WhatsApp; the bot
  never sends by itself). The open query blocks the move to CPA until the officer resolves it
  ("resolve query QRY-…" → Confirm).
- **At FOS, all clear**: case answers show **Move to CPA** → **Confirm** → the case moves (the gate is checked again at
  that moment; a case that is not ready is never moved: the bot lists what to fix).
- **At CPA**: **Raise query to FOS** → the bot asks for the text → **Confirm**. FOS answers with
  "reply to QRY-…: <text>" → Confirm. FOS can also raise a query to CPA.
- All of these are ordinary `ask:` / `action:confirm_write` links -- nothing new for the frontend to build.
- Queries on a case: `GET /api/v1/los/cases/{case_id}/queries` (existing) for a queries tab in the UI.

### 5.6 Streaming (`/copilot/stream`)
Events: `typing` (at once) → maybe one `status` ("Working on it…") → `delta` pieces of markdown (append them) →
`final` (the full `{request_id, markdown, tts}`). Also `cancelled`, `error`.
- **Stop**: `POST /api/v1/fos/copilot/stop {"chat_id": …}`. A new message also cancels the old reply.
- **Reconnect**: `GET /api/v1/fos/copilot/replay/{request_id}`.
- Send an `Idempotency-Key` header (a new UUID per message) so a retry never runs twice.
- **Pushed follow-ups** (e.g. a document finished checking): long-poll
  `GET /api/v1/fos/copilot/updates?chat_id=…&wait=20` and show each message.

## 6. Other screens

| Feature | Call |
|---|---|
| Excel import (home screen) | `POST /api/v1/fos/imports` (multipart `file`, `.xlsx`) → `{import_id, ok, bad, errors[], summary, confirm}`. Show `errors` (plain English, one per bad row) and `confirm`. On **Confirm**: `POST /api/v1/fos/imports/{import_id}/confirm` → `{created[], failed[]}` |
| Chat history | `GET /api/v1/fos/chats` → the user's past chats; `GET /api/v1/fos/chats/{chat_id}` → its messages (already masked) |
| Activity log | `GET /api/v1/fos/cases/{case_id}/activity` → who created / changed what, when, and from where (ui / chat / import) |
| Notifications | `GET /api/v1/fos/notifications?lang=en` → `{heading, items: [{case_id, days, text}]}`. Show them on the home screen. |

## 7. Errors: show `detail.message`

| Status | Meaning | What to show |
|---|---|---|
| 401 | login wrong or token expired | the login screen |
| 403 | link expired / not allowed | `detail.message` |
| 404 | case not found **or** not this user's (same message on purpose) | `detail.message` |
| 409 | not allowed in this state (form locked, case not ready for a handoff note) | `detail.message` |
| 422 | invalid input (bad field, unknown filter, bad file) | `detail.message` / `detail.fields` |
| 429 | too many messages or downloads in a minute | `detail.message`, then let the user retry |
| 503 / 504 | database / service down | "Something went wrong on our side. Please try again." |

Never show stack traces or raw codes. The server never sends them in `markdown`.

## 8. Turning it on (server `.env`)

Everything below is ON in the dev `.env`. The config files have the same defaults.

```
COPILOT_MD_TTS_CONTRACT=true
COPILOT_CASE_WORKSPACE=true
COPILOT_CASE_ACTIONS=true
COPILOT_CASE_LIST_PAGING=true
COPILOT_FAQ=true
COPILOT_PRODUCT_FLOW=true
COPILOT_CHAT_CASE_CREATE=true
COPILOT_ABUSE_GUARD=true
COPILOT_GUARDRAIL_HARDENING=true
COPILOT_STREAMING=true
COPILOT_LANGUAGE_LOCK=true
COPILOT_PROFESSIONAL_FORMAT=true
LOS_STAGE_GATE_IN_SERVICE=true
LOS_FOS_CPA_KYC_RULE=true
LOS_SIGNATURE_MANDATORY=true
LOS_COAPP_MANDATORY_DOCS=true
```

`LOS_LOGIN_SELF_GRANT_LEGACY` stays OFF: it is the old login scope bypass, not a feature.

Check `GET /ready` → `build.chatbot_flags` to see what the server you're calling has on.

## 9. Checklist before you ship

- [ ] Login sends username, password and stage; no applicant ID.
- [ ] Home table uses `columns`, `rows`, `empty_text`, `texts`; search, Pending/Done and status filters, sort and
      paging go to the server.
- [ ] Row click opens the filled form from `/cases/{id}/form`. Edits are confirmed, then `PUT`.
- [ ] Chat sends `chat_id`, `new_chat`, `reply_language`. It renders `markdown` and speaks `tts`.
- [ ] Every `ask:` and `action:` link works as in section 5.3. Quick buttons are always visible.
- [ ] Downloads open with the same token. Import shows row errors and needs Confirm.
- [ ] 401 / 403 / 404 / 409 / 422 / 429 show `detail.message`.
