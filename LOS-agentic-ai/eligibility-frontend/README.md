# Eligibility Frontend

A clean demo screen for the LOS **eligibility agent**. Pick a case, adjust the loan terms, choose how income is
assessed (salaried / ITR / cash profit / gross profit) and any top-up, then **Check eligibility**. The result shows:

- the **decision** and the **eligible amount** against the amount requested;
- **what decided the amount**: FOIR and LTV gauges with their limits, on the request and on the eligible amount;
- the **recommendation**: the offer range (bucket), EMI, and the longer-tenure option;
- every **rule** (age, income, bureau score, DPD, FOIR, LTV) with its value and limit;
- **what it would take** to get the full request, the next steps, and the full working (reasons, exposure, run id).

Every figure comes from the backend (`/api/v1/eligibility/*`); the page computes nothing itself.

## Run

Needs Node 18+ (no `npm install`: no dependencies).

```powershell
# 1. the LOS backend (dev, auth off) on :8010
$env:AUTH_ENABLED = "false"
$env:AUTH_LOCAL_SCOPES = "read_applicant read_application read_documents read_verification read_pending_items read_next_action create_applicant update_applicant create_application upload_document agents:execute"
python -m uvicorn main:app --host 0.0.0.0 --port 8010

# 2. this frontend (another terminal)
cd eligibility-frontend
node server.js
# -> http://localhost:5180
```

`server.js` serves the page and forwards every `/api/...` call to the backend (`BACKEND_URL`, default
`http://127.0.0.1:8010`), so the browser talks to one origin and no CORS change is needed. Port: `PORT` (default 5180).

## Files

| File | What |
|---|---|
| `server.js` | static server + API proxy (zero dependencies) |
| `public/index.html` | the screen |
| `public/styles.css` | design tokens, light + dark |
| `public/app.js` | API calls, form, result rendering |

## Backend endpoints used

| Call | Use |
|---|---|
| `GET /eligibility/form` | products, income methods and their fields, top-up types, collateral / occupancy options |
| `GET /eligibility/cases` | the case picker |
| `GET /eligibility/cases/{id}` | the case's data + any saved entries (pre-fill) |
| `PUT /eligibility/cases/{id}/inputs` | **Save entries** |
| `POST /eligibility/check` (`detail: true`) | **Check eligibility** (full working) |

Demo data: the 36 `eligibility_cases` rows in `app/agents/eligibility/schema.sql`. In development the backend creates
them in its embedded PostgreSQL when `DATABASE_URL` is unreachable (`app/agents/eligibility/dev_db.py`).
