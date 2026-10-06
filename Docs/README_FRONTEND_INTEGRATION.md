# LOS Agentic AI — How the Backend Works & How to Integrate a Frontend

This guide explains, with diagrams, what happens inside the backend when the
frontend calls it, and exactly which response fields a UI should render.

> **Golden rule for the frontend:** never parse the `answer` text to learn
> state. Every state the UI shows (stage, documents, blockers, pending work,
> actions, processing, errors) comes as **structured fields**. `answer` is only
> the sentence you show in the chat bubble.

---

## 1. The big picture

```mermaid
flowchart LR
    FE["Frontend<br/>(web / mobile)"] -->|"Bearer JWT"| API["FastAPI<br/>/api/v1/*"]

    subgraph Backend
        API --> SEC["Security gate<br/>auth · ownership · input guardrail"]
        SEC --> COP["Copilot agent<br/>understand → resolve → authorize → plan"]
        SEC --> UPL["Upload pipeline<br/>upload gate → classify → verify → extract"]

        COP --> TOOLS["MCP tools<br/>(case, documents, checklist, KYC, stage)"]
        COP --> CAP["Capabilities<br/>verify-now · pending work · stage gates"]
        COP --> KNOW["Knowledge (RAG)<br/>handbook only — never live state"]
        COP -. "optional, max 1 call" .-> LLM["Qwen via Ollama<br/>wording only"]

        UPL --> OCR["OCR (RapidOCR, warm pool)"]
        UPL --> VER["Verification agents<br/>identity · passport MRZ · financial"]
        CAP --> SPEC["Specialist agents<br/>Eligibility · Credit · RCU (unchanged)"]

        TOOLS --> DB[("Case store<br/>SQLite / repo")]
        UPL --> DB
        CAP --> DB
        UPL --> DOCS[("Document store")]
        CAP --> Q[("OCR job queue")]
    end
```

| Layer | Source of truth? | Notes |
|---|---|---|
| Case store, verification, KYC, eligibility, credit, stage | **Yes** | Deterministic services. Every fact in an answer comes from here. |
| Knowledge / RAG | No | Explains *how the process works* ("what is KYC?"). Never answers "what is *my* status". |
| Qwen (Ollama) | No | Optional rewording / explanation, behind a fidelity check. Off for live facts. |

---

## 2. What happens on one chat message

`POST /api/v1/fos/copilot` with `action: "CUSTOM_QUERY"`:

```mermaid
sequenceDiagram
    autonumber
    participant FE as Frontend
    participant API as FOS route
    participant G as Guardrails + auth
    participant C as Copilot agent
    participant S as Case store / tools
    participant L as Qwen (optional)

    FE->>API: { applicant_id, case_id, action: CUSTOM_QUERY, message, context }
    API->>G: JWT valid? input safe?
    G-->>API: refused → structured refusal (no reads happen)
    API->>C: question + conversation context
    C->>C: language detect · follow-up / pronoun resolution ("iska", "the other guy")
    C->>G: caller owns this case? (ownership + access policy)
    C->>S: read ONLY what the intent needs (tools)
    S-->>C: authoritative records
    C->>C: deterministic answer + structured blocks
    opt only for explanation-type answers, and only if enabled
        C->>L: reword (one call, bounded)
        L-->>C: candidate → kept only if every fact survives
    end
    C-->>API: answer + structured state
    API-->>FE: FosResponse (answer, response_type, checklist, pending_work, actions, context, ...)
```

**Key points**
- The security decision happens **before** any read, tool, RAG or model call.
- Conversation memory travels in `context` — send back the `context` you
  received on the next turn so follow-ups ("and the co-applicant?", "haan")
  work.
- Compound questions ("loan amount aur pending documents?") are split and
  answered part by part.

---

## 3. What happens on a document upload

`POST /api/v1/fos/copilot` as `multipart/form-data`, `action=UPLOAD_DOCUMENT`:

```mermaid
flowchart TD
    A["files[] + document_types[] (optional, positional)"] --> B{"Upload gate<br/>size · extension · magic bytes<br/>no executables / scripts / PDF launch"}
    B -->|refused| R1["INVALID_UPLOAD for THAT file<br/>(not stored, others continue)"]
    B -->|ok| C["OCR + classification"]
    C --> D{"Type matches the slot<br/>it was uploaded for?"}
    D -->|no| R2["WRONG_DOCUMENT<br/>DOCUMENT_TYPE_MISMATCH, extraction withheld"]
    D -->|yes| E["Verification<br/>(structure, fields, MRZ check digits, expiry)"]
    E --> F{"Verdict"}
    F -->|PASS| G["Extraction released"]
    F -->|REVIEW / FAIL| H["Reason codes recorded"]
    G --> I["Persist + checklist update"]
    H --> I
    R2 --> I
    I --> J["Readiness / blockers / next_action recomputed"]
    J --> K["Response: response_type = UPLOAD_RESULT or UPLOAD_VALIDATION"]
```

- **One bad file never stops the others** — each file gets its own entry in
  `verification.documents_processed`.
- A long scan may be **queued**: its card shows `PROCESSING` with a job id,
  and the durable OCR queue finishes it later (poll, see §6.6).
- `PASS` means *structurally valid and internally consistent* — **not**
  "genuine". `authenticity: "NOT_ESTABLISHED"` and `issuer_verified: false`
  are reported until an issuer/authenticity provider is configured.

---

## 4. The loan stage lifecycle

```mermaid
stateDiagram-v2
    direction LR
    [*] --> FOS
    FOS --> CPA: readiness = READY (configured documents pass)
    CPA --> CREDIT: KYC / name-match / findings clear
    CREDIT --> RCU: human credit decision required
    RCU --> BOPS
    BOPS --> HOPS
    HOPS --> DISBURSEMENT
    DISBURSEMENT --> [*]

    note right of CREDIT
      Eligibility + Credit Underwriting run
      through the EXISTING agents (unchanged).
      Approval always needs a human.
    end note
    note right of BOPS
      RCU / BOPS / HOPS / DISBURSEMENT gate criteria
      are not configured yet → reported as
      CONFIGURATION_GAP, never as "passed".
    end note
```

- Gate criteria live in `app/config/stage_gates.yaml` (marked **UNCONFIRMED**
  until business sign-off). Order comes from the workflow configuration.
- The copilot **never moves a stage itself**. When asked "move it ahead", it
  returns a `STAGE_TRANSITION` action; the frontend shows a confirmation and
  calls the stage endpoint (§6.7) with `expected_stage` + `idempotency_key`.

---

## 5. Endpoints a frontend uses

| Purpose | Method & path | Body |
|---|---|---|
| Login (dev identity provider) | `POST /api/v1/auth/login` | `{username, password}` → `{access_token, refresh_token, token_type, expires_in}` |
| Refresh token | `POST /api/v1/auth/refresh` | `{refresh_token}` → new token pair |
| Open a case (new or existing applicant) | `POST /api/v1/fos/applicants` | `{applicant:{...}, application:{product, loan_amount, tenure_months, ...}, applicant_id?}` |
| Dropdown actions (render from data) | `GET /api/v1/fos/actions` | — |
| **Copilot: chat + actions** | `POST /api/v1/fos/copilot` | JSON `{applicant_id, case_id, action, message?, context?}` |
| **Copilot: upload** | `POST /api/v1/fos/copilot` | multipart `applicant_id, case_id, action=UPLOAD_DOCUMENT, files[], document_types[]` |
| Checklist with per-slot upload buttons | `GET /api/v1/fos/checklist/{case_id}` | — |
| Documents of a case | `GET /api/v1/fos/documents/{case_id}` | — |
| Application status | `GET /api/v1/fos/applications/{case_id}` | — |
| Background processing (jobs) | `GET /api/v1/los/cases/{case_id}/processing` | — |
| Stage transition (after user confirms) | `POST /api/v1/los/cases/{case_id}/stage` | `{target_stage, expected_stage, idempotency_key, reason, mode: "GATED"}` — send the Copilot's action body as is |
| **Raise Query** (the button) | `POST /api/v1/los/cases/{case_id}/queries` | send the `RAISE_QUERY` action's `body` as is: `{target_type, target_id?, query_type, text, evidence_refs?, idempotency_key?}` |
| Queries & deviations of a case | `GET /api/v1/los/cases/{case_id}/queries` | — → `{queries[], deviations[], deviation_rules, query_types[], target_types[]}` |
| Answer / resolve / reopen a query | `POST /api/v1/los/cases/{case_id}/queries/{query_id}/status` | `{status: RESPONDED\|RESOLVED\|REOPENED\|CANCELLED, note?}` |
| Decide a deviation (authority only) | `POST /api/v1/los/cases/{case_id}/deviations/{deviation_id}/decision` | `{decision: APPROVED\|REJECTED\|WITHDRAWN, justification}` |
| Signature with a specimen | `POST /api/v1/los/process` | multipart `files[]` + `expected_types=SIGNATURE` + optional `reference_signature` (and `co_applicant_reference_signature`) |
| Universal copilot (all stages) | `POST /api/v1/copilot/query` | `{message, case_id, applicant_id, conversation_id, context, ...}` |
| Standalone verifiers | `POST /api/v1/verify`, `POST /api/v1/financial/verify` | multipart `file` (+ `expected_type`) |
| Health | `GET /health`, `GET /ready` | — |

Swagger UI: `{BASE_URL}/docs`. All business endpoints need
`Authorization: Bearer <access_token>`.

`action` values for `/fos/copilot`: `GET_APPLICANT`, `GET_APPLICATION_STATUS`,
`GET_DOCUMENTS`, `GET_DOCUMENT_CHECKLIST`, `GET_VERIFICATION_STATUS`,
`GET_PENDING_ITEMS`, `GET_NEXT_ACTION`, `GET_CASE_360`, `CHECK_CPA_READINESS`,
`UPLOAD_DOCUMENT`, `CUSTOM_QUERY`. Prefer loading them from
`GET /api/v1/fos/actions` (each has `label`, `requires_message`,
`requires_file`, `content_type`).

---

## 6. Rendering the response

### 6.1 Which field drives which UI element

```mermaid
flowchart LR
    R["FosResponse"] --> M["answer → chat bubble"]
    R --> RT["response_type → which card/template"]
    R --> ST["stage · case_state → stage badge + progress bar"]
    R --> CL["checklist[] → document slots + Upload buttons"]
    R --> VE["verification.documents_processed[] → per-file result cards"]
    R --> RD["readiness.blocking_items[] → blockers list"]
    R --> PW["pending_work.items[] → task list (who owns each task)"]
    R --> NA["next_action → primary CTA"]
    R --> AC["actions[] / available_actions[] → buttons"]
    R --> SQ["suggested_questions[] → quick-reply chips"]
    R --> CX["context → store, send back next turn"]
    R --> LC["language_contract → reply language / RTL"]
    R --> ER["errors[] · upload_validation → error banners"]
    R --> ID["request_id → support / tracing"]
```

| UI need | Field | Example values |
|---|---|---|
| What kind of reply | `response_type` | `UPLOAD_RESULT`, `UPLOAD_VALIDATION`, `DOCUMENT_VERIFICATION_RESULT`, ... |
| Current stage | `stage`, `case_state.stage` | `BASIC_DOCUMENT_VERIFICATION` |
| Progress | `case_state` | `documents_required: 3, documents_satisfied: 2, collection_progress: 67` |
| Document slots | `checklist[]` | `{slot, label, status, required, accepted_types, actions:[UPLOAD_DOCUMENT]}` |
| Per-file outcome | `verification.documents_processed[]` | `verification: PASS/REVIEW/FAIL`, `reason_codes`, `authenticity` |
| Blockers | `readiness.status`, `readiness.blocking_items[]` | `NOT_READY`, `{code: DOCUMENT_MISSING, slot: ADDRESS_PROOF}` |
| Work list | `pending_work.items[]` | `owner: COMPLETED / PROCESSING / SYSTEM / USER / REVIEWER`, `executable` |
| Primary next step | `next_action` | `{action: COLLECT_DOCUMENT, target: ADDRESS_PROOF, detail}` |
| Buttons | `actions[]`, `available_actions[]` | `STAGE_TRANSITION`, `UPLOAD_DOCUMENT`, `enabled` |
| Who it is about | `subject` | `{party: PRIMARY_APPLICANT / CO_APPLICANT / BOTH}` |
| Stage gate | `gate` | `READY / BLOCKED / CONFIGURATION_GAP` per check |
| Several cases | `portfolio` | the applicant's cases (latest / previous / needing attention) |
| Language | `language_contract` | `response_language`, `reply_language`, `localized` |
| Tracing | `request_id` | `fos_8239fb4c...` |

### 6.2 Status semantics — never merge these

| Value | Meaning | UI |
|---|---|---|
| `PASS` | Verified (structure + fields). **Not** "genuine". | green tick |
| `REVIEW` + `EXPIRY_NOT_ESTABLISHED` | A passport was read but its expiry could not be established | amber, "needs a check" |
| `REVIEW` | Read, but a person must decide | amber, "with reviewer" |
| `FAIL` | Wrong / unreadable / invalid | red, show reason + re-upload |
| `PROCESSING` | Still running in background | spinner, poll |
| `MISSING` | Slot not uploaded | Upload button |
| `CONFIGURATION_GAP` | Business rule not configured yet | grey, "pending configuration" |

### 6.3 Real example — checklist slot with its own upload button

```json
{
  "slot": "BANK_STATEMENT",
  "label": "Bank Statement",
  "status": "MISSING",
  "required": true,
  "accepted_types": ["BANK_STATEMENT"],
  "actions": [
    { "action": "UPLOAD_DOCUMENT", "document_type": "BANK_STATEMENT",
      "accepted_types": ["BANK_STATEMENT"], "enabled": true }
  ]
}
```

Render one row per slot; the Upload button uploads **only** the types in its
`accepted_types`. Send the slot's `document_type` in `document_types[]`.

### 6.4 Real example — per-file verification card

```json
{
  "source_id": "pan.jpg",
  "document_type": "PAN",
  "expected_type": "PAN",
  "verification": "PASS",
  "status": "SUCCESS",
  "reason_codes": [],
  "extraction_released": true,
  "authenticity": "NOT_ESTABLISHED",
  "verification_scope": "DOCUMENT_STRUCTURE_AND_FIELD_CONSISTENCY",
  "issuer_verified": false
}
```

### 6.5 Real example — blockers and next step

```json
"readiness": {
  "status": "NOT_READY",
  "blocking_count": 2,
  "blocking_items": [
    { "type": "DOCUMENT", "code": "DOCUMENT_MISSING", "slot": "ADDRESS_PROOF",
      "detail": "Address Proof has not been uploaded." },
    { "type": "DOCUMENT", "code": "DOCUMENT_MISSING", "slot": "BANK_STATEMENT",
      "detail": "Bank Statement has not been uploaded." }
  ]
},
"next_action": { "action": "COLLECT_DOCUMENT", "target": "ADDRESS_PROOF",
                 "detail": "Collect and upload the missing document: Address Proof." }
```

### 6.6 Long-running work (polling)

There is **no streaming (SSE/WebSocket)** yet. When a document is still being
read, its item shows `PROCESSING` with a job id. Poll:

```mermaid
sequenceDiagram
    participant FE as Frontend
    participant API as Backend
    FE->>API: "verify everything" / upload
    API-->>FE: items: PROCESSING (+ job_id)
    loop every 3–5 s until no PROCESSING
        FE->>API: GET /api/v1/los/cases/{case_id}/processing
        API-->>FE: job states
    end
    FE->>API: GET /api/v1/fos/checklist/{case_id}
    API-->>FE: final statuses
```

Never show "done" while an item is `PROCESSING`.

### 6.7 Moving a stage (human confirmation)

```mermaid
sequenceDiagram
    participant U as User
    participant FE as Frontend
    participant API as Backend
    U->>FE: "can I move to CPA?"
    FE->>API: CUSTOM_QUERY
    API-->>FE: gate = READY + actions[STAGE_TRANSITION {target_stage, expected_stage, idempotency_key}]
    FE->>U: Confirm dialog
    U->>FE: Confirm
    FE->>API: POST /api/v1/los/cases/{id}/stage {target_stage, expected_stage, idempotency_key}
    API-->>FE: new stage (re-read) | 409 GATE_NOT_MET (re-checked now) | 409 GATE_CONFIGURATION_GAP | 409 STALE_STAGE
```

`expected_stage` protects against stale screens; `idempotency_key` makes a
double-click safe. Requires the stage-write scope.

**`mode: "GATED"` (the default)** — the server re-evaluates the configured gate at
the moment of the move. If the case changed after the Copilot offered the move, you
get `409 GATE_NOT_MET` with `detail.gate.blockers[]` (render them as the blocker
list). A stage whose criteria are not configured returns `409
GATE_CONFIGURATION_GAP` — show "pending configuration", never "passed".

**`mode: "OVERRIDE"`** — an explicit, ungated move for operators/integrations. Needs
the `los.stage:override` scope; the response has `override: true` and the gate it
overrode, and the case timeline gets a `STAGE_TRANSITION_OVERRIDE` event. A normal
user screen should never send it.

### 6.8 Raise Query button (queries and deviations)

The chat and the button use **one action and one endpoint**. The frontend never
parses the message and never invents query semantics.

```mermaid
sequenceDiagram
    participant U as User
    participant FE as Frontend
    participant API as Backend
    U->>FE: "query raise kar do" (or a REVIEW / FAIL document card)
    FE->>API: CUSTOM_QUERY (or any answer with document cards)
    API-->>FE: response_type QUERY_PROPOSED + raise_query_action (also in actions[] / a card's actions[])
    FE->>U: [Raise Query] button, prefilled text editable
    U->>FE: Confirm
    FE->>API: POST raise_query_action.endpoint with raise_query_action.body
    API-->>FE: 201 {query{query_id, status: OPEN, history[]}, open_items, gate (re-read)}
```

The `RAISE_QUERY` action:

```json
{"action_id": "RAISE_QUERY", "available": true, "label": "Raise Query", "stage": "FOS",
 "target_type": "DOCUMENT", "target_id": "<document id>", "query_type": "VERIFICATION_ISSUE",
 "prefill": "Please review the Signature: it needs a reviewer (...).", "evidence_refs": [...],
 "requires_confirmation": true, "method": "POST",
 "endpoint": "/api/v1/los/cases/<case_id>/queries", "body": {...}}
```

- **Nothing is created until the button is posted.** A second click (or the same
  question on the same subject while it is open) returns the existing query with
  `result: "EXISTING"`. If one is already open, the chat says so (`QUERY_EXISTS`).
- **Lifecycle** (configured in `app/config/queries.yaml`): `OPEN → RESPONDED →
  RESOLVED`, `RESOLVED → REOPENED`, `OPEN/REOPENED → CANCELLED`. An illegal move
  returns `409 INVALID_QUERY_TRANSITION`. Resolving needs the reviewer-side scope
  (`los.query:resolve` or `los.stage:write`); the person answering is not the
  person who closes it.
- **An open query blocks the gate.** It appears as gate blocker `OPEN_QUERIES`
  (configured, UNCONFIRMED). **RESOLVED is not a stage pass**: the gate's own
  criteria still apply.
- **Deviations** are exceptions to a configured rule, decided by the configured
  authority (`los.deviation:approve`), never by the person who raised them
  (`403 SELF_APPROVAL_FORBIDDEN`), and always with a justification. **No deviation
  rule is configured yet**, so `deviation_rules` is `CONFIGURATION_GAP` and none is
  raised automatically. A pending deviation blocks the gate (`PENDING_DEVIATIONS`).
- Keep these apart in the UI: QUERY, DEVIATION, BLOCKER, HUMAN_REVIEW,
  PROCESSING, PASS, FAIL.

### 6.9 Errors

| Situation | What you get |
|---|---|
| No / bad token | `401 {"detail": "..."}` |
| Not your case | `403 {"detail": {"code": "CASE_ACCESS_DENIED", "message": "...", "request_id": "..."}}` |
| Bad file in an upload | `200`, that file's card has `INVALID_UPLOAD` / `WRONG_DOCUMENT` in `upload_validation`; other files processed |
| Question not understood | `200`, `clarification_required {question, options[]}` → render options as chips; `errors: [{code: UNSUPPORTED_REQUEST}]` |
| Unsafe request | `200`, `intent: GUARDRAIL_BLOCKED`, polite refusal, nothing read |

**HTTP 200 does not always mean "answered"** — check `errors[]` and
`clarification_required`.

---

## 7. Conversation & languages

```mermaid
flowchart LR
    T1["Turn 1: 'is my KYC done?'"] --> CTX1["response.context"]
    CTX1 -->|send back| T2["Turn 2: 'what about the other guy?'"]
    T2 --> R2["co-applicant's KYC<br/>(same topic, other person)"]
```

- Always send back the last `context` object (or `conversation_id` on the
  universal route).
- Supported input: English, Hindi, Hinglish, Marathi (incl. Roman), Bengali,
  Tamil, Telugu, Gujarati, Kannada, Malayalam, Punjabi, Urdu, Nepali and more.
- `language_contract.reply_language` tells you the language the `answer` is
  actually in. If a template does not exist for a fact, the answer stays in
  English and `localized: false` — show it as is (set `dir="rtl"` for Urdu).
- Numbers, names, IDs, statuses never change with language.
- Marathi typed in Latin letters ("KYC zala ka?") is detected as `mr-Latn` and
  answered in Marathi (`response_language: "mr"`). Verification, next step,
  required documents, KYC, case status and document details are localized
  from their structured blocks; document labels and quoted values stay as recorded.
- A word Hindi and Marathi share ("धन्यवाद", "नमस्कार") is answered in the
  conversation's language — another reason to always send `context` back.

---

## 8. Minimal TypeScript client

```ts
const BASE = import.meta.env.VITE_LOS_API;          // e.g. http://127.0.0.1:8765

async function login(username: string, password: string) {
  const r = await fetch(`${BASE}/api/v1/auth/login`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!r.ok) throw new Error("login failed");
  return (await r.json()) as { access_token: string; refresh_token: string; expires_in: number };
}

let lastContext: unknown = null;                     // conversation memory

async function ask(token: string, applicantId: string, caseId: string, message: string) {
  const r = await fetch(`${BASE}/api/v1/fos/copilot`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ applicant_id: applicantId, case_id: caseId,
                           action: "CUSTOM_QUERY", message, context: lastContext }),
  });
  const body = await r.json();
  if (!r.ok) throw body;                              // 401 / 403 structured detail
  lastContext = body.context ?? lastContext;
  return body;                                        // render from structured fields
}

async function upload(token: string, applicantId: string, caseId: string,
                      files: File[], types: string[]) {
  const form = new FormData();
  form.append("applicant_id", applicantId);
  form.append("case_id", caseId);
  form.append("action", "UPLOAD_DOCUMENT");
  files.forEach((f) => form.append("files", f));
  types.forEach((t) => form.append("document_types", t));   // positional, one per file
  const r = await fetch(`${BASE}/api/v1/fos/copilot`, {
    method: "POST", headers: { Authorization: `Bearer ${token}` }, body: form,
  });
  return r.json();     // verification.documents_processed[], checklist[], readiness, next_action
}

async function moveStage(token: string, caseId: string,
                         a: { target_stage: string; expected_stage: string; idempotency_key: string }) {
  const r = await fetch(`${BASE}/api/v1/los/cases/${caseId}/stage`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ ...a, reason: "Confirmed by user in copilot" }),
  });
  if (r.status === 409) throw new Error("Stage changed meanwhile — refresh");
  return r.json();
}
```

---

## 9. Running locally

```bash
# 1. dev identity provider (JWKS on :8020) — development only
ENVIRONMENT=development AUTH_CLIENT_SECRET=<random> python auth_provider_dev.py

# 2. API
ENVIRONMENT=development \
JWT_JWKS_URL=http://127.0.0.1:8020/.well-known/jwks.json \
JWT_ISSUER=los-local JWT_AUDIENCE=los-agentic-ai \
python -m uvicorn main:app --host 127.0.0.1 --port 8765

# 3. open http://127.0.0.1:8765/docs
```

Optional model features (off by default, measured as not improving answers on
CPU — see the latest validation report): `CHATBOT_NATURAL_COMPOSITION=true`,
`COPILOT_UNDERSTANDING_LLM=true`, `OLLAMA_MODEL=qwen2.5:3b`.

---

## 10. Measured performance (local CPU, quiet machine)

| Operation | Typical |
|---|---|
| Chat / follow-up / action / multilingual | 40–50 ms (P95 < 110 ms) |
| Upload PAN / Driving Licence | ~1.1 s / ~1.9 s (first request after start: ~2.5 s / ~3.9 s) |
| Whole FOS document set in one request | ~2.3 s |
| Bank statement / salary slip / ITR verify | 35 ms / 9 ms / ~190 ms |
| Knowledge answer with Qwen | ~2.3 s |

Timings degrade when endpoint-security software or other heavy processes are
active on the same machine.
