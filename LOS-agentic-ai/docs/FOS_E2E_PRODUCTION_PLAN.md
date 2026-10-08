# FOS Stage: End-to-End Production Plan (CPU only)

Status: SAVED 2026-10-07. Supersedes, for this scope, docs/FOS_PRODUCTION_READY.md (part 1) and
docs/STEP11_ASK_ANYTHING.md. Earlier specs still apply where this file does not contradict them
(CHATBOT_SPEC.md, STEP6_MVP, STEP6I).

Goal: the FOS stage works end to end through the chatbot, production quality, with no gaps.
Login, then my cases, open a case, ask anything, fix documents and KYC, check CPA readiness, produce the handoff note.
A person moves the case to CPA (maker-checker). The chatbot never moves stages.

---

## 0. How to run this

- Hardware: CPU only (this machine). Model qwen2.5:3b via Ollama. No GPU, no hosted model, no new model downloads.
- Speed mode: build sections in order, back to back. After each section: related tests + core copilot/guardrail suite
  (fake model, models unloaded), save a patch in runs/patches/, a short CHANGES_LOG entry, a 2-line progress note,
  then continue.
- No hardcoding: phrases, synonyms, labels, thresholds, templates, checklists, glossary, readiness rules come from
  config (yaml) or the example bank. Readiness comes from stage_gates.yaml and the checklist configs, never a fixed
  list in code.
- Safety (unchanged): no git commits or git state changes, no .env or secret edits, no new dependencies without
  asking, no killing processes you did not start, migrations only through the gated backup process, RAM check before
  tests (stop below 2 GB).
- STOP only for: a schema change (show SQL + rollback), a new dependency, a test failing twice, RAM below 2 GB, or
  anything outside this spec.
- Every section gets a flag. For this project the chatbot flags stay ON in dev. Stage gate, KYC rule, signature
  mandatory and co-app mandatory docs stay OFF until the owner says so.

---

## 1. Fix first: bugs from the real frontend transcript

1. Wrong data: "loan amount: Rs 5" on a Home Loan (CASE-6E074B8D10CB). Find whether storage or formatting is wrong.
   Never show an implausible amount; show "Amount needs verification". Read-only report of every case with the same
   issue.
2. "all case ka list", "give a list of case", "my cases", "mere saare cases" must show the caller's own case list.
   Never refused as other customers' data.
3. "dusre case ka details do", "mere dusre case ka detail" must switch to the other case, or ask which one when there
   are several.
4. Details were answered, then "None of the application details are recorded" for the same question. Find and fix the
   cause.
5. Pending count said 3, verify list and upload button said 7. One source of truth for mandatory, optional, pending,
   failed, under review. Labels must be explicit.
6. Ambiguous or typo messages ("mere Aadhar case ka details kaise dekhun", "list of kaise sajao") get one clarifying
   question, not "not recorded" or out of scope.
7. Suggestion chips change with context; never the same chip every turn.
8. If the frontend sends case_id on every request, document in frontend_handoff/FRONTEND_API.md that it must not
   override the workspace, and handle it safely in the backend.

Add every item to evals/golden/ with the expected behaviour.

---

## 2. Language lock

- reply_language from the frontend is authoritative. English selected means every reply is English, even when the
  user writes Hindi, Hinglish or Marathi. Understanding still works in any language.
- Covers all text: answers, clarifying questions, options, buttons, errors, guardrail refusals, document reasons,
  readiness, handoff note.
- No language set: keep the current detection.
- Tests: Hinglish input with English selected gives English output for every intent.

## 3. Professional format (no emojis)

- Remove all emojis from every response field and button. Config style.emojis=false (production default).
- Shape: one direct first line (answer first), then numbered or bulleted points, bold only key words, a final
  "Next step:" line when useful. About 8 lines except lists.
- Plain-text field without markdown markers.
- Update CHATBOT_SPEC section 1, snapshot tests and frontend_handoff examples.

## 4. Name matching across all documents + clean KYC results

- For each party, the name on the application must match the name on every document that carries a name (PAN,
  Aadhaar, DL, passport, voter ID, bank statement, salary slip, ITR, others as configured). Reuse the existing KYC
  name comparison and policy (normalisation, initials, word order). Report which documents are compared today; add
  missing ones through config.
- Same approach for DOB, father's name and address where the policy defines them.
- KYC result shown per party as a clean table:
  check | document | value on document | value on application | result | reason.
- Values only from recorded findings, masked per policy.
- When a mismatch is found, say which document is the likely odd one out if the other documents agree (majority),
  and what to upload to fix it.

## 5. FOS to CPA: ask anything about readiness

- Any question about moving to CPA ("CPA mein kab jayega", "CPA ke liye kya chahiye", "ready hai kya", "what is
  blocking CPA") is answered from the gate config plus live data.
- List every requirement with its state, grouped: application fields, applicant documents, co-applicant documents,
  verification, KYC per party per check, signature, anything else the FOS gate evaluates. PASS / PENDING / FAILED /
  REVIEW, with reason and fix.
- Follow-ups work: "KYC wala detail", "co-applicant ka kya", "kaunsa document galat hai".
- The readiness answer must always agree with the real gate result. Test that it never says ready when the gate would
  fail.

---

## 6. Ask-anything engine (CPU design)

On CPU the model does not plan multiple tool calls. Code gathers the data, the model only reads it. One model call at
most per turn.

6.1 Dialogue state: each turn updates active case, party, topic, last entities (document, field, check), last answer
type, pending question and options.

6.2 Query rewriting: follow-ups (pronouns, "aur", "kyu", "uska", short fragments) become a standalone question from
the state. Deterministic slot filling first; the model only if needed (cached prefix, short output). Route the
rewritten question. Log both.

6.3 Known questions: fast lane, example bank, router (current hybrid).

6.4 Case snapshot QA for everything else: build a compact masked fact sheet of the active case (application fields,
parties, documents with status and reasons, extracted document fields, KYC results, stage and history with
timestamps, queries). The model answers only from it and cites fact keys.

6.5 Fact check: every number, name, ID, status, date and document in a generated answer must exist in the fact sheet.
Otherwise discard and reply "This is not recorded on the case" with what is missing, or ask one clarifying question.
Wrong data rate must be zero.

6.6 Coverage that must work: document contents ("PAN pe DOB kya hai", "last salary kitni", "salary slip kis company
ki"), time ("kal wala verify hua?", "kitne din se atka hai"), counts ("kitne verified"), multi-intent (up to 3
questions in one message), why-questions (reason codes mapped to plain language in config), process knowledge
(knowledge base FAQ with citations).

6.7 Understanding aids: synonym and spelling lexicon in config (documents, fields, stages, statuses); glossary both
ways (full form + one-line meaning + this case's value; a typed full form maps to the abbreviation; first mention in a
session shows the full form); misunderstanding recovery ("nahi, mera matlab tha", "ye nahi poocha").

6.8 Natural replies: answer first, mirror the user's key term, 2 to 4 phrasing variants per template rotated per
session, light professional acknowledgements only where natural, one context-aware next step. Optional fact-locked
rephrase only if it keeps p95 within target.

---

## 7. Features to impress (all deterministic, CPU friendly)

7.1 CPA Readiness Check + Handoff Note
- "CPA Readiness: X of Y checks passed", from the same source as section 5, failing checks ordered by what unblocks
  the case fastest, each with reason and exact fix.
- When all checks pass: "Case is ready for CPA. A person must confirm the move." and a CPA Handoff Note: one page with
  case and parties (masked), loan details, documents with verification status, KYC table per party, signature,
  exceptions and overrides with reasons, generated time, officer. In chat and as a downloadable file
  (Markdown/HTML/PDF with an existing library). Audit logged. Refused when not ready.

7.2 Smart Upload (auto identify and file)
- The officer uploads any file inside a case without saying what it is. The system uses the existing document
  classification to identify the type and the party (from the name on the document vs application names), files it
  in the right slot and replies: "This looks like the co-applicant's PAN. Filed under Co-applicant > PAN.
  Verification started."
- Low confidence: ask one question ("Is this the applicant's or the co-applicant's document?").
- Result arrives in the same chat when verification finishes: verified, or the exact problem and what to retake.

7.3 Case Timeline and Turnaround
- "Case ka timeline", "kab kya hua", "kitne din se FOS mein hai": a dated timeline (created, each upload,
  verification results, KYC runs, queries) and days in FOS.
- A configurable turnaround target per stage; a case past it is flagged in the case list and on open, with the main
  blocker.

---

## 8. Frontend contract

Update frontend_handoff/ (FRONTEND_API.md, openapi.yaml, examples, reference widget): language lock, no emojis,
readiness and handoff note actions, smart upload flow, timeline, case_id handling rule, all action types with example
JSON.

## 9. Production readiness checklist (report PASS/FAIL with evidence, fix the FAILs)

1. Correctness: zero wrong data; implausible values flagged; one source of truth for document states.
2. Consistency: chat, document view and gate never contradict; the same question gives the same facts.
3. Language: section 2 holds everywhere.
4. Errors: every failure path has a clean specific message (DB down, OCR queued, model down, timeout, out of scope, no
   access); no stack traces or internal names.
5. Security: scope checks on every read and action, guardrails, masking, chat rate limit, audit for every write,
   report and download.
6. Performance on this CPU: fast-lane turns p95 under 200 ms; snapshot QA and router turns p95 under 3 s; upload
   acknowledgement under 2 s; model kept warm; /ready degraded until warm.
7. Tests: golden set of at least 200 cases covering sections 1 to 7 and multi-turn conversations; quality gate script
   with a recorded baseline; full regression green (both halves); the verify_e2e hang investigated (max 20 minutes,
   otherwise skipped with an open item).
8. Config: every flag documented with its production value.
9. Docs: README_CHATBOT.md and frontend_handoff final.
10. Ops: /ready reports model warm, DB, migrations, config errors; logs masked; cleanup jobs running.

## 9b. After section 9 (user, 2026-10-08) -- deterministic, CPU, each behind its own flag, language lock + no-emoji format

a. FIX-IT PATH + WHAT-IF: "agar X upload karu toh ready ho jayega?" -> simulate the FOS gate with that item assumed
   passed; answer yes/no + what else remains; the shortest path to CPA.
b. AUTOPILOT REVIEW ON OPEN: when a case is opened, run all checks and show one review card (issues + fixes),
   proactively.
c. STATUS TABLES: a document status table + a "checks passed X/Y" progress element in the presentation payload.
d. CUSTOMER MESSAGE DRAFT: "customer ko bata do kya lana hai" -> a ready-to-send message (selected language) listing
   pending/redo items with exact names; Copy + Edit; never auto-sent.
e. VOICE INPUT: a frontend-only mic button (browser speech recognition, en-IN / hi-IN) in the reference widget +
   documented in FRONTEND_API.md.
f. VISIT CHECKLIST: "visit pe kya le jaun" -> a printable list per party.

Then run docs/HARD_TEST_PLAN.md (added by the user) and produce the REPORT.md verdict. Fix failures (wrong data,
leaks, wrong-case answers first) and re-run until section 9's criteria are met.

## 10. Order and final report

Order: 1, 2, 3, 4, 5, 6, 7.1, 7.2, 7.3, 8, 9.

Final report: the section 9 checklist with evidence; golden-set numbers before and after (accuracy, wrong answers,
wrong data, clarify rate, p50/p95); the list of flags and their production values; open items for the owner; a
5-minute demo script: login, my cases, open a case, ask a few free questions (including Hinglish with English
selected), "CPA ke liye kya chahiye", smart upload of a document, "ready hai kya", handoff note.
