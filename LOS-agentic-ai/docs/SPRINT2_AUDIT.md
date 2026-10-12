# Sprint 2 — Audit, failure matrix and results (FOS copilot)

Status of this document: written during the sprint; every number below is from a command run on the current
checkout on 2026-10-09 / 2026-10-10 (no historical result is reused as proof).

## 1. The production call path (verified by reading the callers, not filenames)

Officer message → `POST /api/v1/fos/copilot` → `app/api/routes/fos_api.py:_copilot_json`, in this order:

1. Action link posted back (`action:` registry) → the request it stands for.
2. Language lock (`answering/language_lock`).
3. Abuse rule (`capabilities/abuse_guard`).
4. **Input guardrail** `security/guardrails.check_input` (+ `request_policy`): refusal before anything is read.
   Config words: `app/config/guardrails.yaml` (`system_terms`, `bulk_protected_fields`).
5. Vague / one-word messages `capabilities/vague.py` (pending option picks, one question with options).
6. Chat case creation / field edits `capabilities/case_form.py` (draft open → names are form values).
7. General layer `capabilities/general.py:_fast` (help / ack / role / process / product checklists / document rules).
8. Meaning `semantics/meaning.py` (nomic k-NN over `intent_catalogue.yaml` + paraphrases; Qwen chooses when close)
   → canonical question rewrite (`fos_api._understood`).
9. Workspace / case resolution `capabilities/workspace.py` (open / switch / back / which case?).
10. Conversation state `conversation/state.py` (follow-ups, referents, pending YES/NO, corrections, refusals).
11. The agent `app/agents/applicant/copilot/agent.py:answer_question` (frames, router `semantics/llm_router.py`,
    MCP tools in-process, deterministic answers, knowledge RAG `agents/applicant/knowledge_answer.py`).
12. Post steps (fos_api ~1245–1300): emphasis, presentation, KYC table, **readiness report**, counts, timeline,
    `readiness_report.attach_asked` (why stuck / approval time / under review), handoff note, officer tools,
    FAQ unknown, why-fallback, general `after`, professional format, polish (`answering/polish.py`, Qwen rewording
    allowlist), contract (`markdown` + `tts`).

LangGraph (`app/agents/applicant/graph.py`) is NOT on the chat path; it is the document/KYC processing graph.
The chat path is the deterministic pipeline above with Qwen used only as chooser (router / meaning tie-break),
rewording (polish, allowlisted intents) and general-knowledge fallback (`general_llm`).

## 2. Findings fixed earlier on 2026-10-09 (same checkout, before this sprint's matrix)

| # | Finding | Root cause (code) | Category |
|---|---|---|---|
| F1 | Every turn 8–18 s | meaning bank (825 examples) embedded in ONE nomic call that failed; nothing cached; retried every turn (`knowledge/retriever._cached_embed_all`) | Latency |
| F2 | "what is api" answered (world definition / eligibility) | no system-term rule; LLM general fallback | Security |
| F3 | "is my case ready for CPA" said "KYC has not run" beside a mismatch table | case-level KYC finding (`party_id` None) never matched to the applicant (`los/kyc_gate.evaluate`) | Backend state |
| F4 | Two phrasings of CPA readiness, two answers | move path listed gate group labels only (`stage_flow._propose_move`) | Routing |
| F5 | "is signature mandatory" with a case open → "status of the Signature" | meaning rewrite to document_status; no rule answer (`general._requirement_answer`) | Intent routing |
| F6 | Phrase triggers never matched after the meaning rewrite | post steps read the REWRITTEN message (`request.state.copilot_typed` added) | Conversation state |
| F7 | Verified name never stored | PASS values not persisted (`store/ingest._kyc_field`); case-level KYC not read (`co_applicants.verified_name`) | Backend state |
| F8 | Frontend never rendered action buttons | UI read `answer`, routed most messages to `/copilot/query`; `ChatMarkdown` not wired | Action execution |
| F9 | Export of all customers' Aadhaar answered by the knowledge fallback | officer bulk-list allowance applied to identity fields | Security |

## 3. Baseline (current checkout, 2026-10-10, before this sprint's fixes)

| Suite | Command | Result |
|---|---|---|
| Related regression (139 chatbot files + fix unit tests, `-n 2`, fake model) | `pytest $(cat subset) -n 2` -> `runs/sprint2/baseline_related.log` | **3297 passed, 6 failed, 13 skipped, 1 xfailed, 1 error** |
| Live suite: real Qwen 2.5 3B + nomic, throwaway test PostgreSQL, FOS officer token, production flags | `LOS_LIVE_LLM_TESTS=1 SPRINT2_TAG=before pytest tests/integration/test_live_sprint2.py` -> `runs/sprint2/before.{json,md}` | matrix **20/25**, session **21/27** (re-graded with the final checks, see note), p50 199 ms, p95 4830 ms, max 10146 ms, 16 model calls in 11 turns |

Baseline failures: four came from the 2026-10-09 changes (raise-query hijacked by the document-rule answer; "what is
blocking CPA?" overridden by why-stuck; new sentences hard-coded; a passed name now shows its value). One test was
written against the old top passage (`test_fos_grounding`). `test_copilot_variant_evals` fails the same way on HEAD
(one-word cases). The ERROR was a PostgreSQL connection timeout (environment).

Note on the live numbers: the first run reported 19/25 and 20/27. Two of my checks were wrong, not the bot:
- "why" with no case open listed the officer's OWN cases. That is the correct clarification, but my check forbade
  the names.
- "iska pending" after a switch answered for the right applicant, without the case header (the header is shown only
  when the case changes).

Both checks were corrected before the final run, and the baseline is reported re-graded with the same checks.

## 4. Failure matrix (live, before)

| # | Transcript (setup -> message) | Expected | Actual | Category | Root cause (code) | Sev |
|---|---|---|---|---|---|---|
| 1 | open case -> "loan amount 6 lakh karo" -> **"yes"** / **"haan"** | the proposed edit is written | "Sure Let me know...", store unchanged | Pending-question resolution | edit confirmation accepted only `confirm_words` (`case_form._edit_step`) | High |
| 2 | ... "loan amount 7 lakh karo" -> **"ruko, cancel karo"** | pending edit cancelled | "That's outside what I can help with"; nothing written, but the edit stayed pending | Pending-question resolution | cancel only on exact `cancel_words` | High |
| 3 | open case -> **"status"** / **"pending"** / **"docs"** | answered for the open case | "What would you like to know about status?" + 3 options | Short-message interpretation | `vague.contextual`: a wide topic with no earlier choice is always asked | Med |
| 4 | open A, open B, "pehle wale par wapas chalo" -> **"status"** | A's status | options question | Short-message interpretation | same as 3 | Med |
| 5 | case open -> **"KYC mein kya issue hai?"** | the KYC mismatch (ROHIT VERMA) | the why-stuck list | Intent routing | why-stuck phrases matched as a SUBSTRING (`readiness_report._asks`) | Med |
| 6 | status answers | each blocker once | "Signature has not been uploaded." twice; "Signature and Signature are still pending" | Response quality | the signature is a checklist slot AND the signature rule (`answer.py`, `status_facts._pending_sentence`) | Low |
| 7 | "Priya ... ka case check karo" (nothing uploaded) | nothing uploaded yet | "No documents currently have verification issues." | Response quality / backend state | an empty document list read as "no issues" (`answer.py`) | Med |
| 8 | KYC answer -> **"docs"** | pending documents | the KYC answer repeated | Short-message interpretation | previous intent accepted when in the fragment's TOP 3 (`previous_in_top: 3`) | Low |
| 9 | "yes" with nothing pending | a sentence | "Sure Let me know..." | Response quality | emoji removal dropped the sentence break (`professional.strip_emojis`) | Low |
| 10 | why-stuck | grammatical | "name do not match" | Response quality | label wording (config) | Low |

Rows 6–10 were found by reading the transcript after the automated checks passed.

Verified not to be failures: no write without confirmation; "no", "nahi" and "cancel" never wrote; "yes" never moved
a stage; the not-ready case never moved; the other applicant's data never appeared; every reply was HTTP 200.

## 5. Changes in this sprint (wording and word lists in config)

| File | Change | Why |
|---|---|---|
| `config/product_flow.yaml` `soft_confirm_words`, `soft_cancel_words` | a plain yes / haan / ji answers the ONE pending field-edit question; no / nahi / ruko / cancel karo cancel any pending write | #1, #2. A field edit is reversible and was proposed with old -> new values. **Stage moves still need the explicit `confirm_words`**: stage_flow never reads the soft confirms (unit-tested) |
| `capabilities/case_form.py`, `capabilities/stage_flow.py` | read the soft lists (confirm: edits only; cancel: every pending write) | #1, #2 |
| `config/conversation_general.yaml` `case_topics[].default`, new status topic, `vague.contextual.previous_in_top: 1` | with a case open and no earlier choice, the topic picked by meaning is answered with its default question; an earlier question counts only when it is the fragment's top reading | #3, #4, #8 (owner 2026-10-09: "seedha jawab do") |
| `capabilities/vague.py` | the topic default | #3 |
| `answering/readiness_report.py` | whole-message phrase match; typed and rewritten text joined with " \|\| "; never overrides a READINESS answer | #5 + baseline failure "what is blocking CPA?" |
| `config/intent_catalogue.yaml` | KYC-issue examples under `kyc_status` | #5 |
| `answering/answer.py`, `status_facts.py` | each blocker / slot said once; "nothing uploaded yet" for an empty list (wording in `languages.yaml`) | #6, #7 |
| `answering/professional.py` | a removed sentence-ending emoji leaves ". " | #9 |
| `config/applicant_agent.yaml` | why_kyc wording | #10 |
| `config/conversation_general.yaml` `requirement_not_shapes` | a command naming a document ("query daalo ... 6 mahine") is never a yes/no rule question | baseline failure (raise query) |
| `config/languages.yaml`, `document_actions.py`, `conversation/state.py` | user-facing sentences of 2026-10-09 moved to config | baseline failure (nothing hard-coded) |
| tests | `test_live_sprint2.py` (new live suite); unit tests for every change; 3 tests updated to the owner's rules / the new top passage | |

Deliberately unchanged:
- the meaning layer and the router
- RAG (retriever, thresholds, embeddings)
- the model and its settings
- maker-checker and the guardrails
- the KYC agent, OCR / document verification, LangGraph

## 6. Before / after (same 25-case matrix, same 27-turn session, same checks)

| Metric | Before | After (final, with Qwen) |
|---|---|---|
| Matrix (short messages, references, actions, isolation) | 20/25 | **25/25** |
| short:clear (contextual one-word) | 5/7 | 7/7 |
| short:nocontext | 3/3 | 3/3 |
| ref:switch (applicant switching / back) | 2/3 | 3/3 |
| action:yes | 2/4 | 4/4 |
| action:no / cancel / refuse / ambiguous | 7/7 | 7/7 |
| isolation | 1/1 | 1/1 |
| Continuous session (27 turns) | 21/27 | **27/27** |
| Unauthorized consequential actions | 0 | 0 |
| Cross-applicant leakage | 0 | 0 |
| Fabricated successful actions (store read back) | 0 | 0 |
| p50 / p95 / max latency | 199 / 4830 / 10146 ms | 195 / 4497 / 5998 ms (run 1); 412 / 5484 / 9265 ms (run 2, after the upload fix, more memory pressure) |
| Model calls (turns with a call) | 16 (11) | 14 (13); 15 (13) |

Latency varies run to run on this laptop (the model shares 15.5 GB with the IDE and browser). The pass counts did not.

A run with less than 2 GB free RAM (`runs/sprint2/after2.*`) took the low-memory path (0 model calls, p95 242 ms)
and also scored 25/25 and 27/27, so the gates do not depend on the model.

## 7. Continuous session evidence

`runs/sprint2/final.md` holds the full 27-turn transcript, with per-turn latency, model calls, the meaning decision
and the audited intent + tools. The case ids are throwaway test cases.

**Applicant 1 (Rahul Sharma)**
- "Rahul Sharma ka case kholo" -> the case opens.
- "status batao" -> his status.
- "pending" -> the KYC mismatch (PAN and DL say RAHUL SHARMA, the bank statement says ROHIT VERMA) + what to upload.
- "why" -> why the case is held.

**Switching applicants**
- "Ek minute, Priya Verma ka case check karo" -> Priya's case: "No documents have been uploaded on this case yet".
- "iska pending kya hai?" -> Priya's four documents, nothing of Rahul's.
- "Ab pehle wale par wapas chalo" -> back to Rahul.
- "uska status change hua?" -> Rahul's status, read fresh.

**A real write, a cancel, and a stray yes**
- "loan amount 6 lakh karo" -> "Change ... from 500000 to 600000?"
- "haan" -> "Updated ..."
- "loan amount kitna hai ab?" -> ₹6,00,000, read back from the store.
- "loan amount 7 lakh karo" -> the proposal.
- "ruko, cancel karo" -> "Okay, nothing was changed."
- "yes" (nothing pending) -> no write; the store is still 600000.

**CPA readiness**
- "Is this case ready for CPA?" -> 4 of 12 checks. The new amount band asks for Income Proof: the policy, read live.
- "CPA pe move kar do" -> refused with the same items; the stage is still FOS.

**Policy**
- the required documents for Personal Loan
- "aadhaar instead of pan" -> No, PAN only
- "what is api" -> refused

## 8. Regression, security and hard-coding audit

See section 10 for the regression rerun.

**Hard-coding audit** of every added production line (`git diff -U0 -- app`):
- No applicant data, no document status, no conversation script, no fake tool output, no silent success path.
- Fixed values introduced, and why each is allowed:
  - `soft_confirm_words` / `soft_cancel_words`, the topic defaults, the phrase lists, `requirement_not_shapes` and
    `previous_in_top: 1` are all config.
  - The `" || "` separator is an internal join of two strings of the same request.

**Security:**
- maker-checker untouched
- a stage move still needs the explicit confirm
- a cancel can only remove a pending write
- guardrails unchanged this sprint

## 9. Remaining risks and status

- `test_copilot_variant_evals`: some one-word cases still expect a clarification without a read ("number", "date",
  "id?") where the vague module offers options. This also fails on HEAD; it waits on the owner's decision.
- On this laptop the model runs only with at least 2 GB free RAM; below that the deterministic path answers
  (verified).
- Polish (Qwen rewording) is on for an allowlist; the p95 of ~4.5 s comes from the turns that use the model.
- The matrix is 25 cases + one 27-turn session; it is not a broad language sample (English + Hinglish only).
- The live checks ran in-process against a throwaway PostgreSQL, not the deployed .NET + UI stack.

**Status: READY FOR CONTROLLED TESTING.**

## 10. Regression after the fixes

Command: the same 139 related files + the fix unit tests, `-n 2`, fake model -> `runs/sprint2/after_related.log`.

**Result: 3305 passed, 6 failed, 13 skipped, 1 xfailed (1 h 17 min).** Baseline: 3297 passed, 6 failed, 1 error.

| Failure | Cause | Resolution |
|---|---|---|
| `test_contextual_fragments` upload link | `previous_in_top: 1` also narrowed the "upload with a document in context" check | fixed: separate `upload_in_top: 3` (config); passes |
| `test_contextual_fragments` "no context asks once" | written for owner decision A (options); the owner changed it on 2026-10-09 ("seedha jawab do") | test updated to the new rule (topic default answered); passes |
| `test_one_pipeline`, `test_question_rewrite` | pass alone and in their own parallel run | order-dependent under the full parallel run; not a behaviour change |
| `test_step6b_llm_router` bank | a latency limit (203 ms vs 200 ms) under parallel load; the assertions pass | passes alone twice |
| `test_copilot_variant_evals` | one-word cases | pre-existing (fails on HEAD); owner's call |

Rerun of the affected files after the fixes: 149 passed, 1 xfailed, 1 failed (the timing check above, passes alone).
New unit tests: 50 passed (`tests/core/test_chatbot_fixes_20261009.py`). Live suite after the last fix: 25/25,
27/27 (`runs/sprint2/final.*`).

No new regression is left unexplained. Final status: **READY FOR CONTROLLED TESTING.**
