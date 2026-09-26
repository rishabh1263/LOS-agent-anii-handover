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

#: Identifiers recognisable in free text, masked wherever they appear.
_PAN = re.compile(r"\b([A-Z]{5})(\d{4})([A-Z])\b")
_AADHAAR = re.compile(r"(?<!\d)(\d{4})[ -]?(\d{4})[ -]?(\d{4})(?!\d)")
_ACCOUNT = re.compile(r"(?i)\b(account|a/c|acct|acc)(\s*(no\.?|number|#))?[\s:.-]*(\d[\d -]{7,20}\d)")


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


def mask(value: str, keep: int = 4) -> str:
    raw = str(value or "")
    visible = raw[-keep:] if len(raw) > keep else ""
    return "X" * max(0, len(raw) - len(visible)) + visible


def mask_identifiers(text: str) -> str:
    """Mask PAN, Aadhaar and account numbers in free text, per policy."""
    if not text:
        return text
    out = text
    if disclosure("pan_number") != "full":
        out = _PAN.sub(lambda m: mask(m.group(0))
                       if disclosure("pan_number") == "masked" else "[withheld]", out)
    if disclosure("bank_account_number") != "full":
        def account(m: re.Match[str]) -> str:
            digits = re.sub(r"\D", "", m.group(4))
            shown = mask(digits) if disclosure("bank_account_number") == "masked" else "[withheld]"
            return m.group(0)[:m.start(4) - m.start(0)] + shown
        out = _ACCOUNT.sub(account, out)
    if disclosure("aadhaar_number") != "full":
        out = _AADHAAR.sub(lambda m: "XXXX XXXX " + m.group(3)
                           if disclosure("aadhaar_number") == "masked" else "[withheld]", out)
    return out


def mask_payload(value: Any) -> Any:
    """`mask_identifiers` over every string in a structure (composer input)."""
    if isinstance(value, str):
        return mask_identifiers(value)
    if isinstance(value, dict):
        return {k: mask_payload(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_payload(v) for v in value]
    return value


__all__ = ["HIGH_SENSITIVITY_IDENTIFIER", "NON_SENSITIVE_BUSINESS", "SECRET_CREDENTIAL",
           "SENSITIVE_PERSONAL", "disclosure", "level", "mask", "mask_identifiers",
           "mask_payload"]
