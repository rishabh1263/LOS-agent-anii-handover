> SUPERSEDED 2026-10-07 for the FOS scope by docs/FOS_E2E_PRODUCTION_PLAN.md (kept for history).

# FOS STAGE: PRODUCTION-READY END TO END

Status: PART 1 of 2 SAVED 2026-10-07 (part 2 to follow). Build AFTER the current transcript bug fixes.
Speed-mode rules apply. NO HARDCODING: everything derived from config (stage_gates.yaml, kyc_policies.yaml,
applicant_agent.yaml checklists) so new rules show up automatically.

## P1. FOS → CPA READINESS (ask anything)
- Any question about moving to CPA ("CPA mein kab jayega", "CPA ke liye kya chahiye", "ready hai kya",
  "what's blocking CPA") → a readiness answer built FROM THE GATE CONFIG + live data, not a fixed list.
- List every requirement and its state, grouped: application fields, applicant documents, co-applicant
  documents, document verification, KYC per party (each check), signature, anything else the FOS gate
  evaluates. Clear PASS / PENDING / FAILED / REVIEW per item, with the reason and the fix.
- Follow-ups work: "KYC wala detail", "co-applicant ka kya", "kaunsa document galat hai".

## P2. NAME MATCHING ACROSS ALL DOCUMENTS (clean KYC results)
- The name on the application (per party) must match the name on EVERY identity/financial document of that
  party (PAN, Aadhaar, DL, passport, voter ID, bank statement, salary slip, ITR… whatever has a name). Use the
  existing KYC name comparison and policy (normalisation, initials, order); don't invent a new matcher.
  Report: confirm which documents are compared today; add missing ones via config.
- KYC result shown cleanly per party: check | document | value on document | value on application | result |
  reason. Values come from recorded findings only, masked per policy.

## P3. LANGUAGE LOCK
- The frontend's selected language (the reply_language field) is AUTHORITATIVE. If English is selected, every
  reply is English even when the user writes Hindi/Hinglish/Marathi; understanding still works in any
  language. Same for other selected languages. No auto-switching when a language is set. If none is set, keep
  the current detection.
- Covers ALL text: answers, clarifying questions, options, buttons, errors, guardrail refusals, document
  reasons. Tests: Hinglish input + English selected → English output for every intent.

## P4. PROFESSIONAL FORMAT (no emojis)
- Remove ALL emojis from responses (answer, markdown, plain, buttons, headers). Config switch
  style.emojis=false (default for production).
- Point-wise: short heading line, numbered or bulleted points, bold only key words, one "Next step:" line at
  the end. Max ~8 lines except lists.
- Update CHATBOT_SPEC section 1 (emoji map now optional/off) and the snapshot tests. Update frontend_handoff
  examples.

## Part 2
(pending -- to be appended when received)
