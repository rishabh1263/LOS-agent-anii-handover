# Credit Document Policy Agent (`docs_verify_credit_agent`)

It answers one question: **is this case's document set complete for credit underwriting, and if not, what is still
open?**

The agent does **not** verify documents. Classification, OCR, extraction and verification stay with the existing
document agents. It reads what they recorded, applies the Credit document policy, and records its answer.

## API

```
POST /api/v1/credit/document-readiness/{case_id}
Authorization: Bearer <JWT with los.credit.underwrite>
Content-Type: application/json

{"correlation_id": "optional-trace-id"}      # optional; any other field -> 422
```

```json
{
  "request_id": "optional-trace-id",
  "application_id": "DOCS-DEMO-02",
  "stage": "CREDIT",
  "product": "PERSONAL_LOAN",
  "policy_version": "PL-CREDIT-0.1.0-SAMPLE",
  "overall_status": "ACTION_REQUIRED",
  "requirements": [
    {"rule_id": "RULE-03", "rule_name": "Missing Documents", "check_id": "PRIMARY_APPLICANT:PAN",
     "status": "ACTION_REQUIRED", "severity": "BLOCKING", "party_role": "PRIMARY_APPLICANT",
     "party_id": "APP-DOCS-DEMO-02", "missing_documents": ["PAN"],
     "reason": "The PAN of the applicant is missing. Accepted: PAN.",
     "recommended_action": "REQUEST_DOCUMENTS", "evidence": ["policy:PL_BASE"]}
  ],
  "recommended_action": "REQUEST_DOCUMENTS",
  "credit_underwriting_allowed": false,
  "audit": {"evaluation_id": "…", "evaluation_status": "COMPLETED", "evaluated_at": "…",
            "policy_id": "CREDIT_DOCS_PERSONAL_LOAN", "policy_version": "PL-CREDIT-0.1.0-SAMPLE",
            "policy_status": "SAMPLE", "policy_file_sha256": "…", "base_policy": {"…": "…"},
            "input_hash": "…", "rules_evaluated": 10, "requirements_satisfied": 7, "requirements_open": 1,
            "reused_documents": [], "waivers_applied": [], "recorded": true, "replayed": false}
}
```

* `requirements` lists **only open items**. When everything is satisfied or validly waived, it is `[]`.
* `overall_status` is decided by blocking items only:
  * `ACTION_REQUIRED`: something blocking is open.
  * `EXCEPTION_PENDING`: the only blocking items are exceptions waiting for approval.
  * `COMPLETE`: nothing blocking is open.
* `credit_underwriting_allowed` = `COMPLETE` **and** the case is at `CREDIT`. It is computed, never configured. The
  agent never moves the stage.
* `recommended_action` is the most urgent blocking action, in this order:
  `RESOLVE_POLICY_CONFIGURATION`, `CAPTURE_APPLICATION_DATA`, `REQUEST_DOCUMENTS`, `REQUEST_FRESH_DOCUMENT`,
  `RECHECK_DOCUMENT`, `AWAIT_VERIFICATION`, `RAISE_EXCEPTION`, `AWAIT_EXCEPTION_APPROVAL`, `AWAIT_CREDIT_STAGE`,
  `PROCEED`.

| Code | When |
|---|---|
| 200 | Evaluated. |
| 401 | No token. |
| 403 | Missing scope, or the caller may not see this case. The answer is the same whether or not the case exists. |
| 404 | No application for the case (only a service principal sees this). |
| 422 | Invalid body, or no single usable policy: `POLICY_NOT_CONFIGURED`, `POLICY_AMBIGUOUS`, `POLICY_INVALID`, `POLICY_NOT_APPROVED`, `CONTEXT_INCOMPLETE`. |
| 503 | `CASE_STORE_UNAVAILABLE` or `AUDIT_UNAVAILABLE`. |
| 504 | `EVALUATION_TIMEOUT`. |

## The ten rules

| Rule | What it checks | Source |
|---|---|---|
| 01 Product requirements | The product's document requirements through the CREDIT stage. A conflict inside that policy is `UNRESOLVED`. | Existing engine `app/agents/policy/engine.resolve` |
| 02 Applicant coverage | Every party is checked with its own documents. A co-applicant's document never fills the applicant's slot. | `parties` + `applicant_agent.yaml` co-applicant checklist |
| 03 Missing documents | A mandatory slot with nothing accepted on file. | base policy |
| 04 Conditional | A slot added by a case condition. A condition the case cannot answer is `UNRESOLVED`. | base policy |
| 05 Alternatives | Only `accepts` counts. A same-kind document outside it is reported, not counted. | base policy + `same_kind` |
| 06 Period / freshness | Months covered, count, age and expiry. An unreadable date is `UNRESOLVED`. | `period_and_freshness` |
| 07 Supporting documents | A document that must come with another one. | `supporting_documents` |
| 08 Cross-stage | Earlier documents are reused. `collect_at_stage` types must be new. | `cross_stage` |
| 09 Exceptions | Only an **APPROVED** deviation that the policy maps to the slot (party-scoped) waives it. `PENDING_APPROVAL` gives `EXCEPTION_PENDING`. Nothing is raised or approved automatically. | deviation / maker-checker workflow |
| 10 Audit | Every evaluation is stored as a `DOCUMENT_POLICY` finding with every outcome, plus a case event. It is idempotent on the input hash. | case store |
| 11 File size & format | The stored file's real format (read from its bytes, not its name) and its size (min / max), per document type. | `file_rules` |

**Every rule can be switched off:** policy `rules` (`"RULE-11": false`) or env `DOCS_CREDIT_RULES_OFF=RULE-07,RULE-11`.
RULE-10 (audit) cannot be. The audit lists `rules_disabled`. Co-applicants get the same period, freshness and file
rules, plus `parties.CO_APPLICANT.also` (sample: signature).

A document counts only when its **recorded** status is satisfied **and** its recorded verification verdict is not a
failure. "Verified" with a failed verdict is a `MISMATCH`.

## Inside the LOS

* **Mounted** in the LOS server (`main.py`). It can also run alone: `uvicorn app.agents.docs_verify_credit_agent.main:app --port 8020`.
* **Credit Underwriting Agent:** reader tool `document_readiness.get` →
  `integrations/credit_underwriting_adapter.py` → the same `readiness_service.evaluate`.
  * Signal: `document_readiness.status`.
  * Rules in `app/config/underwriting_policy.yaml`:
    * `UW_DOCS_ACTION_REQUIRED` (REVIEW, HIGH)
    * `UW_DOCS_EXCEPTION_PENDING` (REVIEW, MEDIUM)
    * `UW_DOCS_COMPLETE` (POSITIVE)
  * Evidence `FINDING:DOCUMENT_POLICY` is verified by provenance.
  * The credit tool budget is `max_tool_calls: 12` (`agents.yaml`).
* **Data read:**
  * application, parties, stage and stage entry time
  * documents: the row for status and verdict, the EXTRACTION finding for values
  * deviations

  It reads no eligibility data, because none of the ten rules needs it.

## Layout

```
api/            routes.py (the endpoint), dependencies.py (scope + case access)
config/         settings.py (env), policy_loader.py (one policy, or a named error)
models/         policy.py (strict schema), application.py, requirement.py, response.py
services/       context_service -> policy_engine (+ reconciliation, document_inventory, exception, dates)
                -> readiness_service (status, action, timeout) -> audit_service
integrations/   existing_document_agents_adapter.py (read), credit_underwriting_adapter.py (credit tool)
policies/       personal_loan.credit.json (SAMPLE), example_policy.json (format), README.md
```

## Configuration

See `.env.example`. Policies are documented in `policies/README.md`.

## Demo data and tests

```
python scripts/seed_docs_credit_demo.py                  # 8 synthetic cases DOCS-DEMO-01..08 in the dev store
pytest tests/agents/test_docs_verify_credit_agent.py     # rules, policy errors, failures, invariants, API, security, credit
```

## Business decisions still needed

The shipped policy is a **SAMPLE**. It is refused outside development. These values are needed from the credit team:

* Bank statement months, maximum statement age (sample: 45 days) and salary slip recency/count (sample: 62 days, 1).
* Which document types must be collected fresh at CREDIT (sample: bank statement).
* Which deviation codes may waive which requirements (sample: DVM-62A2A3 → address proof).
* The non-accepted alternatives list (`same_kind`) and the supporting-document pairs (sample: ITR → ITR computation).
* Whether a guarantor / entity has its own checklist.
* Who approves the policy file, then set `status: APPROVED` with `approval`.
