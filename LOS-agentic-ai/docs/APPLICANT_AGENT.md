# Applicant Agent — the FOS copilot

A field officer asks a question in plain English and gets an answer grounded
in the case's real records, instead of navigating five LOS screens to assemble
it themselves.

```
FOS: "What's pending and what should I do next?"

     Rahul Sharma — application CASE-7DFE2F497522 is at Basic Document
     Verification. Completed: PAN, Driving Licence. Pending: applicant
     address is not captured; Bank Statement has not been uploaded.
     Next action: capture the applicant's address.
     CPA readiness: Not ready — 2 items blocking.
```

---

## 1. What it is, and what it is not

**It answers** questions about the applicant, the application, the documents
collected, their verification verdicts, what is outstanding, what the FOS
should do next, and whether the case may be handed to CPA.

**It does not decide anything.** Every business answer above is computed by
`app/agents/applicant/workflow.py` from stored records and configuration. The
language model is handed those results and asked to phrase them. It never
reads the database, never chooses a tool it was not planned into, and never
supplies a fact.

**It does not answer credit, risk, KYC, RCU or lending questions.** Those are
recognised and routed to the capability that owns them. It invents no result
on their behalf.

> **`readiness` is not a credit decision.** It answers "has the FOS collected
> enough for the next desk to start work?" — nothing about whether the loan
> should be approved.

---

## 2. The FOS lifecycle it serves

```
APPLICANT CREATION → APPLICATION CREATION → BASIC INFORMATION
    → DOCUMENT COLLECTION → UPLOAD → BASIC VERIFICATION
    → MISSING/PENDING ITEMS → CORRECTION / RE-UPLOAD
    → COMPLETENESS CHECK → READY FOR CPA → CPA
```

The four states the store actually distinguishes — configurable in
`app/config/applicant_agent.yaml` under `workflow.states`:

| State | Meaning |
|---|---|
| `APPLICATION_CREATED` | Application exists, no documents yet |
| `DOCUMENT_COLLECTION` | Documents arriving, none adjudicated |
| `BASIC_DOCUMENT_VERIFICATION` | At least one document has a verdict |
| `READY_FOR_CPA` | Nothing is blocking the handoff |

**The stage is derived, not trusted.** It is computed from the records on
every read, so a case cannot sit in `DOCUMENT_COLLECTION` after everything has
been verified because nothing wrote the transition.

Document states: `MISSING` · `UPLOADED` · `PROCESSING` · `VERIFIED` ·
`REVIEW` · `REJECTED`. `MISSING` is never stored — it is the absence of a row,
derived against the product checklist.

---

## 3. Architecture

```
FOS
 │  Authorization: Bearer <JWT>
 ▼
POST /api/v1/applicant-agent/query
 ▼
classify (deterministic patterns — no model)
 ▼
route out-of-scope ──────────────► CREDIT / RISK / KYC / RCU / DECISION
 ▼
check scope  ──┐
check ownership┘   ← both BEFORE any record is read
 ▼
plan (intent → fixed tool list)
 ▼
MCP tool layer          app/mcp/applicant.py
 ▼
Repository (interface)  app/store/repository.py
 ▼
SQLite (current impl)   app/store/sqlite_repo.py
 ▼
deterministic workflow  checklist · pending · next action · readiness
 ▼
phrase: deterministic, then model (validated)
 ▼
structured response
```

**The ordering is the design.** Authorisation happens before any tool runs,
and the business answer is computed before the model is consulted. A model
that is off, slow or wrong costs the phrasing and nothing else.

### The layers

| File | Responsibility |
|---|---|
| `agent.py` | The flow above |
| `intents.py` | What was asked, and which tools answer it |
| `permissions.py` | Who may ask it, and about whom |
| `workflow.py` | Checklist, pending items, next action, readiness |
| `answer.py` | Phrasing — deterministic first, model second |
| `validate.py` | The model may not state a fact it was not given |
| `audit.py` | Who asked what, and what changed |

Nothing in `app/agents/applicant/` imports the repository or `sqlite3`.

---

## 4. The store

**There was no applicant, application or case persistence in this build** —
`CASE_STORE_AVAILABLE = False`. This adds one, as the source of truth the
agent reads.

```python
class Repository(ABC):          # app/store/repository.py
    def get_applicant(...)      def save_applicant(...)
    def get_application(...)    def save_application(...)
    def get_document(...)       def save_document(...)
    def list_documents(...)     def applicant_owns_case(...)
```

SQLite is the only implementation in this build and it adds **no dependency**
(`sqlite3` is stdlib). Replacing it with PostgreSQL, or an adapter onto an
existing LOS over HTTP, means implementing that interface and changing one
value:

```bash
LOS_STORE_BACKEND=sqlite          # register a new name in app/store/__init__.py
LOS_STORE_PATH=./runtime/los_store.sqlite3
```

No agent, MCP tool or route changes.

### How it is populated — real data only

**Nothing is seeded.** Rows arrive two ways, both legitimate:

1. **`POST /api/v1/los/process`** — when it finishes, `app/store/ingest.py`
   copies what the pipeline concluded: the type classification found, the
   verdict the Document Verification Agent reached, its reason codes. It
   **copies**; it never re-derives or second-guesses a verdict.

2. **The FOS record-keeping endpoints** — `POST .../applicants` and
   `POST .../applications`.

Ingest is **non-fatal by design**: it runs after the response is assembled, so
a store that is unavailable cannot turn a successful document-processing
request into a 500.

Ingest stores **field names, never values**. The store records that a PAN
number was extracted; the number itself stays in the pipeline's response
rather than becoming a second copy that would have to be protected.

---

## 5. Intents

| Intent | Example | Tools |
|---|---|---|
| `APPLICANT_DETAILS` | "Who is the applicant?" | `applicant.get` |
| `APPLICANT_MISSING_INFO` | "What information is still missing?" | `applicant.get`, `workflow.pending_items` |
| `APPLICATION_STATUS` | "What's the application status?" | `application.get` |
| `APPLICATION_STAGE` | "Where is this application?" | `applicant.360` |
| `DOCUMENTS_UPLOADED` | "Which documents have been uploaded?" | `documents.get` |
| `DOCUMENTS_REQUIRED` | "Show me the document checklist." | `documents.checklist` |
| `DOCUMENTS_MISSING` | "Which docs are left?" | `documents.checklist` |
| `DOCUMENTS_PENDING` | "Which documents are under review?" | `documents.get`, `workflow.pending_items` |
| `DOCUMENT_VERIFICATION` | "Is PAN verified?" | `documents.verification` |
| `PENDING_ITEMS` | "What's pending?" | `workflow.pending_items` |
| `NEXT_ACTION` | "What should I do next?" | `workflow.next_action` |
| `READINESS` | "Can I send this to CPA?" | `workflow.readiness` |
| `COMPLETENESS` | "Is everything complete?" | `workflow.readiness`, `workflow.pending_items` |
| `FULL_SUMMARY` | "Summarize this case." | `applicant.360` |
| `CREATE_APPLICANT` / `UPDATE_APPLICANT` / `CREATE_APPLICATION` / `MARK_FOR_REUPLOAD` | writes | proposed, then confirmed |
| `OUT_OF_SCOPE` | "What's the credit score?" | none |
| `UNKNOWN` | unrecognised | none |

**Classification is rules, not a model.** A FOS asking "is PAN verified?"
should not wait two seconds for a model to decide the question is about a
document, and a classifier that occasionally routes "what's pending" to the
credit agent is worse than none. Out-of-scope patterns are matched **first**,
so a credit question is diverted before anything tries to answer it.

---

## 6. MCP tools

All data flows through `app/mcp/applicant.py`. Same `ToolEnvelope`, error
codes and boundary discipline as the existing `app/mcp/capabilities.py`.

**Read:** `applicant.get` · `application.get` · `applications.list` ·
`documents.get` · `documents.checklist` · `documents.verification` ·
`workflow.pending_items` · `workflow.next_action` · `workflow.readiness` ·
`applicant.360`

**Write:** `applicant.create` · `applicant.update` · `application.create` ·
`application.update` · `documents.mark_for_reupload`

Every tool is typed, deterministic, timeout-protected
(`timeouts.tool_seconds`), and converts any failure into a structured envelope
— no exception crosses the boundary as a traceback.

`documents.verification` **consumes** the Document Verification Agent's
verdict. It does not re-run or re-derive verification.

---

## 7. API

### `POST /api/v1/applicant-agent/query`

```json
{ "message": "What's pending and what should I do next?",
  "applicant_id": "APP-3D51FFAC6342",
  "case_id": "CASE-7DFE2F497522" }
```

Response carries prose **and** structure, so a copilot panel never parses the
prose:

```json
{ "request_id": "aa_…", "applicant_id": "…", "case_id": "…",
  "intent": "FULL_SUMMARY", "answer": "…",
  "applicant": {…}, "application": {…}, "stage": "BASIC_DOCUMENT_VERIFICATION",
  "documents": […], "checklist": […], "pending_items": […],
  "next_action": {…}, "readiness": {…},
  "actions": [], "route_to": null,
  "response_source": "llm", "processing_ms": 1553.4, "errors": [] }
```

| Endpoint | Purpose |
|---|---|
| `POST /query` | Ask a question |
| `POST /confirm` | Apply a proposed change |
| `POST /applicants` | Create an applicant |
| `POST /applications` | Create an application |
| `GET /applicants/{id}/360?case_id=…` | Structured 360 view, no prose |
| `GET /config` | Resolved configuration |

### Applicant 360

```
Applicant ├── basic information + missing_fields
Application ├── status · product · stage
Documents ├── required · uploaded · verified · review · missing
Pending items
Next action
CPA readiness
```

**Never exposed:** chain of thought, prompts, database queries, raw OCR,
bounding boxes, internal MCP payloads, agent state, secrets, JWTs. Enforced by
`validate_response_shape()` and covered by a test.

---

## 8. Authentication and permissions

Existing RS256 JWT/JWKS — see `docs/AUTHENTICATION.md`. Two independent
checks, both required, **both outside the model**:

**Capability** — does the token carry the scope?

| Scope | Grants |
|---|---|
| `read_applicant` · `read_application` · `read_documents` · `read_verification` · `read_pending_items` · `read_next_action` | the matching reads |
| `los.read` | every read (never a write) |
| `create_applicant` · `update_applicant` · `create_application` · `upload_document` | the matching writes |

**Ownership** — does this case belong to this applicant? Answered by the
repository. A token with every scope still cannot read another applicant's
case. A mismatch is refused without confirming the case exists elsewhere.

| Condition | Result |
|---|---|
| No token / invalid / expired | `401` |
| Missing scope | `403 INSUFFICIENT_SCOPE` |
| Case belongs to another applicant | `403 CASE_NOT_ACCESSIBLE` |

Denied outright whatever the token says: `credit_decision`, `risk_analysis`,
`kyc_decision`, `rcu_analysis`.

---

## 9. Controlled writes

A write is **proposed, never performed** on the first pass:

```
FOS:   "Update the applicant's phone to 9998887776"
Agent: { "actions": [{ "action_id": "act_11eb3e50a4f9",
                       "type": "UPDATE_APPLICANT",
                       "requires_confirmation": true,
                       "summary": "Update mobile to 9998887776" }],
         "answer": "Update mobile to 9998887776. Please confirm…" }

FOS → POST /confirm with that action → scope re-checked → MCP write → audit
```

The scope is checked **again** at confirmation: the two calls are separate
requests and may carry different tokens. The model neither decides that a
write is wanted nor carries one out.

---

## 10. Output validation

The model may not state a fact it was not given. An answer is discarded
**whole** if it is empty, over-long, returns JSON, states a number absent from
the facts, quotes a status the facts do not carry, or uses downstream decision
language (`approved`, `sanction`, `credit score`, `creditworthy`…). A rejected
answer falls back to the deterministic one.

**Known limit, stated honestly:** the status check matches status *tokens*
(`VERIFIED`, `REJECTED`), not lowercase prose — "the address is missing" is
English, not a status claim. A lowercase paraphrase of a status it was not
given would not be caught by that specific rule. It is bounded instead by the
model only ever being shown true facts, by the number check, and by the
forbidden-language check.

**Prompt injection:** applicant and document data is untrusted input. The
system prompt instructs the model to ignore instructions appearing inside the
data, and the question and data are separately labelled. More importantly,
permissions are enforced outside the model entirely — no text in a record can
grant a scope, change a tool plan or reach another applicant's case.

---

## 11. Configuration

`app/config/applicant_agent.yaml`. **Business policy lives here, not in a
prompt** — a readiness gate the model could paraphrase is one nobody can
audit.

| Section | Controls |
|---|---|
| `agent` | name, enabled, LLM on/off, temperature |
| `workflow.states` | the FOS states |
| `documents` | required checklist per product, with `any_of` alternatives |
| `readiness` | which rules apply; a rule switched off is **reported**, not hidden |
| `permissions` | scope names, read-all scope, denied capabilities |
| `routing` | where out-of-scope questions go |
| `timeouts` | tool, LLM, total |

Env overrides: `APPLICANT_AGENT_<NAME>`, plus `LOS_STORE_BACKEND` /
`LOS_STORE_PATH`.

### The LLM budget

The agent's LLM budget is **3.0 s**, deliberately *not* the LOS summary's
1.5 s. Measured on the reference host, the same copilot prompt at four output
caps (4 generations each):

| max_tokens | median | max | within 1.5 s |
|---|---|---|---|
| 48 | 2525 ms | 2911 ms | 0/4 |
| 64 | 2293 ms | 2619 ms | 0/4 |
| 96 | 2356 ms | 2504 ms | 0/4 |
| 160 | 2213 ms | 2515 ms | 0/4 |

Cost is flat across caps, so it is prompt processing rather than output
length: 1.5 s could not have been met by shortening the answer. **The LOS
summary's 1.5 s budget is unchanged.**

Simple single-fact intents (`is PAN verified?`, `what's next?`) skip the model
entirely and answer in **under 3 ms**.

---

## 12. Observability and audit

Every request logs `request_id`, `subject`, `applicant_id`, `case_id`,
`intent`, tools called, `response_source`, LLM ms, total ms.

Audit (`runtime/audit/applicant_agent.jsonl`) records who asked, when, about
which case, the intent, tools invoked, whether it was a write, whether it was
confirmed, and how it ended.

**Never logged or audited:** JWTs, prompts, raw OCR, extracted field values,
answer text. The message is truncated to 120 characters.

---

## 13. Errors

| Condition | Response |
|---|---|
| Applicant / application not found | `404 NOT_FOUND` |
| No token / invalid | `401` |
| Missing scope | `403 INSUFFICIENT_SCOPE` |
| Another applicant's case | `403 CASE_NOT_ACCESSIBLE` |
| Store unavailable | `CASE_STORE_UNAVAILABLE` |
| Tool timeout | `TOOL_TIMEOUT`, partial answer with the gap named |
| LLM unavailable / invalid output | deterministic answer, `200` |
| Unsupported request | `200` with `intent=UNKNOWN` and what it *can* do |

No stack trace ever reaches a caller.

---

## 14. Testing through Swagger

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8010   # no --reload
```

`http://127.0.0.1:8010/docs` → **Authorize** → paste the raw token.

1. `POST /api/v1/applicant-agent/applicants` — note the `applicant_id`
2. `POST /api/v1/applicant-agent/applications` — note the `case_id`
3. `POST /api/v1/los/process` — upload real documents with that
   `applicant_id` and `case_id`; the verdicts are persisted
4. `POST /api/v1/applicant-agent/query` — ask anything in §5
5. `GET /api/v1/applicant-agent/applicants/{id}/360?case_id=…`

A FOS token needs the scopes in §8. Without them the reads return `403` — the
permission layer working, not a fault.

---

## 15. Known limitations

1. **`response_source` is not guaranteed `llm`.** On CPU-only hardware
   generation is 2.2–2.5 s against a 3 s budget; a slow moment falls back.
   Deterministic answers are complete and correct — treat either as normal.
2. **SQLite is single-node.** Right for one branch's FOS stage; not for a
   national deployment. The interface exists so it can be replaced.
3. **Intent classification is pattern-based.** Unusual phrasing returns
   `UNKNOWN` with a list of what the agent can do, rather than a guess. New
   phrasings are added to `intents.py`.
4. **No conversation memory.** Each question is answered from the applicant
   and case supplied with it. Deliberate for the MVP.
5. **Ingest needs an `applicant_id`.** A `/los/process` call without one is
   not persisted; the documents have no owner to attach to.
6. **ITR and salary-slip extraction are out of scope**, as are KYC, credit,
   risk, RCU, underwriting, bureau and the lending decision.

---

## 16. The FOS integration surface (start here)

**A frontend integrates against exactly two endpoints.** Everything in this
document above is how they work inside; a FOS developer does not need it.

| | |
|---|---|
| `POST /api/v1/fos/applicants` | Open a case: applicant + application in one call |
| `POST /api/v1/fos/copilot` | Everything else: questions, dropdown actions, upload |

Nothing else is part of the integration. A frontend that calls only these two
has the whole product; anything else this service exposes is internal or
superseded, and is listed in §18 so it is not mistaken for the contract.

### Open a case

```json
POST /api/v1/fos/applicants
{ "applicant":   { "full_name": "Rahul Sharma", "mobile": "9876543210",
                   "email": "rahul.sharma@example.com",
                   "date_of_birth": "1990-04-12",
                   "address": "Mumbai, Maharashtra" },
  "application": { "product": "PERSONAL_LOAN", "loan_amount": 500000 } }
```

Returns `201` with the generated `applicant_id` and `case_id`, the initialised
stage, and the checklist the product requires.

### The copilot

```json
POST /api/v1/fos/copilot          application/json
{ "applicant_id": "APP-…", "case_id": "CASE-…",
  "action": "GET_PENDING_ITEMS" }

{ "applicant_id": "APP-…", "case_id": "CASE-…",
  "action": "CUSTOM_QUERY", "message": "What documents are pending?" }
```

### Uploading documents

```
POST /api/v1/fos/copilot          multipart/form-data

applicant_id      APP-...
case_id           CASE-...
action            UPLOAD_DOCUMENT
files             one or more documents          <- repeat the field
document_types    optional, one per file, positional
```

**Several documents go in one request.** Repeat the `files` field. Each file
is classified and verified on its own and gets its own entry in the response,
so one bad file never stops the rest:

```
files:           PAN.jpg        DL.jpg              bank.pdf
document_types:  PAN            ADDRESS_PROOF       BANK_STATEMENT
```

`document_types` is **positional** — the Nth type belongs to the Nth file.
Leave the field out entirely to let the pipeline classify everything, or send
an empty value for a single file to classify just that one.

An asserted type is **checked, never applied**. A file that is not what was
claimed fails with `DOCUMENT_TYPE_MISMATCH` and releases no fields; it is
never quietly reclassified to match. A checklist slot name such as
`ADDRESS_PROOF` is accepted and resolves to the document types that satisfy
it.

The single-file form (`file` + `document_type`) still works.

An upload runs the existing LOS pipeline — classification, the verification
gate, then extraction **only** behind a PASS — and stores the verdict. Every
later query reports that stored verdict. Verification is not reimplemented
here, and neither is KYC.

#### What comes back

`verification.documents_processed` carries one row per file:

```json
"verification": {
  "total": 3, "passed": 2, "not_passed": 1,
  "documents_processed": [
    {"source_id": "PAN.jpg",  "document_type": "PAN",
     "verification": "PASS", "extraction_released": true,
     "reason_codes": [], "authenticity": "NOT_ESTABLISHED"},
    {"source_id": "DL.jpg",   "document_type": "DRIVING_LICENCE",
     "verification": "PASS", "extraction_released": true, "reason_codes": []},
    {"source_id": "dummy.png", "document_type": "UNKNOWN",
     "verification": "FAIL", "extraction_released": false,
     "reason_codes": ["DOC_CLASS_UNRECOGNISED"]}
  ]
}
```

`answer` names the files that did not pass, so an officer knows which one to
photograph again without opening all three.

#### KYC

A multi-document upload also returns `kyc` — cross-document consistency over
the documents that cleared the gate, with per-field `match_score` and
`confidence` and source attribution. See §8 of `API_INTEGRATION.md`.

It is **consumed here, never recomputed**. A single-document upload has
nothing to cross-check, so its KYC fields come back `SKIPPED` — which means
nothing was compared, not that nothing matched.

### Actions

`GET_APPLICANT` · `GET_APPLICATION_STATUS` · `GET_DOCUMENTS` ·
`GET_DOCUMENT_CHECKLIST` · `GET_VERIFICATION_STATUS` · `GET_PENDING_ITEMS` ·
`GET_NEXT_ACTION` · `GET_CASE_360` · `CHECK_CPA_READINESS` ·
`UPLOAD_DOCUMENT` · `CUSTOM_QUERY`

These eleven values are the contract — send one of them as `action`. They are
also served with display labels and per-action input requirements, so a
dropdown can be rendered without a release when the list grows (§18).

### One response shape

Every action returns the same 23 fields. Ones an action does not populate come
back null or empty, so a frontend binds one model:

```
request_id · applicant_id · case_id · action · intent · answer
applicant · application · stage
documents · checklist · required_documents · pending_items
verification · kyc · knowledge · next_action · readiness
actions · route_to · response_source · processing_ms · errors
```

`kyc` is present after an upload that cross-checked something. `knowledge` is
present when an answer drew on the FOS knowledge base, and absent otherwise —
citing a source on a pure case answer would imply the facts came from a
handbook.

### Errors

| Condition | Status |
|---|---|
| Unsupported action, missing `message`, unknown `document_type` | `422` |
| `UPLOAD_DOCUMENT` sent as JSON | `415 UPLOAD_REQUIRES_MULTIPART` |
| No / invalid token | `401` |
| Missing scope | `403 INSUFFICIENT_SCOPE` |
| Another applicant's case | `403 CASE_NOT_ACCESSIBLE` |

---

## 17. Who does what

**The line: the backend decides, the frontend displays.** Every field in the
response is already the answer. A frontend that recomputes one has forked the
business rules, and the two copies will disagree on the day it matters.

### The backend owns

| | |
|---|---|
| Verification verdicts | `PASS` / `REVIEW` / `FAIL`, and what releases extracted fields |
| Document classification | What the file actually is, whatever it was called |
| The checklist | Which slots a product requires, and what satisfies each |
| Slot status | `MISSING` / `UPLOADED` / `PROCESSING` / `VERIFIED` / `REVIEW` / `REJECTED` |
| Pending items | What is outstanding, in the order to work through it |
| `next_action` | The single next thing to do, and what it applies to |
| Readiness | Whether the case may go to CPA, and what is blocking |
| Stage | Derived from the records, not stored and trusted |
| Authorisation | Scope and ownership, decided before any record is read |
| Phrasing of `answer` | Including whether a model was used at all |

### The frontend owns

| | |
|---|---|
| Layout, navigation, and which fields are on which screen |
| Rendering `checklist` as a list, and `required_documents` as a progress count |
| Rendering `next_action.detail` as the call to action |
| The file picker, and showing upload progress |
| Retry and offline behaviour, and surfacing `errors` |
| Localisation of static labels — not of `answer`, which arrives phrased |

### The frontend must not

- **Recompute readiness, pending items or next action.** They arrive computed.
  `readiness.status` is the answer; `blocking_items` is why.
- **Decide whether a document is verified.** Read `checklist[].status` and
  `verification`. A green tick drawn from anything else is a lie on screen.
- **Infer a verdict from extracted fields.** Fields are released only behind a
  `PASS`; their absence is not evidence of anything the response has not said.
- **Hardcode the checklist.** It comes from configuration per product and
  changes without a frontend release.
- **Hide a `REVIEW`.** It means a person must look. Rendering it as a pass, or
  as a failure, both misreport it.
- **Send a credit, risk, KYC, bureau or approval question and render the
  reply as an answer.** Those come back with `route_to` set and no answer,
  because this service does not make those decisions.

### One request per screen

Every case-scoped action returns `checklist` and `required_documents` along
with whatever the action was for, so a screen renders from a single call. A
second round trip to fill in the checklist is a sign something was recomputed
that did not need to be.

---

## 18. Not part of the integration surface

Listed here so they are not mistaken for the contract. They are in Swagger and
they work; a FOS frontend does not need them.

| | |
|---|---|
| `GET /api/v1/fos/actions` | Action values with labels and input requirements. Only needed to render a dropdown without hardcoding §16's list. |
| `GET /api/v1/fos/config` | Products, document types and checklists, for tooling and support. |
| `GET /api/v1/applicant-agent/applicants/{id}/360` | The structured 360 view. Superseded by `GET_CASE_360`. |
| `POST /api/v1/applicant-agent/*` | The earlier surface. Still works, marked `deprecated` in Swagger. |

`/api/v1/applicant-agent/*` remains for backward compatibility. The agent
behind it is unchanged — the FOS endpoints are adapters over it, not a
replacement for it — so the two never disagree.

---

## 19. What a PASS means

A `PASS` on an identity document means **structurally valid and internally
consistent**. It does not mean genuine.

There is no issuer API, no government lookup and no issuer signature this
service can check against. Rather than leave that to the documentation, every
identity document carries it in the response:

```json
"verification": "PASS",
"authenticity": "NOT_ESTABLISHED"
```

`authenticity` is `NOT_ESTABLISHED` at every verdict, including `PASS`. A
frontend that shows a verified tick should not imply more than the field says.

### What is actually checked

| | |
|---|---|
| The file is the type the caller asserted | else `DOCUMENT_TYPE_MISMATCH`, `FAIL` |
| The file is a document at all, and legible | else `DOC_ILLEGIBLE` / `PAGE_BLANK`, `FAIL` |
| The identifier has the right structure | else `FAIL` |
| Every required field was read | else `REQUIRED_FIELD_MISSING`, `REVIEW` |
| Dates are plausible | else `REVIEW` |

A forgery that copies the structure correctly passes all of these. That is the
limit, and it is the reason `authenticity` is in the response.

### When the limit is not acceptable

```
VERIFICATION_AUTHENTICITY_POLICY=REQUIRE_EXTERNAL
```

Identity documents are then capped at `REVIEW` with
`AUTHENTICITY_NOT_ESTABLISHED`, whatever else they satisfy. Correct for a
lender that will not treat an unconfirmed card as verified. The cost: no case
reaches `READY_FOR_CPA` on documents alone until an issuer check is wired up.

Default is `STRUCTURAL_PASS`.

### Advisories

Some findings are worth a reviewer's attention but are not reliable enough to
refuse a document on. They come back separately:

```json
"verification": "PASS",
"advisories": ["PAN_NAME_INITIAL_MISMATCH"]
```

They never appear in `reason_codes` and never change a verdict — a code in
`reason_codes` reads as a problem with the document, and these are not that.
Per-rule severity is configurable under `verification.rules` in
`app/config/documents.yaml`: raise one to `review` or `fail` to make it
binding.

---

## 20. The copilot chatbot

`CUSTOM_QUERY` takes a question in plain English. Every question is sorted
into one of four kinds **before** anything is read, by matching phrases — not
by asking a model to classify. A field officer asking "is the PAN verified?"
should not wait on a model, and a classifier that occasionally routes a
pending-documents question to the credit desk is worse than none.

| Kind | Source of the answer | Example |
|---|---|---|
| **Case** | The store, always | "What documents are pending?" |
| **Knowledge** | The FOS knowledge base | "What can be used as address proof?" |
| **Mixed** | Both | "Why is this not ready and what should I collect?" |
| **Downstream** | Neither — routed | "What is the credit score?" |

### Case questions come from the records

```json
{ "action": "CUSTOM_QUERY", "message": "What documents are pending?" }
```

```json
{ "intent": "DOCUMENTS_PENDING",
  "answer": "Bank Statement and Address Proof are pending.",
  "knowledge": null,
  "response_source": "deterministic" }
```

**RAG is never used to answer a question about a case.** The checklist, the
verdicts, what is pending, the next action and readiness are all computed from
stored records. A knowledge base that appeared to answer case questions would
produce fluent, sourced, confident sentences about an applicant it has never
seen — indistinguishable from correct ones.

`knowledge` is `null` on these answers, because none was consulted.

### Knowledge questions come from the corpus

```json
{ "intent": "FOS_KNOWLEDGE",
  "answer": "ADDRESS_PROOF is a checklist slot, not a document type…",
  "knowledge": {
    "stage": "FOS",
    "grounded": true,
    "sources": ["address_proof.md#Address proof"],
    "top_score": 0.69
  } }
```

The corpus lives in `knowledge/fos/*.md` — plain markdown, edited without a
release. It covers the FOS workflow, document requirements, address proof,
verification, statuses, CPA readiness and an FAQ.

### Mixed questions get both

```json
{ "intent": "MIXED",
  "answer": "Bank Statement and Address Proof are pending.\n\nFor this
             product the ADDRESS_PROOF slot accepts a driving licence, a
             passport or a voter ID…",
  "readiness": { "status": "NOT_READY", … },
  "knowledge": { "grounded": true, "sources": [...] } }
```

The case half is answered **deterministically and first**, from exactly the
tools the question would have used on its own. The knowledge paragraph is
appended, never substituted: attaching a second clause to a question must not
turn its facts into a model's paraphrase.

### When the corpus cannot answer

```json
{ "answer": "I don't have enough information in the FOS knowledge base to
             answer that.",
  "knowledge": { "grounded": false, "sources": [], "top_score": 0.09 } }
```

Retrieval scores are normalised so that a query the corpus does not cover
scores near zero, and `KNOWLEDGE_MIN_SCORE` (default `0.28`) is the line. Below
it the agent declines. It does not return the least-bad paragraph with a
hedge — a confident sourced answer to a question the sources never addressed
is the worst failure a grounded system has, because it looks exactly like a
right one.

### Downstream questions are routed

```json
{ "intent": "OUT_OF_SCOPE",
  "route_to": "CREDIT_AGENT",
  "answer": "Credit and bureau information is handled by the downstream
             Credit process.",
  "kyc": null, "knowledge": null }
```

| Question about | `route_to` |
|---|---|
| Credit score, CIBIL, bureau | `CREDIT_AGENT` |
| Risk score or rating | `RISK_AGENT` |
| The full KYC decision | `KYC_AGENT` |
| Fraud, RCU, forgery, tampering | `RCU_AGENT` |
| Income or bank statement analysis | `CREDIT_AGENT` |
| Approval, sanction, disbursal | `DECISION_AGENT` |

Routing is decided **before** the store and before the corpus, and is
configurable in `app/config/applicant_agent.yaml` under `routing`. No
downstream result is ever invented.

### Configuration

| | |
|---|---|
| `KNOWLEDGE_ENABLED` | `true`. Off means knowledge questions are declined; case questions are unaffected. |
| `KNOWLEDGE_ROOT` | Where the corpus lives. Default `knowledge/`. |
| `KNOWLEDGE_BACKEND` | `lexical` (default, BM25) or `embedding`. |
| `KNOWLEDGE_MIN_SCORE` | `0.28`. The line below which the agent declines. |

### Where the model is and is not used

Never for a checklist, a verdict, KYC matching, readiness, a document status
or routing. Those are computed and a model cannot influence them.

Used only to phrase: the applicant narrative, the case briefing, and a
knowledge answer built **from retrieved passages** — never from the question
alone. If the model is off, unreachable or slow, the retrieved text is
returned as written. Losing it costs the prose and nothing else.
