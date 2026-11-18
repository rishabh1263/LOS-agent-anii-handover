# Dedupe: what the core / LMS team must provide for AP Code and PR Code

Status: **REQUEST, no integration exists** (2026-10-22). Nothing in this repository calls a core banking, customer
master or loan management system.

**What was searched:**
- `app/`, `scripts/`, `docs/`, `main.py`, plus every yaml/json/env;
- search terms: `ap_code`, `pr_code`, `prospect`, `customer_master`, `core_banking`, `lms`, `loan_management`,
  `finnone`, `loan_account_no`, `lan_number`, `LMS_`, `CORE_`, `CBS_`.

**The only hits:**
- `lan_number`, a value the officer TYPES into the internal top-up eligibility form (`app/config/eligibility_config.yaml:340`,
  `app/agents/eligibility/domain/requests.py:167`); it is not an authoritative source;
- `loan_account_no` / `customer_id`, parameter NAMES in the fraud agent's dedupe contract
  (`app/config/risk_policy.yaml:99-110`).

Until a real source exists, the dedupe agent answers `INSUFFICIENT_DATA` with `needs_source` for these two parameters
(`app/config/dedupe_policy.yaml` `parameters.AP_CODE` / `parameters.PR_CODE`, `field: null`).

## The two parameters (owner definitions)

| Form label | Meaning | Lifecycle |
|---|---|---|
| AP Code | Customer ID | Permanent; never changes for a customer |
| PR Code | Loan account number | Created at disbursement; the officer enters a previous loan's PR code for an EXISTING customer |

## Interface needed (any transport: REST, a read replica, or a nightly extract; the team decides)

Every call is read-only, authenticated service-to-service, and returns identifiers the LOS may store.

1. **Resolve a PR code:** `PR code → { ap_code, account_status, product, disbursed_on, closed_on }`
   - `account_status` comes from a closed list owned by the LMS (for example ACTIVE / CLOSED / SETTLED / WRITTEN_OFF).
     The LOS will not invent its own list.
2. **Resolve an AP code:** `AP code → { exists, pan_on_record (or a keyed hash of it), mobile_on_record (or hash),
   pr_codes[] }`
   - The PAN / mobile are needed only to confirm that the applicant in front of the officer IS this customer: a PAN
     match = same customer, a mismatch = identity conflict.
   - A keyed-HMAC form, computed with a key both systems hold, avoids moving plain PAN between systems. To be agreed.
3. **Find by PAN (optional, preferred):** `PAN (or its keyed hash) → { ap_code | none }`
   - This tells a "New" customer who is actually existing.

## Non-functional information needed

- **Auth:** mechanism and scopes (mTLS, OAuth client credentials …), and who issues the credentials.
- **Performance:** latency target and rate limits. The dedupe runs inside case processing; today it takes about 1 s
  and has no external call.
- **Freshness:** for a replica or extract, how stale it may be (a loan disbursed today but missing from the extract
  must not read as "no loan").
- **Errors:** "not found" vs "system unavailable" must be distinguishable. The LOS reports the second as
  `DATA_UNAVAILABLE`, never as "no match".
- **Data rules:** retention, and which fields the LOS may persist vs only display.
- **Test access:** a sandbox with synthetic customers, needed for the integration tests.

## What the LOS will do once it exists

- Set `parameters.AP_CODE.field` / `parameters.PR_CODE.field` to the new source (validated in
  `app/agents/dedupe/config.py`).
- Add a source adapter beside `app/agents/dedupe/profiles.py`.
- Propose a separate schema change, if any is needed (SQL shown first; nothing applied without approval).
- Phase C scenarios (repeat loan, top-up, closure reconciliation) additionally need repayment / DPD fields from the
  LMS. Those will be specified separately when Phase C starts.

## Leviosa as the LMS (2026-10-23, owner)

For dedupe, Leviosa gives the **loan account data** (the LMS slot). Adapter: `app/agents/dedupe/leviosa.py`; mapping:
`app/config/leviosa_lms.yaml`. **No Leviosa sample or API document exists yet -- every field name, status word and the
record path are UNVERIFIED placeholders, changed in the YAML (not code) when a sample arrives.**

| Setting | Meaning |
|---|---|
| `LMS_PROVIDER=leviosa` | use Leviosa (`demo` = synthetic data, dev only) |
| `LEVIOSA_LMS_URL` | GET URL with `{prospect_code}` (URL-encoded); unset or no placeholder -> not configured |
| `LEVIOSA_LMS_TOKEN` | bearer token (optional); never logged |

Behaviour: timeout / unreachable / 5xx retried (`client.retries`), then UNAVAILABLE and no further lookups (MIFIN-only
history); a 4xx is an outage, never retried; 404 = the loan is NOT_FOUND in Leviosa; a non-JSON reply or a reply for
another PR code = UNREADABLE (confirms nothing); unknown status words -> UNKNOWN + `STATUS_UNRECOGNISED`; bad numbers /
dates / yes-no -> left out and reported. No failure ever produces a zero: the totals stay incomplete and name the loans.
Tests: `tests/agents/test_lms_leviosa_negative.py` (22).
