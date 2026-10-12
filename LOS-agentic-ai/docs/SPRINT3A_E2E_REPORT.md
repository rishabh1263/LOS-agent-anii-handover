# Sprint 3A — End-to-end report (real UI path)

## 1. What "real path" means here

The directive's path is `UI -> .NET -> Python`. **The .NET layer does not exist in this repository** (no `.cs`,
`.csproj` or `.sln`; `docs/ARCHITECTURE_FINAL.md:423` "not built"). It is reported as **ABSENT**, not as passed.

What exists, and what was exercised end to end in a real browser:

```
Chromium (Playwright) -> React UI (Vite :5173) -> Vite proxy /api -> FastAPI (:8010) -> copilot pipeline
     -> in-process MCP / repositories -> PostgreSQL (isolated, synthetic)        JWKS: auth provider (:8020)
```

| Integration | Real or substitute |
|---|---|
| Browser, React UI, chat panel, buttons | **real** (`gz/frontend`, dev server) |
| Login form -> `/api/v1/auth/login` -> RS256 token -> JWKS check | **real**; the identity provider is the repository's development IdP (one configured user) |
| FastAPI chat / action endpoints | **real** (`main.py` under uvicorn) |
| Pipeline, MCP tools, repositories | **real**, in-process as configured (`LOS_MCP_MODE=in_process`) |
| Database | **real PostgreSQL**, an isolated embedded instance started for this sprint (never the dev DB) |
| Qwen / nomic (Ollama) | **real**, under memory pressure (see the performance report) |
| Applicants | **synthetic**: A "Arjun Mehta" (KYC name mismatch), B "Bhavna Iyer" (nothing uploaded) |
| Production web server, production IdP, .NET | **not available** |

## 2. How to run it

1. Isolated DB + synthetic officer: `scratchpad/s3_env.py` (writes `s3.env`: `LOS_STORE_DSN`, `DUMMY_USERNAME`,
   `DUMMY_PASSWORD_HASH`).
2. Services:
   - `ENVIRONMENT=development AUTH_CLIENT_SECRET=<local> python auth_provider_dev.py`
   - `set -a; . s3.env; set +a; ENVIRONMENT=development python main.py`
   - `cd frontend && npm run dev`
3. Seed through the UI proxy: `python scratchpad/s3_seed.py` -> `seed.json`.
4. `python tests/e2e/run_ui_e2e.py seed.json runs/sprint3a/final` -> `ui_e2e.{json,md}` + screenshots.

## 3. Scenarios covered (directive B.2)

| Scenario | Covered by |
|---|---|
| new conversation, status, pending, next, follow-up, correction | session 1 turns 1–10 |
| switch, ask about the new applicant, return, ask after returning | "switch" / "return" turns |
| ambiguous one-word ("number", "ID?") | "ambiguous" turns |
| read-only operation (case list, readiness) | "start", "refuse" |
| authorized action with confirmation + authoritative read-back | "action" (loan amount, read back through `/api/v1/fos/applications/{id}`) |
| cancel before execution; stray "yes" / "do that" / "continue" | "action-cancel", "stray-yes", "stray" |
| a button in a reply (ask: link) | "button-ask" |
| malformed / hostile input; unknown case | "malformed", "unauthorized" |
| browser refresh | "refresh" |
| second session, isolation | session 2 + the last session-1 turn |
| no token / garbage token / another officer | API probes in the same script |
| failed tool, timeout, LLM unavailable | **real**: the model timed out repeatedly under memory pressure; replies fell back to deterministic text with no invented facts (performance report P2). **Not injected**: no fault-injection hook exists on the real path; the in-process suites cover tool failures |
| service restart | not run (the conversation is persisted; covered in-process by `test_live_fos_scenarios` "H-resume") |

## 4. Results

See section 6 (final run). The defects found on this path and fixed are in `SPRINT3A_FAILURE_MATRIX.md`
(E1–E9).

## 5. Blockers

- **.NET layer**: absent from the repository.
- **Production IdP / web server**: not in the repository; only the dev IdP and the Vite dev proxy were exercised.
- **Fault injection** (tool error, timeout on demand) on the real path: no hook; reported as not injected.

## 6. Final run

(appended below)

Command: `python tests/e2e/run_ui_e2e.py seed.json runs/sprint3a/final` -> `runs/sprint3a/final/ui_e2e.{json,md}`.

**34 UI turns, 32 passed. All 8 hard gates passed.**

| Gate | Result |
|---|---|
| no token -> 401 | **pass** (401) |
| garbage token -> 401 | **pass** (401) |
| another officer reads case A | **pass** (403) |
| another officer chats with case A | **pass** (403) |
| cancel / stray "yes" / "do that" / "continue" never wrote | **pass** (store unchanged) |
| confirmed write verified in the store | **pass** (700000 -> 800000, read back through the API) |
| no cross-applicant leak (switch, return, second session) | **pass** |
| every turn rendered | **pass** |

The 2 failed turns:

| Turn | Result | Status |
|---|---|---|
| "ID?" with a case open | answered the pending/KYC view instead of asking which ID | open product decision (one-word ambiguity policy) |
| browser refresh | (in that run) the chat was not reachable after a reload | **FIXED since** (E10): diagnosed and fixed; see the refresh verification below |

Before (first UI run, before the fixes): 27 turns ran before the script stopped, and 14 of them failed. "Go back to
the previous one" closed the case and every later turn lost its context (E3); every UI turn started a new
conversation (E2). The UI is unusable on a fresh install without E1.


### Refresh (E10), diagnosed and fixed after the final run

Diagnosis, in a real browser:
- After a reload the officer stays logged in, but the app returns to "Select case": the step was React state only.
- The chat history WAS kept (localStorage `chatbot.conversations.v1`).
- The E2E script also waited on the page `load` event, which the 3D robot's model keeps pending.

Fix: `frontend/src/App.tsx` keeps the step in `sessionStorage`. It is cleared on logout (a true -> false auth
change only, never during a refresh's session restore). The script waits for `commit`.

Verified in Chromium after the fix:
- the assistant is back **1.1 s** after the reload, with no click and no case-select screen
- the history is restored
- the next turn works
- logout clears the step and shows the login page

Full rerun: `runs/sprint3a/final2/ui_e2e.json`.

### Rerun after the refresh fix (`runs/sprint3a/final2`, run alone, enough free memory)

**34 turns, 32 passed, 8/8 hard gates passed.**
- UI p50 / p95 / max: 6.2 s / 14.1 s / 15.7 s
- API p50 / p95: 2.0 s / 6.9 s
- Write verified: 1000000 -> 1100000.

| Failed turn | Detail |
|---|---|
| "ID?" | open product decision (unchanged) |
| full-session reload | `Page.reload` did not even reach navigation **commit** within 90 s after 33 turns |

What is known about the full-session reload:
- The renderer stayed responsive (evaluate round trip 5–14 ms).
- There is no `beforeunload` handler in the app.
- The same reload in a short session commits in 0.5 s, and the chat is back in 1.1 s (two dedicated runs).
- Suspected, **not confirmed**: headless Chromium renders the 3D robot (model-viewer / WebGL) in software, and a
  long session leaves the tab unable to navigate.

Status: **refresh behaviour fixed and verified in dedicated runs; the full-session reload in the headless harness
is UNRESOLVED (environment suspected).** A headed browser or a GPU machine would settle it.

A second observation from the same investigation: in headless runs the turns' "generating" phase varied from
2.7 s to 83 s for replies the API returned in 0.07–4.5 s. Before sending, 1.6 s passed on the client. This points
to client-side CPU contention in this harness (the 3D model rendered in software). It is not seen in the API
numbers.
