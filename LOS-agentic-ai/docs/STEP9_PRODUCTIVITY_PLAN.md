# Step 9: Officer Productivity Features

GOAL: make an FOS officer's day easy. Users can ask ANYTHING about their cases in natural language (English/Hindi/Hinglish), with NOTHING hardcoded per question.

## CORE: SEMANTIC QUERY LAYER (build first)
1. Qwen converts the question into a structured query JSON, e.g.
   "7 din se atke KYC fail cases" → {"entity":"cases","filters":[{"field":"kyc_status","op":"eq","value":"FAILED"},{"field":"days_in_stage","op":"gt","value":7}],"metric":"list","sort":"days_in_stage desc","limit":20}
   Supported: metric = count | list | group_by | summary; filters, sort, limit, time ranges ("aaj", "is hafte", "pichhle mahine").
2. A semantic catalogue (yaml, not code) defines allowed entities, fields, synonyms (Hinglish too), operators and computed fields (days_in_stage, pending_docs_count, kyc_status per party, etc.). Adding a field = one config entry.
3. Validation against the catalogue allow-list. Unknown field/op → ONE clarifying question, never a guess.
4. Our code builds parameterized SQL from the validated JSON. Qwen NEVER writes SQL.
5. Scope is injected by code, never by the model: every query is limited to the caller's accessible cases (access_grants / role scope).
6. Results go through templates (Response Style Guide). Numbers come only from the DB.
7. Follow-ups via session memory: "unme se kitne co-applicant wale?", "list dikhao", "pehle wale ka detail".
8. Fast lane first; the semantic layer handles the rest.
9. Guardrail update: "my cases / mere cases" counts and lists are allowed within scope; anything outside scope stays refused.

## FEATURES (all built on the core)

F1. Morning brief ("aaj kya karna hai?", "my day", "kya pending hai"): total cases, ✅ ready for CPA, ❌ KYC failed, ⏳ waiting on documents, ⚠️ stuck 7+ days, then 👉 the next action. Priority: ready > KYC failed > stuck longest > docs pending (configurable).

F2. Ask anything about my cases: counts, lists, filters, group-by. E.g. "kitne pending hain", "kitne pass hue", "KYC fail wale dikhao", "is hafte kitne cases aaye", "kis case mein co-applicant ka doc baaki hai", "sabse purana pending case". Definitions in yaml (show defaults for approval): PENDING = not DISBURSED and not REJECTED; PASSED = reached CPA or beyond.

F3. Case summary + Excel download. Chat summary: parties (masked), loan details, stage, KYC per party, documents, blockers, recent stage history, next step. Excel: single-case (Overview, Parties, Documents, KYC, Stage History) and portfolio (Summary, Cases, Pending Documents), filterable by any semantic query. Use an existing library; no new dependency without asking. PII masked unless the role allows. Secure download endpoint (JWT + scope), 15-min expiry, 📥 link, audit-logged, large reports async ("⏳ Generating your report…"). Include a Glossary sheet (abbreviation rule).

F4. Visit checklist ("Sharma ji ke case mein kya lena hai?"): everything to collect in person per party: missing docs, docs to redo (with reason), signature, originals to verify. Grouped 👤/👥.

F5. Instant upload feedback: ✅ verified / ⚠️ mismatch (retake while the customer is there) / ❌ unreadable, from existing verification/OCR; ⏳ while queued, result pushed via the step 7 stream.

F7. Stuck case alerts: days in current stage > threshold (configurable per stage, default 5), in the morning brief and on demand ("atke hue cases"), each with its main blocker in one line.

## RULES
- Flag COPILOT_PRODUCTIVITY (default off) with sub-flags per feature.
- Follow docs/CHATBOT_SPEC.md; add section 7 "Officer productivity features" (incl. abbreviation rule in Excel/morning brief).
- No new dependencies without asking; RAM budget respected; heavy work async.
- Every count/list/report audit-logged with the caller and the query JSON.

## FIRST (read-only) when step 9 starts, then STOP
1. Roles/scopes today; how "my cases" is determined for an officer.
2. Available Excel library.
3. A field for "days in current stage".
4. Proposed semantic catalogue v1 + PENDING/PASSED/STUCK defaults.
5. Sub-step plan + tests (scope isolation, counts match DB, query-JSON validation, refusal outside scope, Excel correctness, Hinglish phrasings).

Pending with sir (record in the Master checklist): PENDING/PASSED definitions, officer vs branch scope, Excel masking.