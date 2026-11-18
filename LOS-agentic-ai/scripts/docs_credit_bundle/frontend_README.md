# Credit Document Desk: dashboard

A one-screen dashboard for the Credit Document Policy Agent. Every status comes from the backend; the page decides
nothing.

## Run

Node 18+. There is no `npm install`.

```powershell
$env:PORT="5182"; $env:BACKEND_URL="http://127.0.0.1:8020"; node server.js
```

Open http://localhost:5182 and enter a case ID (`/` focuses the search box). `?case=CASE-…` opens a case directly.

| env | default | |
|---|---|---|
| `PORT` / `HOST` | 5181 / 0.0.0.0 | where the dashboard listens |
| `BACKEND_URL` | http://127.0.0.1:8010 | the backend (the bundle's `start.ps1` uses :8020) |
| `BACKEND_TOKEN` | (none) | a bearer token sent when the browser sends none |
| `UPSTREAM_TIMEOUT_MS` | 30000 | how long the backend may take |
| `MAX_BODY_BYTES` | 65536 | the largest request body forwarded |
| `STATIC_MAX_AGE` | 0 | seconds browsers may cache assets (0: always revalidate with an ETag) |

The server proxies only `/api/v1/credit/document-readiness/*`. It sends CSP, nosniff and frame-deny headers, and refuses path traversal, other methods and large bodies.

## The screen

**Rail (left side):**

* the case search
* recent cases (this browser only)
* the 11 rules in 3 sections; click a rule to open its drawer
* a light / dark toggle

**Banner:**

* the route: move ahead, move ahead with notes, refer to higher authority, or awaiting approval
* the Critical / Approval / To collect / Verified counts; click a count to filter the cards
* whether underwriting can start

**People:**

* one card for the applicant and one for the co-applicant
* each card shows the person's employment and every document, problems first
* click a document for its details

**Underwriter brief:**

* the AI or rules note
* the Critical, Awaiting approval and To collect lists; click an item to open its document
* Copy (the brief as text)
* the next step, and the higher authority when the case is referred

**Header:** copy the case ID, see when the case was checked, or Re-check it.

The page fits one screen on a laptop; a long list scrolls inside its card. Below 960 px wide, the page stacks and scrolls.
