# API contract

Base: `http://127.0.0.1:8041/api/v1/bank-verification`. Interactive docs: `/docs`.

Every call carries the **simulated** role (demo mode only): `X-Demo-Role: CPA | AGENCY | VIEWER` and
`X-Demo-User`.

| Role | May |
|---|---|
| CPA | view, process, assign agency |
| AGENCY | view, finalize |
| VIEWER | view |

Every error has one shape:

```json
{"error": {"code": "REPORT_REQUIRED", "message": "Finalization needs: The agency's verification report.",
           "details": {"missing": ["The agency's verification report"]}}}
```

## API 1 — `POST /process`

Reads the statement, runs the eight rules, records the run.

* **JSON** body when no file is sent.
* **multipart/form-data** with a `request` field (the same JSON) and a `statement` file (PDF or CSV).
* **Optional header** `Idempotency-Key`:
  * the same key with the same request returns the stored run (`"replayed": true`);
  * the same key with a different request returns `409 IDEMPOTENCY_KEY_REUSED`.
* The same inputs again without a key also replay the last run, so a double click never makes a second run.

Request:

```json
{
  "case_id": "BV-DEMO-002",
  "mode": "AA_FAILURE",
  "statement_source": "UPLOAD",
  "entered": {
    "bank_name": "Harbourline Bank",
    "account_number": "99902114455661",
    "account_type": "Savings",
    "transactions": [
      {"date": "2026-09-21", "amount": "1240.50", "direction": "DEBIT"},
      {"date": "2026-09-21", "amount": "5000.00", "direction": "DEBIT"},
      {"date": "2026-09-05", "amount": "12500.00", "direction": "DEBIT"}
    ],
    "provenance": {"bank_name": "MANUAL", "account_number": "MANUAL"}
  },
  "agency_id": "AGY-NORTHSTAR"
}
```

`statement_source` takes one of:

* `UPLOAD`: a file is sent in this request;
* `ON_FILE` (the default): the case's latest statement;
* `SEEDED`: the case's seeded synthetic statement;
* `NONE`: no statement.

`entered` is optional. Without it, the form is prefilled from AA (AA_SUCCESS) or from the statement (AA_FAILURE). A
masked account number equal to the prefill's mask means "keep the one on file".

Response (200), shortened:

```json
{
  "case_id": "BV-DEMO-002", "run_id": "RUN-…", "mode": "AA_FAILURE", "source": "MANUAL",
  "processing_status": "COMPLETED", "final_status": null, "overall_status": "REVIEW_REQUIRED",
  "rules": [
    {"rule_id": "R1", "rule_name": "Bank Details Matching", "status": "PASS",
     "reason": "Bank name, account number and account type match the statement.",
     "comparisons": [{"field": "account_number", "left_label": "Account number", "left": "XXXXXXXXXX5661",
                      "right_label": "Statement", "right": "XXXXXXXXXX5661", "match": true, "note": null}],
     "evidence": ["Leviosa form", "Bank statement header"], "missing_evidence": [], "risk_flags": [],
     "recommended_action": null, "details": {}}
  ],
  "risk_flags": [], "missing_evidence": ["Branch confirmation of account ownership"], "mismatches": [],
  "checks": {"checks_applicable": 7, "checks_passed": 6, "checks_failed": 0, "checks_review": 1,
             "not_applicable": ["R6"], "score_note": "checks_passed / checks_applicable counts rules; it is not a probability of fraud or ownership."},
  "recommended_action": "AWAIT_OWNERSHIP_CONFIRMATION",
  "explanation": "1 check(s) need a person: R4 … 6 of 7 applicable checks passed. Next: …",
  "form": {"…": "the form as used, account number masked"},
  "sources": {"aa": null, "statement": {"…": "what was read"}, "statement_file": {"extraction_status": "COMPLETE"}},
  "replayed": false
}
```

Errors:

| Code | When |
|---|---|
| 404 `CASE_NOT_FOUND` | the case does not exist |
| 409 `CASE_FINALIZED` | the case already has a final decision |
| 409 `NO_AA_DATA` | AA_SUCCESS was chosen but there is no AA data |
| 409 `NO_STATEMENT_ON_FILE` | ON_FILE with no statement on the case |
| 409 `IDEMPOTENCY_KEY_REUSED` | the key was used with a different request |
| 413 `FILE_TOO_LARGE` | over the configured size (10 MB) |
| 415 `UNSUPPORTED_FILE_TYPE` / `FILE_TYPE_MISMATCH` | not a PDF or CSV by content |
| 422 `EMPTY_FILE`, `STATEMENT_FILE_REQUIRED`, `UNEXPECTED_FILE`, `VALIDATION_ERROR` | invalid request |
| 403 `FORBIDDEN_FOR_ROLE` | not a CPA |

## API 2 — `POST /{case_id}/finalize`

The agency's decision. Send JSON, or multipart with a `request` field and a `report` file (PDF / PNG / JPG). AGENCY
role only.

```json
{
  "agency_id": "AGY-NORTHSTAR",
  "checklist": {"bank_name": "VERIFIED", "account_number": "VERIFIED", "account_type": "VERIFIED",
                "account_holder": "VERIFIED", "latest_transactions": "VERIFIED", "statement_authentic": "VERIFIED"},
  "branch_visit": "COMPLETED",
  "ownership": "CONFIRMED",
  "requested_decision": "POSITIVE",
  "rationale": "Branch visit done; bank records, holder and the latest transactions were confirmed.",
  "remarks": null,
  "report_document_id": null
}
```

Response (200):

```json
{"case_id": "BV-DEMO-002", "final_status": "POSITIVE", "requested_decision": "POSITIVE", "is_final": true,
 "blocked_reasons": [], "negative_findings": [], "run_id": "RUN-…", "rules": ["… re-run with the ownership outcome"],
 "overall_status": "PASS", "report_document_id": "DOC-…", "agency_id": "AGY-NORTHSTAR", "decision_id": "DEC-…",
 "replayed": false}
```

A requested POSITIVE that the evidence does not support comes back as `final_status: "REVIEW_REQUIRED"`, with each
reason in `blocked_reasons`. The case stays open.

Errors:

| Code | When |
|---|---|
| 422 `REPORT_REQUIRED` / `MANDATORY_EVIDENCE_MISSING` | evidence missing; `details.missing` lists it |
| 422 `UNKNOWN_CHECKLIST_ITEM`, `UNKNOWN_AGENCY`, `UNKNOWN_REPORT` | an unknown item, agency or report id |
| 403 `AGENCY_NOT_ASSIGNED` | another agency holds the case |
| 403 `FORBIDDEN_FOR_ROLE` | not the agency |
| 409 `NOT_PROCESSED` | no verification run yet |
| 409 `CASE_FINALIZED` | already final; the identical request replays instead |

## Read-only

| Endpoint | Returns |
|---|---|
| `GET /config` | demo flag, agencies, checklist, actions, the caller's role and permissions |
| `GET /summary` | dashboard counts (cards configured in `summary_cards`) |
| `GET /cases?q=&source=&status=&risk=` | filtered case list |
| `GET /cases/{id}` | header, latest run, AA response (masked), documents, decisions, runs, demo hints |
| `GET /cases/{id}/summary` | `{text, source: ai or rules, pending, note, label, checklist[8]}` |
| `GET /cases/{id}/history` | the audit events |
| `GET /cases/{id}/report?format=pdf\|html` | the verification report |
| `GET /cases/{id}/documents/{doc}` | a stored statement / report file |

Demo only: `POST /demo/reset` wipes and re-seeds the scenarios.
