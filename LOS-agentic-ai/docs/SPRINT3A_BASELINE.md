# Sprint 3A — Baseline

Written 2026-10-10 from the current checkout. Every claim is labelled **verified** (run or read in source),
**inferred**, or **absent**.

## 1. The actual architecture (verified from source)

The directive assumes `Officer/UI -> .NET API -> Python Agent Service`. **There is no .NET layer.**

- `docs/ARCHITECTURE_FINAL.md:423`: "**.NET 8 / REST API layer** | not built | no `.cs` files in the repo; the API
  is FastAPI (`main.py`)".
- A workspace scan (`*.csproj`, `*.sln`) up to the workspace root found nothing.
- `gz/login-api` is a separate Python FastAPI service; it is not on the chat path.

The real path, hop by hop:

| # | Hop | Source / entry point | Contract | Identity / context | Status here |
|---|---|---|---|---|---|
| 1 | Officer -> React UI | `gz/frontend` (Vite 8, React 19): `builders/auth/LoginPage.tsx`, `builders/api-tester/pages/ProcessPage.tsx` mounts `<Chatbot>` | login form (stage, username, password); chat panel | access token in the auth context; chat id = conversation id (localStorage) | **verified (browser)** |
| 2 | UI -> API | Vite dev proxy `/api -> http://127.0.0.1:8010` (`frontend/vite.config.ts`) | same-origin HTTP | `Authorization: Bearer` | **verified** (dev only; no production web server config in the repo) |
| 3 | Login | `POST /api/v1/auth/login` (`app/api/routes/auth_api.py:login`) -> dev token (`app/security/dev_idp`) | username / password / stage | single configured user `DUMMY_USERNAME` + `DUMMY_PASSWORD_HASH` (env); rate-limited | **verified**; a development identity provider, not a production IdP |
| 4 | Token check | `require_jwt`: RS256 against the JWKS at `JWT_JWKS_URL` (`auth_provider_dev.py` on :8020 in dev) | JWT | subject, scopes, stage claim | **verified** |
| 5 | Chat | `POST /api/v1/fos/copilot` -> `fos_api._copilot_json` | `{action, message, chat_id, case_id?, reply_language}` -> `{markdown, tts, request_id}` | `chat_id`, subject; open case held in the workspace state | **verified** |
| 6 | Actions | `POST /api/v1/fos/action` (`action:` links) | `{href, chat_id}` -> reply / file / upload target / route | same | **verified** (HTTP) |
| 7 | Pipeline | guardrail -> vague -> case form -> general -> meaning (nomic + Qwen chooser) -> workspace -> conversation state -> agent -> post steps -> polish (Qwen) | — | — | **verified** (Sprint 2 audit, section 1) |
| 8 | Tools | in-process MCP (`LOS_MCP_MODE=in_process`), repositories | — | per-case authorization (`workspace.authorize`, `security/access`) | **verified** |
| 9 | Data | PostgreSQL (`app/store/postgres_repo`) | — | — | **verified** (isolated test DB for this sprint) |
| 10 | Orchestration | LangGraph `app/agents/applicant/graph.py` | — | — | **absent from the chat path** (document / KYC processing only) |

Correlation: every reply carries `request_id`; the backend logs one `copilot_turn` JSON line per turn with
`correlation_id`, intent, tools, model calls and per-stage latency. The UI does not send a correlation id of its own.

## 2. How the Sprint 2 harness differs from the real path

| Sprint 2 harness | Real path (this sprint) |
|---|---|
| FastAPI `TestClient`, in-process | browser -> Vite proxy -> uvicorn over TCP |
| test token minted in the test | real login form -> `/api/v1/auth/login` -> JWKS validation |
| no UI | React chat panel, typed messages, clicked buttons, page refresh, second browser context |
| throwaway test DB per run | isolated embedded PostgreSQL started for this sprint (scratchpad), never the dev DB |

## 3. Environment (verified)

- Windows 11, 15.5 GB RAM, typically 2–5 GB free; Qwen 2.5 3B (Q4_K_M, ~2 GB) and nomic-embed-text in Ollama.
- Services for the E2E runs: auth provider :8020, backend :8010 (isolated `LOS_STORE_DSN`, synthetic officer),
  UI :5173.
- Synthetic applicants (seed script through the UI proxy + repository):
  - A "Arjun Mehta": PAN / DL / bank statement verified, KYC NAME FAIL (bank statement holder "KARAN SETHI").
  - B "Bhavna Iyer": nothing uploaded.

## 4. Baseline tests (verified)

| Suite | Command | Result | Duration |
|---|---|---|---|
| Related regression (139 chatbot files + unit tests, fake model) | `pytest $(cat subset) tests/core/test_chatbot_fixes_20261009.py -n 2` -> `runs/sprint2/after_related.log` | **3305 passed, 6 failed, 13 skipped, 1 xfailed** | 1 h 17 min |
| Live in-process conversation suite (real Qwen) | `LOS_LIVE_LLM_TESTS=1 SPRINT2_TAG=final pytest tests/integration/test_live_sprint2.py` | 25/25 matrix, 27/27 session; 15 model calls; p50 412 ms, p95 5484 ms | 108 s |

This regression run is the last one on the checkout; after it only `vague.py` (upload_in_top) and one test changed,
and their files were re-run (149 passed, 1 timing failure that passes alone). It is not re-run here: it takes over an
hour on this laptop, and nothing on its path has changed since.

## 5. The six regression failures, classified

| Test | Classification | Evidence |
|---|---|---|
| `test_contextual_fragments::…upload_link` | **Confirmed product defect (Sprint 2 regression) — fixed** | `previous_in_top: 1` narrowed the upload check; `upload_in_top: 3` added; passes |
| `test_contextual_fragments::…asks_once` | **Disputed expectation — resolved by the owner's decision** (2026-10-09 "seedha jawab do") | test updated to the new rule; passes |
| `test_one_pipeline::…same_replies` | **Test isolation / order-dependent flakiness** | passes alone and in its own `-n 2` group (149 passed) |
| `test_question_rewrite::…routed_again` | **Test isolation / order-dependent flakiness** | same as above |
| `test_step6b_llm_router::…without_the_model` | **Resource-sensitive performance check** | the logic assertions pass; 203.28 ms against a 200 ms limit under the full parallel run; alone 2/2 pass (2.6 s, 2.9 s test time) |
| `test_copilot_variant_evals` | **Existing baseline failure** (fails on HEAD) | one-word cases expect a clarification without a read; open owner decision |

Not hidden: the full suite is not green. Two failures are flaky under the parallel run, one is a timing threshold,
and one is a pre-existing evaluation set waiting on a product decision.
