# Sprint 3A — Performance report

## 1. Environment

- One Windows 11 laptop, 15.5 GB RAM, typically 2–5 GB free during the runs.
- Running together: Chromium (Playwright), the Vite dev server, uvicorn, Ollama with Qwen 2.5 3B (Q4_K_M,
  ~2 GB) and nomic-embed-text, the embedded test PostgreSQL, the IDE and a browser.
- These are development conditions, not a sized server. Every number below comes from this machine, under memory
  pressure.

## 2. What was measured, at which boundary

| Boundary | How | Where |
|---|---|---|
| UI round trip (typed -> reply rendered and stable) | Playwright: Send click -> last bubble text stable for 0.6 s | `ui_ms` in `runs/sprint3a/*/ui_e2e.json` |
| API round trip (browser -> proxy -> FastAPI -> response) | Playwright `expect_response` on `/api/v1/fos/copilot` | `api_ms` in the same file |
| Rendering (UI - API) | difference of the two | derived |
| Agent (pipeline core) | the backend's `copilot_turn` log line `latency.total_ms` and `model_ledger` | backend log |
| Model calls / timeouts | `model_ledger` (`calls`, `timeouts`, `ms`); availability log ("marked slow") | backend log |

Not measured separately: retrieval (no knowledge-heavy turns in the E2E set), proxy overhead (in the API number),
and the .NET layer (it does not exist).

## 3. Findings

**P1 — rendering, not the server, dominated the UI time (fixed).** In the first UI run, UI time grew with the
conversation, from 4 s to 99 s, while the agent's `total_ms` stayed at 45–2,615 ms.
- Cause 1: every typing-animation tick re-rendered every bubble and re-parsed its markdown. Fix: `MessageBubble`
  memoized.
- Cause 2: the typing effect had no cap (a 2,000-character table typed for 5–10 s). Fix: capped at ~1 s.

**P2 — server time is model-timeout time under memory pressure (documented, not changed).** API time was
0.2–23 s, while the agent core was ≤ 2.6 s. The rest is LLM steps waiting out their budgets:
- polish: 4 s
- applicant-agent generation
- the general LLM

The log shows `TimeoutError` -> "fell back to deterministic" and "Model provider marked slow for 2s". Only 3 model
HTTP responses were logged in one session that ran ~60 turns: most attempts timed out. The 2 s slow-cooldown
(`app/llm/availability.py`) was set by measurement on an unloaded machine (documented there: 2 s = 67 % model use
vs 47 % at 5 s). Under sustained pressure it lets every request pay the full budget again.
- **Recommendation**, not implemented without a comparative run: escalate the cooldown after N consecutive overruns
  (e.g. 3 within 60 s -> 30 s). That keeps the measured single-overrun behaviour and caps the waste when the model
  is genuinely unable to answer.
- **Measurement needed:** the same E2E set with the escalation on and off, at least 3 runs each.

**P3 — the deterministic path is fast.** Turns that never reach the model answer in 0.1–1 s at the API (open
case, edit / confirm / cancel, guardrail refusals, the readiness report).

## 4. Numbers

Filled in from the final UI run (`runs/sprint3a/final/ui_e2e.json`), section 5.

## 5. Final run

(see below)

Same machine, same synthetic data, sequential turns, warm model.

| Metric | Before fixes (first UI run) | After fixes (final UI run) |
|---|---|---|
| Sample | 27 turns | 34 turns (32 with an API timing) |
| UI p50 / p95 / max | 26.9 s / 91.0 s / 98.9 s | **7.6 s / 14.8 s / 21.9 s** |
| API p50 / p95 | not measured in that run | 3.4 s / 10.9 s |
| Rendering (UI - API) p50 / p95 | — | 3.5 s / 4.4 s (includes the script's 0.6 s stability wait and the ~1 s typing effect) |

Caveat: the before run had a different turn mix after turn 14, because the E3 defect closed the case. The
before/after numbers show the direction (rendering no longer grows with the conversation), not a controlled A/B.
One final run, not several: treat it as one sample.

Remaining server time is model time. Turns that reach Qwen (router / polish / generation) wait out their budgets
under memory pressure (P2). Deterministic turns answer in 0.1–1 s at the API.
