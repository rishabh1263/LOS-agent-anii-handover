"""
FIELD SENSITIVITY -- how much of a value an answer may show.

    NON_SENSITIVE_BUSINESS      product, loan amount, tenure, stage, status
    SENSITIVE_PERSONAL          name, mobile, email, date of birth, address
    HIGH_SENSITIVITY_IDENTIFIER PAN, Aadhaar, bank account number
    SECRET_CREDENTIAL           tokens, passwords, keys (never shown)

Each level maps to a DISCLOSURE: `full`, `masked` (last four characters) or
`withhold`. The mapping is configuration (`chatbot.sensitivity` in
applicant_agent.yaml) and its defaults are NOT a legal or compliance policy
-- they are the conservative reading of what the repository already does:

  * SENSITIVE_PERSONAL -> full, to the authorised owner of the case. This is
    the existing APPLICANT_DETAILS behaviour (the FOS stage already shows the
    applicant's captured details to whoever holds the case).
  * HIGH_SENSITIVITY_IDENTIFIER -> masked. No existing rule shows a full PAN,
    Aadhaar or account number in chat; minimum-necessary disclosure applies.
  * SECRET_CREDENTIAL -> withhold, always, whatever configuration says.

This module never authorises anything. It runs only on data the caller is
already authorised for (ownership and scopes decide that, first), and it is
applied to published answers AND to what a composer model is shown, so a
full identifier never reaches a prompt either.
"""

from __future__ import annotations

import re
from typing import Any

NON_SENSITIVE_BUSINESS = "NON_SENSITIVE_BUSINESS"
SENSITIVE_PERSONAL = "SENSITIVE_PERSONAL"
HIGH_SENSITIVITY_IDENTIFIER = "HIGH_SENSITIVITY_IDENTIFIER"
SECRET_CREDENTIAL = "SECRET_CREDENTIAL"

_DEFAULT_FIELDS = {
    "full_name": SENSITIVE_PERSONAL, "mobile": SENSITIVE_PERSONAL,
    "email": SENSITIVE_PERSONAL, "date_of_birth": SENSITIVE_PERSONAL,
    "address": SENSITIVE_PERSONAL,
    "product": NON_SENSITIVE_BUSINESS, "loan_amount": NON_SENSITIVE_BUSINESS,
    "employment_type": NON_SENSITIVE_BUSINESS, "tenure_months": NON_SENSITIVE_BUSINESS,
    "interest_rate_pct": NON_SENSITIVE_BUSINESS,
    "pan_number": HIGH_SENSITIVITY_IDENTIFIER, "aadhaar_number": HIGH_SENSITIVITY_IDENTIFIER,
    "bank_account_number": HIGH_SENSITIVITY_IDENTIFIER,
    "password": SECRET_CREDENTIAL, "token": SECRET_CREDENTIAL, "api_key": SECRET_CREDENTIAL,
}
_DEFAULT_DISCLOSURE = {
    NON_SENSITIVE_BUSINESS: "full", SENSITIVE_PERSONAL: "full",
    HIGH_SENSITIVITY_IDENTIFIER: "masked", SECRET_CREDENTIAL: "withhold",
}

#: Identifiers recognisable in free text, masked wherever they appear. Bounded
#: so they never match INSIDE a longer token (a hex request id that happens to
#: hold twelve digits is not an Aadhaar number).
_PAN = re.compile(r"(?<![\w-])[A-Z]{5}\d{4}[A-Z](?![\w-])")
_AADHAAR = re.compile(r"(?<![\w-])\d{4}[ -]?\d{4}[ -]?\d{4}(?![\w-])")
_ACCOUNT = re.compile(r"(?i)\b(account|a/c|acct|acc)(\s*(no\.?|number|#))?[\s:.-]*"
                      r"(\d[\d -]{7,20}\d)(?![\w-])")

#: Keys that carry identifiers of the SERVICE's own records (never customer
#: identifiers) and must not be rewritten by a pattern.
ID_KEYS = frozenset({"request_id", "correlation_id", "case_id", "applicant_id",
                     "party_id", "conversation_id", "document_id", "problem_id",
                     "event_id", "record_id", "source_id"})


def _settings() -> dict[str, Any]:
    try:
        from app.agents.applicant import config

        return config.chatbot("sensitivity")
    except Exception:  # pragma: no cover
        return {}


def level(field: str) -> str:
    configured = (_settings().get("fields") or {}).get(field)
    return str(configured or _DEFAULT_FIELDS.get(field, SENSITIVE_PERSONAL))


def disclosure(field: str) -> str:
    """full | masked | withhold for this field. Secrets are always withheld."""
    lvl = level(field)
    if lvl == SECRET_CREDENTIAL:
        return "withhold"
    configured = (_settings().get("disclosure") or {}).get(lvl)
    value = str(configured or _DEFAULT_DISCLOSURE.get(lvl, "masked")).lower()
    return value if value in {"full", "masked", "withhold"} else "masked"


def keep_last() -> int:
    try:
        return max(0, min(6, int(_settings().get("mask_keep_last", 4))))
    except (TypeError, ValueError):
        return 4


def mask(value: str, keep: int | None = None) -> str:
    """Every character but the last `keep` replaced by X; spaces dropped."""
    raw = re.sub(r"[\s-]", "", str(value or ""))
    keep = keep_last() if keep is None else keep
    visible = raw[-keep:] if keep and len(raw) > keep else ""
    return "X" * (len(raw) - len(visible)) + visible


def mask_identifiers(text: str) -> str:
    """
    PAN, Aadhaar and bank account numbers in free text, per the disclosure
    policy: `masked` keeps the last 4 (XXXXXXXX1234 / XXXXXX234F), `withhold`
    replaces the value entirely, `full` leaves it.
    """
    if not text:
        return text
    out = text

    def shown(field: str, value: str) -> str:
        policy = disclosure(field)
        return value if policy == "full" else mask(value) if policy == "masked" else "[withheld]"

    # Accounts first: a twelve-digit account number is not an Aadhaar number.
    def account(m: re.Match[str]) -> str:
        return m.group(0)[:m.start(4) - m.start(0)] + shown("bank_account_number", m.group(4))
    out = _ACCOUNT.sub(account, out)
    out = _AADHAAR.sub(lambda m: shown("aadhaar_number", m.group(0)), out)
    out = _PAN.sub(lambda m: shown("pan_number", m.group(0)), out)
    return out


def mask_payload(value: Any, *, _key: str | None = None) -> Any:
    """
    `mask_identifiers` over every string in a structure -- a composer's input
    or a whole published response. The service's own record-id fields are
    left untouched (ID_KEYS).
    """
    if _key in ID_KEYS:
        return value
    if isinstance(value, str):
        return mask_identifiers(value)
    if isinstance(value, dict):
        return {k: mask_payload(v, _key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_payload(v) for v in value]
    if isinstance(value, tuple):
        return tuple(mask_payload(v) for v in value)
    return value


__all__ = ["HIGH_SENSITIVITY_IDENTIFIER", "NON_SENSITIVE_BUSINESS", "SECRET_CREDENTIAL",
           "SENSITIVE_PERSONAL", "ID_KEYS", "disclosure", "keep_last", "level", "mask",
           "mask_identifiers", "mask_payload"]
