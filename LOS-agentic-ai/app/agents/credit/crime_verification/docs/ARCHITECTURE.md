# Architecture: Crime Record Verification

## Where it sits

```
Browser (/crime-verification/, React)
   │  same-origin fetch, LOS login (no role header, no own login)
   ▼
LOS server main.py (8010) ── require_jwt ── app/api/errors.py (one error shape)
   │  /api/v1/credit/crime-verification/*    (tag: Credit Underwriting)
   ▼
api/routes.py ─► services/access.py   caller = verified token: permissions by scope, branches by user (config)
   │
   ├─► services/verification_service  initiate · refresh · finalize   (writes)
   │        └─► providers/ (CrimeCheckProvider) ─► crimecheck_mock (demo) │ real vendor adapter (later)
   ├─► services/queries                read views (branch-scoped)
   ├─► services/summary_service        plain-words summary: local Qwen, checked, rules fallback (background worker)
   └─► services/report_service         per-subject PDF (PyMuPDF), stored file + DB reference
   ▼
db/ ── the agent's OWN PostgreSQL database (CRIME_VERIFICATION_DSN, or the demo's embedded cluster)
       migrations 0001 (core tables) · 0002 (summaries, report content key)
```

The LOS case store is **not** changed. The only LOS file touched is `main.py`: one router include and one static
mount, next to Bank Verification.

## Tables (db/schema.py)

| Table | Holds | Key constraints |
|---|---|---|
| crime_verification_applications | synthetic LOS application header, branch | PK application_id |
| crime_verification_parties | PRIMARY / CO_APPLICANT details on the application | PK (application_id, role); role CHECK; UNIQUE (application_id, pan) |
| crime_verification_cases | one verification request: vendor, workflow status, kind (INITIAL/RETRY/RETRIGGER/WAIVER), idempotency key + request hash, who/when, waiver, final decision | status CHECK; idempotency_key UNIQUE; UNIQUE (case_id, application_id) |
| crime_verification_subjects | one person in a case: the confirmed snapshot (PAN masked in it), status, provider ref/state | FK (case_id, application_id) → cases **and** FK (application_id, role) → parties, so a person cannot attach to the wrong application. A partial UNIQUE index on (application_id, role) while INITIATED/PROCESSING blocks duplicates in flight |
| crime_verification_results | provider result per subject: risk, counts per source, per matter type, summary, full payload | UNIQUE subject_id; risk/outcome CHECK |
| crime_verification_reports | generated PDF: file ref, sha256, version, content key | FKs to case, subject, result; UNIQUE (result_id, version) |
| crime_verification_audit | every action: actor, event, time, safe metadata (no PAN, no name) | FKs to application, case |
| crime_verification_summaries | the case summary: AI text or why not (refused / unavailable), facts hash | FK case |

## Statuses

- **Subject:** INITIATED → PROCESSING → COMPLETED | REVIEW_REQUIRED | FAILED (WAIVED for a waiver).
  REVIEW_REQUIRED means the risk is in `review_required_risks` (Average, High, Very High).
- **Case:** a **workflow** status derived from its subjects (config `case_status_from_subjects`). It is never a
  combined risk. Each person's risk stays on that person.
- The screen's DRAFT / READY / CONFIRMATION_REQUIRED are client states before the server is called. The server
  creates nothing until the confirmed request arrives.

## Security

- **Identity:** the verified LOS token only. In local dev with auth off, it is the LOS local identity.
- **Permissions:**
  - `access.scopes` maps a scope to permissions (`los.credit.underwrite` / `los.write` = officer, `los.read` = viewer).
  - In demo mode only, `access.demo_grants` adds the reset permission for the local developer.
- **Branches:** `access.branches` lists the branches each user may open (`*` = all).
  - An application, case, subject or report outside them answers 404, exactly like one that does not exist.
- **PAN:**
  - The full PAN never leaves the server. The screen gets `XXXXXX001Q`.
  - An officer may type the full PAN; it must equal the application's.
  - The audit metadata and the model's facts carry no PAN or name.
- **Errors:** the LOS handler. An unexpected exception is logged; the screen gets a generic message.
- **Idempotency:**
  - The same `Idempotency-Key` with the same body replays the case (200, `replayed: true`).
  - The same key with a different body gives 409.
  - The database index stops two submissions for one person while one is in flight.

## The provider boundary

`providers/base.py` defines `CrimeCheckProvider`:

- `initiate(ProviderRequest) -> ProviderResponse`;
- `status(reference, state) -> ProviderResponse`.

The mock (`crimecheck_mock.py`) answers deterministically from the scenario. A real, authorised vendor adapter
implements the same two methods and is registered in `providers/__init__.py`; then config `vendors.<id>.provider` points to it.
Nothing else changes.

## The AI summary

When every subject of a case is done, the case is queued. One background worker:

1. sends the model only the results (role, status, risk, counts, sources with findings);
2. checks the answer: every figure must be in the facts; no banned word ("approved", "criminal record",
   "clean record"…); length limit;
3. stores the text.

The screen shows the rules-written summary at once and switches to the AI text when it is ready. The PDF includes
whichever is current, and is rewritten when the summary changes (`content_key`). Measured on the local Qwen: 15.5 s
from initiation to the AI summary on screen.

## Test results (2026-10-27)

| Suite | Where it ran | Result |
|---|---|---|
| `tests/agents/test_crime_verification.py` | **real PostgreSQL** (fresh database on the test cluster), real signed LOS tokens, LOS error handler | **48 passed** |
| `frontend/src/test/app.test.tsx` (vitest, jsdom, mocked fetch) | browser simulation, no server | **13 passed** |
| `tsc --noEmit`, `vite build` | | clean |
| Browser walkthrough (Playwright, my preview on 8043 with the same router and the embedded database) | dashboard, applications, APP-2026-008 popup → confirm → results, validation block on APP-2026-011, real Qwen summary (15.5 s), summary inside the PDF, no horizontal overflow at 390 / 768 / 1280 px, dark mode | passed, no console errors |

## Migration 0003: several co-applicants (2026-10-27)

What changes:
- `crime_verification_parties` and `crime_verification_subjects` gain `seq`. The applicant is (PRIMARY, 0);
  co-applicants are (CO_APPLICANT, 1..N), as a CHECK on both tables.
- The parties' primary key becomes (application_id, role, seq).
- The subject's foreign key to its party, the "one row per person per case" key, and the "never twice in flight"
  partial unique index all move to (…, role, seq).

Existing data keeps its meaning: the one co-applicant of an older database becomes seq 1. There is a test for this:
`test_a_database_with_the_old_one_co_applicant_shape_upgrades`.

The migration runs by itself on the first use of the agent's own database. The LOS case store is not touched.

The AI summary grows with the number of people (config `ai_summary.sentences_per_person`, `max_sentences`,
`num_predict_per_person`, `max_chars_per_person`). Its check is per sentence:
- figures must be from the facts;
- totals and risks must belong to the people the sentence names;
- "not available / pending" (wording `pending_words`) is refused about a person whose result is in.
