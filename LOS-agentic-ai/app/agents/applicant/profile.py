"""
The caller's OWN application details -- the basic information FOS captured.

    "What loan amount did I enter?"   -> application.loan_amount
    "Which mobile is registered?"     -> applicant.mobile
    "What product did I choose?"      -> application.product
    "What information have I given?"  -> which details are recorded

FROM THE RECORD, AND ONLY THE RECORD. Every value is read through the
applicant.get / application.get tools for the case the caller is authorised
for -- the same persisted applicant and application rows the FOS form wrote.
Never from conversation memory, retrieval or a model; never inferred. A field
the record does not carry is said to be not recorded, never guessed, never
replaced by an identifier.

SENSITIVITY. Each value passes app/security/sensitivity.py before it is
said: business fields in full, personal fields per the configured disclosure,
high-sensitivity identifiers masked, credentials never.

LANGUAGE-NEUTRAL. The field is detected on the canonical English words
(language.py + normalize.py), so "mera loan amount kya hai" and "माझ्या
application मध्ये loan amount किती आहे" reach the same field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

ALL = "ALL"

#: field -> (label, regex on canonical English). Order matters: the first
#: field a question names is the one answered.
_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("loan_amount", "loan amount",
     # RECORDED amounts only: "how much CAN I borrow" is an eligibility
     # question, and an EMI / income / balance amount is a different figure.
     r"^(?!.*\b(emi|instal\w*|foir|income|salary|balance|credit|fee|charges?)\b)(.*\b(loan\s+)?"
     r"(amount|amt|principal)\b|.*\bhow\s+much\b[^?]{0,20}\b(did|have|had)\s+(i|we)\b|"
     r".*\bkitna\s+loan\b|.*\bloan\s+(value|size)\b)"),
    ("tenure_months", "tenure",
     r"\b(tenure|loan\s+term|term\s+of\s+(the\s+|my\s+)?loan|repayment\s+(period|term)|"
     r"how\s+many\s+months|duration\s+of\s+(the\s+|my\s+)?loan)\b"),
    ("interest_rate_pct", "interest rate",
     r"\b(interest\s+rate|rate\s+of\s+interest|roi)\b"),
    ("product", "product",
     r"\b(product|loan\s+type|type\s+of\s+loan|kind\s+of\s+loan|scheme)\b"
     r"|\b(which|what)\s+loan\s+(did|have|am|was)\b"),
    ("employment_type", "employment type",
     r"\b(employment(\s+type)?|occupation|job\s+type|profession|work\s+type)\b"),
    ("mobile", "mobile number",
     r"\b(mobile|phone|cell)(\s+(no|number|num))?\b|\bcontact\s+(no|number|num|details)\b"),
    ("email", "email",
     r"\b(e-?mail|mail\s+id)(\s+(id|address))?\b"),
    ("date_of_birth", "date of birth",
     r"\b(dob|d\.o\.b|date\s+of\s+birth|birth\s*date|birthday|born)\b"),
    ("address", "address",
     r"\baddress\b(?!\s+proof)"),
    ("full_name", "name",
     r"\b(name|naam|full\s+name)\b(?!\s+(mismatch|match|on\s+(my|the)\s+(pan|aadhaar|bank|passport)))"),
)

#: A RECORDED-VALUE cue: a possessive, "did I / have I", or the record
#: itself. A bare "I" is not one: "I am self-employed, what do I need?" asks
#: about requirements, not about what was recorded.
_OWNERSHIP = re.compile(
    r"\b(my|mine|our|mera|meri|mere|hamara|humara|registered|recorded|provided?|entered|"
    r"submitted|submit|gave|given|filled|on\s+file|this\s+(case|application)|"
    r"the\s+applicant|applicant'?s|application|applied)\b|\b(did|have|had)\s+(i|we)\b", re.I)
_ASKING = re.compile(
    r"\b(what|which|whats|how|tell|show|give|confirm|check|do\s+you\s+have|is\s+there|kya|kitna|"
    r"kaunsa|batao|bata)\b|\?\s*$", re.I)
_WRITE = re.compile(
    r"\b(update|change|correct|edit|modify|set|add|capture|save|replace|fix|remove|delete)\b", re.I)
_EVERYTHING = re.compile(
    r"\bwhat\s+(all\s+)?(information|details|info|data)\s+(have|did)\s+(i|we)\s+"
    r"(submit\w*|provid\w*|give|given|enter\w*|fill\w*|share\w*)\b"
    r"|\bwhat\s+(all\s+)?(have|did)\s+(i|we)\s+(submit\w*|provid\w*|fill\w*|enter\w*)\b"
    r"|\b(show|give|tell)\s+(me\s+)?my\s+(basic\s+|personal\s+)?(details|information|info|profile)\b"
    r"|^\s*my\s+(basic\s+|personal\s+)?(details|information|info|profile)\s*\??\s*$", re.I)
#: "Are my basic details complete?" -- answered by the missing-information intent.
COMPLETENESS = re.compile(
    r"\b(are|is)\s+(my|the|our)\s+(basic\s+|personal\s+|applicant\s+)?(details|information|info|"
    r"profile|data)\s+(complete|completed|filled|done|full|captured)\b"
    r"|\b(details|information|info)\s+(are\s+|is\s+)?(still\s+)?(missing|pending|left|incomplete)\b"
    r"|\bwhat\s+(details|information|info)\s+(are|is)\s+(still\s+)?(missing|pending|left|needed)\b",
    re.I)


@dataclass(frozen=True)
class Question:
    field: str          # a field name, or ALL


def detect(text: str) -> Question | None:
    """Which recorded detail the question asks about, or None."""
    said = " ".join(str(text or "").split())
    if not said or _WRITE.search(said):
        return None
    if _EVERYTHING.search(said):
        return Question(ALL)
    if not (_OWNERSHIP.search(said) and _ASKING.search(said)):
        return None
    for field, _label, pattern in _FIELDS:
        if re.search(pattern, said, re.I):
            return Question(field)
    return None


def label(field: str) -> str:
    return next((lbl for name, lbl, _p in _FIELDS if name == field), field.replace("_", " "))


def _rupees(raw: str) -> str | None:
    """₹ in Indian grouping, only when the recorded value is a plain number."""
    digits = re.sub(r"[,\s₹]|rs\.?|inr", "", str(raw or ""), flags=re.I)
    if not re.fullmatch(r"\d+(\.\d{1,2})?", digits):
        return None
    whole, _, paise = digits.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", head)
        whole = f"{head},{tail}"
    return f"₹{whole}" + (f".{paise}" if paise and paise.strip("0") else "")


def _date(raw: str) -> str:
    from datetime import date

    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(raw or "").strip()[:10])
    if not match:
        return str(raw)
    try:
        return date(int(match[1]), int(match[2]), int(match[3])).strftime("%d %B %Y").lstrip("0")
    except ValueError:
        return str(raw)


def _readable(value: str) -> str:
    return " ".join(w.capitalize() for w in str(value).replace("_", " ").split())


def _values(results: dict[str, Any]) -> dict[str, Any]:
    from app.agents.applicant.answer import _get

    applicant = _get(results, "applicant.get", "applicant") or {}
    application = _get(results, "application.get", "application") or {}
    return {**{k: applicant.get(k) for k in ("full_name", "mobile", "email",
                                             "date_of_birth", "address")},
            **{k: application.get(k) for k in ("product", "loan_amount", "employment_type",
                                               "tenure_months", "interest_rate_pct")},
            "_missing": list(applicant.get("missing_fields") or [])}


def _said(field: str, value: Any) -> str:
    from app.security import sensitivity

    disclosure = sensitivity.disclosure(field)
    if disclosure == "withhold":
        return f"For your security, I can't show your {label(field)} here."
    shown = sensitivity.mask(str(value)) if disclosure == "masked" else str(value)
    if field == "loan_amount":
        return f"Your application records show a loan amount of {_rupees(value) or shown}."
    if field == "product":
        return f"You applied for a {_readable(shown)}."
    if field == "employment_type":
        return f"Your employment type is recorded as {_readable(shown).lower()}."
    if field == "tenure_months":
        return f"Your application has a tenure of {shown} months."
    if field == "interest_rate_pct":
        return f"The interest rate on your application is {shown}%."
    if field == "date_of_birth":
        return f"The date of birth on your application is {_date(shown) if disclosure == 'full' else shown}."
    return f"The {label(field)} on your application is {shown}."


def answer(question: Question, results: dict[str, Any]) -> str:
    """The answer, from the records the tools returned."""
    values = _values(results)
    if question.field == ALL:
        recorded = [label(f) for f, _l, _p in reversed(_FIELDS) if values.get(f)]
        missing = [label(f) for f in values["_missing"]]
        if not recorded:
            return "I don't have any of your details recorded on this application yet."
        said = f"Your application has your {_and(recorded)} recorded."
        if missing:
            said += f" Still to add: {_and(missing)}."
        return said
    value = values.get(question.field)
    if value in (None, ""):
        return f"I don't have your {label(question.field)} recorded on this application yet."
    return _said(question.field, value)


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


__all__ = ["ALL", "COMPLETENESS", "Question", "answer", "detect", "label"]
