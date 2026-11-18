# Credit Document Readiness Dashboard

A one-screen demo of the **Credit Document Policy Agent** (`app/agents/docs_verify_credit_agent`), in the eligibility
frontend's design language (Inter, one indigo accent, white cards, light and dark).

1. **Start:** a single search box. Enter a case ID; nothing about other cases is shown.
2. **The case:**
   * **Case card:**
     * who the loan is for (the applicant & co-applicant, each with their open count)
     * the loan, stage and employment
     * the **route**:
       * Move ahead: nothing open
       * Move ahead with notes: only non-critical items open
       * Refer to higher authority: a critical item is open
       * Awaiting authority's approval: only an exception is open
     * whether credit underwriting can start
   * **Document checklist** (the main tab): one row per required document, one column per person.
     * Each cell shows ✓ Verified, ✕ Missing, ↻ Replace, ! Issue, … In review, Awaiting approval or Waived.
     * Click a cell for that document's details.
     * Open items that are not about one document (for example, an ITR's computation) are listed under "Also open".
   * **To do** tab: every open item, person by person, tagged Missing / Fix needed / Being verified / Awaiting approval.
   * **Right column:** the rules-clear score (Pass / Not critical / Critical), the AI summary (it states the route and
     names each person), the next step and, when the case is referred, the higher authority.
   * **Sidebar:** the 11 rules in 3 sections with progress bars. Click a rule for a drawer with its plain explanation
     and every check.

Keys: `/` focuses the search. `?case=CASE-…` in the URL opens that case directly.

Every status comes from the backend. The page computes nothing itself. The page never scrolls (checked at 1440x900
and 1366x768); a long list scrolls inside its own box.

## Run

Needs Node 18+. There is no `npm install`, because nothing is installed.

```powershell
# 1. the LOS backend (dev, auth off) on :8010
$env:AUTH_ENABLED="false"; $env:ENVIRONMENT="development"; $env:AUTH_LOCAL_SCOPES="los.credit.underwrite"
python -m uvicorn main:app --port 8010

# 2. demo data (once): 12 synthetic cases + their AI notes
python scripts/seed_docs_credit_demo.py --ai

# 3. this dashboard (another terminal)
cd docs-credit-frontend
node server.js
# -> http://localhost:5181   then enter a case ID, e.g. CASE-D0C500000003
```

`server.js` serves the page and forwards only `/api/v1/credit/document-readiness/*` to the backend
(`BACKEND_URL`, default `http://127.0.0.1:8010`). The browser therefore talks to one origin, and no CORS change is
needed. Port: `PORT` (default 5181).

## Backend routes it uses

| Route | What |
|---|---|
| `POST /api/v1/credit/document-readiness/{case_id}/rules` | The case, the readiness answer, all 11 rules (PASS / WARN / FAIL / N/A / OFF) and their sections (`app/agents/docs_verify_credit_agent/config/dashboard.yaml`). |

## Files

| File | What |
|---|---|
| `server.js` | Static server + API proxy (zero dependencies). |
| `public/index.html` | The screen. |
| `public/styles.css` | Layout and colours. One screen, no page scroll. |
| `public/app.js` | API calls and rendering. |
