# LOS Agentic AI — API Integration Guide

For the team integrating against this service. Everything below is the
contract as implemented and verified over real HTTP; nothing here is
aspirational.

---

## 1. Purpose

One call takes an applicant's documents end to end: each is classified,
verified and — only behind a verification PASS — extracted. The normalised
results are cross-checked against each other by KYC, a deterministic decision
is computed, and one response comes back.

**Every decision is deterministic.** A language model, when switched on,
writes the one-sentence `summary` and nothing else. It cannot change
verification, extraction, KYC, `cross_document`, `decision` or `next_action`.

You do **not** need to know about the internal agents, OCR engines, MCP, the
orchestrator, or the model. You consume one endpoint and one JSON contract.

---

## 2. Base URL

```
{BASE_URL}          e.g. http://localhost:8010   (local)
                         https://los.internal.example   (deployed)
```

All paths below are relative to `{BASE_URL}`.

---

## 3. Authentication

Every business endpoint requires a Bearer JWT. Health and metrics do not.

```
Authorization: Bearer <access_token>
```

- Algorithm **RS256**, verified against a JWKS endpoint.
- `iss`, `aud`, `exp`, `nbf` and `iat` are all validated. Signature
  validation is never skipped.
- Keys are fetched from `JWT_JWKS_URL` and cached for
  `JWT_JWKS_CACHE_SECONDS`.
- A small clock skew is tolerated (`JWT_LEEWAY_SECONDS`, default 30s).

Obtain tokens from your identity provider. No token is embedded in this
repository or this document.

| Failure | Response |
|---|---|
| No `Authorization` header | `401` `{"detail": "Missing Bearer token."}` |
| Expired / not yet valid | `401` `{"detail": "JWT token expired."}` |
| Wrong issuer or audience | `401` `{"detail": "JWT issuer is invalid."}` |
| Signature or `kid` unresolvable | `401` `{"detail": "Unable to resolve JWT signing key."}` |

---

## 4. Endpoint

```
POST {BASE_URL}/api/v1/los/process
Content-Type: multipart/form-data
Authorization: Bearer <access_token>
```

This is the **only** endpoint an integration needs. Other routes exist for
backward compatibility and internal use; do not build against them.

### Request fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `files` | file[] | yes | One or more documents in a single request. Repeat the field once per file. Max **10** per request, max **25 MB** each. |
| `operation` | string | no | `PROCESS` (default), `EXTRACT`, `VERIFY`. |
| `applicant_id` | string | no | Stable identifier for the **person**. |
| `case_id` | string | no | Identifier for **one application**. Omit to start a new case; the generated id is returned. |
| `expected_types` | string | no | Comma-separated, **positionally matched** to `files`. |

### `operation`

| Value | Meaning |
|---|---|
| **`PROCESS`** | The production verb. Runs the whole application: classify → verify → extract behind the gate → specialist capabilities → KYC → cross-document → decision → summary. |
| `EXTRACT` | The Document Agent's own mode. Behaves as `PROCESS`; kept for existing callers. |
| `VERIFY` | Verdict only — **never** releases extracted fields. |

Case and surrounding whitespace are tolerated. Anything else is `422`.

### `expected_types` is positional

```
files[0] = PAN.pdf          expected_types[0] = PAN
files[1] = DL.pdf           expected_types[1] = DRIVING_LICENCE
```

```
expected_types=PAN,DRIVING_LICENCE
```

- Use `AUTO` (or omit the field entirely) to let classification decide. No
  type check is performed for `AUTO`.
- A short list is allowed: files beyond the last entry are treated as `AUTO`.
- Asserting a type is a **commitment**. See §6.

Supported types: `PAN`, `DRIVING_LICENCE`, `VOTER_ID`, `PASSPORT`,
`BANK_STATEMENT`, `ITR`, `SALARY_SLIP`, `SALE_DEED`, `BUSINESS_PROOF_1`,
`BUSINESS_PROOF_2`, `SIGNATURE`.

---

## 5. The verification gate

```
classification  ->  verification  ->  extraction
                                      (only when verification == PASS)
```

| `verification` | `extraction` |
|---|---|
| `PASS` | released (unless extraction is disabled by configuration) |
| `FAIL` | `null` |
| `REVIEW` | `null` |
| `SKIPPED` | `null` |

**`SKIPPED` is never treated as `PASS`.** A check that did not run has
established nothing. If verification is switched off service-wide, every
document reports `SKIPPED`, no fields are released, and the application
cannot reach `PASS` — it returns `REVIEW` with a `NO_VERIFIED_DOCUMENTS`
entry in `errors`.

---

## 6. Strict document-type validation

If you assert a type and a different one arrives, the document fails
immediately — **before** any type-specific extractor or specialist runs.

```
expected_types = PAN          uploaded = a driving licence
```

```json
{
  "source_id": "pan.jpg",
  "expected_type": "PAN",
  "type": "DRIVING_LICENCE",
  "status": "REJECTED",
  "verification": "FAIL",
  "reason_codes": ["DOCUMENT_TYPE_MISMATCH"],
  "extraction": null
}
```

Symmetric: asserting `DRIVING_LICENCE` and uploading a PAN behaves the same
way.

**At application level this is a wrong upload, not a rejected applicant:**

```
status       PARTIAL | REVIEW
decision     REVIEW            (never REJECT for this alone)
next_action  REQUEST_CORRECT_DOCUMENT
```

The applicant has the document and sent the wrong file. That is why
`REQUEST_CORRECT_DOCUMENT` is distinct from `REQUEST_VALID_DOCUMENT`, which
means the right kind of document could not be read.

---

## 7. Response contract

`200 OK`, `application/json`. Top-level fields, always present:

| Field | Type | Notes |
|---|---|---|
| `request_id` | string | Correlation id for this call. Quote it in support requests. |
| `applicant_id` | string \| null | Echoed. |
| `case_id` | string | Echoed, or generated if you omitted it. |
| `status` | enum | Application roll-up. |
| `documents` | array | **Same order as uploaded.** |
| `kyc` | object | `{status, reason_codes}` |
| `cross_document` | object | `{status, checks[]}` |
| `decision` | enum | Deterministic outcome. Not a credit decision. |
| `next_action` | enum | What should happen next. |
| `summary` | string | One sentence. |
| `summary_source` | enum | `deterministic` or `llm`. |
| `processing_ms` | number | Server-side duration. |
| `errors` | array | `{code, message, source_id?}`. Empty when none. |

### Enums

**`status`** (application)

| Value | Meaning |
|---|---|
| `SUCCESS` | Every document succeeded and KYC passed. |
| `PARTIAL` | Some documents need attention. |
| `REVIEW` | Needs a human; also used when nothing was verified. |
| `REJECTED` | A blocking condition was met. |
| `FAILED` | A document could not be processed at all. |

**`documents[].status`** — `SUCCESS`, `REVIEW`, `SKIPPED`, `REJECTED`, `FAILED`

**`documents[].verification`** — `PASS`, `REVIEW`, `FAIL`, `SKIPPED`

**`decision`** — `PASS`, `REVIEW`, `REJECT`

> `decision` restates the deterministic roll-up. It is **not** an
> underwriting outcome and weighs no credit policy. `PASS` additionally
> requires that something was actually verified.

**`next_action`**

| Value | Meaning |
|---|---|
| `CONTINUE` | Proceed. |
| `MANUAL_REVIEW` | A human should look at it. |
| `REQUEST_CORRECT_DOCUMENT` | Wrong document type uploaded. |
| `REQUEST_VALID_DOCUMENT` | Right kind of document, could not be read. |

**`kyc.status` / `cross_document.status`** — `PASS`, `REVIEW`, `FAIL`, `SKIPPED`

> `SKIPPED` means nothing was comparable — not that everything agreed.

### `documents[]`

| Field | Notes |
|---|---|
| `source_id` | The filename you uploaded. Your join key. |
| `type` | Type this service identified, or `UNKNOWN`. |
| `expected_type` | What you asserted. Absent for `AUTO`. |
| `status` | Per-document roll-up. |
| `verification` | The verdict. |
| `extraction` | Fields, or absent/`null`. Keys depend on the type. |
| `reason_codes` | Present only when there is something to say. |
| `specialist` | Present only for specialist-handled evidence. |
| `evidence_refs` | `{source_id, locator}`. Locators are anchored on your `source_id` — never a server path. |
| `errors` | Per-document problems. |

### Common reason codes

| Code | Meaning |
|---|---|
| `DOCUMENT_TYPE_MISMATCH` | Detected type ≠ asserted type. |
| `VERIFICATION_DISABLED` | Verification switched off by configuration. |
| `CLASSIFICATION_DISABLED` | Classification switched off. |
| `EXTRACTION_DISABLED` | Passed verification, but extraction is off. |
| `CAPABILITY_DISABLED` | That specialist is switched off. |
| `NO_VERIFIED_DOCUMENTS` | Application-level: nothing cleared verification. |
| `NAME_MISMATCH` / `DOB_MISMATCH` / `PAN_MISMATCH` | Documents disagree. |
| `*_SINGLE_SOURCE` / `*_MISSING` | Nothing to compare against. |
| `INSUFFICIENT_SOURCES` | Fewer than two comparable documents. |

---

## 8. KYC and cross-document consistency

KYC runs **after** every document finishes, over normalised values only. It
establishes that the documents describe the same person consistently. It does
**not** establish that any document is genuine.

Checks: `NAME`, `DOB`, `ADDRESS`, `PAN`, `FATHER_NAME`, `INCOME`.

### The field-level view

`kyc.fields` is the operational shape — one row per comparable field, which is
what a field officer reads:

```json
"kyc": {
  "status": "REVIEW",
  "overall_score": 87,
  "overall_confidence": 94,
  "fields": [
    {
      "field": "NAME",
      "status": "PASS",
      "match_score": 100,
      "confidence": 98,
      "reason_code": "EXACT_MATCH",
      "reason": "Name matches across PAN and Driving Licence.",
      "sources": [
        {"source_id": "pan.jpg", "document_type": "PAN",
         "value": "RISHABH AJIT SINGH", "normalized_value": "RISHABH AJIT SINGH"},
        {"source_id": "dl.jpg", "document_type": "DRIVING_LICENCE",
         "value": "Rishabh Ajit Singh", "normalized_value": "RISHABH AJIT SINGH"}
      ]
    },
    {
      "field": "ADDRESS",
      "status": "PARTIAL",
      "match_score": 69,
      "confidence": 79,
      "reason_code": "PARTIAL_ADDRESS_MATCH",
      "reason": "Core address components overlap across PAN and Driving Licence, but the addresses are not identical.",
      "sources": [...]
    }
  ]
}
```

Field statuses: `PASS` · `PARTIAL` · `REVIEW` · `FAIL` · `SKIPPED`.
`PARTIAL` is the case that matters — materially overlapping but not identical,
which is neither a pass nor a rejection.

### `match_score` and `confidence` are different questions

**Do not treat them as one number.**

| | |
|---|---|
| `match_score` | how closely the available values match **each other** |
| `confidence` | how far **that answer** can be relied on |

They come apart in both directions, and both directions matter on screen:

| Situation | match | confidence | What it means |
|---|---|---|---|
| Cleanly-read names, agree | 100 | high | Nothing to doubt |
| Barely-legible names, agree | 100 | **low** | They agree on a value that may itself be wrong |
| Cleanly-read names, differ | 0 | **high** | The disagreement is real — act on it |
| One clean, one illegible, differ | 0 | low | Probably a misread, not a different person |

Confidence is built from the comparison method used, how many documents were
compared, how complete the field was across the bundle, the extraction quality
reported upstream, and how near the score sits to a decision threshold. Every
number is configurable in `app/config/kyc_policies.yaml` under `confidence:`,
and none of them is derived from `match_score`.

`overall_score` and `overall_confidence` are weighted means over the fields
that were **actually compared** — skipped fields are dropped and the remaining
weights renormalised, so an absent field neither helps nor hurts. Weights are
in `kyc_policies.yaml` under `fields.weights`.

**Neither is a risk score.** They say how consistent the documents are, and
nothing about creditworthiness.

### `SKIPPED` is not a failure

A field that could not be compared reports `SKIPPED` with `match_score: 0`.
That zero means *nothing was compared*, not *nothing matched*:

| Reason code | Meaning |
|---|---|
| `*_MISSING` | No document carried the field |
| `*_SINGLE_SOURCE` | Only one document carried it — nothing to compare it against |

One document is never compared with itself. Absent data never becomes `FAIL`.

**Policy: ordinary disagreement is non-blocking.** Two documents naming
different people is a reason to involve a human, not a finding of fraud —
married names, corrected dates, expanded initials and transliterations all
look the same to this service. So:

```
NAME / DOB / PAN mismatch
  -> the CHECK reports FAIL, with the values
  -> kyc.status          REVIEW
  -> cross_document      REVIEW
  -> decision            REVIEW
  -> next_action         MANUAL_REVIEW
```

A check can be made blocking in `app/config/kyc_policies.yaml`
(`blocking: true`), which restores `FAIL` → `decision: REJECT`. That is a
business policy decision, not a code change.

**Disagreements are source-attributed** — you get which documents and what
each said:

```json
{
  "check": "NAME",
  "status": "FAIL",
  "reason_codes": ["NAME_MISMATCH"],
  "sources": ["pan.jpg", "dl.jpg", "voter4.jpg"],
  "details": {
    "pan.jpg": "RISHABH AJIT SINGH",
    "dl.jpg": "RISHABH AJIT SINGH",
    "voter4.jpg": "KUNTI"
  }
}
```

Two documents that agree are **not** reported as mismatching each other. A
document that simply lacks the field does not cause a mismatch — the check
reports `SKIPPED` with a `*_MISSING` / `*_SINGLE_SOURCE` reason. Values are
never invented.

---

## 9. Bank statements

Extraction is released only after verification `PASS`, which for a statement
means **the transaction rows reconcile against the balance the statement
itself prints**.

```json
"extraction": {
  "account_number_masked": "XXXXXXXXXX5119",
  "period_start": "2026-01-22",
  "period_end": "2026-07-21",
  "signals": {
    "average_monthly_credit": "133877.63",
    "closing_balance": "229.70",
    "total_credits": "789878.00",
    "total_debits": "794473.00",
    "months_covered": 5.9,
    "declared_annual_income": null,
    "monthly_net_salary": null,
    "monthly_gross_salary": null
  },
  "evidence": {
    "reconciled": true,
    "opening_balance_source": "printed",
    "transaction_count": 524,
    "credit_count": 325,
    "debit_count": 199,
    "minimum_balance": "229.70",
    "maximum_balance": "214981.70",
    "average_transaction_balance": "34453.15",
    "largest_credit": "200000.00",
    "largest_debit": "90000.00",
    "monthly_credit_count": 7,
    "credit_volatility": 0.6612
  }
}
```

`evidence` is **deterministic evidence for a risk engine, not a credit
decision.** Nothing in it concludes anything about affordability.

Read these two fields carefully:

- **`reconciled`** — the rows explain the printed closing balance exactly.
- **`opening_balance_source`** — `"printed"` means the opening balance was
  read off the statement, so every row including the first was checked
  against an independent figure. `"derived"` means it was inferred from row
  one, which leaves row one's own side unverified: a wrong side there and a
  wrong opening cancel out and the chain still balances. Most Indian
  statements do not print an opening balance, so `"derived"` is common.
  Weight the signals accordingly.

Absent evidence is **absent, not zero** — no `salary_credit_count` means the
statement does not use the word, not that there were no salary credits. A
statement whose rows did **not** reconcile produces **no** `evidence` at all.

Raw transaction rows are never returned in this response.

---

## 10. HTTP status codes

As implemented:

| Code | When | Body |
|---|---|---|
| `200` | Processing completed. **Includes REVIEW and REJECT** — those are business outcomes, not transport errors. | Full contract |
| `400` | `NO_DOCUMENTS`, `EMPTY_FILE`, `INVALID_REQUEST` | `{"detail": {"request_id", "error", "message"}}` |
| `401` | Missing/invalid/expired token | `{"detail": "<reason>"}` |
| `413` | `TOO_MANY_DOCUMENTS` (>10), `FILE_TOO_LARGE` (>25 MB) | `{"detail": {...}}` |
| `422` | Invalid `operation`; FastAPI request validation (e.g. no `files` part) | `{"detail": ...}` |
| `500` | `LOS_PROCESSING_FAILED` | `{"detail": {"request_id", "error", "message"}}` |

**Do not treat `decision: REVIEW` or `REJECT` as an HTTP failure.** They are
`200`.

A `500` never contains a traceback, an internal path or a provider error —
only a safe message and the `request_id` to quote.

---

## 11. What is never in the response

OCR tokens · bounding boxes · candidate lists · confidence matrices · model
prompts · MCP payloads · stack traces · filesystem or temporary upload paths
· internal stage timings · internal agent state · thread ids · provider
internals · raw transaction rows.

`evidence_refs[].locator` is always anchored on your own `source_id`
(`sig.png#region=0,136,640,166`, `deed.pdf#page=1`) and never a server path.

Only `processing_ms` is published; the per-stage breakdown stays internal.

---

## 12. Examples

### curl

```bash
curl -X POST "{BASE_URL}/api/v1/los/process" \
  -H "Authorization: Bearer <TOKEN>" \
  -F "files=@PAN.pdf" \
  -F "files=@DL.pdf" \
  -F "operation=PROCESS" \
  -F "applicant_id=APP-12345" \
  -F "case_id=CASE-12345" \
  -F "expected_types=PAN,DRIVING_LICENCE"
```

### Clean PASS

```json
{
  "request_id": "los_db6ff7249ee64d45a5858369691d395c",
  "applicant_id": "APP-12345",
  "case_id": "CASE-12345",
  "status": "SUCCESS",
  "documents": [
    {
      "source_id": "PAN.pdf", "type": "PAN", "expected_type": "PAN",
      "status": "SUCCESS", "verification": "PASS",
      "extraction": {
        "pan_number": "ABCPV1234K", "name": "SUNIL KUMAR VERMA",
        "father_name": "RAMESH KUMAR VERMA", "date_of_birth": "1988-04-12"
      }
    },
    {
      "source_id": "DL.pdf", "type": "DRIVING_LICENCE",
      "expected_type": "DRIVING_LICENCE",
      "status": "SUCCESS", "verification": "PASS",
      "extraction": {
        "dl_number": "UP3220110004567", "name": "SUNIL KUMAR VERMA",
        "date_of_birth": "1988-04-12", "valid_till": "2031-08-04"
      }
    }
  ],
  "kyc": { "status": "PASS", "reason_codes": [] },
  "cross_document": {
    "status": "PASS",
    "checks": [
      { "check": "NAME", "status": "PASS", "reason_codes": [],
        "sources": ["PAN.pdf", "DL.pdf"] },
      { "check": "DOB", "status": "PASS", "reason_codes": [],
        "sources": ["PAN.pdf", "DL.pdf"] },
      { "check": "ADDRESS", "status": "SKIPPED",
        "reason_codes": ["ADDRESS_MISSING"] },
      { "check": "PAN", "status": "SKIPPED",
        "reason_codes": ["PAN_SINGLE_SOURCE"], "sources": ["PAN.pdf"] },
      { "check": "INCOME", "status": "SKIPPED",
        "reason_codes": ["INCOME_MISSING"] }
    ]
  },
  "decision": "PASS",
  "next_action": "CONTINUE",
  "summary": "2 document(s) processed (2 success). Cross-document KYC checks passed. Overall SUCCESS.",
  "summary_source": "deterministic",
  "processing_ms": 3421.8,
  "errors": []
}
```

### Wrong document type

```json
{
  "request_id": "los_...", "applicant_id": "APP-12345", "case_id": "CASE-12345",
  "status": "REVIEW",
  "documents": [
    {
      "source_id": "PAN.pdf", "expected_type": "PAN",
      "type": "DRIVING_LICENCE", "status": "REJECTED",
      "verification": "FAIL",
      "reason_codes": ["DOCUMENT_TYPE_MISMATCH"],
      "extraction": null
    }
  ],
  "kyc": { "status": "SKIPPED", "reason_codes": ["INSUFFICIENT_SOURCES"] },
  "cross_document": { "status": "SKIPPED", "checks": [] },
  "decision": "REVIEW",
  "next_action": "REQUEST_CORRECT_DOCUMENT",
  "summary": "1 document(s) processed (1 rejected). Overall REVIEW.",
  "summary_source": "deterministic",
  "processing_ms": 2140.6,
  "errors": []
}
```

### KYC REVIEW with source attribution

```json
{
  "status": "PARTIAL",
  "documents": [
    { "source_id": "pan.jpg", "type": "PAN", "status": "SUCCESS",
      "verification": "PASS", "extraction": { "name": "RISHABH AJIT SINGH" } },
    { "source_id": "dl.jpg", "type": "DRIVING_LICENCE", "status": "SUCCESS",
      "verification": "PASS", "extraction": { "name": "RISHABH AJIT SINGH" } },
    { "source_id": "voter4.jpg", "type": "VOTER_ID", "status": "SUCCESS",
      "verification": "PASS", "extraction": { "name": "KUNTI" } }
  ],
  "kyc": { "status": "REVIEW", "reason_codes": ["NAME_MISMATCH"] },
  "cross_document": {
    "status": "REVIEW",
    "checks": [
      { "check": "NAME", "status": "FAIL", "reason_codes": ["NAME_MISMATCH"],
        "sources": ["pan.jpg", "dl.jpg", "voter4.jpg"],
        "details": { "pan.jpg": "RISHABH AJIT SINGH",
                     "dl.jpg": "RISHABH AJIT SINGH",
                     "voter4.jpg": "KUNTI" } },
      { "check": "DOB", "status": "PASS", "reason_codes": [],
        "sources": ["pan.jpg", "dl.jpg"] }
    ]
  },
  "decision": "REVIEW",
  "next_action": "MANUAL_REVIEW",
  "summary": "3 document(s) processed (3 success). KYC REVIEW: name differs across pan.jpg, dl.jpg, voter4.jpg. Overall PARTIAL.",
  "summary_source": "deterministic",
  "processing_ms": 5210.4,
  "errors": []
}
```

Note `DOB` is `PASS`: the two documents that carry a date of birth agree, and
the one that does not carry it is not counted as a disagreement.

### Error

```json
{
  "detail": {
    "request_id": "los_...",
    "error": "TOO_MANY_DOCUMENTS",
    "message": "At most 10 documents per application."
  }
}
```

---

## 13. Calling it

**TypeScript**

```ts
const form = new FormData();
form.append("files", panFile);
form.append("files", dlFile);
form.append("operation", "PROCESS");
form.append("applicant_id", "APP-12345");
form.append("case_id", "CASE-12345");
form.append("expected_types", "PAN,DRIVING_LICENCE");

const res = await fetch(`${BASE_URL}/api/v1/los/process`, {
  method: "POST",
  headers: { Authorization: `Bearer ${token}` },   // no Content-Type: the
  body: form,                                      // browser sets the boundary
});
const result = await res.json();
```

**Python**

```python
import requests

files = [
    ("files", ("PAN.pdf", open("PAN.pdf", "rb"), "application/pdf")),
    ("files", ("DL.pdf", open("DL.pdf", "rb"), "application/pdf")),
]
data = {
    "operation": "PROCESS",
    "applicant_id": "APP-12345",
    "case_id": "CASE-12345",
    "expected_types": "PAN,DRIVING_LICENCE",
}
r = requests.post(
    f"{BASE_URL}/api/v1/los/process",
    headers={"Authorization": f"Bearer {token}"},
    files=files, data=data, timeout=120,
)
result = r.json()
```

**Java (JDK 11+)** — use any multipart builder (OkHttp, Apache HttpClient):

```java
RequestBody body = new MultipartBody.Builder()
    .setType(MultipartBody.FORM)
    .addFormDataPart("files", "PAN.pdf",
        RequestBody.create(panBytes, MediaType.parse("application/pdf")))
    .addFormDataPart("files", "DL.pdf",
        RequestBody.create(dlBytes, MediaType.parse("application/pdf")))
    .addFormDataPart("operation", "PROCESS")
    .addFormDataPart("applicant_id", "APP-12345")
    .addFormDataPart("case_id", "CASE-12345")
    .addFormDataPart("expected_types", "PAN,DRIVING_LICENCE")
    .build();
```

**.NET**

```csharp
using var form = new MultipartFormDataContent();
form.Add(new ByteArrayContent(panBytes), "files", "PAN.pdf");
form.Add(new ByteArrayContent(dlBytes),  "files", "DL.pdf");
form.Add(new StringContent("PROCESS"), "operation");
form.Add(new StringContent("APP-12345"), "applicant_id");
form.Add(new StringContent("CASE-12345"), "case_id");
form.Add(new StringContent("PAN,DRIVING_LICENCE"), "expected_types");

client.DefaultRequestHeaders.Authorization =
    new AuthenticationHeaderValue("Bearer", token);
var res = await client.PostAsync($"{BASE_URL}/api/v1/los/process", form);
```

Set a client timeout of at least **120 s**: OCR on several scanned documents
is CPU-bound.

---

## 14. Swagger

```
{BASE_URL}/docs           Swagger UI
{BASE_URL}/redoc          ReDoc
{BASE_URL}/openapi.json   Raw OpenAPI 3.1
```

`files` renders as a real multi-file picker. Click **Authorize**, paste the
raw token (no `Bearer ` prefix — Swagger adds it), then **Try it out**.

---

## 15. Applicant and case

- `applicant_id` identifies a **person**, reused across applications.
- `case_id` identifies **one application**. One applicant may hold several,
  and they stay separate.
- Supply `case_id` to keep results under the same id; omit it and one is
  generated and returned.

This call is **stateless**. Everything in the response is scoped to the case
named in it. There is no server-side case store — persistence is the
integrator's responsibility.

---

## 16. LLM summary (implementation detail)

Only `summary` and `summary_source` are affected.

- Exactly **one** application-level generation call per request. Never one
  per document.
- Runs **last**, after every decision is final. It is shown statuses and
  counts — never extracted values such as a PAN or a date of birth.
- Budget **1.5 s**, output capped at 64 tokens. No retries.
- If the provider is unreachable, slow, or its sentence fails validation, the
  deterministic summary is returned and `summary_source` is `deterministic`.
  The request still returns `200` and **no other field changes**.

### `summary_source` is observed, not guaranteed

Measured on the reference host — Intel Core 7 240H, 10 cores, **CPU-only, no
GPU**, `qwen2.5:3b`, 1.5 s budget, 64-token cap — by calling the running HTTP
API with real multipart uploads, 8 requests per row:

| Documents | `summary_source: "llm"` |
|---|---|
| 1 | **7 of 8** |
| 2 | **2 of 8** |
| 4 | **7 of 8** |

Before the tuning described in `docs/API_HANDOVER.md` §5, the same benchmark
returned `deterministic` on **every** request.

The two-document row is the weak one, and the reason is visible in the
internal timings: that bundle carries two KYC mismatch codes, so the sentence
is longer, and generation lands around 1.5 s — right on the boundary.

**Generation cost alone**, model resident, 12 runs per shape: median
**1076–1496 ms** depending on how much there is to say.

**Cold start is always a fallback.** With the model evicted, the first call
costs ~8 s (3.7 s to load the model, 2.9 s to read the prompt). This is why
the service warms the model at startup and asks Ollama to keep it resident —
see `LOS_LLM_KEEP_ALIVE`.

**This is not a fault.** The budget is a fast-fail bound on an optional
final-stage nicety. Overrunning it costs the sentence and nothing else: every
decision in the response was final before the model was consulted.

To see `llm` more often: give Ollama a GPU or a host with spare CPU, raise
`LOS_LLM_SUMMARY_TIMEOUT_SECONDS`, lower `LOS_LLM_SUMMARY_MAX_TOKENS`, or set
`LLM_SLOW_COOLDOWN_SECONDS=0`.

**Do not write client logic that depends on `summary_source` being `llm`.**
Read the field, show the sentence, and treat either value as normal.
`deterministic` is not an error.

If `summary_source` is `deterministic`, nothing is wrong with the
application — only the sentence was written without a model.

---

## 17. Deployment

### Run

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8010
```

Behind a reverse proxy terminating TLS. Allow multipart bodies of at least
**256 MB** (10 files × 25 MB) and a proxy read timeout of **≥120 s**.

Workers: the service uses internal thread pools for OCR and document work and
holds one OCR model **per OCR worker**. Start with **one uvicorn worker per
2–4 cores** and size by memory, not by core count.

### Endpoints for infrastructure

| Path | Auth | Purpose |
|---|---|---|
| `/health` | none | Liveness. |
| `/ready` | none | Readiness. |
| `/metrics` | none | Metrics. |

### Environment variables

**Authentication (required)**

| Variable | Notes |
|---|---|
| `JWT_JWKS_URL` | JWKS endpoint. Service refuses to start without it. |
| `JWT_ISSUER` | Expected `iss`. |
| `JWT_AUDIENCE` | Expected `aud`. |
| `JWT_ALGORITHM` | `RS256`. |
| `JWT_LEEWAY_SECONDS` | Default `30`. |
| `JWT_JWKS_CACHE_SECONDS` | Default `300`. |

**LLM summary (optional)**

| Variable | Notes |
|---|---|
| `OLLAMA_HOST` | Default `http://127.0.0.1:11434`. |
| `OLLAMA_MODEL` | `qwen2.5:3b`. Use a small **non-reasoning** model — a reasoning model thinks before answering and cannot meet the budget. |
| `LOS_LLM_SUMMARY_TIMEOUT_SECONDS` | Default `1.5`. |
| `LOS_LLM_SUMMARY_MAX_TOKENS` | Default `64`, chosen by measurement. |
| `LOS_LLM_KEEP_ALIVE` | Default `30m`. How long Ollama holds the model resident. Ollama's own default of 5 min makes an idle deployment pay a ~3.7 s reload, which always overruns the budget. |
| `LLM_CONNECT_TIMEOUT_SECONDS` | Default `0.5`. |
| `LLM_UNAVAILABLE_COOLDOWN_SECONDS` | Default `60`. Provider **not listening**. |
| `LLM_SLOW_COOLDOWN_SECONDS` | Default `2`. Provider **answered late**. Deliberately much shorter — see `docs/API_HANDOVER.md` §5. |

The LLM is **not** a hard dependency. With no provider at all the service
runs normally and every summary is deterministic.

**OCR and processing**

| Variable | Notes |
|---|---|
| `DOCUMENT_OCR_ENGINE` | `rapidocr`. |
| `DOCUMENT_OCR_WORKERS` | Default 2, capped at CPU count. **One model per worker** — raising it costs memory. Do **not** set it to 1: recognition then serialises and four documents took 29.3 s against 3.5 s with two workers. |
| `DOCUMENT_OCR_THREADS` | ONNX intra-op threads **per engine**. Default `(logical CPUs / 2) / workers` — 4 on a 16-CPU host. Previously unbounded, which asked for `workers × cores` threads and lost the difference to contention. `0` restores the old behaviour. |
| `DOCUMENT_OCR_WARMUP` | Default `true`. Loads every worker's model at startup; leave on or the first request pays it. |
| `AGENT_UPLOAD_ROOT` | Scratch directory for staged uploads. |
| `MAX_UPLOAD_BYTES` | Applies to `/api/v1/extract-document` **only**. Default 25 MB. |

**The limit on `POST /api/v1/los/process` is not configurable.** It is the
constant `MAX_UPLOAD_BYTES` in `app/agents/document_agent/workflow.py`, fixed
at **25 MB per file**, with at most **10 files** per application. Setting the
environment variable does not change it. Over the per-file limit returns `413`
`FILE_TOO_LARGE`; over ten files returns `413` `TOO_MANY_DOCUMENTS`.

**Feature flags** — `app/config/documents.yaml`, section `los:`, each
overridable as `LOS_<NAME>`:

`classification_enabled`, `verification_enabled`, `extraction_enabled`,
`financial_enabled`, `kyc_enabled`, `conflict_detection_enabled`,
`signature_enabled`, `business_evidence_enabled`, `sale_deed_enabled`,
`collateral_customer_enabled`, `collateral_property_enabled`,
`llm_summary_enabled`.

**Off means off** — the capability does not run and the response says so with
an explicit `SKIPPED` / disabled reason. It never silently behaves as enabled.

**KYC policy** — `app/config/kyc_policies.yaml`: thresholds and per-check
`blocking` flags. Changing whether a mismatch can reject an applicant is a
config change, reviewable in version control.

### Operational notes

- **Temporary files.** Uploads are staged under `AGENT_UPLOAD_ROOT` and
  unlinked after processing. Mount it writable; size it for peak concurrency
  × 250 MB.
- **Startup.** Risk policy is validated and OCR models are loaded before the
  service reports ready. Expect a few seconds; do not route traffic until
  `/ready` passes.
- **Logging.** Structured to stdout, level via `LOG_LEVEL`. Logs carry
  `request_id` — the same one in the response body.
- **Signed policy.** If the risk policy is not signed off, the service logs a
  warning at startup and thresholds marked `[PLACEHOLDER]` are not
  authoritative. Sign off before production use.

---

## 18. Stability

- Field names, enum values and reason codes are stable; treat them as the
  contract.
- Optional keys (`extraction`, `reason_codes`, `specialist`,
  `evidence_refs`, `expected_type`) are **absent** rather than `null` when
  they do not apply — except `extraction`, which is explicitly `null` when
  the gate withheld it. Handle both.
- Lists are always present and may be empty; they are never `null`.
- Monetary values are JSON **strings** to preserve precision. Parse as
  decimal, not float.
- Dates are ISO `YYYY-MM-DD`.
- `documents` is always in the order you uploaded — join on `source_id`.
