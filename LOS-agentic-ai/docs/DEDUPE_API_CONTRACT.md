# Dedupe API: frontend contract (v1.0, 2026-10-22)

**One endpoint:** `POST /api/v1/credit/dedupe`. It sits under the Credit Underwriting agent and is called from the
FOS stage with the application form's dedupe block.

It answers, in one call:
- the LOS's own dedupe;
- the MIFIN existing-customer match;
- the loan history (MIFIN + LMS);
- the policy outcome;
- a validated explanation.

> **Sources today:** MIFIN and the LMS run on **demo providers** (`MIFIN_PROVIDER=demo`, `LMS_PROVIDER=demo`,
> refused in production). Every response says `is_demo: true` while they are used. The real providers need the
> MIFIN / LMS endpoint, request format and credentials; see "Not yet real" below.

## Request

```json
{
  "case_id": "CASE-3D51FFAC6342",
  "customer_type": "Existing",
  "dedupe_parameter": "Mobile No",
  "dedupe_parameter_value": "9876502222",
  "requested_product": "PERSONAL_LOAN",
  "requested_amount": 500000
}
```

| Field | Required | Values |
|---|---|---|
| `case_id` | yes | A case the caller owns |
| `dedupe_parameter` | yes | `PAN`, `Mobile No`, `AP Code` (customer ID), `PR Code` (loan account no.). Synonyms are in `dedupe_policy.yaml` |
| `dedupe_parameter_value` | yes | A valid PAN / 10-digit mobile / `AP` + 8 digits / `PR` + 8 digits. **Never echoed back** |
| `customer_type` | no | `New`, `Existing` |
| `requested_product`, `requested_amount` | no | The amount is in rupees and must be > 0 |
| `party_role` (alias `check_for`) | no | `Applicant` (default) or `Co-Applicant` (`dedupe_policy.yaml party_roles`). The customer-type check applies to the applicant only |
| `confirm_match` + `candidate` | no | The officer's decision on a possible match: `SAME_CUSTOMER` / `NOT_SAME_CUSTOMER`, with `history.customer.confirmation.candidate` from the previous answer. Needs a scope from `dedupe_policy.yaml officer_confirmation.scopes`. 403 `CONFIRMATION_NOT_ALLOWED`, 409 `NOTHING_TO_CONFIRM` |

Unknown fields are refused (422), so a caller cannot set a status or a result.

**Headers:** `Authorization: Bearer <token>`. The scope must be `los.dedupe.read` or the credit underwriting scope.

## Response: top level

| Key | Meaning |
|---|---|
| `api_version`, `request_id`, `case_id`, `stage` | Identifiers |
| `searched_parameter` | `PAN`, `MOBILE`, `AP_CODE` or `PR_CODE` (the value itself is never returned) |
| `customer_status` | `EXISTING`, `POSSIBLE_EXISTING`, `AMBIGUOUS`, `NEW` or `UNKNOWN` (MIFIN could not answer) |
| `customer_type_check` | The form's Customer Type compared with MIFIN: `CONSISTENT`, `DECLARED_NEW_BUT_FOUND`, `EXISTING_NOT_FOUND_IN_LOS`, `NOT_VERIFIABLE` … |
| `los_dedupe.search` | The value searched across LOS cases. Matches on cases the caller cannot open are `REDACTED` |
| `los_dedupe.case` | This case's LOS dedupe: parties and matches, role swaps, file reuse, simultaneous applications, scenarios, coverage |
| `history.customer` | MIFIN: `match`, masked `customer_ref` (`AP******18`), `customer_code` (full AP code, only on a CONFIRMED match and with `mifin.yaml privacy.show_full_codes`; else null), `match_basis` (flags, score, `score_threshold: null`), `searched_identifier_matches` |
| `history.history_status` | `COMPLETE`, `PARTIAL`, `MIFIN_ONLY`, `NO_PREVIOUS_LOANS`, `NOT_APPLICABLE`, `NOT_ATTRIBUTED`, `UNKNOWN` or `UNAVAILABLE` |
| `history.loans[]` | Per loan (see below) |
| `history.aggregates` | See below |
| `evaluation.rules[]` | Each rule: `rule_id`, `policy_version`, `outcome` (`TRIGGERED` / `NOT_TRIGGERED` / `NOT_EVALUATED` / `EVALUATION_FAILED`), `reason` (`THRESHOLD_UNCONFIGURED` / `DATA_UNAVAILABLE` …), `reason_code`, `next_action`, `evidence` |
| **`outcome`** | `READY_FOR_DECISION`, `REVIEW_REQUIRED` or `DATA_INSUFFICIENT`. **Never an approval or a rejection.** `READY_FOR_DECISION` means the verified history is complete for a credit decision |
| `reason_codes[]`, `next_actions[]` | From the policy and the LOS dedupe (`LOS_IDENTITY_CONFLICT`, `LOS_DOCUMENT_REUSE` …) |
| `completeness` | `complete` (false while any check is not evaluated), `history_status`, `sources[]` (MIFIN / LMS with `status` and `is_demo`) |
| `policy` | `policy_id`, `policy_version`, `signed_off` |
| `explanation` | `text`, `source` (`LLM` or `DETERMINISTIC`), `fallback_reason` |
| `checked_party` | `APPLICANT` or `CO_APPLICANT` |
| **`dedupe`** | The one dedupe status (below) |
| `is_demo`, `audit.recorded`, `timings_ms` | Demo flag, audit / timeline written, timing |

### The one dedupe status (`dedupe`, 2026-10-22)

Derived only from the assessment above (`app/agents/dedupe/dedupe_status.py`); it runs no new search and reads no new source.

| Key | Meaning |
|---|---|
| `status` | `DUPLICATE_FOUND`, `POSSIBLE_DUPLICATE`, `NO_DUPLICATE_FOUND` or `CHECK_INCOMPLETE` |
| `customer_match`, `existing_loan_match`, `application_match` | Existing customer confirmed in MIFIN / that customer has loans / another same-product application of this customer in the LOS |
| `match_reasons[]` | e.g. `PAN_MATCHES_EXISTING_CUSTOMER`, `EXISTING_LOANS_ON_RECORD`, `POSSIBLE_MATCH_NOT_CONFIRMED`, `IDENTIFIER_ON_OTHER_LOS_CASES`, `SAME_PRODUCT_APPLICATION_EXISTS`, `ROLE_CHANGE_ON_ANOTHER_CASE`, `PAN_ON_ANOTHER_CUSTOMER_RECORD`, `DOCUMENT_REUSED_ACROSS_CUSTOMERS`, `IDENTIFIER_DIFFERS_ON_MIFIN_RECORD`, `MORE_THAN_ONE_CUSTOMER_MATCHED` |
| `matched_records[]` | `record_ref` (masked MIFIN ref, or a LOS case id the caller may open, else `REDACTED`), `record_type` (`EXISTING_CUSTOMER` / `EXISTING_LOAN` / `LOS_APPLICATION`), `source`, `matched_fields`, `role` / `status` |
| `search_complete`, `searches_missing[]` | Whether every required search ran (`MIFIN_UNAVAILABLE`, `LOS_SEARCH_FAILED` …) |
| `recommended_action` | From `dedupe_policy.yaml decision.actions` (today `CONTINUE` or `MANUAL_REVIEW`; `BLOCK_DUPLICATE` is not used until the business authorises it) |
| `policy` | `policy_id`, `policy_version`, `signed_off` |

Rules:
- `CHECK_INCOMPLETE` when a required search did not run. It is **never** `NO_DUPLICATE_FOUND` on a missing source.
- `DUPLICATE_FOUND` needs a **signed-off** dedupe policy. While the policy is unsigned, the same evidence is `POSSIBLE_DUPLICATE`.
- An existing customer with loans is **not** a duplicate: `NO_DUPLICATE_FOUND` with `customer_match` / `existing_loan_match` true.
- Any review evidence (possible or ambiguous match, identifier conflict, other LOS cases with the identifier, role swap, document reuse, same-product application) is `POSSIBLE_DUPLICATE`.

### The trigger, the parties and the recommendation (`dedupe`, added 2026-10-22)

**Trigger** (`dedupe.trigger`): the customer lookup for the checked person decides whether the dedupe agent runs.
The lookup is MIFIN plus the LOS's own records.

| `customer_lookup` | When | Agent |
|---|---|---|
| `NEW` | MIFIN answered "no record" **and** the LOS search ran with no match (or the identifier is one the LOS does not store) | **Not invoked**: `dedupe.invoked: false`, `status: null`, `assessment.duplicate_application: NOT_EVALUATED`, recommendation `trigger.new_customer_recommendation` |
| `EXISTING` | MIFIN confirmed the customer, or the LOS holds the identifier on another case | Invoked |
| `POSSIBLE_EXISTING` | MIFIN possible match, not yet decided | Invoked |
| `CONFLICTING` | Several MIFIN customers, or MIFIN's record holds a different identifier | Invoked |
| `LOOKUP_FAILED` | MIFIN unavailable / errored / malformed, or the LOS search failed | Invoked; **never treated as NEW** (CHECK_INCOMPLETE) |

If the applicant is NEW but a co-applicant is not provably new, the agent still runs
(`trigger.customer_lookup_parties: CO_APPLICANT_NOT_NEW`; config `trigger.include_co_applicants`, default on).

**Parties** (`dedupe.parties[]`): every party on the case is evaluated separately with its own identifier on record
(PAN preferred, else mobile). Each entry has:

- `party_id`, `role` (`PRIMARY_APPLICANT` / `CO_APPLICANT`)
- `identifiers_on_record`, `identifier_used`, `identifier_source`
- `mifin` (match, officer_decision, masked customer_ref, customer_code (full, CONFIRMED only), role_in_source, loan_count, loans_by_status_class)
- `los` (status, matches, reason_codes)
- `status`, `match_reasons`, `existing_customer`

A party with no PAN or mobile on record is `CHECK_INCOMPLETE`, listed in `parties_incomplete`. The raw identifiers
are never returned. `status` is the **application-level** aggregate; `checked_identifier_status` is the result for the
searched identifier alone.

**Workflow recommendation** (`dedupe.workflow_recommendation`): `recommendation` (`PROCEED_TO_NEXT_STAGE` /
`HOLD_FOR_REVIEW` / `BLOCK_APPLICATION` / `UNABLE_TO_DECIDE`), `reason`, `next_action`, `policy_question`. It comes
from `dedupe_policy.yaml decision.workflow`. A status the policy does not map (today `DUPLICATE_FOUND`) is
`UNABLE_TO_DECIDE`, with the open question. `PROCEED_TO_NEXT_STAGE` means "continue through the normal LOS checks",
**never a loan approval**.

**Assessment** (`dedupe.assessment`): `existing_customer_or_loan`, `duplicate_application` (`CONFIRMED` / `POSSIBLE`
/ `NOT_FOUND` / `UNKNOWN` / `NOT_EVALUATED`), `conflicts[]`, `search_complete`, and
`credit_decision: NOT_DECIDED_BY_DEDUPE`. The page's *Dedupe Assessment Summary* is built only from these fields.

**Loans:** `history.loans[].searched_loan_account` is true on the one loan whose account number was the searched PR
code. The other loans belong to the same customer.

### The customer (`history.customer`), added 2026-10-22

| Key | Meaning |
|---|---|
| `role_in_source` | MIFIN `CUSTTYPE` mapped by `mifin.yaml customer_types` (`APPLICANT`, `CO_APPLICANT`, `GUARANTOR`, else `UNMAPPED`) |
| `identity_check` | `searched_identifier`, `name`, `dob`: each `MATCH`, `MISMATCH` or `NOT_COMPARED`. Compared in memory with MIFIN's record; **the values are never returned or stored** |
| `confirmation` | `required` (a possible match needs a decision), `candidate` (an opaque HMAC reference), `decision`, `decided_by` (`OFFICER` or `AUTO_RULE`). Auto-confirm is `dedupe_policy.yaml officer_confirmation.auto_confirm` and is **off** |

### Each loan (`history.loans[]`)

| Key | Meaning |
|---|---|
| `prospect_ref` | The masked PR code (`PR******53`) |
| `scheme` | The loan scheme |
| `source_status` | MIFIN's own `LOAN_STATUS` |
| `status_class` | MIFIN's status mapped by config, else `UNKNOWN` |
| `amounts` | `sanctioned_amount`, `os_principal`, `os_interest`, `os_othercharges` as strings; `null` = unknown, **never 0** (meanings UNVERIFIED) |
| `lms_status` | `FOUND`, `NOT_FOUND` or `UNAVAILABLE` |
| `lms` | Typed LMS fields, only those returned |
| `lms_unavailable_fields` | The LMS fields that were not returned |
| `effective_status` | `ACTIVE`, `CLOSED`, `SETTLED`, `WRITTEN_OFF`, `CANCELED` or `UNKNOWN` |
| `status_source` | `LMS` or `MIFIN` |
| `resolved` | Whether the loan's state is known |
| `duplicates_collapsed`, `conflicting_duplicate` | Repeated records counted once; conflicting repeats flagged |

### Totals (`history.aggregates`)

- **Counts:** `by_status`, `active_loans`, `unknown_status_loans`, `conflicting_duplicates`.
- **Money totals:** `active_exposure` and `monthly_emi_burden`. Each has `status` (`COMPLETE` / `PARTIAL` /
  `NOT_EVALUATED` / `NOT_APPLICABLE`), `value` (rupees, string), `loans_missing`, `loans_status_unknown` and
  `loans_conflicting`.
- **Risk:** `worst_dpd_12m` and `adverse_events` (settled / written off).
- **Rule:** totals come only from LMS figures on LMS-active loans. Any gap makes a total `PARTIAL`; it is never
  silently summed.

## Errors

All error bodies are `{detail: {code, message, request_id}}`, with no value echoed.

| HTTP | Code | When |
|---|---|---|
| 401 | — | No token |
| 403 | `INSUFFICIENT_SCOPE`, `CASE_ACCESS_DENIED` | Missing scope, or not the caller's case |
| 404 | `CASE_NOT_FOUND` | A service caller named a missing case |
| 422 | `INVALID_PARAMETER_VALUE` | Bad value, parameter or customer type; or a validation error |
| 500 | `POLICY_INVALID` | A broken policy file |
| 503 | `CASE_STORE_UNAVAILABLE` | The case store is down |

## Example: generated by a real call on demo data (trimmed)

```json
{
  "api_version": "1.0", "case_id": "CASE-EXAMPLE0001", "searched_parameter": "MOBILE",
  "customer_status": "EXISTING",
  "customer_type_check": {"declared": "EXISTING", "result": "CONSISTENT"},
  "requested": {"product": "PERSONAL_LOAN", "amount_rupees": "500000"},
  "los_dedupe": {"search": {"status": "NO_MATCH", "matches": []}, "case": {"status": "INSUFFICIENT_DATA"}},
  "history": {
    "customer": {"match": "CONFIRMED_MATCH", "customer_ref": "AP******18",
                 "match_basis": {"strong_flag": true, "score": 8, "score_threshold": null,
                                 "interpretation_status": "UNVERIFIED"}},
    "history_status": "COMPLETE", "loan_count": 3, "is_demo": true,
    "loans": [{"prospect_ref": "PR******53", "scheme": "ME NEW CUSTOMER", "source_status": "DISBURSED",
               "effective_status": "ACTIVE", "status_source": "LMS",
               "lms": {"principal_outstanding": "312400", "emi": "11200", "dpd_max_12m": 0}}],
    "aggregates": {"by_status": {"ACTIVE": 1, "CLOSED": 1, "CANCELED": 1},
                   "active_exposure": {"status": "COMPLETE", "value": "312400"},
                   "monthly_emi_burden": {"status": "COMPLETE", "value": "11200"}}
  },
  "outcome": "REVIEW_REQUIRED",
  "reason_codes": ["POLICY_NOT_CONFIGURED:H07_EXISTING_LOAN_COUNT", "POLICY_NOT_CONFIGURED:H08_TOTAL_EXPOSURE",
                   "POLICY_NOT_CONFIGURED:H09_EMI_BURDEN", "POLICY_NOT_CONFIGURED:H10_DPD"],
  "next_actions": ["Apply the existing-loan limit once the business owner sets it.", "..."],
  "policy": {"policy_id": "CUSTOMER_HISTORY", "policy_version": "0.1.0-UNSIGNED", "signed_off": false},
  "explanation": {"source": "DETERMINISTIC", "text": "MIFIN confirmed an existing customer (AP******18). 3 previous loan record(s) were found: 1 active, 1 canceled, 1 closed. Live exposure is Rs 312400 with a monthly EMI of Rs 11200. 4 policy check(s) could not be evaluated ... The case needs review before a credit decision. ..."}
}
```

## Not yet real (blockers)

| Item | State | Owner / action |
|---|---|---|
| MIFIN live client | An interface only: no endpoint, request format or credentials in the repository | MIFIN team: URL, request template, token |
| LMS / core client | An interface + typed record only | Core / LMS team: API or extract contract |
| MIFIN meanings | `SCORE` scale, `STRONG` / `POTENTIAL` together, `LOAN_STATUS` list, amount meanings: all UNVERIFIED | MIFIN team + credit policy |
| Limits | H07–H10 thresholds null (`NOT_EVALUATED` → `REVIEW_REQUIRED`); duplicate window (P05 / P15) null | Business owner |
| Policy sign-off | `CUSTOMER_HISTORY 0.1.0-UNSIGNED`, `DEDUPE 0.2.0-UNSIGNED` (production refuses them) | Credit policy owner |
| HMAC key | `LOS_IDENTITY_HMAC_KEY` vault + rotation not done | Security / infra |
| Migration 0010 | Proposal only; not applied | Owner approval |

**Full codes (2026-10-23).** Each `history.loans[]` also carries `prospect_code` (the PR code in full) when
`privacy.show_full_codes` is on; logs, audit and summary text keep the masked `*_ref`.

**Every previous case (2026-10-23).** `history.customer.identity_check.pan` (MATCH / MISMATCH / NOT_COMPARED);
`history.loans[].applicant_check` {common_applicant, customer_code, role_in_source, pan, name, dob};
`history.loans[].cancel_reason` (source text, masked, capped; null when not sent); `history.common_applicant`
{cases, same_customer_on_all, basis, customer_code, pan, cancelled_cases, cancelled_with_reason} -- present only when
the customer is confirmed and has loans.

