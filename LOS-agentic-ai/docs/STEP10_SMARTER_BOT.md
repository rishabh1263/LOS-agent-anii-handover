# STEP 10: SMARTER BOT

Status: SPEC SAVED 2026-10-07. Build AFTER the current FINISH MODE items are done.

Rules: same speed-mode rules (related tests + core suite after each sub-step, patch per sub-step, 2-line note,
continue). Each sub-step behind its own flag, default off. No new dependency or model download without asking.
Masked data only.

## 10a. GOLDEN SET + QUALITY GATE (do first: it measures everything else)
- Build `evals/golden/`: 150+ labeled cases, stratified: status, documents, KYC, co-applicant, case workspace
  (list/open/switch), follow-ups (multi-turn), yes/no/option picks, knowledge, out-of-scope, guardrail attacks,
  Hinglish/Hindi/typos. Include the 27-set, the 17-question script and the user's 10 scenarios.
- Each case: input (or turns), expected intent/tool, expected key facts, must-not-contain, language.
- A script scores: accuracy, wrong-answer rate, clarify rate, wrong-data rate, latency p50/p95, per stratum.
- GATE: a baseline file; any change that drops accuracy > 1 pt or raises wrong-answer/wrong-data rate fails
  (a script the user can run before commits). Record the baseline now.

## 10b. FAST-LANE CONFIDENCE
- Rules return a confidence. Strong/exact matches answer; weak matches pass to the bank → Qwen instead of
  answering. Thresholds in config.
- Goal: wrong-answer rate down without slowing most turns.

## 10c. QUERY REWRITING (follow-ups)
- Before routing, turn a follow-up into a standalone question using session state + memory:
  "uska KYC?" → "co-applicant ka KYC status", "kyu?" → "why is CASE-x stuck", "aur EMI?" → "EMI for CASE-x".
  Deterministic slot-filling first; Qwen only if needed (cached prefix, short output). Log both the original and
  the rewritten question.

## 10d. MEANING-BASED EXAMPLE BANK
- Check the available embedding models for Hindi/Hinglish quality (report candidates, size, RAM, CPU latency).
  ASK before downloading any. Benchmark on the golden set vs the current char n-gram bank; keep a hybrid
  (n-gram + embedding) if it wins.

## 10e. FEEDBACK LOOP (= 6j spec in docs/STEP6J_LEARNING.md)
- 👍/👎 + reason chips, the review queue, approved items → bank/golden set, weekly report, long-term safe
  preferences + "kal aap CASE-xxx pe the", forget-me, RAG citations.
- Tables → the migration 0007 proposal (show the SQL, WAIT).

## 10f. TRACE REPLAY
- Record masked real turns (question, state, routed intent, answer type) under retention.
- A script replays recorded traces against the current code and reports answers that changed. Failures become
  golden cases after review.

## 10g. OFFLINE QUALITY JUDGE
- A script that scores sampled masked turns for relevance, correctness vs facts, format and language.
  Default judge: rule checks + local Qwen for simple checks. Optional hosted bigger model via the existing
  OpenAI-compatible config ONLY if the user approves (data policy); off by default.
- Calibrate on 30 human-labeled samples and report agreement.

## 10h. CLASSIFIER DATA PIPELINE (no training yet)
- Export approved labeled data (golden + feedback + traces) into a training-ready format. Report the count.
- Training a small classifier starts only when ≥1000 approved examples exist; note that in the Master checklist.

## REPORT
- After each sub-step: golden-set scores before → after (accuracy, wrong-answer, wrong-data, clarify, p50/p95).
- At the end: one summary table + what improved + what to do next.

## STOP ONLY FOR
A model download, a hosted-model use, a migration (0007), a test failing twice, RAM < 2 GB.
