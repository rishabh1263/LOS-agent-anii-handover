# Bank Verification Agent (credit agent)

The credit agent's bank verification step for Leviosa. It automates the bank checks a CPA user and a
verification agency do by hand today, and leaves people only the items that need a person.

> **DEMO MODE — SYNTHETIC DATA.** Every bank, person, account number (all start `9990`) and IFSC (all carry
> `DEM`) is fictional. The Account Aggregator (AA) and the branch verification are simulated and labelled as such.

## What it does

* **AA Success**: the simulated AA response fills the Leviosa form (bank name, account number, account type, the
  latest three transactions). The agent reads the statement and runs every rule.
* **AA Failure**: the CPA uploads the statement (PDF or CSV). The agent reads what it can, and the CPA enters or
  corrects the rest. The same rules run.
* **Eight rules**, deterministic and explainable:
  * R1 bank details;
  * R2 latest three transactions;
  * R3 statement integrity;
  * R4 ownership;
  * R5 balances;
  * R6 AA vs statement;
  * R7 reuse / duplicates;
  * R8 risk assessment and report.
* **AI summary in easy words**: local Qwen via Ollama. It explains the results; it never decides. Every summary is
  checked before it is shown; if refused, slow, or the model is down, a summary written from the rules shows instead.
* **Agency finalization**: checklist, branch visit, ownership, report and rationale give POSITIVE, NEGATIVE, or
  REVIEW_REQUIRED (not final), by configured rules.

## Run it

**On the LOS server (8010).** `main.py` mounts the API under `/api/v1/bank-verification` and serves the built screen.
After a restart of 8010 (for example `.\scripts\start_backend_auth_off.ps1`):

* UI: <http://127.0.0.1:8010/bank-verification/>
* API docs: <http://127.0.0.1:8010/docs> (tag "Bank Verification Agent")

**On its own** (port 8041), for development: see below. Both use the same store, `runtime/credit_bank_verification`.

Use the repository's Python environment. No new Python packages are needed: FastAPI, PyMuPDF and PyYAML are already
in `requirements.txt`.

```powershell
# from the LOS-agentic-ai folder
python -m uvicorn app.agents.credit.bank_verification.main:app --port 8041
```

* UI: <http://127.0.0.1:8041/> (the built frontend is served by the API).
* API docs: <http://127.0.0.1:8041/docs>.
* The first start seeds the 12 demo scenarios. Reset them any time with **Reset demo data** on the dashboard, or with
  `python -m app.agents.credit.bank_verification.demo_data.seed`.

Frontend development (hot reload on <http://localhost:5174>, API proxied to 8041):

```powershell
cd app/agents/credit/bank_verification/frontend
npm install
npm run dev      # npm run build -> dist/, which the API serves at /
```

The AI summary uses the Ollama the rest of the LOS uses (`OLLAMA_HOST`, `OLLAMA_MODEL`; see `app/llm/config.py`).
Without Ollama everything works and the summary is the rules-written one.

## Tests

```powershell
python -m pytest tests/agents/test_bank_verification_rules.py tests/agents/test_bank_verification_scenarios.py `
                 tests/agents/test_bank_verification_api.py tests/agents/test_bank_verification_summary.py `
                 tests/agents/test_bank_verification_prod.py -q
# the same suite on PostgreSQL (a per-test clone of a seeded template database):
$env:BANK_VERIFICATION_TEST_PG = "1"; python -m pytest tests/agents -k bank -q
cd app/agents/credit/bank_verification/frontend; npx vitest run
```

Production (`demo_mode: false`): set `BANK_VERIFICATION_DSN` (PostgreSQL). Without it the agent does not start,
and a router mounted on the LOS server answers 503 `STORE_NOT_CONFIGURED`. The role comes from the LOS token's scopes
(`security.role_by_scope`), an agency's id from the token claim `security.agency_claim`; demo headers are ignored.
`test_bank_verification_prod.py` checks all of it on a real PostgreSQL database.

## Where things are

| Path | What |
|---|---|
| `app/agents/credit/bank_verification/` | the agent: `api.py`, `main.py`, `schemas.py`, `rules/`, `services/`, `repository.py`, `security.py` |
| `app/config/credit_underwriting/bank_verification.yaml` | policy: limits, decision rules, roles, agencies, checklist, AI summary settings |
| `app/config/credit_underwriting/bank_verification_wording.yaml` | every sentence, label, rule name and flag severity |
| `app/config/credit_underwriting/bank_verification_demo.yaml` | the synthetic banks, transactions and the 12 scenarios |
| `runtime/credit_bank_verification/` | the SQLite store and stored files (`BANK_VERIFICATION_HOME` overrides) |
| `frontend/` | React + TypeScript + Vite |
| `docs/` | architecture, API contract, business rules, user guide, the manager explanation |

The LOS server (`main.py`) is not changed. The router can be mounted there later (see `docs/ARCHITECTURE.md`).
