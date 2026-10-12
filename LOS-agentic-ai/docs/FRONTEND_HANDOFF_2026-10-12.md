# Frontend handoff -- LOS Copilot backend, 2026-10-12

For the frontend team. Read with `frontend_handoff/API_CONTRACT.md` (the contract), which is still **version 1.0**.

## 1. What changed for the UI

**No new endpoint, no request or response field changed, contract version unchanged (1.0).** Everything new arrives
inside the existing `markdown` (with the usual `ask:` / `action:` links) on `POST /api/v1/fos/copilot`,
`/copilot/stream` and `/fos/action`. The contract tests (`tests/integration/test_frontend_contract*.py`) still pass.

Two HTTP behaviours to handle (both use the existing error body `{detail, error:{code, message, retryable, request_id}}`):

| Status | When | UI |
|---|---|---|
| **413** `PAYLOAD_TOO_LARGE` | a request body above ~26 MB (declared `Content-Length`), refused before it is read | show `error.message`; for uploads say the file is too large (limit 25 MB) |
| **429** `RATE_LIMITED` | more than 600 requests a minute from one signed-in session (and the chat's own 30 messages a minute) | show `error.message`; it now carries a **`Retry-After`** header (seconds) -- wait that long before retrying |

## 2. New chat behaviour the UI will see (no UI change needed)

| The officer types | The reply |
|---|---|
| "how many of my cases have KYC problems?" | a count over the officer's own cases, then the matching rows with Open links |
| "which of my cases is closest to CPA?" / "rank my cases by pending documents" | a ranked list with Open links |
| "compare CASE-A and CASE-B" | the two cases side by side; another officer's case: "I can't find that case" |
| "show me 2 recent kyc issue cases" | exactly 2 rows, newest first, + "Show more" |
| "Priya ka pending" when the officer has several Priyas | "Which one did you mean?" with the cases as `ask:` links |
| a question outside LOS ("write me a poem", cricket, politics) | a polite out-of-scope line + what the bot can do -- **no outside information** (general model answers are OFF) |
| "back to the case: what is pending?" | answered on the open case (it no longer closes the case) |
| "close this case please" | closes (an exit word inside a question no longer closes) |
| a readiness question | the CPA readiness report -- each document named once |

## 2b. Queries and deviations (no new API)

**Queries (FOS <-> CPA)** -- unchanged, in the chat: "raise query to CPA: <text>" / "raise query to FOS: <text>" ->
draft -> **Confirm** (`confirm_write` link) -> `QRY-...`; "reply to QRY-...: <text>"; "resolve query QRY-..."; "query
status". The Raise Query button keeps using `POST /api/v1/los/cases/{case_id}/queries`.

**Deviations** -- from the owner's **deviation master** (`docs/frontend/DEVIATION_MASTER.ods`, read-only; imported into
`app/config/deviation_master.yaml` by `scripts/import_deviation_master.py`): 69 deviations, each with its category
(NFA / Other critical parameter) and the authorities who may approve it (ACM / RCM / ZCM / Head Credit).

| Who | Types in the chat (`POST /api/v1/fos/copilot`) | What happens |
|---|---|---|
| CPA / credit officer | "raise deviation LTV > 70%: <reason>" | draft (deviation, category, approvers, reason) -> **Confirm** -> `DEV-...` PENDING_APPROVAL; the case cannot move forward |
| | "raise deviation itr" (unclear) | the top matches from the master as `ask:` links -- tap one; nothing is written on a guess |
| | "raise deviation beauty parlour" (no reason) | the bot asks why; the next message is the reason -> draft -> Confirm |
| anyone on the case | "show deviations" | every deviation on the case with status and approvers |
| approver (ACM / RCM / ZCM / Head Credit) | "approve deviation DEV-...: <reason>" / "reject deviation DEV-...: <reason>" | Confirm -> an approval goes to a **second person** (four eyes); a rejection acts at once |

Or the existing route `POST /api/v1/los/cases/{case_id}/deviations/{deviation_id}/decision` (`{decision, justification}`):
**403** `DEVIATION_AUTHORITY_INSUFFICIENT` (wrong level for this deviation), **403** `RAISER_CANNOT_DECIDE`, **202**
`PENDING_CHECK` (approval waiting for the checker: `POST /api/v1/approvals/{approval_id}/decision`).

Authority comes from the login token's `roles` (ACM / RCM / ZCM / HEAD_CREDIT -- the identity team's real role names
go in `app/config/queries.yaml deviations.authority_roles`). The master sheet has 4 internal disagreements (listed at
the end of `deviation_master.yaml`); the matrix (Sheet2) is used until the business settles them.

## 3. How to run the backend for UI work

- Python 3.12 venv, `pip install -r requirements.txt` (**no new runtime dependency** was added).
- `requirements-train.txt` lists training-only libraries (torch, transformers, peft) for an optional offline
  experiment -- **do not install them for the app**.
- Start as before (`main.py`, port 8010; the Vite dev server proxies `/api`).
- The meaning layer needs Ollama with `nomic-embed-text` (as before); the new meaning adapter file is
  `app/models/meaning_adapter.npy` (config `intent_catalogue.yaml adapter.enabled`).

## 4. What to check in the UI (15 minutes)

1. Log in as a test officer; "show my cases" -> list; tap Open on one.
2. "how far along is this application?", "what exactly is blocking CPA?", "who is the applicant and what loan did they
   ask for?" -- answers about the open case.
3. "what is pending?" then "why?" -- the reason, not the list again.
4. "back to the case: what is pending?" -- the case stays open.
5. "how many of my cases have KYC issues?", "compare <two of your case ids>".
6. "write me a poem" -- out of scope, no poem.
7. Send 31 chat messages within a minute -- the 31st is refused politely; the UI does not hang.
8. Upload a file over 25 MB -- a clear "too large" message.

## 5. Known limits (backend)

- Understanding unseen phrasings: 73% on a fresh held-out set (was 55%); the remaining misses mostly confuse close
  topics (status vs stage, KYC vs document status). The answer is still a real record, never invented.
- Live answers take 3-4 s on this CPU-only test machine for some turns; production hardware will differ.
- The model-directed planner is OFF (50% correct, 6 s on CPU -- not good enough).

## 6. Test evidence (backend, 2026-10-12)

- Focused suites after the last fix: 81 passed (case list, follow-ups, case history, sticky context, query/deviation
  e2e, eligibility/credit chat, chat raise-query); 164 passed / 1 skipped (general, routing, vague, case list).
- Held-out conversations (offline): set C 19/20, set O (LOS-only boundary) 17/17; meaning set F 73% (was 55%).
- Run 10 = chatbot subset (181 files), not the full suite (owner's call, for time): 4808 passed / 1 failed / 8 skipped in 27.5 min (runs/final_regression10_subset.log). The 1 failure is
  test_copilot_verify_e2e::test_process_mode_reads_each_document_once... -- a heavy real-OCR test (3 real bank
  statements in worker processes); under -n 3 load the case store timed out (CASE_STORE_UNAVAILABLE) and the
  worker did not finish in 300 s. It also hung at the same line in Run 9. Classified ENVIRONMENT / LOAD (OCR path
  not changed by this work). SOLO RERUN: it hung again (>20 min, stopped) -- so it is NOT only load: a
  PRE-EXISTING hang of the process-mode verify executor on this Windows machine (also in Run 9, before
  port 3). Open defect for the document/OCR owner; not caused by the chatbot changes.
  The document / OCR suites were not re-run after the last port (those files were not changed by it).
- Not production-ready: the 90% understanding target was not reached; deviation authority mapping and 4 conflicting
  rows of the deviation master need business confirmation; eligibility uses the DEMO policy.
- Close topics (status vs stage, KYC vs document status): when the bot is unsure it answers BOTH in one reply
  (or follows the previous question) -- no extra buttons, no UI change.
