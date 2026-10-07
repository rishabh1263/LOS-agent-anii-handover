# 6j: Learning & personalisation (flag COPILOT_LEARNING, default off) -- PARTIAL, the message was cut off

Saved word for word from the user's message (2026-10-07). Build after step 8's tests are green, before step 9.
Any new tables go in a separate migration 0007 proposal: show the SQL and wait.

1. FEEDBACK LOOP: 👍/👎 actions on every reply (FEEDBACK_UP/DOWN + turn_id); 👎 reason chips ("wrong answer", "didn't understand", "too long"). Store: masked question, intent, source (fast lane/bank/Qwen), latency. 👎 → evals/feedback_review.yaml review queue. Nothing auto-updates the bank: a person approves, then a script adds approved items to the example bank / router_misses.yaml. Weekly report script: top 👎 intents, unanswered phrasings, accuracy trend.
2. LONG-TERM MEMORY (safe preferences only): preferred language, last active case_id, terms already explained, reply length. No case data, no PII. New

[CUT OFF HERE -- the rest of point 2 and any further points were not received; asked the user to resend.]
