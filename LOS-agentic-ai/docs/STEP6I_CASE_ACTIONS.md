# 6i: CASE ACTIONS (flag COPILOT_CASE_ACTIONS, default off)

Saved from the user's MASTER RUN message (2026-10-07), word for word:

1. 👁️ VIEW_DOCUMENT action → short-lived signed URL (5 min), scope-checked, audited, no file paths exposed.
2. RAISE QUERY on a not-verified doc / KYC mismatch: offer → show a DRAFT from the actual reason (Send/Edit buttons) → create via queries.raise_query only after explicit confirmation. Never auto-send.
3. SEND TO CUSTOMER: read-only check for an existing channel (SMS/WhatsApp/email/portal/notifications). If one exists, use it after confirmation. If not: record the query, show a copyable message + a "mark as sent" button, and log "no customer channel" for my sir.
4. TRACK QUERY: list the case's queries (ID, reason, status, days open, reply).
5. NEW CASE: return an OPEN_UI_NEW_CASE action (button), no case creation in chat.
