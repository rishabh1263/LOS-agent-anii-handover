# Dedupe agent, Phase A: implementation review and go/no-go (2026-10-22)

**Scope:** a review plus non-schema validation only.
- Migration 0010 is **not applied**; the schema is unchanged.
- No AP Code / PR Code source was invented.
- The duplicate window stays unset.

## 1. Against the audit and the repository's real fields

| Capability | State | Evidence |
|---|---|---|
| PAN match: primary ↔ primary (another customer record) | **Implemented** | `match.compare`; P01 TRIGGERED → `IDENTITY_CONFLICT_REVIEW`; fraud `pan` |
| PAN match: co-applicant ↔ primary (both directions = role swap) | **Implemented** | P02 TRIGGERED, `role_change: true`; never a fraud `pan` hit |
| PAN match: co-applicant ↔ co-applicant on another customer's case | **Implemented** | MATCH reported, no conflict (a co-applicant is not a customer record) |
| PAN sources | **Implemented**, with one limit | Primary: gate-released EXTRACTION findings. Co-applicant: `co_applicants.pan` (declared, migration 0004) **and** EXTRACTION. Limit: a case with no released PAN document has no PAN to match. |
| Mobile | **Partial** | Primary only (`applicants.mobile`); **a co-applicant's mobile is not stored** in this LOS. Supporting only: mobile alone = POSSIBLE_MATCH; same mobile with a different PAN = shared signal (family), never a match. |
| AP Code (customer ID) | **Unavailable** | No source in the repo → `INSUFFICIENT_DATA` + `needs_source: CUSTOMER_MASTER` (`docs/DEDUPE_CORE_LMS_INTERFACE.md`) |
| PR Code (loan account no.) | **Unavailable** | Created at disbursement; this LOS ends before it → `INSUFFICIENT_DATA` + `needs_source: LOAN_MANAGEMENT_SYSTEM` |
| Duplicate / simultaneous application | **Partial** | Same customer + same product **detected** (P05 structural). The time window (P05_DUPLICATE_WINDOW, P15) is **unset** → NOT_EVALUATED, `completeness.complete = false`. |
| Concurrency | **Partial** | Assessments of the same customer / PAN are serialised (session advisory lock). Case **creation** is not guarded: simultaneous creates are detected afterwards, not prevented. |
| Document reuse | **Implemented (scan mode)** | Byte-identical stored file on another customer's case → P06 → `DOCUMENT_VERIFICATION_REQUIRED`. Scan bounded (500 other files) → PARTIAL beyond. Files missing from the store are counted, not hidden. |
| Missing data | **Implemented** | No PAN and no mobile → `INSUFFICIENT_DATA` / `DATA_UNAVAILABLE`; the fraud agent keeps its old data gap |
| Scan limit | **Implemented** | `limits.max_scan_cases` (2000) → `truncated`, P13, `PARTIAL_ANALYSIS` |
| Cross-customer isolation | **Implemented** | Matched cases **and** record ids the caller cannot open → `REDACTED`, also inside `dedupe_result`. A leak found and fixed in this review. |
| Masking | **Implemented** | No PAN, mobile, HMAC token or search value in any response (tests scan the whole body) |
| Audit / timeline | **Implemented** | Masked JSONL (`runtime/audit/dedupe_decisions.jsonl`) + a `DEDUPE_ASSESSED` case event per assessment. The JSONL has no retention policy of its own (it uses the shared rotating writer). |
| Policy engine | **Implemented** | Credit conventions, a closed signal set, a sign-off gate (unsigned is refused in production), validated at startup |
| HMAC key | **Implemented** | `LOS_IDENTITY_HMAC_KEY`, required in production at startup, rotation by key id |
| Fraud-agent integration | **Implemented** | `DedupeResult` shape unchanged; a failure or no identifiers → None → the old data gap (tested) |
| Loan history, repayment, DPD, top-up, closure, RCU outcomes, collateral | **Unavailable** | NOT_EVALUATED / DATA_UNAVAILABLE in every response; the scenarios are listed with `needs_source` |

## Tests actually run (2026-10-22)

| Command | Result | Duration |
|---|---|---|
| `pytest tests/agents/test_dedupe_agent.py tests/integration/test_dedupe_api.py` | **68 passed**, 0 failed, 0 skipped | 3 min 34 s |
| `pytest -m "not ocr and not live_llm and not live_jev" tests/agents/test_fraud_risk_agent.py tests/agents/test_fraud_risk_api.py tests/integration/test_health.py tests/integration/test_kyc_eligibility_credit_flow.py tests/integration/test_los_co_applicant.py tests/integration/test_los_flow.py tests/integration/test_step5d_co_applicant_identity.py` | **175 passed**, 0 failed, 14 deselected | 10 min 36 s |
| Full suite `pytest -m "not ocr and not live_llm and not live_jev and not live_pg" tests` | **Not completed.** Stopped after 53 min at the owner's direction (Phase A needs only the suites of the files it touched); no pass/fail claimed | — |

What the 68 dedupe tests cover:
- PAN primary ↔ primary, co-applicant ↔ primary (both directions), and co-applicant ↔ co-applicant;
- a mobile-only family match, and different PANs sharing a mobile;
- role swaps, document reuse, missing data, and the scan limit / partial analysis;
- response-wide masking of case ids, record ids, PAN, mobile and tokens;
- the HMAC key: missing at startup, malformed, and rotation;
- policy validation, and the sign-off gate in production;
- the fraud agent's contract, the clean case, and the failure fallback (service and LOS flow);
- concurrency;
- the form fields;
- the unset duplicate window → `completeness.complete = false`.

## 2. Go / no-go

**NO-GO for production.** It is acceptable for a **development / UAT trial** behind `DEDUPE_ENABLED`, on the user's
approval.

**Blockers:**
1. **Policy not signed off** (`signed_off: false`). Production refuses to start unless `ALLOW_UNSIGNED_DEDUPE_POLICY=true`.
2. **`LOS_IDENTITY_HMAC_KEY` not provisioned** (vault location, access, rotation runbook). Production refuses to start
   without it.
3. **Scan mode cost:** every LOS run with risk on reads up to 2000 cases (about 1 s on the dev store) and hashes up to
   500 stored files. That is acceptable for UAT; production scale needs migration 0010 (approval pending) or a lower
   bound.
4. **The existing-customer path depends on the PR code** (the owner's main use), which is unanswerable until the
   LMS / customer-master source exists.

**Residual risks:**
- A case without a released PAN document is matched on mobile only (weak); an early-FOS case is often INSUFFICIENT_DATA.
- Mobile is primary-only, so a co-applicant can only be matched by PAN.
- Document reuse is byte-identical only: the same document rescanned is not detected.
- Thresholds of `gt 0` are rule definitions, not SBFC values, but they are still `UNCONFIRMED` until policy sign-off.

## 3. Approvals needed next

1. **Migration 0010:** decisions 1, 2, 4, 5, 6 and 7 in `docs/MIGRATION_0010_PROPOSAL.md`.
2. **Duplicate window and concurrency window:** the business owner's time limits (P05_DUPLICATE_WINDOW, P15).
3. **Policy sign-off** for `app/config/dedupe_policy.yaml` (P01, P02, P05, P06, P13 definitions and severities).
4. **Core / LMS team:** the interface in `docs/DEDUPE_CORE_LMS_INTERFACE.md`.
5. **Whether to enable** `DEDUPE_ENABLED` in UAT (default on in code: switch it off if not approved).
