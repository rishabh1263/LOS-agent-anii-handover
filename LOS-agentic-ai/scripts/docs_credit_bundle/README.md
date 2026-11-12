# Credit Document Desk

**One question:** is this loan file's document set complete and usable for credit underwriting, and if not,
what is still open, for whom, and who decides?

This folder holds both halves:

* **backend:** the Credit Document Policy Agent (a FastAPI service).
* **frontend:** the dashboard (a zero-dependency Node server).

All policies are **SAMPLE**: the limits are set for the demo until the credit team signs them off.

```
credit-document-desk/
  README.md      this file
  start.ps1      seeds the demo once, starts the backend (:8020) and the dashboard (:5182)
  check.py       the acceptance check: positive + negative, against the running services
  backend/       main.py, app/ (the agent + only the LOS code it runs on), scripts/seed_docs_credit_demo.py,
                 requirements.txt, .env.example, runtime/ (case store + files, created on first run)
  frontend/      server.js, public/ (index.html, app.js, styles.css, theme.js)
```

## 1. Install (once)

You need **Python 3.12 (64-bit)** and **Node 18+**. Unzip to a **short** path, for example `C:\credit-document-desk`.

```powershell
cd C:\credit-document-desk\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
```

The frontend needs nothing installed. There is no `npm install`.

## 2. Run

```powershell
cd C:\credit-document-desk
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

On the first run, `start.ps1` seeds 14 demo cases. It then opens two windows:

* backend at http://127.0.0.1:8020/docs
* dashboard at **http://localhost:5182**

Try `CASE-D0C500000005`, `…07`, `…09` and `…12`. `?case=CASE-…` in the URL opens a case directly.

To run the two by hand:

```powershell
# backend
cd backend; .\.venv\Scripts\python.exe scripts\seed_docs_credit_demo.py --quiet   # once
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8020
# dashboard (another window)
cd frontend; $env:PORT="5182"; $env:BACKEND_URL="http://127.0.0.1:8020"; node server.js
```

## 3. Check it

```powershell
backend\.venv\Scripts\python.exe check.py            # --backend / --frontend to point elsewhere
```

About 900 checks run in about 15 s. It ends with "`N checks passed, 0 failed`". What it checks:

* **Positive:**
  * Each of the 14 demo cases gives exactly its expected answer: status, route, critical and to-collect counts, the gate, and every open item.
  * The route follows the items. Critical comes only from missing, not-accepted, rejected or mismatched documents, or a pending exception.
  * Every reason is a full sentence, says whose document it is, and has no template leak.
  * The rules board has all 11 rules once each, in 3 sections.
  * The checklist cells agree with the open items.
  * The note never contradicts the route.
  * The same case asked twice, or 36 times at once, gives one answer.
  * A warm answer takes under 2 s.
* **Negative:**
  * The service refuses an unknown or lower-case ID, SQL-looking, quoted, traversal, null-byte, unicode, emoji, overlong or script IDs.
  * It refuses a caller that sends its own status, documents, stage or policy.
  * It refuses a bad, overlong, numeric or list correlation ID.
  * It refuses an array, string, broken, deeply nested, 1 MB or form body.
  * It refuses GET, PUT, DELETE and PATCH, and unknown routes.
  * No case list is ever served.
  * No answer leaks a traceback, path or SQL.
* **Dashboard:** the CSP and nosniff headers are set, assets come with an ETag, the proxy gives the backend's answer untouched, and static traversal, POST to a page, other APIs and over-limit bodies are refused.

## 4. The API

```
POST /api/v1/credit/document-readiness/{case_id}          the answer
POST /api/v1/credit/document-readiness/{case_id}/rules    the answer + rules board, sections, checklist (dashboard)
Body (optional): {"correlation_id": "your-trace-id"}      any other field -> 422
Auth: Bearer JWT with scope los.credit.underwrite (local demo: tokens off, .env)
```

The answer has these parts:

* `overall_status`: `COMPLETE`, `ACTION_REQUIRED` or `EXCEPTION_PENDING`.
* `credit_underwriting_allowed`
* `requirements[]`: each item has its rule, check, status, severity, whose it is, a reason and an action.
* `summary.route`:
  * `MOVE_AHEAD`
  * `MOVE_AHEAD_WITH_NOTES`
  * `REFER_TO_AUTHORITY`, for the credit manager (ACM / RCM)
  * `AWAIT_APPROVAL`
* `summary.ai_summary`: the AI note, or the rules note.
* `audit`: the policy version and file hash, the input hash and the evaluation ID.

Errors come back as `{"detail": {"code", "message", "request_id"}}`:

* 404 `CASE_NOT_FOUND`
* 422 `POLICY_NOT_CONFIGURED`, `POLICY_AMBIGUOUS`, `POLICY_NOT_APPROVED`, `CONTEXT_INCOMPLETE`, or a bad request
* 403 `INSUFFICIENT_SCOPE`
* 503 `CASE_STORE_UNAVAILABLE` or `AUDIT_UNAVAILABLE`
* 504 `EVALUATION_TIMEOUT`

## 5. The 11 rules (all in config)

| Section | Rule | Checks |
|---|---|---|
| Document coverage | RULE-01 Product Requirements | the product's document list for the credit stage |
| | RULE-02 Applicant Coverage | each person on the loan has their own documents (the co-applicant on their own employment) |
| | RULE-03 Missing Documents | every required document is on file and verified |
| | RULE-04 Conditional Requirements | documents the case's details call for (for example, self-employed means ITR and business photos) |
| | RULE-05 Alternative Documents | only accepted document types count |
| Validity and quality | RULE-06 Period and Freshness | statement months and age, salary slip age, ID expiry |
| | RULE-07 Supporting Documents | documents that must come with another (ITR with its computation) |
| | RULE-08 Cross-Stage Reconciliation | earlier-stage (FOS / CPA) documents are reused, never asked again |
| | RULE-11 File Size and Format | the real type and size of each uploaded file |
| Approvals and data | RULE-09 Exceptions and Waivers | only an APPROVED exception (maker-checker) that the policy maps to the document counts |
| | RULE-10 Compliance and Audit | every check is recorded with the policy version and inputs |

**Critical** (refer to the credit manager) applies only when a required document is missing, not of an accepted type, rejected, or mismatched (possibly fake), or when an exception is awaiting approval. Everything else is **to collect**: the case moves ahead with notes.

### Where to change things

Nothing is hard-coded.

| I want to change | File (under `backend/app/agents/docs_verify_credit_agent/`) |
|---|---|
| a limit (days, months, file size), what is critical, a rule on / off, exception → document | `policies/<product>.credit.json` |
| which documents a product needs | `backend/app/config/policies/*.yaml`, `applicant_agent.yaml`, `stage_requirements.yaml` |
| any sentence, rule name or error message | `config/reasons.yaml` (checked at start-up: a missing key or unknown placeholder stops the service by name) |
| route words, next step, the AI note | `config/wording.yaml` |
| dashboard sections, plain-word help, labels | `config/dashboard.yaml` |
| switch rules off without editing a policy | env `DOCS_CREDIT_RULES_OFF=RULE-07,RULE-11` (RULE-10 cannot be) |

## 6. Production notes

* Set `ENVIRONMENT=production` and turn tokens on (`JWT_*` in `.env`). `AUTH_ENABLED=false` is refused outside development.
* Point `LOS_STORE_DSN` at the LOS case store. The embedded store is for development only.
* Set `LOS_DATA_ENCRYPTION_KEY`: production refuses to start without it.
* The SAMPLE policies are refused in production unless `DOCS_CREDIT_ALLOW_SAMPLE_POLICY=true`. Approve real ones (`status: APPROVED` plus the approval block) first.
* The AI note is optional (`OLLAMA_HOST` / `OLLAMA_MODEL`). The rules answer never waits for it, and a note that contradicts the facts is refused and replaced by the rules note.
