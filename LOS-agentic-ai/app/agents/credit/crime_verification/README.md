# Crime Record Verification (Credit Underwriting)

The criminal record check of the **applicant and the co-applicant**: a sub-agent of Credit Underwriting. It runs on the
LOS server (8010) under `/api/v1/credit/crime-verification`, and its screen is served at `/crime-verification/`.

> **Demo mode (config `demo_mode: true`):** the applications are fictional, and the vendor is a
> **mock CrimeCheck provider**. No real search is ever made.

## Folder structure

```
app/agents/credit/crime_verification/
  api/routes.py            HTTP routes (mounted in main.py with require_jwt)
  domain/models.py         request bodies (InitiateRequest, FinalizeRequest), PAN masking
  domain/validation.py     field rules (the same rules are sent to the screen)
  services/
    verification_service   initiate / refresh / finalize (the only code that changes a case)
    queries.py             read side: application screen, lists, dashboard, status, audit, screen config
    views.py               rows -> responses, branch-scoped lookups
    summary_service.py     plain-words summary (local Qwen, checked; rules fallback)
    report_service.py      per-subject PDF report (PyMuPDF)
    access.py              caller from the LOS token: permissions by scope, branches by user
    audit.py, errors.py
  providers/base.py        the vendor boundary (CrimeCheckProvider)
  providers/crimecheck_mock.py   deterministic synthetic answers
  db/database.py, db/schema.py   the agent's OWN PostgreSQL database + migrations 0001, 0002
  demo_data/seed.py        loads the synthetic applications
  frontend/                React + TypeScript + Vite
  docs/                    ARCHITECTURE, API_CONTRACT, BUSINESS_WORKFLOW, EXPLANATION_FOR_MANAGER
Config: app/config/credit_underwriting/crime_verification.yaml (policy),
        crime_verification_wording.yaml (every sentence), crime_verification_demo.yaml (synthetic data, scenarios)
Tests:  tests/agents/test_crime_verification.py (real PostgreSQL), frontend/src/test/app.test.tsx
```

## Run it

1. **Database.** By default, demo mode starts a private local PostgreSQL cluster for this agent under
   `runtime/crime_verification/pgdata` (pgserver, real PostgreSQL binaries; nothing to install). The cluster creates
   the `crime_verification` database, runs migrations 0001–0002, and loads the 11 synthetic applications on first use.
   - To use your own PostgreSQL, put this in `.env`:
     `CRIME_VERIFICATION_DSN=postgresql://user:password@localhost:5432/crime_verification`
   - Create the database first (`CREATE DATABASE crime_verification;`). The migrations run by themselves.
   - Outside demo mode, a missing DSN is an error. There is no SQLite or in-memory fallback.
   - This never touches the LOS case store.
2. **Frontend build** (once, and after UI changes):
   ```
   cd app/agents/credit/crime_verification/frontend
   npm ci
   npm run build
   ```
3. **LOS server:** restart 8010 as usual. Then open:
   - Screen: http://127.0.0.1:8010/crime-verification/
   - API docs: http://127.0.0.1:8010/docs (tag "Credit Underwriting")
4. **Frontend dev server** (optional): `npm run dev` gives http://127.0.0.1:5175, which proxies `/api` to 8010
   (`CRV_API` overrides).
5. **Reset or seed the demo data:**
   - `python -m app.agents.credit.crime_verification.demo_data.seed` (add `--check` to only count);
   - The screen shows no reset button by default (`ui.show_reset`).
   - The screen and the PDF show nothing that says "demo" (owner 2026-10-27): no banner, no scenario picker,
     no reset button (`ui.show_demo_banner`, `ui.show_scenario_picker`, `report.show_demo_notice`, all false).
6. **AI summary:** uses the app's local Ollama model (`OLLAMA_MODEL`). If the model is off, the rules-written summary
   is shown. `CRIME_VERIFICATION_AI_SUMMARY=off` disables it.

## Tests

```
python -m pytest tests/agents/test_crime_verification.py tests/agents/test_crime_verification_prod.py -q -p no:cacheprovider
cd app/agents/credit/crime_verification/frontend && npx tsc --noEmit -p . && npx vitest run
```

The backend tests run against a **real PostgreSQL database**: a fresh database on the test cluster, migrated, seeded,
then dropped. They use real signed LOS tokens and the LOS error handler. Results are in `docs/ARCHITECTURE.md`.
`test_crime_verification_prod.py` runs the agent with `demo_mode: false`: LOS intake, a registered test vendor adapter
(result, pending + refresh, crash, malformed answer), the mock refused, and nothing demo on screen or in the PDF.

## Demo scenarios (crime_verification_demo.yaml)

| Application | Scenario |
|---|---|
| APP-2026-001 | No Risk (both) |
| APP-2026-002 | Low Risk |
| APP-2026-003 | Average Risk → Review required (Pune branch) |
| APP-2026-004 | High Risk → Review required (Pune branch) |
| APP-2026-005 | Multiple Findings (8 synthetic cases, 4 matter types) |
| APP-2026-006 | Vendor Failure: the first attempt fails, the retry succeeds |
| APP-2026-007 | Pending: stays in progress (Pune branch) |
| APP-2026-008 | Applicant No Risk + Co-Applicant Average Risk |
| APP-2026-009 | Applicant High Risk + Co-Applicant No Risk (Pune branch) |
| APP-2026-010 | Previously Waived: ReTrigger through the same confirmation |
| APP-2026-011 | Incomplete Details: the co-applicant's father's name and address are missing, so initiation is blocked |

## More than one co-applicant

- An application has one applicant and up to `subjects.max_co_applicants` (10) co-applicants.
- A person is `(role, seq)` (migration 0003 in the agent's own database). Applicant = `PRIMARY` / 0;
  co-applicants = `CO_APPLICANT` / 1..N.
- In the API, a person is keyed by `party` (`PRIMARY`, `CO_APPLICANT-2`). A request names a co-applicant with
  `seq`; a co-applicant sent without one is co-applicant 1.
- Labels are numbered when there is more than one co-applicant: "Co-Applicant 2" (wording `roles_numbered`).
- One verification may include any subset of them.
- Demo application **APP-2026-012** carries 4 co-applicants.

## Every word in config

- Every word on the screen is in `crime_verification_wording.yaml` under `screen`. The screen reads it through
  `GET /config`.
- `tests/agents/test_crime_verification.py::test_every_text_key_the_screen_uses_is_in_the_config` fails if the
  screen uses a key that is not in the config.
- The rules (limits, statuses, celebration, summary length, report background) are in `crime_verification.yaml`.
