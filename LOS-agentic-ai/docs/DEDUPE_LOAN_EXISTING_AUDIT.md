# Dedupe Agent: repository audit for "Loan Existing" integration (2026-10-22)

This is the Phase 1 audit for the *Final implementation prompt: LOS Dedupe Agent*. Everything here was checked in
this repository; anything that could not be checked is marked **GAP**. No schema, endpoint or identifier meaning was
invented. Synthetic data only.

## 1. The "Loan Existing" screen: not in this repository (GAP, blocking for Phase 4)

The prompt asks to integrate dedupe into an existing **Loan Existing** section. It names fields such as Prospect Code,
Applicant Type, Entity Type, Primary Applicant, Customer Code, Principal / EMI Outstanding, Disbursement Date, Tenor,
ROI and Loan Status, and actions **Copy** and **Match Found**. The section is described from a screenshot.

**None of these exist in this repository.** Every frontend was searched: `los-copilot-frontend/`,
`docs-credit-frontend/`, `eligibility-frontend/`, `app/static/`, `docs/frontend/` and `frontend_handoff/`. There is no
React or Vue app, no "Loan Existing" component, and no Copy or Match Found action on loan rows.

The screen therefore belongs to another LOS application, and neither its code nor the screenshot is available here.
The integration cannot be done without that code. What *is* here, and is reusable by that screen:

- **The one API** `POST /api/v1/credit/dedupe` (`app/api/routes/dedupe_api.py`). Its contract is in
  `docs/DEDUPE_API_CONTRACT.md`. It now also returns a single `dedupe` block (status, reasons, safe record refs,
  completeness, recommended action), which is the shape the prompt asks for.
- **The Dedupe Agent page** (`los-copilot-frontend/`). This is the closest existing screen. It has a "Past loans with
  SBFC" panel (customer strip plus a card per loan), "What matched" with roles, R01–R11, and the officer's decision on a
  possible match. Its view logic (`public/core.js`) is DOM-free and unit-tested, so it can be lifted into the other
  screen.

## 2. Data flow (confirmed)

UI → `POST /api/v1/credit/dedupe` (JWT, scope `los.dedupe.read` or the credit scope, case ownership via
`access.authorize_claims`) → `customer_history.assess`, which runs these steps in order:

1. Identifier validation.
2. MIFIN lookup (`mifin.py`, client `MIFIN_PROVIDER` / `MIFIN_DEDUPE_URL`).
3. Loan history (`loan_history.py`: MIFIN loans + LMS per loan).
4. History policy (`history_policy.py`, H01–H10).
5. LOS dedupe (`service.search` for the entered identifier + `service.assess` for the case: P-rules and scenarios).
6. One outcome, then the one dedupe status (`dedupe_status.py`).
7. Explanation.
8. Audit (`runtime/audit/customer_history.jsonl`) and case event `CUSTOMER_HISTORY_ASSESSED`.

Case creation is guarded by `app/agents/applicant/create_guard.py` (advisory locks, Idempotency-Key replay, possible
duplicate at create).

## 3. Database (confirmed)

| Table | What it holds |
|---|---|
| `applicants` | applicant_id, full_name, mobile, email, date_of_birth, address |
| `applications` | case_id, applicant_id, product, loan_amount, tenure, rate, declared income / obligations, property_value / type … |
| `co_applicants` | co_applicant_id, case_id, applicant_id, name, dob, pan, relationship … |
| `case_events` | event_id, case_id, event_type, ref_id, summary, sequence |

There is **no table for customers, loans or prospects**, and **no column for customer_code, ap_code, prospect_code,
pr_number or loan account** (`dedupe_policy.yaml`: `field: null`; `docs/MIGRATION_0010_PROPOSAL.md` is not applied).
Existing customers and loans come only from **MIFIN** (and the **LMS** per loan). Neither is connected for real yet;
the demo providers are used for dev and UAT.

## 4. Identifier meanings

| Identifier | In this repository | Status |
|---|---|---|
| PAN | Stored from documents; matched exactly as keyed HMAC | Confirmed |
| Mobile | `applicants.mobile`; supporting evidence only (family / business numbers) | Confirmed |
| AP Code | = MIFIN `CUSTID` = **customer ID**, permanent (owner, 2026-10-22). Format `AP` + 8 digits | Confirmed by the owner. Format observed, not specified |
| PR Code | = MIFIN `PROSPECTCODE` = **loan account no.**, created at disbursement (`mifin.yaml`, `dedupe_policy.yaml`) | Confirmed by the owner |
| **Customer Code** | Not used anywhere | **GAP**: is it the AP Code, or a different code? |
| **Prospect Code vs PR Number** | Treated as the **same** (PROSPECTCODE = PR code) | **GAP**: the prompt says they are not interchangeable. The business must define both |
| Applicant Type, Entity Type | Not stored (no individual / non-individual field). MIFIN `CUSTTYPE` is now read as the customer's role | **GAP** for Entity Type |
| ROI on existing loans | Not in MIFIN's reference shape or the LMS contract | **GAP** |

## 5. The prompt's 10 rules against what exists

| # | Rule | State | Where |
|---|---|---|---|
| 1 | Identifier validation and normalisation | Done | `identity.py`, `service.resolve`, API 422 on bad values; missing = never a match |
| 2 | PAN matching | Done | LOS search + case profiles (`P01_IDENTITY_UNIQUENESS`), MIFIN lookup, HMAC compare |
| 3 | Mobile matching | Done | Supporting only (`P01_POSSIBLE_MATCH`); never a duplicate on its own |
| 4 | AP Code matching | Done via MIFIN | The LOS does not store it; uniqueness is not assumed (AMBIGUOUS when MIFIN returns several) |
| 5 | Customer Code / PR / Prospect Code | **Partial** | PR code via MIFIN (now also the demo lookup by loan account). Customer Code / Prospect Code: **GAP** (semantics) |
| 6 | Multi-identifier + conflict detection | Done | `searched_identifier_matches`, `identity_check` (name / DOB in memory), `S11_IDENTITY_CONFLICT`, AMBIGUOUS. No arbitrary score is used |
| 7 | History and existing loans | Done (MIFIN + LMS) | Loan history, H01–H10; existing customer ≠ duplicate (`dedupe.status`) |
| 8 | Applicant / co-applicant / related party | Done for roles | `party_role`, role swaps (`P02_ROLE_HISTORY`), roles in matches. Related parties beyond co-applicants: **GAP** (no policy) |
| 9 | Concurrent / repeated submission | Done | `create_guard.py` (locks, Idempotency-Key, `POSSIBLE_DUPLICATE_AT_CREATE`), per-case lock on the dedupe check |
| 10 | Decision, evidence, audit | Done | `dedupe` block, officer decision as a case event, audit JSONL + timeline, no merge or delete ever |

## 6. Statuses (Phase 3), implemented

`DUPLICATE_FOUND` / `POSSIBLE_DUPLICATE` / `NO_DUPLICATE_FOUND` / `CHECK_INCOMPLETE` are produced by
`app/agents/dedupe/dedupe_status.py`, with the action from `dedupe_policy.yaml decision.actions`. `DUPLICATE_FOUND`
needs a signed-off policy (today it is unsigned). The invariants are tested over every input combination in
`tests/agents/test_dedupe_status.py`.

## 7. Security (confirmed and preserved)

- JWT, scope and case ownership are enforced.
- Other customers' cases are `REDACTED`.
- Values are never echoed or logged, and references are masked.
- Name / DOB are compared in memory only.
- The officer's decision needs a configured scope.
- Unknown request fields are refused (a caller cannot set a status).
- A missing source is never a negative result.
- No automatic merge or delete.

## 8. Blocking questions for the business

1. **Where is the "Loan Existing" screen's code** (repository / app), and can the screenshot be shared? Without it,
   Phase 4 cannot be done.
2. **Customer Code**: is it the AP Code (MIFIN CUSTID), or a different code?
3. **PR Number vs Prospect Code**: the repository treats PROSPECTCODE as the PR code / loan account. What are the two,
   and where does each live?
4. **Entity Type / Applicant Type** and **ROI** on existing loans: which source provides them?
5. **Duplicate definition and actions**: the duplicate window, whether `BLOCK_DUPLICATE` is allowed, policy sign-off,
   who may confirm a possible match, and whether auto-confirm should be turned on.

## 9. Implementation report (2026-10-22)

**Interim for Phase 4 (until the real screen's code is available).** The Dedupe Agent page
(`los-copilot-frontend/`) shows an **Existing Loan Details** table at full width, in the Loan Existing screen's
column order:

1. Prospect Code (MIFIN `PROSPECTCODE`, masked)
2. AP Code (`CUSTID`, masked)
3. Applicant Type (`CUSTTYPE`)
4. Product Name (`SCHEME`)
5. Loan Amount (`SANCTIONED_AMOUNT`)
6. Principal Outstanding / EMI / Overdue / Disbursement Date / Tenor (LMS)
7. ROI
8. Loan Status
9. Match Found

Each column is mapped in `public/ui_text.json existing_loan_columns`, together with its source. A value with no source
shows "—", with the reason on hover, and is never guessed. ROI, Applicant Name, Entity Type and Customer Code have no
verified source.

**API change.** One additive block, `dedupe`, on `POST /api/v1/credit/dedupe`, plus the optional request fields
`party_role`, `confirm_match` and `candidate`. Existing keys are unchanged.

**Security fix found by the new tests.** A case-store outage during the access check returned a generic 500. It now
returns 503 `CASE_STORE_UNAVAILABLE` (`dedupe_api.py`).

**Tests (actually run).**

| Command | Result |
|---|---|
| `python -m pytest -q tests/agents/test_dedupe_status.py tests/agents/test_dedupe_agent.py tests/agents/test_profile_match.py tests/agents/test_mifin_adapter.py tests/agents/test_loan_history.py tests/integration/test_customer_history.py tests/integration/test_dedupe_api.py tests/integration/test_match_decision.py tests/integration/test_los_profile_match.py tests/integration/test_fos_create_concurrency.py tests/integration/test_form_property_type.py tests/uat/test_dedupe_phase_a_uat.py tests/integration/test_create_case_validation.py` | **2238 passed, 0 failed** (103 s) |
| `python -m pytest -q tests/integration/test_dedupe_failures.py tests/integration/test_dedupe_api.py` | **30 passed** |
| `cd los-copilot-frontend && node --test test/*.test.js` | **53 passed, 0 failed** |
| Full regression (`-m "not ocr and not live_llm and not live_jev and not live_pg" tests`) | Running; counts to be added when it ends |

`test_dedupe_failures.py` covers:

- MIFIN timing out
- the LOS search or case assessment failing (CHECK_INCOMPLETE)
- the store down (503)
- an audit write failing (check still answers, `audit.recorded: false`)
- no identifier in logs or answers
- a changed applicant changing the next answer
- malformed requests
- 500 cases at realistic volume

**Measured latency** (demo MIFIN / LMS, local Postgres):

- 20–170 ms per check.
- 269 ms with 501 cases in the store and 167 sharing the searched mobile.
- The model-written summary is the slow part when Ollama is busy (a 6 s timeout). The deterministic summary is the
  fallback.

**Not production-ready.** These are still open:

- the real MIFIN / LMS endpoints and credentials
- the unsigned policies (dedupe and customer history)
- business limits (H07–H10, the duplicate window)
- the Loan Existing integration (§1)
- the identifier questions (§8)
- migration 0010 (party_identity) not applied
- the HMAC key not yet in a vault

## 10. Master prompt + trigger requirement (2026-10-22, later the same day)

**The "Loan Existing" section.** The master prompt's field list (Customer ID, Role in MIFIN, Checked For, Match,
Prospect Code … Match Found) is exactly the Dedupe Agent page's **Existing Loan Details** section. So the
integration is in that section, and the §1 GAP is resolved for this screen. *Match Found* now says what matched:
"This loan account (searched)" on the searched loan, and "Same customer (by …)" on the others.

**Trigger.** A confirmed NEW customer does not invoke the agent. A failed, incomplete or conflicting lookup is never
NEW. See `dedupe_status.trigger` and the API contract.

**Applicant / co-applicant.** Each party is evaluated separately (`party_eval.py`), then aggregated. A
co-applicant's match never marks the applicant.

**Recommendation vs credit.** The workflow recommendation is separate from the dedupe status and never a credit
decision. `DUPLICATE_FOUND` is `UNABLE_TO_DECIDE` until the business maps it.

**Dedupe Assessment Summary.** Inside the Existing Loan Details section. Every line comes from the answer.

**Open business questions (added to §8):**

6. A confirmed duplicate application: BLOCK or HOLD? (`decision.workflow.DUPLICATE_FOUND`)
7. A new applicant with an existing co-applicant: should the agent run? (`trigger.include_co_applicants`, on)
8. Is a co-applicant without a PAN on record an incomplete check (today, yes) or acceptable?

**Tests added:** `tests/integration/test_party_evaluation.py` (11) and `tests/agents/test_dedupe_status.py` (trigger,
recommendation and assessment invariants over every input combination).
