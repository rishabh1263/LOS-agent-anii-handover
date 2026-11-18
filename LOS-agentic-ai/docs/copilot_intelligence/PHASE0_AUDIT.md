# LOS Copilot — Phase 0 audit and Phase 1 baseline (2026-11-02)

Scope: the EXISTING FOS copilot (`POST /api/v1/fos/copilot`). Read from the code, then measured on an isolated server
(127.0.0.1:8765, a throwaway clone on the TEST Postgres, every real DSN blank, synthetic identities only).
OCR / bank statement / ITR modules are out of scope and were not touched by this work.

## 1. Actual architecture (not the documented one)

* Entry: `app/api/routes/fos_api.py:1419` `copilot()` -> `_copilot_json` (`:2941-3837`). Second entry
  `POST /copilot/query` (`app/api/routes/copilot_api.py:1461`) calls the same `answer_question`.
  Streaming (`/fos/copilot/stream`) wraps the finished answer as SSE; it is not token streaming.
* **The chat does NOT run on LangGraph.** `app/agents/applicant/graph.py` is imported only by
  `tests/agents/test_fos_routing.py`. The live LangGraph (`app/orchestration/graph.py`) serves document extraction.
* The chat is an ordered chain of ~10 early-return layers in `_copilot_json`:
  safety / guardrail -> typo fix -> clear-reading + Qwen follow-up reader -> task layer -> case insights -> vague pick
  -> case form -> definitions -> **simple pipeline** (`copilot/simple/pipeline.py`) -> **Qwen tool agent**
  (`capabilities/qwen_agent.py`) -> meaning layer (embeddings, Qwen only when unsure) -> general layer
  (`capabilities/general.py`, handbook + optional `general_llm`) -> workspace / actions / glossary ->
  `answer_question` (`copilot/agent.py` rules + `llm_router` for UNKNOWN) -> ~15 reply decorators incl. `polish`.
* Model: `qwen3:4b-instruct-2507-q4_K_M` (YAML overrides the code default `qwen2.5:3b`), `num_ctx` 2048, per-call
  timeouts 2.5–12 s, a whole-turn model budget of 6 s (`copilot_reply.yaml:324`).
* Embeddings: `nomic-embed-text` on the SAME Ollama, used on most turns by the meaning layer.

## 2. Tool lifecycle

| Picker | Tools | Schema to the model | Executes? |
|---|---|---|---|
| Qwen agent (`qwen_agent.py`) | 11 case tools + `accepted_documents`, `case_record`, `handbook` | hand-written from YAML, pasted into the prompt (`choice: compact`); `format: json` with NO schema | yes, `asyncio.gather`, results inserted verbatim |
| Fallback planner (`fallback_planner.py`) | 16 read-only intents | enum JSON schema (constrained) | **disabled** (`intent_catalogue.yaml:49`) |
| LLM router (`llm_router.py`) | 10 labels | enum | never runs a tool; label becomes a canonical question re-routed by rules |

Findings:
1. **Tests never exercise the agent tool path**: `tests/conftest.py:269` sets `COPILOT_QWEN_AGENT=false`; no test calls
   `tool_schemas`, `_compact_calls` or `_run`. This is how "selects tools in isolation, not at runtime" went unseen.
2. Tools silently vanish when: no case open (case tools dropped); `intent_catalogue.yaml` unreadable (`meaning._load`
   returns `{}`); a YAML tool name that is not a `kind: case` intent with a canonical question; `copilot_reply.yaml`
   unreadable; < 1.5 GB free RAM; provider unreachable. None of these is logged as "agent had no tools".
3. Handler code switches on hardcoded names (`case_record`, `accepted_documents`, `handbook`, `application_details`):
   a rename in YAML disables the tool silently.
4. `SPENT` is set before the agent's model call: a failed agent call turns off the router, meaning chooser and polish for
   the rest of the turn.
5. Tool execution inside `gather` has no timeout; exceptions are dropped without a log; partial failures are not told.
6. Result check only rejects UNKNOWN / GUARDRAIL_BLOCKED / empty — a clarification reply could be shown as a fact.
7. The model ledger in the response (`observability.model_ledger`) does **not** count the Qwen agent's calls: the golden
   run reported `model_calls: 0` while the server logged 9 agent timeouts.
8. No write path: write intents only PROPOSE an action that needs an `action_id` confirmation (safe).

## 3. Routing / handbook priority

* A handbook question **can reach `general_llm`**: `general.py:1145-1180` uses a passage only if "confident" AND
  `_about()` (short questions need every subject word in the passage), else `_general_llm` (`:1170`); a "knowledge"
  reading also drops the case id (`:920`). Today only `general_llm.enabled: false` prevents it; `COPILOT_GENERAL_LLM=true`
  re-opens the regression recorded in `docs/AUDIT_2026-10-11.md:256`.
* Measured: "can I upload several documents at once?" (a how-to question) answered from live data
  ("Signature is still missing") — golden U2.
* "What is KYC?" answers from the glossary with no handbook source — golden U1.

## 4. Analytics / aggregation — the capability gap

Present (deterministic): count by status / missing document, closest to CPA, most pending, highest / lowest amount,
compare 2+ cases (`case_insights.py`), list filters (`case_list.py`), pending/completed/rejected counts
(`workspace.list_view`). **Absent**: SUM, AVG, MIN/MAX of a metric, general GROUP BY, any date period ("this month",
"last month"), period-over-period comparison. `ListQuery.created_from/created_to` exists but chat never sets it.

## 5. RAG

* Handbook answers use **BM25** (`knowledge.yaml backend: lexical`), not dense: nomic was "confident" on 10/10 off-topic
  questions. Dense cutoff default is 0.57 (raised from 0.53 on 2026-10-10; `runs/eval/dense_calibration.json` still
  says 0.53). No RRF, no reranker (both measured worse).
* The historical P@1 0.805 / MRR 0.854 (41 questions) do not match the current file `runs/eval/retrieval.json`
  (24 questions, larger corpus): BM25 P@1 0.375 / MRR 0.491, nomic 0.50 / 0.612. RAG answer metrics
  (`runs/rag_now.json`): hit 23/24, top-1 21/24, citation 26/26, false confidence 0/10.
* No-answer: `knowledge_answer.answer()` returns `NO_ANSWER` with `refused=True` when retrieval is not confident.

## 6. Memory / sessions

* Keyed `(JWT subject, server conversation id)`, stored in Postgres `conversation_state`, TTL 30 min (24 h with session
  memory), a new conversation when the case changes. Structured state exists (frame, party, list query with
  created_from/to, listed case ids). Cross-officer isolation is tested.
* Risk: every unauthenticated caller shares subject `"anonymous"`.
* Follow-ups do not carry analytic context: "how many were rejected?" after a list starts a fresh query;
  "compare with September" has no handler.

## 7. Phase 1 baseline (measured 2026-11-02)

Environment: 16 GB laptop, no GPU (`size_vram: 0`), Ollama on CPU: prompt ingest **~55–65 tokens/s**, generation
~20 tokens/s. A 200-token prompt takes ~5 s, 900 tokens ~15 s — above every configured model timeout (2.5–4 s).

Golden A–W (`evals/copilot/golden_aw.py`, 41 cases, the suite's own checks) run with synthetic identities
(`samples/lPan.jpg` is a real PAN and is not processed; runner: setup replaced, checks unchanged), Phase-3 flags off
as the harness runs it:

| Arm | Passed | P50 | P95 | Note |
|---|---|---|---|---|
| Historical record | 41/41 | 19 ms | 96 ms | not reproducible: no results file exists |
| Model live (CPU) | 14/41 | 4.2 s | 16.9 s | 17 model timeouts (agent 9, polish 6, knowledge 2) — most failures are latency |
| **Model down, before fix** | **6/41** | 4.5 s | 8.6 s | **every turn HTTP 500 / empty** |
| Model down, after fix 1 | 34/41 | 0.70 s | 2.27 s | 7 left (below) |

**Root cause #1 (fixed):** with Ollama down, `meaning.rank` let `EmbeddingError` escape from the simple pipeline
(`simple/pipeline.py:190`), so every chat turn failed after two ~2 s refused connections.
Fix: `rank()` returns `[]` (as its docstring promised) and the embedder uses the shared availability breaker
(`app/llm/availability.py`) — one short probe per cooldown. Tests: `tests/agents/test_copilot_embeddings_down.py`.

Remaining golden failures (model down): A1 cold first request (6.9 s); J1 wording ("Still waiting on ..." lacks
"not"); J2 "move it ahead" asks which action instead of naming blockers; M3 Hinglish reply in English (language lock
is a Phase-3 flag, off in this profile); P1 intent label `NOT_IN_SCOPE` vs expected `OUT_OF_SCOPE` (answer correct);
**U1** no handbook source on "What is KYC?"; **U2** how-to question answered from live data.

The 30-question live chat evaluation (14 / 7 / 9) has no file in the repo; a new 30-question set is built in Phase 1b.

## 8. Prioritised plan

1. Reliability: model / embeddings down never fail a turn (done); count agent calls in the model ledger; timeouts and
   logging around tool execution.
2. Routing: handbook-first for how-to / policy questions (U2), definitions carry the handbook source (U1), and
   `general_llm` can never override a handbook answer even when enabled.
3. Analytics: one deterministic, read-only aggregation tool over the case store (count / sum / avg / min / max, group by
   stage / status / product / branch, date periods, period-over-period), strict schema, no model-written SQL.
4. Follow-up state for analytics: keep metric + filters + period, change only what the user changed.
5. Tool path tests that run with the agent ON (schemas, parsing, execution, missing tools).
6. Latency: on CPU-only hosts, keep the model off the clear-message path (it already is) and do not add model calls.
