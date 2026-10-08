> SUPERSEDED 2026-10-07 for the FOS scope by docs/FOS_E2E_PRODUCTION_PLAN.md (kept for history).

# STEP 11: ASK-ANYTHING

Status: SPEC SAVED 2026-10-07. Build AFTER the FOS production-ready work (docs/FOS_PRODUCTION_READY.md).
Flag COPILOT_ASK_ANYTHING (default ON in dev). Speed-mode rules; no new dependency without asking.

## 11a. DIALOGUE STATE
Every turn updates active case, party, topic, last entities (document, field, check), the last answer type.
Stored with session memory.

## 11b. QUERY REWRITING (condense)
If the message is a follow-up (pronouns, "aur", "kyu", "uska", short fragments, or it depends on state),
rewrite it into a standalone English question using the state (deterministic slot-filling first; Qwen only if
needed, cached prefix, short output). Route the REWRITTEN question. Log both. First turns / standalone
questions pass through unchanged.

## 11c. CASE SNAPSHOT QA (long tail)
If no known intent fits with confidence, build a compact masked fact sheet of the active case (all application
fields, parties, documents + statuses + reasons, KYC results, stage + history, queries) and ask Qwen to answer
ONLY from it, citing fact keys. Then FACT-CHECK: every number, name, ID, status, date and document in the
answer must exist in the fact sheet; otherwise discard the answer → "This isn't recorded on the case" or one
clarifying question. Never invent. Respect the language lock and the no-emoji format.

## 11d. ANSWERABILITY
If the fact sheet lacks the answer, say exactly what is missing and where it would come from
(e.g. "Tenure isn't captured at FOS").

## 11e. CONTEXT-AWARE NEXT SUGGESTIONS
2–3 follow-up suggestions generated from state (what's pending/blocking/related), not fixed chips.

## 11f. EVAL
Add 60+ long-tail and multi-turn cases to the golden set (field questions, dates, amounts, "aur uska", topic
switches, unanswerable ones). Report: answered correctly, wrong-data (must be 0), "not recorded" correct,
clarify rate, p50/p95. Measure snapshot QA latency on CPU and estimate it on GPU/hosted.

## STOP
Only on the usual conditions (model download, hosted-model use, migration, a test failing twice, RAM < 2 GB).

## Overlap note
11b overlaps STEP 10c (query rewriting); build it once and reference it from both.
