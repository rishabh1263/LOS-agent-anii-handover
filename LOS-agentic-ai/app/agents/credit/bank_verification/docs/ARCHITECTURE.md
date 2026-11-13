# Architecture

## Place in the repository

The agent is a sub-package of the credit agent, `app/agents/credit/bank_verification/`, beside `repayment/`. It reuses
the following, with no second copy:

* `app/agents/bank_statement` (the existing statement reader) for the transactions and balances of a digital PDF;
* `app/llm/config.py` (the LOS Ollama settings) for the AI summary;
* the summary guard helpers of `app/agents/eligibility/output/summary.py` (figure check, whole sentences).

It runs as its own FastAPI app on port 8041 and serves its own frontend. The LOS server on 8010 is untouched.

```
browser (React, 5174 dev / served at 8041)
   │  X-Demo-Role / X-Demo-User (simulated roles, demo mode only)
   ▼
api.py ── security.py (role -> permissions, config roles)
   │
   ├─ POST /process ──► verification_service ─► statement_service ─► bank_statement (existing reader)
   │                          │                 (PDF header labels / CSV / honest failure for scans)
   │                          ├─ transaction_service (latest-by-date, prefill)
   │                          ├─ rules R1..R7 ─► R8 risk_assessment
   │                          ├─ repository (SQLite)  ─── audit events
   │                          └─ summary_service.precompute ─► one background worker ─► Ollama (Qwen)
   │
   ├─ POST /{id}/finalize ─► decision_service (mandatory evidence, decision rules, re-run with ownership)
   ├─ GET  /cases/{id}/summary ─► summary_service (AI text if ready, else rules-written + pending)
   └─ GET  /cases/{id}/report  ─► report_service (HTML; PDF via PyMuPDF)
```

## Data flow

1. **Case.** It comes from the seeded scenarios. Production would read the LOS case.
2. **Statement.**
   * An upload is checked by its content (PDF / CSV, size, page count).
   * A digital PDF is read: the header by its printed labels, the rows and balances by the existing reader.
   * A scan, a password-protected file or a damaged file gives `UNREADABLE` with the reason. **Nothing is ever filled
     in from demo data for an uploaded file.**
3. **AA data** (AA_SUCCESS only). This is the simulated AA response stored for the case.
4. **Form.**
   * What the CPA sent; otherwise prefilled from AA (AA_SUCCESS) or from the statement (AA_FAILURE).
   * Each value carries its provenance: AA / EXTRACTED / MANUAL / AGENCY.
5. **Rules.** R1–R7 run on one `Evidence` bundle; R8 sums them up. All of it is deterministic Python with Decimal money.
6. **Store.** The run, the eight results, the extraction, the original inputs and the audit events are recorded.
7. **Summary.** It is queued at once; the single worker writes it, checks it and stores it for the run.
8. **Finalize.**
   * The agency's input is validated for mandatory evidence.
   * The rules are re-run with the agency's ownership outcome, and the decision rules give the final status.
   * The decision is recorded and the case status updated.

## Database (SQLite, `runtime/credit_bank_verification/bank_verification.sqlite3`)

Records are kept apart so nothing overwrites what came before:

| Table | Holds |
|---|---|
| `cases` | header, mode, status, risk level, assigned agency |
| `sources` | every ORIGINAL input as received: AA response, the CPA form (account number masked), demo form |
| `documents` | every statement / report file: SHA-256, content fingerprint, account, transaction keys, stored path |
| `extractions` | what was read from a statement: extraction status, data, file facts |
| `runs` | each verification run: idempotency key, input hash, full response, the evidence it used |
| `rule_results` | the eight results of each run, one row each |
| `decisions` | every finalize attempt: payload, outcome, report reference, who, when |
| `summaries` | the AI summary of a run (or that the check refused it), keyed by run and by facts hash |
| `audit_events` | timestamped history: seeded, statement received / read / rejected, run, agency update, decision |

## Latency of the AI summary (CPU Qwen 4B, measured 2026-10-26)

* **Before:** 9–29 s per summary.
* **After:** 6–13 s per summary, from a compact prompt (~500–1,300 characters), a small context window (`num_ctx`
  2048) and a cap of 4 sentences.
* **Precompute:** queued the moment a run finishes. All 12 seeded summaries were ready in about 2 minutes in the
  background.
* **Reuse:** identical results (by facts hash) reuse the stored text with no model call.
* **Warm-up:** a prime call at start loads the model.
* **The screen never waits.** It shows the rules-written summary with "AI is writing…" and polls every 3 s.

## Configuration (all of it)

* `bank_verification.yaml`: upload limits, account-type synonyms, statement labels, integrity signals, balance
  tolerance, duplicate threshold, agencies, checklist, finalization rules, roles, actions, server, summary cards, AI
  summary settings. Status **DEMO_DUMMY / UNCONFIRMED**.
* `bank_verification_wording.yaml`: rule names, every sentence (rules, reader, errors, finalize, report), flag
  severities, case status mapping, the AI instructions and banned words, the easy-words checklist.
* `bank_verification_demo.yaml`: synthetic banks, the quarter's transactions, statement layout, the 12 scenarios and
  their expected results, the historical cases.

## Mounting in the LOS server later

```python
from app.agents.credit.bank_verification.api import router as bank_verification_router
app.include_router(bank_verification_router, prefix="/api/v1", dependencies=[Depends(require_jwt)])
```

Replace `security.caller` with the LOS JWT claims first: in production the role must come from the login, never a
header.
