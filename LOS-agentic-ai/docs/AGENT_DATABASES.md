# Agent databases: every agent in its own PostgreSQL database

2026-10-28. The owner asked for this: "real pgadmin me postgres me … sab alag alag db … aage jaake humko sirf real data
connect connection env me".

Five agents each keep their data in their **own** PostgreSQL database. Each database is reached through **one**
environment variable. A laptop, pgAdmin on a test server, and production differ only in those variables. The code and
the tables are the same everywhere.

| Agent | Env variable | Database (config) | Its tables |
|---|---|---|---|
| Eligibility | `ELIGIBILITY_DSN` | `los_eligibility` | `eligibility_cases`, `eligibility_inputs`, `eligibility_runs`, `audit_events`, `agent_logs` |
| Document Desk (credit document policy) | `DOCS_POLICY_DSN` | `los_docs_policy` | `docs_policy_evaluations`, `docs_policy_requirement_results`, `audit_events`, `agent_logs` |
| Bank verification | `BANK_VERIFICATION_DSN` | `los_bank_verification` | `cases`, `sources`, `documents`, `extractions`, `runs`, `rule_results`, `decisions`, `summaries`, `audit_events`, `agent_logs` |
| Crime record verification | `CRIME_VERIFICATION_DSN` | `los_crime_verification` | `crime_verification_applications`, `_parties`, `_cases`, `_subjects`, `_results`, `_reports`, `_summaries`, `_audit`, `agent_logs` |
| Chatbot (applicant copilot) | `CHATBOT_DSN` | `los_chatbot` | `chat_audit`, `chat_turns_log`, `chat_labelled_turns`, `chat_feedback`, `agent_logs` |
| Dedupe and customer history (own schema `dedupe` since 2026-11-03) | `DEDUPE_DSN` | `los_dedupe` | `audit_dedupe_decisions`, `audit_customer_history`, `rule_sets`, `rule_set_active`, `rule_set_changes`, `api_responses`, `agent_logs` |
| Common audit, every agent: schema `common_audit` (was `audit`; `scripts/setup_common_audit.py`) -- `audit_events` = a copy of EVERY agent's audit rows (a trigger on each agent's audit table; config `common_audit`) | `LOS_AUDIT_DSN` | `los_audit` | `audit_events`, `audit_agent_runs`, `audit_issuer_verifications`, `audit_field_corrections`, `audit_risk_decisions`, `agent_logs` (its `audit_dedupe_decisions` / `audit_customer_history` keep the rows from before 2026-11-03; `scripts/move_dedupe_to_own_schema.py` copies them to `dedupe`) |

The databases are configured in `app/config/agent_databases.yaml`: the variable names, database names, pool size,
log level and retention. The LOS case store itself (`LOS_STORE_DSN`) is unchanged. So is the chatbot's encrypted
conversation memory (`chat_turns`), which stays in the LOS store next to its encryption key.

**`los_audit`** holds the audit trails of the agents that have no database of their own. Each of them still writes
its JSONL audit file. `app/observability/audit_log.py` hands every line it writes to `app/agent_db/audit_store.py`,
which recognises the file by its configured path (config `audit.sources`) and stores the line in that source's table,
in the background. Field corrections keep only who corrected which field of which document, and why. The values never
go there; they stay in the label file.

The old files are brought in with `python scripts/copy_local_data_to_agent_databases.py --yes --only audit`.

**An agent database added later** (like `los_audit`): run `python scripts/configure_agent_databases.py --add-missing`.
It writes only the missing DSN lines into `.env`, built from an existing one (same user, password and server), backs up
`.env` first, then creates and migrates the database. It asks nothing and prints no value.

## What every database has

- **Versioned migrations.** Every migration is recorded in `schema_migrations` with its version, description,
  checksum and time. The crime agent keeps its own `crime_verification_migrations` table.
  - Each migration is applied once, under an advisory lock, so two servers starting together do not collide.
  - If someone edits a migration after it was applied, the agent stops with an error instead of re-running or
    skipping it. To change the schema, add the next migration.
- **Typed, clean columns.**
  - Dates and times: `TIMESTAMPTZ`.
  - Money: `NUMERIC`.
  - Structured data: `JSONB`.
  - Flags: `BOOLEAN`.
  - Foreign keys use `ON DELETE CASCADE`, so a case takes everything under it when it is deleted.
  - `CHECK` constraints on the values that have rules.
- **An audit trail**, in `audit_events`, or in the agent's own audit table for bank, crime and chatbot. It records who
  did what, when, and on which case. It holds codes and ids only: never a raw identifier and never document content.
- **`agent_logs`**, the agent's WARNING and above application logs.
  - What is stored: time, level, logger, message, module, function and line, request id, exception, host and process
    id.
  - Every message is masked before it is stored (PAN, Aadhaar, account numbers, phone, e-mail, tokens, keys, and
    passwords inside connection strings).
  - Writing is queued in the background, so a request never waits for it.
  - `logs.retention_days` in the config sets how long rows are kept.
- **`is_demo`** on the rows that demo data creates. Production removes exactly those rows with one command, and never
  touches a real row.

## Setting it up in pgAdmin (step by step)

**Quickest way: one command, interactive.**
```
python scripts/configure_agent_databases.py
```
It asks for the host, the port, the admin user and password, and the app user and password. Passwords are typed
hidden and never printed. Then it:
- creates or updates the app role;
- backs up `.env` and writes the six DSN lines into it, keeping every other line;
- creates the five databases and their tables, and loads the demo data if you say yes;
- prints the `--check` report.

The manual steps below do the same thing by hand.


1. **Create a login role for the app.** In pgAdmin, open Query Tool on the `postgres` database and run:
   ```sql
   CREATE ROLE los_app LOGIN PASSWORD '<a strong password>';
   ```
   This step is optional, but recommended: with it, the app does not run as the superuser.
2. **Edit `.env`.** Copy the block "EVERY AGENT'S OWN POSTGRESQL DATABASE" from `.env.example`. Then:
   - Set `AGENT_DB_ADMIN_DSN` to a user that may `CREATE DATABASE` (for example `postgres`).
   - Set the five agent DSNs to the `los_app` user, with each agent's own database name.
3. **Create the databases and their tables:**
   ```
   python scripts/setup_agent_databases.py --create
   ```
   - This creates the five databases and makes `los_app` their owner.
   - It then applies every migration and prints every table with its row count.
   - If an agent's variable is not set yet, the script prints the exact line to add to `.env`, with `<password>` in
     place of the password.
4. **Optional, for a demo:** load the demo rows, which are marked `is_demo = true`:
   ```
   python scripts/setup_agent_databases.py --seed-demo
   ```
   This fills **every table** of every agent database. First come the demo rows (cases, applications). Then each
   agent's own code runs on them, the same services the screens call:
   - eligibility checks for every demo case, plus a few saved entries;
   - bank: every demo case processed, three cases finalized by the agency with its report, and the AI summaries;
   - crime: every complete demo application verified, with its AI summary and PDF report;
   - Document Desk: evaluations of the LOS demo cases (`DEMO-CASE-001` … `008`; nothing is written to the LOS
     store);
   - chatbot: demo conversations (turns, audit, feedback, a resolved question).

   What runs and how much is set in `app/config/agent_databases_demo.yaml`.
   - Running `--seed-demo` again replaces the demo activity; it never doubles it.
   - Bank and crime reset their demo tables, so they refuse a database that already holds real rows. The other three
     replace only their demo rows.
   - The AI summaries come from the local model (Qwen). If the model is not running, the bank stores none and the crime
     worker records "unavailable". The screens then write the summary when a case is opened.
5. **Check:**
   ```
   python scripts/setup_agent_databases.py --check
   ```
   For each database it prints whether it is reachable and whether its migrations are up to date. For every table it
   prints the rows, split into real and demo.

   It ends with a **Connections** report covering every connection the LOS server makes: `LOS_STORE_BACKEND`,
   `LOS_STORE_DSN`, the five agent DSNs, and the vector DB (Qdrant). For each one it says whether it is set, whether
   its format is valid, and whether the server is reachable. Values are never printed.

   Connection strings must look like `postgresql://user:password@host:5432/database`. The .NET form
   `Host=...;Port=...` is not understood by the PostgreSQL driver.
6. **Restart the LOS server (8010).** The agents now read and write their own databases. The startup line
   `agent logs in DB : …` names the agents whose logs are being stored.

## Going to production: remove the demo data, connect the real data

1. Set `demo_mode: false` in the bank and crime configs.
2. Point the five variables at the production server.
3. Run `python scripts/setup_agent_databases.py --create`, or just the migrate step if the DBA creates the databases.
4. If demo rows were ever loaded, remove them:
   `python scripts/setup_agent_databases.py --purge-demo --yes`.
   - This deletes every `is_demo` row and everything that hangs off it (runs, documents, results, audit rows), and the
     demo files on disk.
   - It never deletes a real row.
5. Real data now flows in through the agents' own paths:
   - the LOS pushes crime-check applications (`PUT /api/v1/credit/crime-verification/applications/{id}`);
   - bank cases come from the LOS case;
   - eligibility reads the LOS case (`ELIGIBILITY_DATA_PROVIDER=los`);
   - the Document Desk and the chatbot record what they evaluate and answer.

Nothing falls back silently:
- In production, an agent without its DSN says so: crime refuses to start, bank answers 503
  `STORE_NOT_CONFIGURED`, eligibility answers "data source unavailable".
- The script never prints a DSN or a password.

## The chatbot's vector DB (knowledge search)

The chatbot's knowledge search does **not** use PostgreSQL. It uses **Qdrant** (`app/knowledge/vector_store.py`,
client `qdrant-client`), with two collections:
- `los_process_knowledge`: process guides and policy text;
- `los_case_context`: case chunks. Every search is scoped to one applicant (`app_id`) and never runs unscoped.

The embeddings come from Ollama `nomic-embed-text` (`EMBEDDING_PROVIDER`, `OLLAMA_EMBED_MODEL`).

| Setting | What it means |
|---|---|
| `QDRANT_URL` (+ `QDRANT_API_KEY`) | A Qdrant server. **Production needs this.** Without it the app refuses to use the vector DB in production, because the embedded store allows one process only and the in-memory store loses the index on every restart. |
| `QDRANT_PATH` | An embedded folder, for one local server. Today that is `runtime/qdrant_demo`, with both collections. |
| neither | In memory: the index is rebuilt on every start. Fine for tests only. |

The collection names can be changed with `QDRANT_CASE_COLLECTION` and `QDRANT_KNOWLEDGE_COLLECTION`.

**Getting a Qdrant server** (`QDRANT_URL`). Pick one:
- **Docker** (Docker Desktop is on this laptop): start Docker Desktop, then run
  `docker run -d --name los-qdrant -p 6333:6333 -v qdrant_data:/qdrant/storage --restart unless-stopped qdrant/qdrant`.
  Set `QDRANT_URL=http://localhost:6333`. The data lives in the `qdrant_data` volume and survives restarts.
- **Production server:** the same image on the bank's server or Kubernetes. Turn on its API key
  (`QDRANT__SERVICE__API_KEY`), and set `QDRANT_URL=https://<host>:6333` and `QDRANT_API_KEY=<key>`.
- **Qdrant Cloud:** a managed cluster. It gives you a URL and an API key; set the same two variables.

Then fill the knowledge index once (and again whenever the guides change):
`python scripts/index_knowledge.py`. Run `python scripts/index_knowledge.py --check` to see the point counts.
Case chunks are indexed by the LOS server itself. Finally, `python scripts/setup_agent_databases.py --check` should
show `vector DB (Qdrant) ok (a Qdrant server)`.

## What runs where

| Piece | Where |
|---|---|
| The DSN of each agent, the shared tables, migration runner | `app/agent_db/core.py` |
| Logs in the database (masked, queued) | `app/agent_db/logs.py`, installed at LOS server start (`main.py`) |
| Background row writer (chatbot) | `app/agent_db/writer.py` |
| Masking | `app/agent_db/redact.py`, shared with the chatbot audit |
| Eligibility migrations, demo seed | `app/agents/eligibility/data/migrations.py`, `schema.sql`, `demo_seed.sql` |
| Document Desk store | `app/agents/docs_verify_credit_agent/services/store_service.py` |
| Bank typed schema | `app/agents/credit/bank_verification/db_schema.py` |
| Crime schema | `app/agents/credit/crime_verification/db/schema.py` (0004 `is_demo`, 0005 `agent_logs`) |
| Chatbot store | `app/agents/applicant/chat_store.py` |
| Setup / check / seed / purge / connections | `scripts/setup_agent_databases.py` |
| The demo activity (every table) | `scripts/demo_activity.py`, config `app/config/agent_databases_demo.yaml` |
| One-time interactive setup (.env) | `scripts/configure_agent_databases.py` |
| Copy local data into the new databases | `scripts/copy_local_data_to_agent_databases.py` |
| End-to-end test | `tests/agents/test_agent_databases.py` (real PostgreSQL, five fresh databases) |

## Notes

- **Bank.** The first PostgreSQL bank store had only TEXT columns. A database with those old tables is refused and
  never converted in place. Export its data, then recreate the database. The SQLite store is still the local demo
  default.
- **Chatbot.** Its JSONL files (audit, turn log, labelled turns, feedback) are still written. The review and training
  scripts read them, and they keep every row if the database is down.
- **Eligibility.** The older variable name `DATABASE_URL` still works. A warning in the log asks you to rename it to
  `ELIGIBILITY_DSN`.
