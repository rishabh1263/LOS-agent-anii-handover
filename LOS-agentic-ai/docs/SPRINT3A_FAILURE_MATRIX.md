# Sprint 3A — Failure matrix (found on the real UI path)

Every row was reproduced in a real browser (Playwright / Chromium) against the running stack:
UI :5173 -> Vite proxy -> FastAPI :8010 -> isolated PostgreSQL, synthetic officer and applicants. Reproduction for
any row: start the stack (`SPRINT3A_E2E_REPORT.md` section 2), then
`python tests/e2e/run_ui_e2e.py <seed.json> <out>`. HTTP-only repro: `s3_probe.py "open {A} || ..."` (scratchpad).

| # | Scenario (sanitized) | Expected | Actual (before) | Root cause | Sev | Fix | Regression test | After |
|---|---|---|---|---|---|---|---|---|
| E1 | Open the UI | login page | blank page, Vite overlay "Failed to resolve import three" | `@google/model-viewer` 4.3.1 needs peer `three@^0.183.0`; not declared, not installed | **Critical** (UI unusable on a fresh install) | `three@^0.183.0` added to `frontend/package.json` (installed with `--legacy-peer-deps` because of a separate pre-existing TypeScript 6 vs `openapi-typescript` peer conflict) | the UI run itself (login step) | login renders, no page errors |
| E2 | "open A" -> "status" -> "pending" -> **"why?"** | why A is held (name mismatch) | "I can help with the application details…" (UNKNOWN) | the client sends `context: {}`; the server recalled the chat's context only when `context` was None, so every UI turn started a NEW conversation (turn_id 1 each time) | **High** (every follow-up broken in the UI) | `fos_api._copilot_json`: an empty / partial context is merged with the recalled one (client keys win); frontend no longer sends an empty context | `tests/integration/test_sprint3a_context.py` (fails on HEAD: "a new conversation per turn", passes now) | "why?" -> "The name on the PAN, ARJUN MEHTA, does not match the bank account holder name, KARAN SETHI." |
| E3 | open A, open B, **"go back to the previous one"** | back to A | B **closed**, case list shown; the 12 turns after it asked "which case?" | `exit` phrases contain "back" and are checked before `previous`; "previous one" was not a `previous` phrase | **High** (context loss cascades) | workspace: exit never fires when the text also says `previous`; English phrases added to `phrases.previous` (config) | `test_chatbot_fixes_20261009::test_going_back…` | returns to A |
| E4 | after B is opened, **"pending"** | B's pending list | "No Bank Statement document has been uploaded" (the previous bank-statement question replayed on B) | `vague.contextual` let the previous question win whenever it was in the fragment's topic | Med | a fragment with its own clear reading (meaning `accept_with_lead`) wins over the previous question; topic words ("docs") still follow the conversation | `test_contextual_fragments` (15 passed) | B's pending documents |
| E5 | **"no, I meant the bank statement"** | the bank statement's status | "That isn't in the knowledge base yet" | meaning read the English correction as `knowledge_documents` (0.825) | Med | correction examples under `document_status` (intent_catalogue.yaml) | `test_chatbot_fixes_20261009::test_an_english_correction…` | "Bank Statement is verified…" |
| E6 | a 30-turn chat | each reply shows when it arrives | 17–99 s per turn, growing with the conversation | every typing-animation tick re-rendered EVERY bubble and re-parsed its markdown (O(turns × ticks)) | **High** (perceived latency) | `MessageBubble` memoized (only the changed bubble renders) | UI run (ui_ms vs api_ms) | see the performance report |
| E7 | a long reply (table) | shown promptly | typed for 5–10 s after the server answered | the typing effect had no cap (4 chars / 12 ms) | Med | the typing effect is capped at ~1 s | UI run | see the performance report |
| E8 | case-select screen | truthful access copy | "any valid combination can be accessed" | stale copy; the backend enforces ownership | Med (security copy) | text corrected | — | corrected text |
| E10 | **browser refresh** | back where the officer was, chat available | "Select case" screen; the chat only after "Create new case" again (the history survived) | the app step lived in React state only | **High** (refresh lost the workspace) | step kept in `sessionStorage` (`App.tsx`), cleared on logout | the UI run's "refresh" turn | assistant back 1.1 s after reload, history restored |
| E9 | the E2E script's read-back | — | 401 mid-run | dev access tokens are short-lived (the script held one token) | Test defect | re-login per read-back | — | — |

Not fixed (documented):

| # | Observation | Why not fixed |
|---|---|---|
| N1 | "ID?" with a case open answers the pending/KYC view instead of asking which ID | open product decision (the one-word ambiguity policy for "number" / "ID" / "date", also in `test_copilot_variant_evals`) |
| N2 | "what about this one?" / "continue" answer the generic capability list | no clear reading; a clarification-style answer, no wrong data |
| N3 | server latency 2–15 s on turns that call Qwen (router / polish) under memory pressure | model-bound; see the performance report |
| N4 | stray "·" separators between reply chips in the UI | cosmetic |
