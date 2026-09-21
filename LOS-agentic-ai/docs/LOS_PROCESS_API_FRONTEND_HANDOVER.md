# LOS Process API — Frontend Integration Guide

One call takes an applicant's documents — and optionally a co-applicant's —
end to end, and returns one JSON result.

```
POST /api/v1/los/process
Content-Type: multipart/form-data
Authorization: Bearer <JWT>          scope: los.read
```

Swagger UI `…/docs` · OpenAPI `…/openapi.json`

> The OpenAPI document is generated at import time. After a deployment,
> **restart the server** or `/docs` will serve the previous schema.

---

## 1. Request

| field | type | required | meaning |
|---|---|:--:|---|
| `files` | file[] | **yes** | the **primary applicant's** documents (max 10, 25 MB each) |
| `expected_types` | string[] | no | one expected type **per file, in order** |
| `co_applicant_id` | string | no | required if you send co-applicant files |
| `co_applicant_files` | file[] | no | the **co-applicant's** documents |
| `co_applicant_expected_types` | string[] | no | one type per co-applicant file, in order |
| `applicant_id` | string | no | generated when omitted |
| `case_id` | string | no | generated when omitted; both parties share one |
| `operation` | enum | no | `PROCESS` (default), `EXTRACT`, `VERIFY` |

Optional declared profile, matched against that party's own documents only:
`applicant_name`, `applicant_dob`, `applicant_pan`, `applicant_father_name`,
`applicant_address` — and the `co_applicant_*` equivalents.

**`files` and `co_applicant_files` are different people.** A document sent
under `files` belongs to the primary applicant and can never satisfy the
co-applicant's checklist, or the reverse.

### Positional mapping

`files[0]` pairs with `expected_types[0]`; `co_applicant_files[0]` with
`co_applicant_expected_types[0]`. Send `AUTO` to let classification decide
for one file. Both type fields are repeatable string items, and a single
comma-separated string is also accepted.

Both parties routinely upload a file called `pan.jpg`. That is fine — they
are told apart by which field they arrived in, never by filename.

---

## 2. Verification and extraction

Verification is a **hard gate**. Extracted fields are released **only**
behind a `PASS`:

| verification | `extraction` |
|---|---|
| `PASS` | released |
| `REVIEW` / `FAIL` / `SKIPPED` | `null` |

`SKIPPED` is never treated as a pass.

A document that is not the type you declared fails with
`reason_codes: ["DOCUMENT_TYPE_MISMATCH"]`, releases nothing, and sets
`next_action: "REQUEST_CORRECT_DOCUMENT"` — the applicant sent the wrong
file and can send the right one. It is **not** a rejection.

`has_extracted_fields` tells you plainly whether fields came back, so
"extraction absent" and "extraction found nothing" stay distinguishable.

### Extraction values

JSON-native. Money is a **number**, never a stringified decimal. An
`address` is an **object** of confidently identified components, omitted
entirely when none could be identified:

```json
"address": { "house": "1-17", "city": "BENGALURU",
             "state": "MAHARASHTRA", "pincode": "560074" }
```

Possible keys: `house`, `street`, `locality`, `city`, `district`, `state`,
`pincode`. Cities and states come from controlled vocabularies and a
pincode is always six digits — a component that could not be identified is
left out rather than guessed.

---

## 3. KYC

KYC asks whether several documents describe **one person**. It is
**scoped to a single party**: the primary applicant's documents are never
compared with the co-applicant's. Two people on a joint application have
different names and dates of birth — that is what a joint application is,
not a mismatch.

Fields: `NAME`, `DATE_OF_BIRTH`, `PAN_NUMBER`, `FATHER_NAME`, `ADDRESS`,
`INCOME`. Statuses: `PASS`, `PARTIAL`, `FAIL`, `SKIPPED`.

Each field row:

| key | meaning |
|---|---|
| `field` | which identity field |
| `status` | `PASS` / `PARTIAL` / `FAIL` / `SKIPPED` |
| `match_score` | 0–100, how closely the values agree |
| `confidence` | 0–100, how far that answer can be relied on |
| `reason_code` | machine-readable cause, e.g. `NAME_MISMATCH` |
| `sources` | which documents contributed, and what they said |

**`match_score` and `confidence` are different questions.** A perfect
match read off a poor photograph is a high score at a lower confidence.
Never treat one as a rescale of the other.

**`SKIPPED` is not a failure.** It means nothing was comparable — the
field was missing, or only one document carried it
(`INSUFFICIENT_SOURCES`). `match_score: 0` on a `SKIPPED` row means
*nothing was compared*, not *nothing matched*.

`reason` (prose) is omitted whenever `reason_code` is present, and
`normalized_value` is omitted when it is identical to `value`.

---

## 4. Party sections

`primary_applicant` is always present. `co_applicant` appears **only**
when the request supplied a second party.

| key | meaning |
|---|---|
| `party_id` | `applicant_id` or `co_applicant_id` |
| `role` | `PRIMARY_APPLICANT` / `CO_APPLICANT` |
| `status` | that party's own documents + their own KYC |
| `document_ids` | this party's entries in the top-level `documents[]` |
| `verification_summary` | `total_documents`, `passed`, `review`, `failed`, `skipped` |
| `profile_match` | present when a profile was supplied |
| `kyc` | that party's own KYC (two-party cases) |

`document_ids` are **references, not copies** — the full objects live once
in the top-level `documents[]`, each carrying its own `party_id`. Join on
`source_id` within a party, or filter `documents[]` by `party_id`.

There is deliberately **no party-level `decision` or `next_action`**. A
party-level `CONTINUE` beside a case-level `MANUAL_REVIEW` would read as
permission to proceed, and there is one decision on a loan.

---

## 5. Decision and next action

Both are **case-level and authoritative**.

`decision`: `PASS` · `REVIEW` · `REJECT` — a restatement of the
deterministic roll-up. **Not a credit decision.**

`next_action`: `CONTINUE` · `MANUAL_REVIEW` · `REQUEST_VALID_DOCUMENT`
(the right document could not be read) · `REQUEST_CORRECT_DOCUMENT` (the
wrong file was sent). Those two mean different things to the applicant.

`cross_document` reports agreement **between documents at case level**. On
a two-party case it is `{"status": "SKIPPED", "checks": []}` — nothing is
compared across parties, and `SKIPPED` says that honestly where `PASS`
would claim agreement was established.

---

## 6. Summary

`summary` is one sentence; `summary_source` says who wrote it
(`deterministic` or `llm`).

The deterministic sentence is always computed and is party-aware:

```
5 document(s) processed (5 success). Primary applicant KYC requires review:
DOB, father name and name mismatch. Co-applicant KYC passed. Overall PARTIAL.
```

A generated sentence replaces it only if it is at least as informative —
on a two-party case it must name both parties and state an outcome for
each. **The request never depends on model availability**: a timeout, an
outage or a vague answer falls back to the deterministic sentence and
nothing else in the response changes.

---

## 7. Errors

| status | when |
|---|---|
| `200` | processed — read `status`, `decision`, `errors[]` |
| `400` | ownership is ambiguous, e.g. `CO_APPLICANT_ID_REQUIRED` |
| `401` / `403` | missing or insufficient JWT scope |
| `422` | malformed request — no files, unknown `operation` |

A per-document problem does not fail the call: it appears in `errors[]`
with its `source_id`, and the rest of the application still processes.
Stack traces, filesystem paths and internal state never appear.

---

## 8. Sample — primary only

```json
{
  "request_id": "los_8604ee9d1f2b4c0a9e7d3f1a2b3c4d5e",
  "applicant_id": "APP-001",
  "case_id": "CASE-001",
  "status": "PARTIAL",
  "documents": [
    {
      "source_id": "pan.jpg",
      "type": "PAN",
      "status": "SUCCESS",
      "verification": "PASS",
      "party_id": "APP-001",
      "party_role": "PRIMARY_APPLICANT",
      "extraction": {
        "pan_number": "NUHPS4875K",
        "name": "RISHABH AJIT SINGH",
        "father_name": "AJIT SINGH",
        "date_of_birth": "2002-06-12"
      },
      "verification_score": 100,
      "verification_confidence": 56,
      "has_extracted_fields": true,
      "authenticity": "NOT_ESTABLISHED",
      "expected_type": "PAN"
    }
  ],
  "kyc": {
    "status": "REVIEW",
    "reason_codes": ["INSUFFICIENT_SOURCES"],
    "overall_score": 0,
    "overall_confidence": 0,
    "fields": [
      { "field": "NAME", "status": "SKIPPED", "match_score": 0,
        "confidence": 0, "reason_code": "INSUFFICIENT_SOURCES",
        "sources": [{ "source_id": "pan.jpg", "document_type": "PAN",
                      "value": "RISHABH AJIT SINGH" }] }
    ]
  },
  "cross_document": { "status": "SKIPPED", "checks": [] },
  "decision": "REVIEW",
  "next_action": "MANUAL_REVIEW",
  "summary": "1 document(s) processed (1 success). KYC REVIEW: insufficient sources. Overall PARTIAL.",
  "summary_source": "deterministic",
  "processing_ms": 2543.0,
  "errors": [],
  "primary_applicant": {
    "party_id": "APP-001",
    "role": "PRIMARY_APPLICANT",
    "status": "PARTIAL",
    "document_ids": ["pan.jpg"],
    "verification_summary": { "total_documents": 1, "passed": 1,
                              "review": 0, "failed": 0, "skipped": 0 }
  }
}
```

`co_applicant` and `co_applicant_id` are **absent**, not null.

---

## 9. Sample — primary + co-applicant

Both parties uploaded a file named `pan.jpg`. Trimmed for length.

```json
{
  "request_id": "los_1f2b4c0a9e7d3f1a2b3c4d5e8604ee9d",
  "applicant_id": "APP-001",
  "co_applicant_id": "COAPP-001",
  "case_id": "CASE-002",
  "status": "PARTIAL",
  "documents": [
    { "source_id": "pan.jpg", "type": "PAN", "verification": "PASS",
      "party_id": "APP-001", "party_role": "PRIMARY_APPLICANT",
      "extraction": { "pan_number": "NUHPS4875K",
                      "name": "RISHABH AJIT SINGH" } },
    { "source_id": "pan.jpg", "type": "PAN", "verification": "PASS",
      "party_id": "COAPP-001", "party_role": "CO_APPLICANT",
      "extraction": { "pan_number": "EVPPG6189E",
                      "name": "LAXMI SANTOSH GUPTA" } }
  ],
  "kyc": {
    "status": "REVIEW",
    "reason_codes": ["NAME_MISMATCH", "DOB_MISMATCH"],
    "overall_score": 16,
    "overall_confidence": 86
  },
  "cross_document": { "status": "SKIPPED", "checks": [] },
  "decision": "REVIEW",
  "next_action": "MANUAL_REVIEW",
  "summary": "4 document(s) processed (4 success). Primary applicant KYC requires review: DOB and name mismatch. Co-applicant KYC passed. Overall PARTIAL.",
  "summary_source": "deterministic",
  "processing_ms": 8918.85,
  "errors": [],
  "primary_applicant": {
    "party_id": "APP-001",
    "role": "PRIMARY_APPLICANT",
    "status": "PARTIAL",
    "document_ids": ["pan.jpg", "dl.jpg"],
    "verification_summary": { "total_documents": 2, "passed": 2,
                              "review": 0, "failed": 0, "skipped": 0 },
    "kyc": {
      "status": "REVIEW",
      "reason_codes": ["NAME_MISMATCH", "DOB_MISMATCH"],
      "overall_score": 16,
      "overall_confidence": 86,
      "fields": [
        { "field": "NAME", "status": "FAIL", "match_score": 30,
          "confidence": 82, "reason_code": "NAME_MISMATCH",
          "sources": [
            { "source_id": "pan.jpg", "document_type": "PAN",
              "value": "RISHABH AJIT SINGH" },
            { "source_id": "dl.jpg", "document_type": "DRIVING_LICENCE",
              "value": "NARAYANAPPA" }
          ] }
      ]
    }
  },
  "co_applicant": {
    "party_id": "COAPP-001",
    "role": "CO_APPLICANT",
    "status": "SUCCESS",
    "document_ids": ["pan.jpg", "dl.jpg"],
    "verification_summary": { "total_documents": 2, "passed": 2,
                              "review": 0, "failed": 0, "skipped": 0 },
    "kyc": { "status": "PASS", "reason_codes": [],
             "overall_score": 100, "overall_confidence": 88, "fields": [] }
  }
}
```

**Note the case-level `kyc` has no `fields` on a two-party case** — every
row is published under the party it belongs to, where it can say whose it
is. On a single-applicant case the top-level object keeps its rows.

---

## 10. Integration notes

- **Group documents by `party_id`**, never by filename or upload order.
- **Read `match_score` beside `fields_compared`** — a gap in the evidence
  is not a disagreement with it.
- **Never show `verification_score` as authenticity.** `authenticity` is
  always `NOT_ESTABLISHED`: there is no issuer lookup behind this service,
  so a `PASS` means structurally valid and internally consistent, never
  government-verified.
- **Treat `decision` as the only verdict.** Party `status` describes that
  party's documents, not their eligibility.
- Latency is dominated by OCR and scales with document count — budget a
  few seconds per image. A 5-document two-party case measured ~8.9 s.

### Environment

| variable | default | notes |
|---|---|---|
| `OLLAMA_MODEL` | `qwen2.5:3b` | summary only; must be a pulled model |
| `LOS_LLM_SUMMARY_ENABLED` | `true` | `false` skips the model entirely |
| `LOS_LLM_SUMMARY_TIMEOUT_SECONDS` | `1.5` | falls back on expiry |
| `DOCUMENT_OCR_WORKERS` | `3` | host-dependent; see `ocr.py` |
| JWT | — | `JWT_ISSUER`, `JWT_AUDIENCE`, `JWT_JWKS_URL` |

Limits: **10 documents** per request, **25 MB** per file.
