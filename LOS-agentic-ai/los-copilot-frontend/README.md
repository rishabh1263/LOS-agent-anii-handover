# Dedupe Agent — frontend

A small, zero-dependency web page for three jobs:

1. **New application**: fill in the applicant and loan form (fields come from `GET /api/v1/fos/form-schema`), confirm, create the case, then run dedupe and the loan history check on it.
2. **Check an existing case**: enter a case ID plus one dedupe parameter and run the check.
3. **Dedupe & loan history result**: shows the outcome, previous loans (sortable), policy rules, a Copilot summary, and the customer, LOS and source panels.

It needs no npm install. Node 18 or later is enough (tested on v24).

## Run

```bash
cd los-copilot-frontend
node server.js                                    # http://127.0.0.1:5183 → backend http://127.0.0.1:8010
BACKEND_URL=http://127.0.0.1:8030 PORT=5190 node server.js
```

| Env | Default | Meaning |
|---|---|---|
| `PORT` / `HOST` | `5183` / `127.0.0.1` | where the page is served |
| `BACKEND_URL` | `http://127.0.0.1:8010` | the LOS API |
| `UPSTREAM_TIMEOUT_MS` | `45000` | proxy gives 504 `UPSTREAM_TIMEOUT` after this |
| `MAX_BODY_BYTES` | `65536` | larger request bodies get 413 |
| `QUIET` | unset | `1` turns off the access log |

## What the proxy forwards (and nothing else)

| Method | Path |
|---|---|
| GET | `/api/v1/fos/form-schema` |
| GET | `/api/v1/fos/config` |
| POST | `/api/v1/fos/applicants` (sends `Idempotency-Key`) |
| POST | `/api/v1/credit/dedupe` (the one dedupe API) |
| POST | `/api/v1/auth/login`, `/api/v1/auth/refresh` |

Any other `/api/...` call gets 404 `ROUTE_NOT_PROXIED` and never reaches the backend. Cookies are not forwarded; the bearer token is.
If the backend is down the proxy answers 502 `BACKEND_UNREACHABLE`.
Every response carries a strict CSP (`default-src 'self'`), `X-Frame-Options: DENY`, `nosniff` and `no-referrer`.
The access log records method, path (without the query string), status and time only. It never logs bodies.

## Behaviour notes

- **Customer type**: for a *New* customer the dedupe parameter fields are hidden, and the check uses the applicant's mobile. For *Existing*, pick PAN or Mobile No (searches LOS records and MIFIN) or AP Code or PR Code (MIFIN only), then enter the value.
- **Validation** comes from the schema rules. The same rules run again on the server.
- **Create** is idempotent: a single key per submission, safe retries (2), and a replayed answer is shown as such.
- **Sign-in**: a 401 opens the sign-in dialog. The token is kept in `sessionStorage` (`los-copilot-token`) and is gone when the tab closes.
- **Masking**: identifiers are shown exactly as the API masked them, and `safeText` refuses to render anything that looks like a full PAN or mobile number.
- **Property type** comes from the form schema per product: required for LAP, optional with a default for HL, hidden for PL.
- **Fill sample**: one small button on the New application page. Every click fills the whole form with a new random, realistic, synthetic applicant and loan: name, mobile, age, city and pincode, product-wise amount, tenure and rate, income and obligations within the configured FOIR, and property value by LTV. Existing samples also get a dedupe parameter, usually one of the demo MIFIN customers. All pools and ranges are in `public/sample_config.json`, and the backend schema's own rules always win.
- **Theme**: light or dark, remembered in `localStorage` (`los-copilot-theme`).

## Result page (2026-10-22)

- **What to do next**: one plain message picked from the answer, e.g. "Existing customer – history is clean", "Confirm the customer first" or "New customer – continue onboarding". Officer actions are listed below it, and limits the business hasn't set are mentioned once.
- **Policy checks**: traffic lights (Passed green, Failed red, Not checked yellow), each with a short title, what it checks and a plain reason. Rule ids are never shown.
- **Past loans with SBFC**: a customer strip (Customer ID, role in MIFIN, who was checked, match), then one card per loan with loan account no., product, status, sanctioned, outstanding, EMI, overdue, worst DPD, tenure and dates. Anything the source doesn't give says "Not available".
- **What matched**: plain sentences, plus every other LOS case holding the identifier, with the person's role on that case (applicant / co-applicant).
- **Co-applicant**: "Check an existing case" has *Check for: Applicant / Co-applicant* (API `party_role`). The customer type applies to the applicant only.
- **Possible match**: the officer picks *Yes, same customer* or *No, different person*, then *Record decision*. The API (`confirm_match` + `candidate`) stores it on the case timeline with the officer's name. It needs a scope from `dedupe_policy.yaml officer_confirmation`, and later checks on that case reuse the decision. Checks that wait for the decision, or for business limits, are grouped into one row each.
- **Fill sample on Check an existing case** uses the last case opened in this browser plus a random party and identifier from `sample_config.json`.
- Words are in `public/ui_text.json` (next-step messages, rule titles, reasons, role names); nothing is hardcoded in the page.

## Not in the form, on purpose

The create API has no loan purpose, applicant type or co-applicant fields, so the page does not show them. They are not faked.

## Tests

```bash
node --test test/*.test.js   # core.test.js (state, validation, request, result view) + server.test.js (proxy vs a synthetic backend)
```

The fixture `test/fixtures/dedupe_existing_complete.json` is an already-masked answer generated from the **synthetic** demo sources. No real customer data is involved.

## Known limits / blockers (backend side)

- MIFIN and LMS run on the demo providers (`MIFIN_PROVIDER=demo`, `LMS_PROVIDER=demo`), which production refuses. The real endpoints, request format and credentials are still pending.
- The MIFIN match semantics are UNVERIFIED. The score threshold, duplicate window and history limits are unset, and the policy is unsigned (`0.1.0-UNSIGNED`).
- The backend's mobile `strip` rule leaves `+91 98765 43210` as `919876543210`. The page strips until the value stops changing, but the server-side rule still needs a fix.
- Migration 0010 is not applied. The HMAC key is not in a vault yet.

**Production: NO-GO** until the items above are closed.

## The create form (LOS Sourcing screen, 2026-10-23)

Order and labels follow the LOS Sourcing screen (`public/ui_text.json` `form_layout`, `form_labels`,
`form_hidden_fields`):

1. **Application Details**: Loan Amount Requested (₹), Customer Type, Dedupe Parameter, Dedupe Parameter Value,
   Loan Tenure (Years) (sent as `tenure_months`), Loan Purpose, Applicant Type (both free text until the business
   lists arrive), Applicant Category, Applicant's Mobile No., and Is it Aadhaar Linked Mobile No (a yes/no about the
   mobile; no Aadhaar number is ever taken).
2. **Primary Applicant KYC Details**: name, date of birth, address (required by the create API).
3. **Co-applicants**: up to 3 (schema `co_applicants`).
4. **Loan Details**: product, plus property type only where the product needs it.

Optional fields not on the screen are not shown and never sent. A required field cannot be hidden. A new backend
field that the layout doesn't name still appears under Loan Details.
