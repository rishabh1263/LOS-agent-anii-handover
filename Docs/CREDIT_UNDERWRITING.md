# Credit Underwriting Agent — on the Common Agent Harness

## 1. Common Agent Harness (`app/agents/runtime/`)

Shared, domain-agnostic infrastructure for every LOS agent. The Credit
Underwriting Agent is its first consumer; the others migrate by wrapping their
existing entry point (their domain engines are unchanged).

| Module | Responsibility |
|---|---|
| `harness.py` | `run(spec, handler)`: run id, request/correlation ids, caller propagation (never recorded), hard deadline backstop, `agent.run` OTEL span, output validation (spec checks; failure ⇒ `INVALID_OUTPUT`, output withheld), normalised errors, audit sink (redacted JSONL) and eval observers |
| `execution.py` | `RunContext.call_tool`: allowlist → budget → deadline → per-call timeout → **one** bounded retry on a declared-transient failure → `ToolCallRecord` + `agent.tool` span. `call_model`: max-model-calls budget, deadline, timeout, `ModelCallRecord` (no prompt/completion), `agent.model` span |
| `limits.py` | `Limits` (tool calls, replans, model calls, retries ≤ 1, deadline, call timeout) and `Budget` |
| `trajectory.py` | events / tool calls / model calls, **redacted on write** (credential keys and token-like values dropped, PAN/Aadhaar/account masked); deterministic `replay()` + `replay_digest` |
| `errors.py` | normalised `ErrorInfo(code, category, message, retryable)` — never a traceback or raw exception text |
| `evals/` | agent-agnostic `Expectations` / `EvalCase` / `evaluate` / `run_suite`: tool selection, forbidden / unnecessary / duplicate tools, planning order, exact trajectory, bounded execution, retries, model calls, latency, events, no-hallucination words, PII absence, generic provenance completeness, failure codes, plus agent-supplied checks |

**Never in the harness:** credit policy, underwriting, eligibility, KYC,
document, risk logic, or decisions (a test scans its source for domain terms).

## 2. Credit flow

```
POST /api/v1/credit/underwriting/run  (JWT)
  -> harness.run(credit spec)
     -> build_context : scope (los.credit.underwrite) -> ownership (access.authorize)
                        -> authoritative stage (CREDIT) -> application.get
     -> plan          : policy evidence_plan (+ by_employment / by_product), allowlisted
     -> select_next -> execute_tool -> observe -> sufficiency   (bounded loop)
          max 10 tool calls, max 2 replans, each tool once per party (reuse),
          bureau transient retry x1, deadline, loop guard
     -> evaluate_policy -> findings -> assess -> memo (<= 1 Qwen call, validated)
     -> persistence (UNDERWRITING finding + CREDIT_ASSESSED event, idempotent)
  -> READY_FOR_DECISION | REVIEW_REQUIRED | DATA_INSUFFICIENT  ->  Decision Agent
```

## 3. Data-flow boundary

The Credit Agent **reads** each upstream result as it was recorded in the
authoritative case store; it never re-runs, re-extracts or recomputes it.

| Upstream owner | What it produces (and owns) | Where it is recorded | Credit reads via | Credit does with it |
|---|---|---|---|---|
| Document Agent / verification | classification, extraction, per-document verdict | `documents.verification_status` (copied verdict); VERIFICATION + EXTRACTION findings | `documents.get` (MCP) | counts recorded verdicts; unverified ≠ passed; `UW_REC_DOCUMENT_NOT_PASSED` |
| KYC Agent | cross-document identity match | KYC finding (per party / case) | `kyc.get` | echoes status per party; `UW_REC_KYC_NOT_PASS` |
| Bank Statement Agent | rows, reconciliation, `signals.derive`, `income_evidence` | EXTRACTION finding `fields.evidence` / `fields.income_evidence` (reconciled rows only) | `bank_behaviour.get` | applies rules to recorded counts; an absent count is **unavailable, never zero** |
| Salary Slip / ITR (Financial Agent) | `IncomeSignals` (net / gross salary, ITR total income) | EXTRACTION `fields.signals`, released only past the verification gate | `financial_documents.get` | income evidence only from a **PASS** verdict; never annualised / de-annualised |
| Income consistency (LOS flow) | slip-vs-bank comparison | FINANCIAL / INCOME_CONSISTENCY finding | `income.get` | consumes status; compares **declared** income with the recorded slip amount |
| Eligibility Agent | EMI / FOIR / LTV / eligibility | FINANCIAL / ELIGIBILITY finding | `eligibility.get` (MCP) | consumes status; echoes FOIR/LTV as recorded |
| Risk / Fraud Agent | risk score, flags, outcome | RISK finding (G2) | `risk.get` | consumes outcome; echoes score |
| Bureau (DEMO provider) | normalised bureau report | provider response (`is_demo`) | `bureau.get` | credit-profile / repayment / adverse rules |
| FOS / application | declared income, obligations, employment, product | `applications` | `application.get` (MCP) | context + declared-vs-recorded comparisons |

Qdrant is never read for any of this: it remains RAG / semantic context only.

## 4. Demo vs production

| Item | Status |
|---|---|
| Underwriting policy (`underwriting_policy.yaml`) | **DEMO_UNCONFIRMED**, `signed_off: false`; refused in production unless `ALLOW_UNSIGNED_UNDERWRITING_POLICY=true` |
| Bureau (`DemoBureauProvider`, `config/demo/bureau_demo.yaml`) | **DEMO / NON_PRODUCTION**; synthetic profiles; every report `is_demo`; refused in production unless `ALLOW_DEMO_BUREAU=true`; no real integration exists |
| Outputs | labelled `["DEMO", "NON_PRODUCTION", "UNCONFIRMED"]` while either is demo |

## 5. Verification

* `tests/agents/test_agent_runtime_harness.py` — the common harness
* `tests/agents/test_credit_slice{1,2,3,4}_*.py` — contracts, graph, assessment, agent/API
* `tests/agents/test_credit_evals.py` / `python -m evals.credit.run` — 15 agentic eval cases
* `tests/integration/test_credit_http_e2e.py` / `python -m evals.credit.http_e2e` — real HTTP + JWT
