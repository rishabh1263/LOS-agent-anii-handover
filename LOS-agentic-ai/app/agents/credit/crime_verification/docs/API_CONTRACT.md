# API contract: `/api/v1/credit/crime-verification`

**Authentication:** the LOS bearer token (`Authorization: Bearer <JWT>`), enforced by `require_jwt` on the mount.
With LOS auth off (local dev), the LOS local identity is used.

**Permissions** come from the token's scopes (config `access.scopes`); branches come from config `access.branches`.

**Errors**, in the LOS shape:

```json
{"error": {"code": "VALIDATION_ERROR", "message": "Some details are not valid.", "retryable": false, "request_id": "err_…"},
 "detail": {"code": "VALIDATION_ERROR", "message": "…", "details": [{"field": "subjects[1].pan", "message": "…"}]}}
```

| Code | HTTP | When |
|---|---|---|
| CONFIRMATION_REQUIRED | 422 | `confirmation_accepted` is not true |
| VALIDATION_ERROR | 422 | field errors in `detail.details` (name, DOB, address, PAN format, PAN not the application's, same PAN twice) |
| NO_SUBJECTS / DUPLICATE_SUBJECT / UNKNOWN_VENDOR / UNKNOWN_SCENARIO / SUBJECT_NOT_ON_APPLICATION | 422 | |
| NOT_PERMITTED | 403 | the caller's scopes do not give the action |
| APPLICATION_NOT_FOUND / CASE_NOT_FOUND / SUBJECT_NOT_FOUND | 404 | missing **or** not in the caller's branches |
| ALREADY_IN_PROGRESS | 409 | the person already has a verification in flight |
| RETRIGGER_REQUIRED | 409 | the person's latest is Completed / Review required / Waived and `retrigger` is not true |
| IDEMPOTENCY_CONFLICT | 409 | the same key was sent with different details |
| NOTHING_TO_FINALIZE / ALREADY_FINALIZED | 409 | |
| REPORT_NOT_AVAILABLE | 409 | no provider result yet |
| UNKNOWN_DECISION / REMARKS_REQUIRED | 422 | |

## API 1: `POST /initiate`

Header `Idempotency-Key: <uuid>` (recommended; the screen sends one per confirmation).

```json
{
  "application_id": "APP-2026-008",
  "vendor": "CRIME_CHECK",
  "subjects": [
    {"role": "PRIMARY", "full_name": "Arjun Nair", "father_name": "Krishnan Nair", "date_of_birth": "1989-09-15",
     "permanent_address": "C-21, Neelkanth Apartments, Chembur, Mumbai, Maharashtra 400071"},
    {"role": "CO_APPLICANT", "full_name": "Varun Nair", "father_name": "Krishnan Nair", "date_of_birth": "1991-12-01",
     "permanent_address": "C-21, Neelkanth Apartments, Chembur, Mumbai, Maharashtra 400071"}
  ],
  "confirmation_accepted": true,
  "demo_scenario": null,
  "retrigger": false
}
```

- `pan` is optional per subject. When omitted, the PAN on the application is used. When given, it must equal it.
- `demo_scenario` works in demo mode only: it overrides each subject's seeded scenario.

**Response: 201** (or **200** with `"replayed": true` for the same key and body). The case:

```json
{"case_id": "CRV-CD5B92BDC4", "application_id": "APP-2026-008", "vendor": "CRIME_CHECK", "vendor_name": "Crime Check",
 "status": "REVIEW_REQUIRED", "status_label": "Review required", "kind": "INITIAL", "initiated_by": "local-developer",
 "subjects": [
   {"subject_id": "SUB-…", "role": "PRIMARY", "pan_masked": "XXXXXX001Q", "status": "COMPLETED", "outcome": "NO_MATCH",
    "provider_reference": "CC-…", "error": null,
    "result": {"risk_level": "NO_RISK", "risk_label": "No Risk", "total_cases": 0, "summary": "…",
               "groups": [{"group": "GENERAL_COURTS", "label": "General Courts",
                           "items": [{"code": "SUPREME_COURT", "name": "Supreme Court", "coverage_since": 1950, "cases_found": 0}]}],
               "clusters": [], "synthetic": true}},
   {"role": "CO_APPLICANT", "status": "REVIEW_REQUIRED", "result": {"risk_level": "AVERAGE_RISK", "total_cases": 3}}],
 "can": {"refresh": false, "finalize": true}, "synthetic": true, "replayed": false}
```

Per subject, `outcome` is one of:

- `NO_MATCH`: no synthetic matches;
- `FINDINGS`;
- `INCONCLUSIVE`: pending;
- `PROVIDER_FAILURE`: with `error.code` / `error.message`.

## API 2: `POST /{case_id}/finalize`

```json
{"decision": "REFER_TO_CREDIT", "remarks": "Co-applicant findings."}
```

- Decisions come from config `finalize.decisions` (ACCEPTED, REFER_TO_CREDIT, DECLINE_RECOMMENDED).
- Remarks are required for those listed in `remarks_required_for`.
- It is allowed only when no subject is still processing and at least one result exists.
- It returns the case with `final_decision`, `finalized_by` and `finalized_at`.
- A provider result or a risk can never be sent or changed: unknown fields are refused (422).

## Intake: `PUT /applications/{application_id}` (the LOS pushes an application)

In production this is the only way an application and its people enter (the demo seed runs in demo mode only).
Permission `manage_applications` (config `access.scopes`: `los.write`, the LOS service account; it sees every branch
via `access.all_branches_scopes`). The body carries ALL the people; the list replaces the one held, in one transaction.

```json
{"title": "Home loan", "branch": "MUM", "product": "Home Loan", "loan_amount": 4500000,
 "stage": "Credit Underwriting", "status": "In progress",
 "people": [{"role": "PRIMARY", "full_name": "…", "father_name": "…", "date_of_birth": "1988-04-12",
             "permanent_address": "…", "pan": "…"},
            {"role": "CO_APPLICANT", "seq": 1, "pan": "…"}]}
```

| Check | Answer |
|---|---|
| no / two applicants, PAN format, same PAN twice, one co-applicant number twice, more than `subjects.max_co_applicants`, a given detail in a wrong format | 422 `VALIDATION_ERROR`, every problem in `details` (`people[i].field`) |
| unknown field, missing PAN / branch, negative amount, unknown role | 422 |
| caller without `manage_applications` | 403 `NOT_PERMITTED` |
| branch (new or current) outside the caller's branches | 403 `BRANCH_NOT_PERMITTED` |
| removing a person with verification history, or changing their PAN | 409 `PARTY_HAS_HISTORY`, nothing changes |

Missing details are accepted (the desk shows them and blocks initiation until they are complete). People without
history may be removed, renumbered or have their PANs corrected (also swapped between two people). Returns the
application view. Audit `APPLICATION_SYNCED` (counts only).

## Production switches

| Switch | Where | Effect |
|---|---|---|
| `demo_mode: false` | crime_verification.yaml | no seed, no `/demo/reset` (403 `DEMO_ONLY`), no demo scenario (422), no samples / banner / scenario picker, demo grants ignored |
| `CRIME_VERIFICATION_DSN` | environment | the PostgreSQL database; required outside demo mode (no embedded fallback) |
| `vendors.<id>.provider` | crime_verification.yaml | the vendor adapter, registered in `providers/__init__.py`. The mock is demo-only: in production it is refused with 503 `PROVIDER_NOT_CONFIGURED` and no case is written |

A vendor adapter that throws gives a FAILED person (`PROVIDER_UNAVAILABLE`), an answer with an unsupported risk level
gives `PROVIDER_REJECTED`; neither is a 500 and nothing technical reaches the screen.

## Follow-up and read-only

| Method / path | Returns |
|---|---|
| `POST /{case_id}/refresh` | asks the provider again for subjects still PROCESSING; returns the case |
| `GET /config` | field rules, messages, labels, vendors, decisions, demo scenarios, screen settings, caller's permissions |
| `GET /dashboard` | counts from the database: applications, total, in_progress, completed, review_required, failed, waived |
| `GET /applications?q=` | applications in the caller's branches, with the latest case status |
| `GET /applications/{id}` | header, both subjects (masked PAN, source, missing fields, latest result), latest case, history, `can` |
| `GET /applications/{id}/audit` | the application's audit trail |
| `GET /cases?q=&status=` | case table: application, applicant, subject count, vendor, status, each person's result |
| `GET /cases/{id}` / `/status` / `/audit` | the case / its statuses / its audit trail |
| `GET /cases/{id}/summary` | `{available, text, source: ai or rules, pending, label}`: plain-words summary once results are in |
| `GET /subjects/{id}/result` | one person's result |
| `GET /subjects/{id}/report` | `application/pdf`: generated on first download, then the stored file |
| `POST /demo/reset` | demo mode + `reset_demo`: reloads the synthetic applications |

Example:

```
curl -X POST http://127.0.0.1:8010/api/v1/credit/crime-verification/initiate \
  -H "Content-Type: application/json" -H "Idempotency-Key: 7c1e…" \
  -d '{"application_id":"APP-2026-001","confirmation_accepted":true,"subjects":[{"role":"PRIMARY","full_name":"Aarav Mehrotra","father_name":"Suresh Mehrotra","date_of_birth":"1991-03-14","permanent_address":"Flat 12, Sunrise Residency, Andheri East, Mumbai, Maharashtra 400069"}]}'
```
