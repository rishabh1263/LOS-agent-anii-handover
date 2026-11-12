# LOS Copilot — end-to-end audit: why it is not yet an industry-level assistant (2026-11-02)

Evidence-based: every number below was measured on this repository today (isolated server, synthetic data, the same
laptop) or counted from the code. Companion documents: `PHASE0_AUDIT.md` (architecture / tools / RAG / memory) and
`REPORT.md` (today's fixes and before/after).

**Short version (Hinglish):** bot ke andar safety, data-correctness aur LOS tools bahut mazboot hain — case ke sawaal
0.1–2 s me sahi aate hain. Lekin "samajhne" ka kaam abhi ~45 phrase-lists, ~140 regex aur ~57 early-return layers ki
chain karti hai, jo har nayi phrasing par patch maangti hai (340 tareekh-wale patch comments). Model (Qwen 3 4B) CPU par
chalta hai — 20 token/sec — isliye model wale jawab 4–9 s lete hain, aur model ON karne se golden correctness 36/41 se
23/41 gir jaati hai. Industry-level bot ka core ek tez model + ek planner + tools + achhi retrieval + bada eval hota hai;
yahan core rules ka jungle hai aur model sirf kinaron par.

---

## 1. Scorecard (measured today)

| Area | Measured | Industry bar | Gap |
|---|---|---|---|
| Case data via tools | 5/5 correct, 0.1–2 s | correct, < 2 s | ✅ meets |
| Portfolio analytics | 30/30 eval, critical 17/17 (after today's tool) | correct + explained | ✅ meets (new) |
| Handbook answers | 5/5 eval; retrieval P@1 0.375 / MRR 0.49 (BM25, 24 Qs) | P@1 ≥ 0.8 on 200+ Qs | ❌ retrieval weak, small eval |
| General / social talk | 24/30 natural-chat eval; social 3/5 | ≥ 95 % | ⚠ partial |
| Golden A–W (41 cases) | model off 36/41 correct; model on 23/41 | ≥ 95 %, model improves it | ❌ model makes it worse |
| Latency (model path) | p50 4.5–5.2 s, p95 7.5–9 s | first token < 1 s, streamed | ❌ |
| Latency (deterministic) | p50 0.2–0.9 s | < 1 s | ✅ |
| Model down | every turn HTTP 500 → fixed today (34/41, p50 0.9 s) | graceful | ✅ now |
| Fabrication | polish turned "not recorded" into "No cases were rejected" → fixed today | zero | ✅ now |
| Tool path tested | 0 tests run the Qwen agent's tool path | every tool path in CI | ❌ |
| Eval size | 41 + 30 + 30 items, hand-run | 500+ items, CI gate, judged | ❌ |

## 2. How it works today (the real path)

```
message → guardrails → typo fix → [handbook heading] → [analytics] → [natural chat]
        → clear-reading check → Qwen follow-up reader → task layer → case insights → vague → case form
        → definitions → simple pipeline (embeddings) → Qwen tool agent → meaning layer (embeddings, Qwen when unsure)
        → general layer (handbook BM25 / general model) → workspace → actions → glossary
        → answer_question (rules → LLM router for UNKNOWN) → ~15 reply decorators (polish = another Qwen call)
```
57 `return` points in `_copilot_json`, 164 in the route; the first layer that matches answers. LangGraph is in the repo
but **not on the chat path**.

## 3. The gaps (root causes, not symptoms)

### G1 — Understanding is a rule chain, not a model
* **Evidence:** 47 k lines of copilot code; 45 phrase-list keys and ~140 regex lines across the chat YAML; a 685-line
  paraphrase file; 340 dated "fix" comments, 122 "owner said" comments — each new phrasing became a patch.
* **Impact:** a phrasing nobody listed falls to the wrong layer ("can I upload several documents at once?" →
  "Signature is still missing"; "why is the sky blue?" → a KYC passage). Order collisions between layers are the main
  failure mode (also the conclusion of `docs/CHATBOT_AUDIT.md`, 2026-10-08).
* **Industry:** one intent/plan step (a capable model with function calling, or a trained classifier) chooses among
  ~15 well-defined tools; rules only for guardrails.

### G2 — Model too small and on CPU
* **Evidence:** `qwen3:4b-instruct Q4` on CPU: prompt ingest 55–65 tok/s, generation ~20 tok/s; `num_ctx` 2048.
  A 200-token prompt costs ~5 s. 25 call sites with timeouts from 1 s to 20 s; a 6 s per-turn budget had to be added
  because turns reached 15–17 s. With the model ON, golden correctness drops 36/41 → 23/41 (timeouts, weaker readings,
  translation steps that time out → English replies to Hindi questions).
* **Industry:** a 7–14 B (or hosted) model on a GPU: < 1 s to first token, streamed; the model *raises* accuracy.

### G3 — Many model passes per turn instead of one plan
* **Evidence:** follow-up reader, tool agent, meaning chooser, LLM router, polish, opening line, general chat — up to
  6 Qwen calls in one turn were measured on 2026-10-28; the budget now cuts them.
* **Impact:** latency and partial failure; one failed call disables the model for the rest of the turn.
* **Industry:** one planning call (tools + arguments) → tools in parallel → one answer call (or none: templated).

### G4 — Tool calling is home-made and untested
* **Evidence:** schemas are hand-written YAML text pasted into the prompt; `format: json` without a schema (the model
  may name a tool that does not exist); handler code switches on hard-coded names; no timeout around tool execution;
  failures dropped silently; `tests/conftest.py` turns the agent OFF for the whole suite, so no test exercises it.
* **Industry:** schemas generated from the function signatures, constrained decoding, per-tool timeout / retry,
  traced, and every tool path covered by tests (with a fake model) and by a live eval.

### G5 — Retrieval (handbook) is weak and small
* **Evidence:** BM25 only (dense was confident on 10/10 off-topic questions, so it was switched off); P@1 0.375 on 24
  questions; 15 handbook files, some written for API users ("`POST /api/v1/fos/copilot` …"); no reranker; sources
  hidden by an owner decision.
* **Industry:** hybrid (BM25 + embeddings) with a reranker, metadata filters, an eval of 200+ real questions, answers
  with their source.

### G6 — Conversation memory is split
* **Evidence:** four stores carry context: `ConversationState`, the workspace state, the Qwen history (3 exchanges ×
  300 chars), and the contract recall; the bundled frontend sends no `chat_id`, so one officer = one chat; every
  unauthenticated caller shares the subject `anonymous`; follow-ups after a general answer are lost
  ("give me one example of it" → option menu).
* **Industry:** one conversation object per chat (messages + structured slots), summarised when long.

### G7 — Evaluation is too small and manual
* **Evidence:** golden A–W (41) is not reproducible as written (it uploads a real PAN and needs Phase-3 flags off); the
  30-item evals were built today; the historical "41/41" has no results file; no eval runs in CI; the model is OFF in
  every test.
* **Industry:** a versioned eval set of 500+ real (anonymised) questions per capability, LLM-as-judge + exact checks,
  run on every change, with dashboards of accuracy / latency / cost.

### G8 — Latency and UX
* **Evidence:** "streaming" wraps the finished answer as SSE (no token streaming); model answers 4–9 s on this host;
  the first minute after a restart collides with warm-ups (first greetings fall back to the fixed line).
* **Industry:** token streaming, warm pools, < 1 s perceived latency.

### G9 — Language
* **Evidence:** Hinglish / Marathi understanding comes from phrase lists; the 4 B model's Hinglish is poor
  ("Aapka haath hai!"); the language lock is a feature flag.
* **Industry:** a multilingual model (or translation step) evaluated per language.

### G10 — Data the bot cannot answer
* **Evidence:** the FOS store has no branch, credit decision, sanctioned amount or repayment data — the bot now says so,
  but "approved / rejected / overdue / by branch" questions cannot be answered at all.

## 4. What is genuinely strong (keep it)

* Authorisation per case, other officers' data never leaked (canary tests), write actions only proposed + confirmed.
* Deterministic case answers are fast and correct; numbers never invented (analytics in code, "not recorded" said).
* Guardrails (abuse, prompt injection, internal-detail refusal), masking of identifiers, audit and turn logs.
* Everything configurable (no hard-coding), feature flags for rollback.

## 5. Roadmap to industry level

| Phase | Work | Expected result |
|---|---|---|
| 0 (now, days) | GPU host (or a hosted model) for Qwen 7–14 B; token streaming; one warm-up; same model everywhere | model answers < 1–2 s; model ON stops hurting accuracy |
| 1 (2–3 weeks) | Collapse the chain: guardrails → **one planner call** (native function calling, schemas from code, constrained JSON) → tools in parallel → templated / model answer. Keep the deterministic tools and analytics as the tools. Retire the phrase layers behind a flag as the planner's eval passes | one routing decision per turn; new phrasings handled without patches |
| 2 (parallel) | Eval: 500+ anonymised real questions (case, analytics, handbook, general, Hinglish, follow-ups), judged, in CI; agent path tested with a fake model | every change measured; regressions blocked |
| 3 (1 month) | Retrieval: rewrite handbook for officers, hybrid + reranker, 200-question retrieval eval, show sources (owner decision) | handbook P@1 ≥ 0.8 |
| 4 | One conversation store (messages + slots), `chat_id` from every client, per-user isolation without `anonymous` | reliable multi-turn |
| 5 | Data: decision / branch / repayment feeds into the analytics tool | "approved / rejected / by branch" answerable |

## 6. Fixed today (for reference)

Embeddings-down HTTP 500 · analytics tool (count / sum / avg / group / period compare, "not recorded" for missing
data) · handbook-heading priority · general model never overrides a covering handbook · polish never rewrites
computed / model answers · agent calls on the model ledger · natural conversation (social + general to Qwen first,
history, no menu) · correct model for general chat · per-layer model time.
