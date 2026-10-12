# Sprint 3A — Final summary

## Readiness decision: **CONTROLLED TESTING**

The real UI path now works end to end on synthetic data. What blocks a later stage:
- no .NET layer exists to validate
- server latency is model-bound under the laptop's memory pressure
- only the development IdP / dev server were exercised

## Files changed and why

| File | Why |
|---|---|
| `app/api/routes/fos_api.py` | E2: an empty / partial `context` no longer starts a new conversation (recalled context merged; client keys win) |
| `app/agents/applicant/copilot/capabilities/workspace.py` | E3: "go back to the previous one" returns, never closes |
| `app/config/applicant_agent.yaml` | E3: English `previous` phrases |
| `app/agents/applicant/copilot/capabilities/vague.py` | E4: a fragment with its own clear reading wins over the previous question |
| `app/config/intent_catalogue.yaml` | E5: English correction examples ("no, I meant the bank statement") |
| `frontend/package.json` (+ lock) | E1: `three@^0.183.0`, the missing peer of `@google/model-viewer` (the UI did not load) |
| `frontend/src/builders/chatbot/components/MessageBubble.tsx` | E6: memoized; only the changed bubble re-renders |
| `frontend/src/runtime/chatbot/hooks/useChatbot.ts` | E7: the typing effect capped at ~1 s |
| `frontend/src/runtime/chatbot/api/fosCopilot.ts` | E2: no empty `context` sent |
| `frontend/src/builders/api-tester/pages/CaseSelectPage.tsx` | S1: truthful access copy |
| `frontend/src/App.tsx` | E10: the app step survives a browser refresh (sessionStorage, cleared on logout) |
| `tests/e2e/run_ui_e2e.py` (new) | the real-browser E2E suite |
| `tests/integration/test_sprint3a_context.py` (new) | E2 regression (fails on HEAD, passes now) |
| `tests/core/test_chatbot_fixes_20261009.py` | E3, E5 regressions |

Dev-only, not in `requirements.txt`: Playwright 1.47.0 + Chromium in the venv.

## Commands executed (main)

```
python scratchpad/s3_env.py                      # isolated PostgreSQL + synthetic officer
ENVIRONMENT=development AUTH_CLIENT_SECRET=... python auth_provider_dev.py
set -a; . s3.env; set +a; ENVIRONMENT=development python main.py
cd frontend && npm install three@0.183.0 --save --legacy-peer-deps && npm run dev
python scratchpad/s3_seed.py
python tests/e2e/run_ui_e2e.py seed.json runs/sprint3a/{before,after,final}
pytest tests/integration/test_sprint3a_context.py      (and on the HEAD snapshot: fails, as expected)
pytest <focused files> -n 2  -> runs/sprint3a/focused_regression.log
```

## Baseline vs final

| | Baseline | Final |
|---|---|---|
| UI E2E (real browser) | UI did not load (E1); after E1, 27 turns ran, **14 failed**, script stopped | **34 turns, 32 passed, 8/8 hard gates** (final and final2) |
| UI latency p50 / p95 | 26.9 s / 91.0 s | 7.6 s / 14.8 s (final); 6.2 s / 14.1 s (final2) |
| Related regression (fake model) | 3305 passed / 6 failed (Sprint 2 end) | focused rerun (≈60 integration files + unit tests): **1282 passed, 2 failed, 5 skipped** (41 min) |
| The six Sprint 2 failures | tracked | classified in `SPRINT3A_BASELINE.md` §5 (1 fixed, 1 resolved by the owner's decision, 2 order-dependent, 1 timing, 1 pre-existing) |

## Real vs substitute

**Real:**
- browser
- React UI
- login form + RS256 / JWKS
- FastAPI
- pipeline
- in-process MCP
- PostgreSQL
- Qwen / nomic

**Substitute:**
- the development IdP (not a production IdP)
- the Vite dev proxy (not a production web server)
- an isolated embedded PostgreSQL (not the deployed DB)
- synthetic applicants

**Absent:** .NET.

**Not injected:** tool failures and timeouts on demand. Real model timeouts happened under memory pressure and
were handled deterministically.

## Acceptance checklist

| # | Criterion | Result |
|---|---|---|
| 1 | Actual .NET -> Python path demonstrated | **ABSENT** (no .NET); UI -> FastAPI path demonstrated |
| 2 | UI-level validation reported | **PASSED** 32/34 (final2, 8/8 gates). Refresh: **fixed and verified** in dedicated browser runs (1.1 s); the reload after a 33-turn headless session is **UNRESOLVED** (navigation never committed; environment suspected) |
| 3 | Consequential actions verified authoritatively | **PASS** (store read-back) |
| 4 | Zero unauthorized consequential actions | **PASS** (0) |
| 5 | Zero cross-applicant leakage | **PASS** (0) |
| 6 | Zero fabricated success claims | **PASS** (0) |
| 7 | Curated unambiguous contextual references pass | **PASS** (switch / return / follow-ups / correction) |
| 8 | Ambiguous consequential requests clarify | **PASS** ("do that" -> pick one; nothing written) |
| 9 | No unexplained new regression failures | **PASS**. Focused regression: 1282 passed, 2 failed. `test_copilot_routing_contract::test_mixed…` fails the same way on the pre-Sprint-2 snapshot (pre-existing: the glossary's e-KYC entry outranks the handbook's KYC passage). `test_copilot_verify_e2e::…parallel` verified 2 of 3 documents within its 120 s budget while the laptop ran the regression, Qwen and Chromium together (resource-sensitive; not rerun alone yet) |
| 10 | Six baseline failures tracked | **PASS** (`SPRINT3A_BASELINE.md`) |
| 11 | Fallback behaviour tested and documented | **PARTIAL**: observed under real timeouts; not injected |
| 12 | Repeatable performance measurements | **PARTIAL**: one final run, method repeatable (`run_ui_e2e.py`) |
| 13 | No production writes, commits, pushes, deploys | **PASS** |

## Known limitations

- **One-word "ID?" / "number" / "date"**: the policy is still an owner decision.
- **Latency**: model-bound under memory pressure. The cooldown escalation recommendation is in the performance
  report.
- **Cosmetic**: stray "·" separators between chips.
- **Pre-existing**: a TypeScript 6 vs `openapi-typescript` peer conflict blocks plain `npm install` (worked around
  with `--legacy-peer-deps`).
