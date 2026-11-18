"""
Masking of free text before it is stored (audit excerpts, log messages): personal identifiers, credentials and bearer
tokens are replaced by a label. Shared by the chatbot audit trail (app/agents/applicant/audit.py) and the agent_logs
handler (app/agent_db/logs.py), so both mask exactly the same things.
"""

from __future__ import annotations

import re

#: what is masked, and what replaces it. Redact first, then cut: cutting first could leave half an identifier that the
#: pattern no longer recognises.
REDACTIONS = (
    (re.compile(r"\b[A-Za-z]{5}\d{4}[A-Za-z]\b"), "[PAN]"),
    (re.compile(r"(?i)\b(account|a/c|acct|acc)(\s*(no\.?|number|#))?[\s:.-]*\d[\d -]{7,20}\d"),
     r"\1 [ACCOUNT]"),
    (re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"), "[AADHAAR]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[EMAIL]"),
    (re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)"), "[PHONE]"),
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"), "[TOKEN]"),
    # a credential pasted into a question or an error message
    (re.compile(r"\bBearer\s+[\w.~+/-]{12,}", re.IGNORECASE), "[TOKEN]"),
    (re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9]{16,}|\bAKIA[0-9A-Z]{16}\b"), "[KEY]"),
    (re.compile(r"\b(api[_-]?key|secret|password|passwd|client_secret|"
                r"access_token|refresh_token|token)\s*[:=]\s*\S+", re.IGNORECASE),
     r"\1=[REDACTED]"),
    # a connection string with a password (a driver error can carry one)
    (re.compile(r"\b(postgres(?:ql)?|mysql|redis|amqp)://[^\s:/@]+:[^\s@]+@", re.IGNORECASE), r"\1://[CREDENTIALS]@"),
)


def redact(text: str) -> str:
    """The text with personal identifiers, credentials and bearer tokens masked."""
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


__all__ = ["REDACTIONS", "redact"]
