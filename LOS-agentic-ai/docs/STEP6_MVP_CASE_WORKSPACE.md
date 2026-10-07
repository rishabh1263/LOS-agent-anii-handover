# 6-MVP: Case Workspace (demo priority, minimal and useful)

FLOW
1. "mere cases dikhao" / "my cases" / "kaunse cases hai" → list ONLY the caller's cases (access_grants scope, injected by code):
   - a header with counts: total, ⏳ pending, ✅ completed, ❌ rejected (definitions in yaml; PENDING = not DISBURSED and not REJECTED)
   - numbered rows: case ID, applicant name (masked per policy), 📍 stage, one-line status (⚠️ KYC issue / ⏳ N docs pending / ✅ ready)
   - max 10 rows, then "aur dikhao" for more
   - 👉 "Kis case mein jaana hai?"
2. SELECT a case by number ("2", "doosra"), case ID, applicant ID (APP-…), co-applicant ID (COAPP-… → its case), or applicant name ("Priya ka case"). Two matches → one clarifying question.
3. ENTER the case: store active_case_id in the session state (a minimal slice of 6c memory; conversation_state, no migration). Reply: 🔓 **CASE-xxx (Name)** opened, with a 2-line snapshot (stage + main blocker) and 👉 a suggested question.
4. INSIDE the case: every question uses active_case_id automatically: status, docs, KYC, co-applicant, why stuck, next step, summary. Existing tools only. Every reply starts with a small "📍 CASE-xxx" line.
5. SWITCH/EXIT: "dusra case kholo", "CASE-B623 kholo", "bahar aao", "list dikhao" → close (🔒) and switch or show the list. Never mix data between cases.
6. A question about ANOTHER case while inside one → answer it if in scope, then ask "Is case mein jaana hai?"; never switch silently.

CLICKABLE CASES (structured actions)
- The list returns presentation.case_list: [{case_id, applicant_id, applicant_name (masked), stage, status_label, emoji, action: {type: "open_case", case_id}}]. The frontend renders each row as a clickable card.
- A click sends action=OPEN_CASE with case_id (not free text). The backend checks scope and enters the case exactly like a typed selection. Out of scope → the same 403 as today.
- Typed selection and direct opening ("CASE-852C… kholo", "APP-18E7… ka case") also work, without the list.
- Inside a case: a structured "📍 CASE-xxx" header + an EXIT_CASE button + a "switch case" button (shows the list) + quick-question buttons: "Kya baaki hai?", "Kyu atka hai?", "Co-applicant", "Summary" (a click sends that text).
- Document the payload contract (fields, action types, example JSON) in README_CHATBOT.md for the frontend.

RULES
- Guardrail: "my cases" listing is allowed within scope; outside scope stays refused.
- Flag COPILOT_CASE_WORKSPACE (default off).
-
  (the cut bullet, 2026-10-07:) Follow CHATBOT_SPEC formatting. English, Hindi, Hinglish.
