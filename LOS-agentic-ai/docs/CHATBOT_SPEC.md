# Chatbot Spec

> Saved word for word from the user's instructions (received 2026-10-07, in three messages).
> **COMPLETE:** sections 1–6.

=== 1. RESPONSE STYLE GUIDE ===
Goal: clean, scannable, professional. Never a wall of text.
- First line: short direct answer with a status emoji.
- Bold only the key words: status, document name, mismatch field, amount, date.
- Bullet points for any list, one short item per line.
- Sections get a short bold heading plus an emoji, with a blank line between sections.
- End with ONE next step (👉) or ONE question.
- Max ~8 lines, except document lists.
- Same style in English, Hindi, Hinglish and Marathi.

Emoji map (use ONLY these, consistently):
✅ verified/done | ❌ rejected/failed | ⏳ pending/under review | ⚠️ mismatch/attention
📄 document | 📤 upload action | 👤 applicant | 👥 co-applicant
💰 amount/EMI | 📍 current stage | 👉 next step | ℹ️ info/definition

Example (documents):
⚠️ **KYC verification failed.** A few details don't match.

**❌ Mismatch found**
- **Name**: PAN shows "Rahul Kumar Sharma", DL shows "Rahul Sharma"

**📤 Upload correct documents**
- 📄 **PAN Card**

**⏳ Still pending**
- 📄 **Bank Statement**

👉 Upload these to move your case to **CPA**.

Example (status): 📍 Your application **LN1023** is in **FOS stage**. ⏳ Waiting on **2 documents**. 👉 Want to see which ones?
Example (definition): ℹ️ **CIBIL score** is a 3-digit number (300–900) showing repayment reliability. 👉 Your credit check is handled by the Credit team.
Example (out of scope): 🙏 I can only help with your loan application. 👉 Check your **status** or **pending documents**?

Technical: emojis and bullets go in both answer_markdown and answer_plain; answer_plain drops the ** markers. Add snapshot tests (English + Hinglish).

ABBREVIATION RULE (added by the user, 2026-10-07)
- The FIRST time an abbreviation appears in a chat session, show its full form in brackets: "**KYC (Know Your Customer)**". After that, only the short form in the same session. Track "already explained" terms in session memory.
- If the user asks "X kya hai?" / "what is X?" / "X ka full form?", reply with the full form plus a one-line meaning, in the user's language.
- Keep the full form in English, with the explanation in the user's language, e.g. Hinglish: "**KYC (Know Your Customer)**: aapki identity verify karne ki process."
- Apply it to bot replies, case summaries, Excel reports (a glossary sheet) and the morning brief.

GLOSSARY
- Put it in a yaml file (config, not code) so new terms can be added without code changes.
- Read-only first: scan the codebase, configs and knowledge files for every abbreviation the bot can show (e.g. KYC, PAN, FOS, CPA, CREDIT, RCU, BOPS, HOPS, FOIR, LTV, EMI, ITR, DL, OVD, CIBIL, GST, AA, JEV, OCR, ...) and propose a glossary table: abbreviation | full form | one-line meaning | source where found.
- Mark any term whose full form isn't defined in the project as "NEEDS CONFIRMATION". Don't guess company-specific terms (stage names like BOPS/HOPS/RCU especially).
- Show me the glossary for approval before using it.

Add snapshot tests: the first mention is expanded, the second isn't, and "KYC kya hai?" gives full form + meaning.

=== 2. RESPONSE FORMAT CONTRACT ===
1. Direct answer first. No greeting, no filler.
2. ONE next step or ONE question at the end, never both, never more than one question.
3. Reply in the user's language.
4. Data not available: say so with the reason. Never invent.

=== 3. CLARIFYING QUESTIONS & UNDERSTANDING (intelligence) ===
The bot must behave like a smart human assistant, not a menu.
a) Unclear question: never show a generic menu. Ask ONE short, specific question using what it already knows (case, party, last topic), offering 2–3 concrete options.
   Example: "Aap **status** jaanna chahte hain ya **pending documents**?"
   Example: "Applicant ke docs dekhne hain ya **co-applicant (Priya)** ke?"
b) Partly clear: answer the clear part, then ask only about the missing piece.
c) Never ask something the bot can find out itself (case ID with one case, the party from context, etc.).
d) Yes/No understanding: when the bot asked a question, the next reply is read in that context.
   - YES: haan, ha, haa, yes, ok, okay, theek hai, kar do, chalega, sure, bilkul, hmm ok, 👍
   - NO: nahi, na, no, mat karo, rehne do, abhi nahi, baad mein, 👎
   - YES → do exactly what was offered. NO → acknowledge and offer one alternative.
   - Ambiguous ("pata nahi", "shayad") → rephrase the question more simply.
e) Choosing an option: "pehla wala", "second", "co-applicant wala", "2" → map to the options the bot offered.
f) Corrections: "nahi, mera matlab tha..." → drop the last assumption and redo it.
g) Typos and mixed language must still be understood ("pandig docs", "kyc kab hoga").
h) Store the pending question and its offered options in the session state, so (d) and (e) work.

=== 4. "VERIFY / UPLOAD" DIAGNOSE RULE ===
When the user says anything like "verify karna hai", "upload karna hai", "document dalna hai", "KYC complete karna hai" or "kya upload karu", do NOT ask "which document?". Diagnose the case and show exactly what's needed:
- Check every party (applicant + each co-applicant) for: mandatory documents not uploaded, REJECTED documents (with reason), KYC failures (field + values on each document), signature missing/rejected, documents in REVIEW (info only).
- For each item, show WHAT is wrong, WHERE (party, document, field) and WHAT to upload. For a KYC mismatch, suggest the likely wrong document when the finding shows it; otherwise ask for the correct one of the two.
- Order: ❌ fix first → ⏳ pending → ℹ️ under review.
- All complete: "✅ All documents are verified. Your case is ready for **CPA**."
- Group by party (👤 / 👥). Extend the step 4 document action view, don't build a new one.

=== 5. FULL-SESSION CHATBOT MEMORY ===
The bot remembers the WHOLE chat session, not just the last 6 turns. Reuse conversation_state, no new dependencies.
1. Full history: every turn stored, masked.
2. Structured session state, updated each turn: active case + party (by applicant/co-applicant ID), issues/documents last shown, uploads in this chat and their latest results, the pending question + offered options, language preference.
3. Rolling summary: older turns compressed into a short factual summary (deterministic where possible).
4. The router gets summary + state + last 6 turns, never the full raw history (CPU speed).
5. Lifetime: the whole session. Replace the 30-min TTL with a configurable 24-hour inactivity timeout. A new chat starts fresh.
Must work:
- "LN1023 ka status" → later "uske docs?" → uses LN1023
- PAN mismatch shown → user uploads PAN → "ab theek hai?" → re-checks PAN
- "co-applicant ka dikhao" → later "aur applicant ka?" → switches party
- 20+ messages later: "pehle wala case" → remembers
- After an upload: "aur kya baaki?" → excludes what was just verified

=== 6. DOCUMENT & STAGE RULES ===
- Co-applicant mandatory: PAN, address proof, employment proof (configurable).
- Every co-applicant has their own ID, like the applicant (step 5d).
- Signature mandatory (new cases only, from activation_date), presence check only, no matching.
- FOS → CPA only when KYC passes for all parties (name, DOB, address, PAN, father's name; income excluded).
- Qwen router is ON by default; its flag is only an emergency kill-switch; the low-memory guard falls back to the fast lane.

Co-applicant ID (step 5d -- recorded from the approved design, 2026-10-07; LOS_COAPP_IDENTITY):
- Every co-applicant has a system-generated id COAPP-<12 hex>, like APP-<12 hex>; globally unique.
- Understood by id ("COAPP-1023 ke docs kya baaki hai?"), by name ("Priya ke docs") and by role ("co-applicant ka KYC").
- Allowed only when the caller owns the case the id is on; otherwise the same refusal as any other customer's case.
- Session memory keeps the active party by id; "aur applicant ka?" switches party.
- Answers about a co-applicant start with: 👥 **Co-applicant: Priya Sharma (COAPP-…)**
- A name not declared on the form is shown only once their KYC name check has PASSED -- never from unverified OCR.

Implementation: sections 1–2 into the templates; sections 3 and 5 in step 6; section 4 extends step 4's view.
Testing in step 8: 40+ real phrasings including yes/no replies, option picks, corrections, typos and Hinglish, with accuracy per category.
