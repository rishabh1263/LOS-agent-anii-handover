# Gap analysis: master spec v2 (`docs/MASTER_FINAL_SPEC.md`) vs the code (final state, 2026-10-08)

`docs/frontend/MASTER_FINAL_SPEC (2).md` is byte-identical to `docs/MASTER_FINAL_SPEC.md`; `(1)` is an older draft
(182 lines) and is superseded. Section 15 (approved flow) and 15.5 (owner decisions) win where sections differ.

Status: **DONE** = built and tested; **PARTIAL** = built, with a limit stated; **N/A** = not a chat feature by design.
Every row's tests are in `tests/integration/test_master_*.py` unless named.

## Login, scope, home, form (2, 15.1)

| Item | Spec | Status | Evidence (file:function) | What changed in this run |
|---|---|---|---|---|
| Login = username + password + stage, no applicant ID | 2, 15.1.1 | DONE | `app/api/routes/auth_api.py:login` | `stage` validated against the LOS stages (422 + plain message), echoed in the token response; ids ignored for scope (done earlier) |
| Same JWT for UI and chat; scope = own cases | 2, 15.2.1 | DONE | `app/security/access.py:authorize`, `capabilities/workspace.py:authorize` | `/copilot/query` no longer needs `applicant_id` (filled from the held case) |
| Home table: columns, search, filter, sort, pagination, empty / loading | 15.1.2 | DONE | `fos_api.py:list_cases`, `capabilities/case_list.py:run` | columns + labels from config, `group` / `status` filters, `empty_text`, `texts`; old row fields kept (no break) |
| Statuses Created / Review / Disbursal → Pending / Done (config) | 15.5 | DONE | `product_flow.yaml case_status`, `product_flow.py:status_of/group_of` | new; used by table, chat lists, filters, counts, follow-up |
| "Create New Case" wording everywhere | 15.5 | DONE | `copilot_reply.yaml action_links.new_case`, `product_flow.yaml` | renamed from "New case" |
| Row click → filled form, continue where stopped | 15.1.4 | DONE | `fos_api.py:case_form_values`, `case_form.py:values_of/missing` | new `GET /cases/{id}/form` (values, missing, editable) |
| Edit fields (UI) | 15.5 | DONE | `fos_api.py:case_form_update`, `case_form.py:check/save` | new `PUT /cases/{id}/form`, same rules as create and chat, audited + activity log |
| One form definition for UI and chat | 15.5 | DONE | `case_form.py:schema/check`, `GET /fos/form-schema` | new; fields from `CreateCaseRequest`, rules from config |

## Chat flow (15.2)

| Item | Spec | Status | Evidence | What changed |
|---|---|---|---|---|
| Case questions vs FAQ questions | 15.2.2 | DONE | `workspace.py:handle`, `faq.py:answer_for` | FAQ "how to" answers (numbered steps + source) from `faq.yaml answers` |
| Unknown FAQ → "I don't know that yet" | 15.2.2 | DONE | `faq.py:apply_unknown/unknown`, `fos_api.py` (no-case path) | new, both endpoints |
| Case answer = summary + Download Excel / Doc / PDF + Show in UI | 15.2.3, 15.2.6 | DONE | `contract.py:_action_links`, `product_flow.py:case_links` | new registered links; not on refusals |
| "Do you want to know about a particular case?" → Yes/No → Pending or Done → list → pick → open → "What do you want to know?" | 15.2.4 | DONE | `product_flow.py:closing_for_list/step/after_open` | new; state per chat; typing anything else skips it |
| Quick buttons (My pending cases, Create New Case, How to upload, FAQ) | 15.2.5 | DONE | `GET /fos/quick-actions`, `product_flow.yaml quick_buttons` | new |
| One workspace per chat (new chat starts fresh) | 4 | DONE | `fos_api.py:_copilot_json` (workspace_id = chat_id) | **fixed**: the open case / draft leaked across chats (found by the self-check) |
| Response contract: ONLY markdown + tts | 8 | DONE | `contract.py:publish` | blocked replies, FAQ / help voice text (`tts_text`), dead `[Upload]` chip text removed, list voice names every item |
| Case list at scale, NL phrases, paging | 3 | DONE | `case_list.py` | courtesy words ("sorry, my cases") accepted |
| Workspace / follow-up rewriting | 4 | DONE | `workspace.py`, `conversation/state.py` | named person lookup ("Zoravar Khanna ka case") → open if own, else neutral line |
| Ask anything + fact check | 5 | DONE | `capabilities/snapshot_qa.py`, `answering/validate.py` | out-of-scope (no case open) → one polite line + next option |
| KYC A (form vs documents) / B (cross-document) tables | 6 | DONE | `answering/kyc_table.py` | (earlier run) |
| CPA readiness X of Y, never a stage move | 6, golden rule 4 | DONE | `answering/readiness_report.py`, `product_flow.py:stage_move_note` | a stage-move request now says plainly that a person moves it |
| FAQ config | 7 | DONE | `faq.yaml`, `faq.py` | answers + unknown added |
| Real-time streaming | 11 | DONE | `answering/realtime.py:events` | blocked message: no status, one delta |
| Features (review, what-if, timeline, notes …) | 9 | DONE | `answering/officer_tools.py` etc. | (earlier run) |

## Actions from chat: parity with the frontend (15.3, 15.5)

| Frontend action (API) | In chat | Status | How |
|---|---|---|---|
| Login / refresh / logout | -- | N/A | session actions of the app, not chat requests |
| List cases, search, filter (Pending/Done, status, KYC, ready…), sort, page | "my pending cases", "review wale", "Rahul", "top 5", "aur dikhao" | DONE | same query as the table |
| Open a case (row click) | "2", "CASE-…", "Rahul ka case kholo" | DONE | Open link / typed |
| Create a case (`POST /fos/applicants`) | "Create new case" → fields → summary → **Confirm** | DONE | `case_form.chat_turn` → the same `create_case` |
| Continue a partly filled case | on open: "This case still needs: …"; fill with "<field> <value> karo" | DONE | `case_form.missing`, `_propose_edit` |
| Edit fields (`PUT /cases/{id}/form`) | "loan amount 600000 karo" → **Confirm** | DONE | `case_form._propose_edit/_edit_step` |
| Upload documents | attach in chat / Upload link | DONE | multipart `/fos/copilot`, smart upload |
| View a document | View link | DONE | `case_actions.view_document` (signed, 5 min) |
| Check status / documents / checklist / readiness | typed questions | DONE | the agent + workspace |
| Handoff note | "handoff note" / link | DONE | `answering/handoff_note.py` |
| Raise / track / mark a query | "query raise karo" → **Send** | DONE | `case_actions.py` |
| Export Excel / Doc / PDF (case, filtered list) | Download links, "excel download karo" | DONE | `exports.py`, `GET /fos/exports` |
| Import Excel | send the .xlsx in chat → row errors → **Confirm** | DONE | `importer.chat_reply`, `case_form._import_step` |
| Notifications | in the greeting | DONE | `realtime.greeting` + `product_flow.notifications` |
| Activity log | "case ki history / timeline" | PARTIAL | the timeline answer reads the same case events; the full who/what list is the UI route `GET /cases/{id}/activity` |
| Chat history | -- | N/A | a UI panel (`GET /fos/chats`) |

## FOS ⇄ CPA workflow and list default (owner, 2026-10-08)

| Item | Status | Evidence | Notes |
|---|---|---|---|
| "my cases" = the officer's 5 most recent | DONE | `case_list.yaml default_sort: recent`, workspace `page_size: 5` | counts still shown (`ListQuery.with_counts`); the table stays one SQL page |
| FOS: anything wrong → query to the customer | DONE | `capabilities/stage_flow.py:_propose_customer_query`, `queries.yaml` target `CUSTOMER` | one query lists every problem; Confirm; copyable message (no customer channel exists); raiser resolves it |
| FOS: all clear → move to CPA | DONE | `stage_flow.py:_propose_move/_move` → `stage_lifecycle.transition` (gated) | Confirm; `stage_flow.move_scopes` (owner decision: FOS officer may move FOS→CPA) |
| CPA → FOS query, FOS → CPA query / reply | DONE | `stage_flow.py:_propose_stage_query/_propose_query_move`, `queries.yaml routes` | text → Confirm; reply = RESPONDED |
| All feature flags ON (incl. gate / KYC) | DONE | `.env`, `applicant_agent.yaml signature_mandatory.activation_date: 2026-10-08` | legacy login self-grant stays OFF (scope bypass) |

## Abuse rule (16 / 19)

| Item | Status | Evidence | What changed |
|---|---|---|---|
| Lexicon per language, variants, allow-list, severities | DONE | `abuse_lexicon.yaml`, `abuse_guard.py:detect` | **fixed** a deadlock (`screen()` re-entered its own lock); wired in (it was never called) |
| Never answered, warning only, masked bold word, tts warning only | DONE | `abuse_guard.screen`, `contract.publish` | both endpoints + stream (one delta, no status) |
| Same everywhere: history, logs, exports, drafts, import, generated text | DONE | `chat_history.mask`, `exports._clean`, `importer.validate`, `contract.publish` | one module; tts drops the word |
| Threats / self-harm keep their own handling | DONE | `abuse_guard.screen` defers to `safety` | |
| One cooldown (no overlap) | DONE | `safety.screen` skips its abuse branch when the guard is on | **fixed** double counting |

## Production guardrails (17)

| Item | Status | Evidence | What changed |
|---|---|---|---|
| Input guardrail first, same on both endpoints | DONE | `fos_api.py:_copilot_json` (early `check_input`) | was reached only after case resolution on `/fos/copilot` |
| Own list allowed, other people's refused | DONE | `case_list.yaml other_people`, `names_other_people` | "saare cases" lists own; "cases of other officers" refused |
| Scope at every tool boundary; not-yours = not-found | DONE | `fos_api.py:_own_case`, workspace not-found labels | new routes use it |
| Rate limits (chat, downloads) | DONE | `safety.screen` (once per request), `exports.allowed` | **fixed** a duplicate rate-limit count on `/copilot/query` |
| Audit of refusals, writes, exports, downloads, abuse | DONE | `audit.record` in each path | |
| Attack suite ≥ 120 cases, both endpoints | DONE | `test_master_17_attack_suite.py` (130 attacks × 2 endpoints, case open) | 0 cross-user / PII / stage-change / leakage |

## Nothing hardcoded (14), conflicts removed

| Item | Status | Evidence |
|---|---|---|
| New modules have no user-facing sentence in code | DONE | `test_master_14` STRICT set now includes product_flow, case_form, exports, importer, abuse_guard |
| Every officer-facing word in config | DONE | `product_flow.yaml`, `faq.yaml`, `case_list.yaml`, `copilot_reply.yaml`, `abuse_lexicon.yaml` |
| Old response fields / chips / emoji | DONE (behind compat flag) | `COPILOT_MD_TTS_CONTRACT=false` restores the old envelope for one release |
| Applicant-ID login | DONE | removed from scope; `LOS_LOGIN_SELF_GRANT_LEGACY` off |
| YAML booleans in phrase lists (`yes`, `no`) | DONE | quoted; a scan found none left in any config |

## Self-check (12)

30 messy multi-turn conversations (`tests/integration/selfcheck_conversations.py`, run by
`test_master_12_selfcheck.py`): 73 turns, **0 wrong**, clarify rate 2.7%. The first run found 25 problems. They
were fixed in the code, never by loosening the checks (except two places where the test itself was wrong:
"of 2" when the officer had 3 cases, and link targets counted as shown text).
