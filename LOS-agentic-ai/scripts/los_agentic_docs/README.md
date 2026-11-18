# Credit Underwriting: the agents

The credit underwriting agents of the LOS, as one service:

- **eligibility**
- **docs policy** (credit document readiness)
- **dedupe**
- **bank verification**
- **crime record verification**
- **legal verification**
- **underwriting**, the parent agent. It reads what the agents recorded and writes the credit assessment. Its code is here (the agents use its bureau, policy and schemas); its API runs on the LOS server, where the KYC / income / financial results its tools read live.

Every rule each agent decides with, and every decision it takes, is stored in **one PostgreSQL database, `los_agentic`**, with **one schema per agent**, and with an append-only audit trail.

## Layout

```
credit_underwriting/
  main.py                 the service: every agent's API, start-up checks, /health, /ready
  conftest.py  pytest.ini the tests (each agent's own folder: <agent>/tests)
  .env.example            every setting the service reads (copy to .env)
  requirements.txt
  eligibility/            code, api.py, conf/ (its rules: the seed of the first version), tests/, README.md
  docs_policy/            policies/ (one file per product)
  dedupe/
  bank_verification/
  crime_verification/
  legal_verification/
  underwriting/           the parent agent: assessment, planner, graph, memo, bureau
  shared/
    db/                   los_agentic: DSN, schema per agent, migrations, rules store, audit, logs
    config/               agent_databases.yaml (databases, rules), preflight.yaml, ...
    security/ store/ api/ llm/ observability/ ...   login (JWT), the LOS case store, errors, the local model
  scripts/                setup_agent_databases, rules, preflight_production, move_agents_to_los_agentic
```

Each agent is layered the same way:

- its API (`api.py` or `api/`) checks the caller and the input;
- its services decide;
- its own database tables (in its schema) keep the result.

Nothing in an agent reaches another agent's tables.

## Start (a new machine)

1. Install Python 3.12, then:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and set at least:
   - `LOS_AGENTIC_DSN=postgresql://<user>:<password>@<host>:5432/los_agentic`
   - `LOS_STORE_DSN` (the LOS case store)
   - the JWT settings
   - `ENVIRONMENT`
3. Create the database and every agent's schema:
   ```
   python scripts/setup_agent_databases.py --create
   ```
   This needs `AGENT_DB_ADMIN_DSN`, a user that may `CREATE DATABASE`. Without `--create` it only migrates.
4. Put the rules in force. In production they are never loaded on their own:
   ```
   python scripts/rules.py import <rule set> --by "<your name>" --note "<why>" --activate
   ```
   Do this for every rule set listed by `python scripts/rules.py list`.
5. Check everything before go-live. It must print `READY for production`:
   ```
   python scripts/preflight_production.py
   ```
6. Run:
   ```
   uvicorn main:app --host 0.0.0.0 --port 8010
   ```
   Then check `/health` and `/ready`.

**Coming from the old layout** (one database per agent)? Run `python scripts/move_agents_to_los_agentic.py --copy`. It copies every agent's database into its schema and never changes the old databases. Then remove the per-agent `*_DSN` lines from `.env`.

## The rules live in the database

```
python scripts/rules.py list                     every rule set: the version in force, its status
python scripts/rules.py export <set> --out x.yaml   the version in force, to edit
python scripts/rules.py import <set> --file x.yaml --by "..." --note "..."   a new version: checked, stored, NOT in force
python scripts/rules.py activate <set> <hash> --by "..." --note "..."        put it in force (or back)
python scripts/rules.py history <set>            who put which version in force, when, why
```

- A version is checked by the agent's own validator before it is stored.
- A newly activated version is in force everywhere within `rules.cache_seconds` (60 s), with no restart.
- Every decision row stores `rule_set_hash`: the exact rules it was made with.
- Deployment settings (where a folder is, timeouts, who may do what) stay in the agent's `conf/` file (`file_sections` in `shared/config/agent_databases.yaml`).

## Audit and logs

- **Audit:** every agent writes who did what, when, on which case, in its schema's audit table. Audit rows can be added, **never changed or deleted**; a database trigger refuses it. The only exception is demo rows, removed by `--purge-demo`.
- **Logs:** WARNING and above are stored in `agent_logs`, masked (PAN, Aadhaar, account, phone, e-mail, tokens).
- **Never logged:** a DSN, a password or a document's content.

## Before production: what is NOT done by this code

| Item | Today | Needed |
|---|---|---|
| Credit bureau (CIBIL etc.) | demo provider only | the bureau's API and contract; an adapter in `underwriting/bureau/` |
| Crime record vendor | mock provider | the vendor's API; an adapter in `crime_verification/providers/` |
| Legal agency reply | demo flow | the agency integration |
| MiFin / LMS dedupe sources | HTTP client present, URL / template not set | the endpoints and credentials (`MIFIN_DEDUPE_URL`, ...) |
| Every agent's rules | DEMO / UNSIGNED values | the credit team confirms them; import + activate (`status: CONFIRMED` / `signed_off: true`) |

`preflight_production.py` reports every one of these as FAIL until it is done.

## Tested here, and where else

Every agent's own tests are in its `tests/` folder (`python -m pytest`). The LOS end-to-end pipeline (KYC -> income -> eligibility -> fraud -> underwriting on one case, `los/flow.py`) is not part of this project; it is tested in the LOS repository.
