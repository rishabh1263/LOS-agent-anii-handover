"""
MIFIN CUSTOMER DEDUPE -- the response normaliser and the client interface (Phase B, 2026-10-22).

WHAT IS REAL: the RESPONSE SHAPE (a reference response from the owner, used for structure only -- app/config/mifin.yaml
records what each field is taken to mean and which meanings are UNVERIFIED).
WHAT IS NOT: a MIFIN endpoint, request format or credentials. `client()` returns an HTTP client only when
MIFIN_DEDUPE_URL is set; otherwise every lookup is UNAVAILABLE (never "no match").

Match classes: CONFIRMED_MATCH | POTENTIAL_MATCH | NO_MATCH | AMBIGUOUS | SOURCE_ERROR | UNAVAILABLE | MALFORMED.
A match is an identity statement only -- never fraud, never a decision on the loan.

PRIVACY: PAN, mobile, DOB, names, bank and address sections never leave this module in clear. Identity fields are
reported as present / absent; the customer id (AP code) and prospect codes (PR codes) are masked in everything public;
a supplied search value can be compared by keyed HMAC (dedupe/identity.py) without being returned.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import yaml

from app.core.exceptions import ConfigurationError

logger = logging.getLogger(__name__)
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "mifin.yaml"

CONFIRMED, POTENTIAL, NO_MATCH = "CONFIRMED_MATCH", "POTENTIAL_MATCH", "NO_MATCH"
AMBIGUOUS, SOURCE_ERROR, UNAVAILABLE, MALFORMED = "AMBIGUOUS", "SOURCE_ERROR", "UNAVAILABLE", "MALFORMED"
LOAN_UNKNOWN = "UNKNOWN"


@lru_cache(maxsize=2)
def _load(path: str) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    for key in ("contract_version", "client", "status", "flags", "matching", "loans", "privacy"):
        if key not in (data or {}):
            raise ConfigurationError(f"mifin.yaml missing: {key}")
    if data["matching"].get("score_threshold") not in (None,) and not isinstance(
            data["matching"]["score_threshold"], (int, float)):
        raise ConfigurationError("mifin.yaml matching.score_threshold must be a number or null.")
    return data


def cfg() -> dict[str, Any]:
    return _load(str(Path(os.getenv("MIFIN_CONFIG_PATH") or CONFIG_PATH)))


def reload() -> None:
    _load.cache_clear()


def full_codes() -> bool:
    """mifin.yaml privacy.show_full_codes: the AP / PR codes in full in the answer (never in logs / audit)."""
    return bool((cfg().get("privacy") or {}).get("show_full_codes", False))


def mask_ref(value: str | None) -> str | None:
    """AP00905983 -> AP******83: the prefix tells the kind of reference, the tail lets an officer compare."""
    v = str(value or "").strip()
    if not v:
        return None
    head = 2 if len(v) > 6 else 0
    return v[:head] + "*" * max(0, len(v) - head - 2) + v[-2:]


def money(raw: Any) -> tuple[Decimal | None, str | None]:
    """(amount, problem). "" / None -> (None, None): UNKNOWN, never zero. Text that is not a number -> a problem."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, None
    try:
        value = Decimal(str(raw).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None, "NOT_A_NUMBER"
    if not value.is_finite():
        return None, "NOT_A_NUMBER"
    return value, ("NEGATIVE" if value < 0 else None)


@dataclass
class Loan:
    prospect_code: str                      # full: internal only (LMS lookups); public: masked
    scheme: str | None
    source_status: str | None
    status_class: str
    amounts: dict[str, Decimal | None]
    problems: list[str] = field(default_factory=list)
    duplicate_count: int = 0
    conflict: bool = False
    cancel_reason: str | None = None        # the source's reason (mifin.yaml loans.cancel_reason), already masked

    def public(self, currency: str) -> dict[str, Any]:
        return {"prospect_ref": mask_ref(self.prospect_code),
                "prospect_code": self.prospect_code if full_codes() else None, "scheme": self.scheme,
                "source_status": self.source_status, "status_class": self.status_class,
                "amounts": {k: (str(v) if v is not None else None) for k, v in self.amounts.items()},
                "currency": currency, "amount_problems": self.problems,
                "duplicates_collapsed": self.duplicate_count, "conflicting_duplicate": self.conflict,
                "cancel_reason": self.cancel_reason}


@dataclass
class MifinResult:
    match: str
    source_status_code: str | None = None
    source_message: str | None = None
    customer_id: str | None = None          # the AP code: internal only
    strong_flag: bool = False
    potential_flag: bool = False
    score: Any = None
    loans: list[Loan] = field(default_factory=list)
    identity_fields_present: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    candidates: int = 0
    retrieved_at: float = field(default_factory=time.time)
    is_demo: bool = False
    customer_type: str | None = None        # CUSTTYPE mapped by mifin.yaml customer_types (UNMAPPED if unknown)

    @property
    def loan_section(self) -> str:
        """PRESENT (the list, even empty, is the source's word) | ABSENT (unknown) | MALFORMED."""
        if "PROSPECTCODES_MALFORMED" in self.warnings:
            return "MALFORMED"
        return "ABSENT" if "LOAN_SECTION_ABSENT" in self.warnings else "PRESENT"

    def public(self) -> dict[str, Any]:
        c = cfg()
        currency = c["loans"].get("currency") or "UNKNOWN"
        by_class: dict[str, int] = {}
        for loan in self.loans:
            by_class[loan.status_class] = by_class.get(loan.status_class, 0) + 1
        return {
            # MIFIN's free-text MESSAGE is never returned: it is free text, and free text can carry a name
            "source": "MIFIN", "contract_version": c["contract_version"], "match": self.match,
            "source_status_code": self.source_status_code,
            "customer_ref": mask_ref(self.customer_id),
            "customer_code": self.customer_id if full_codes() else None,
            "match_basis": {"strong_flag": self.strong_flag, "potential_flag": self.potential_flag,
                            "score": self.score, "score_threshold": c["matching"].get("score_threshold"),
                            "interpretation_status": c["matching"].get("interpretation_status")},
            "candidates": self.candidates,
            "identity_fields_present": self.identity_fields_present,
            "customer_type": self.customer_type,
            "loans": [loan.public(currency) for loan in self.loans], "loan_section": self.loan_section,
            "is_demo": self.is_demo,
            "loan_count": len(self.loans), "loans_by_status_class": by_class,
            "fields_not_in_source": list(c["loans"].get("not_in_source") or []),
            "warnings": self.warnings, "retrieved_at": self.retrieved_at,
        }


def fits(kind: str, value: str | None) -> bool | None:
    """Whether an AP / PR code has the observed MIFIN format (mifin.yaml formats); None when no format is set."""
    import re

    pattern = (cfg().get("formats") or {}).get(kind)
    if not pattern or not value:
        return None
    return re.fullmatch(pattern, str(value).strip().upper()) is not None


def _yes(value: Any) -> bool:
    return str(value or "").strip().upper() in {str(v).upper() for v in cfg()["flags"]["yes"]}


_REASON_MASKS = (re.compile(r"[A-Za-z]{5}\d{4}[A-Za-z]"), re.compile(r"(?i)\b(AP|PR)\d{6,}\b"), re.compile(r"\d{6,}"))


def cancel_reason(row: dict[str, Any]) -> str | None:
    """A prospect's cancellation reason from the first configured field present (mifin.yaml loans.cancel_reason),
    trimmed, length-capped, any PAN / code / long number masked. None when the source gives none."""
    spec = cfg()["loans"].get("cancel_reason") or {}
    for name in spec.get("fields") or []:
        text = " ".join(str(row.get(name) or "").split())
        if text:
            for rx in _REASON_MASKS:
                text = rx.sub("[masked]", text)
            limit = int(spec.get("max_length") or 120)
            return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
    return None


def _loans(details: dict[str, Any], warnings: list[str]) -> list[Loan]:
    c = cfg()["loans"]
    info = details.get("APPLICANTLOANINFO")
    if info in (None, "", {}):
        warnings.append("LOAN_SECTION_ABSENT")           # NOT "no loans": the source did not say
        return []
    rows = info.get("PROSPECTCODES") if isinstance(info, dict) else None
    if rows in (None, ""):
        warnings.append("LOAN_SECTION_ABSENT")
        return []
    if isinstance(rows, dict):
        rows = [rows]                                   # one record sent as an object
    if not isinstance(rows, list):
        warnings.append("PROSPECTCODES_MALFORMED")
        return []
    status_map = {str(k).upper(): str(v) for k, v in (c.get("status_map") or {}).items()}
    by_code: dict[str, Loan] = {}
    order: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            warnings.append("PROSPECT_RECORD_MALFORMED")
            continue
        code = str(row.get("PROSPECTCODE") or "").strip()
        if not code:
            warnings.append("PROSPECT_WITHOUT_CODE")
            continue
        source_status = (str(row.get("LOAN_STATUS")).strip().upper() or None) if row.get("LOAN_STATUS") else None
        problems: list[str] = [] if fits("PR_CODE", code) is not False else ["PR_CODE_FORMAT_UNEXPECTED"]
        amounts: dict[str, Decimal | None] = {}
        for name in ("SANCTIONED_AMOUNT", "OS_PRINCIPAL", "OS_INTEREST", "OS_OTHERCHARGES"):
            value, problem = money(row.get(name))
            amounts[name.lower()] = value
            if problem:
                problems.append(f"{name}_{problem}")
        loan = Loan(code, (str(row.get("SCHEME")).strip() or None) if row.get("SCHEME") else None, source_status,
                    status_map.get(source_status or "", LOAN_UNKNOWN), amounts, problems,
                    cancel_reason=cancel_reason(row))
        seen = by_code.get(code)
        if seen is None:
            by_code[code] = loan
            order.append(code)
        elif (seen.scheme, seen.source_status, seen.amounts) == (loan.scheme, loan.source_status, loan.amounts):
            seen.duplicate_count += 1                   # the same record twice: counted once
        else:
            seen.conflict = True                        # the same code, different facts: kept flagged, never summed
            warnings.append("CONFLICTING_DUPLICATE_PROSPECT")
    return [by_code[c] for c in order]


def normalise(raw: Any) -> MifinResult:
    """A MIFIN dedupe response (dict or JSON text) -> MifinResult. Never raises on bad input: MALFORMED instead."""
    c = cfg()
    if isinstance(raw, (bytes, str)):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return MifinResult(MALFORMED, warnings=["NOT_JSON"])
    response = raw.get("RESPONSE") if isinstance(raw, dict) else None
    if not isinstance(response, dict):
        return MifinResult(MALFORMED, warnings=["NO_RESPONSE_OBJECT"])
    code = str(response.get("STATUS") or "").strip() or None
    message = str(response.get("MESSAGE") or "").strip()[:120] or None
    if message:
        from app.security.sensitivity import mask_identifiers

        message = mask_identifiers(message)
    if code not in {str(s) for s in c["status"]["success"]}:
        return MifinResult(SOURCE_ERROR, code, message, warnings=["STATUS_NOT_SUCCESS"])
    details = response.get("APPLICANTDETAILS")
    if details in (None, "", {}, []):
        return MifinResult(NO_MATCH, code, message)
    candidates = details if isinstance(details, list) else [details]
    candidates = [d for d in candidates if isinstance(d, dict)]
    if not candidates:
        return MifinResult(MALFORMED, code, message, warnings=["APPLICANTDETAILS_MALFORMED"])
    ids = {str(d.get("CUSTID") or "").strip() for d in candidates} - {""}
    if len(ids) > 1:
        return MifinResult(AMBIGUOUS, code, message, candidates=len(candidates),
                           warnings=["MORE_THAN_ONE_CUSTOMER_ID"])
    d = candidates[0]
    warnings: list[str] = []
    strong, potential = _yes(d.get("STRONG_MATCH")), _yes(d.get("POTENTIAL_MATCH"))
    customer_id = next(iter(ids), None)
    if strong:
        match = c["matching"]["strong_flag_class"]
    elif potential:
        match = c["matching"]["potential_flag_class"]
    else:
        match = NO_MATCH if not customer_id else POTENTIAL
        if customer_id:
            warnings.append("CUSTOMER_ID_WITHOUT_MATCH_FLAG")
    if match == CONFIRMED and not customer_id:
        match, warnings = AMBIGUOUS, warnings + ["STRONG_MATCH_WITHOUT_CUSTOMER_ID"]
    if fits("AP_CODE", customer_id) is False:
        warnings.append("AP_CODE_FORMAT_UNEXPECTED")      # kept: the format is observed, not documented
    present = sorted(f for f in c["privacy"]["identity_fields"] if str(d.get(f) or "").strip())
    return MifinResult(match, code, message, customer_id, strong, potential, d.get("SCORE"), _loans(d, warnings),
                       present, warnings, len(candidates), customer_type=_customer_type(d.get("CUSTTYPE")))


def _customer_type(raw: Any) -> str | None:
    """CUSTTYPE as a role: mapped by config, UNMAPPED when MIFIN sends a value the config does not know, None when
    MIFIN sends none."""
    text = re.sub(r"\s+", "", str(raw or "")).upper()
    if not text:
        return None
    return str(((cfg().get("customer_types") or {}).get("map") or {}).get(text) or "UNMAPPED")


def same_identifier(result_raw: dict[str, Any], kind: str, value: str | None) -> bool | None:
    """Whether MIFIN's PAN / MOBILE equals the searched one -- compared as keyed HMACs, never returned. None when
    either side is missing or not a valid instance."""
    from app.agents.dedupe import identity

    try:
        details = result_raw["RESPONSE"]["APPLICANTDETAILS"]
        details = details[0] if isinstance(details, list) else details
        theirs = identity.normalise(kind, str(details.get(kind) or ""))
    except (KeyError, TypeError, IndexError, AttributeError):
        return None
    ours = identity.normalise(kind, value)
    if not theirs or not ours:
        return None
    return bool(identity.tokens(kind, theirs) & identity.tokens(kind, ours))


def _name_tokens(text: Any) -> list[str]:
    return re.sub(r"[^A-Z ]", " ", str(text or "").upper()).split()


def _iso_date(text: Any) -> str | None:
    """YYYY-MM-DD from ISO or DD-MM-YYYY / DD/MM/YYYY; None when it is not a date."""
    raw = str(text or "").strip()[:10]
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw) or None
    if m:
        return raw
    m = re.fullmatch(r"(\d{2})[-/](\d{2})[-/](\d{4})", raw)
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def compare_identity(result_raw: dict[str, Any] | None, *, name: str | None, dob: str | None) -> dict[str, str]:
    """The applicant's name and date of birth against MIFIN's record -- IN MEMORY ONLY: neither side is returned,
    logged or stored, only MATCH / MISMATCH / NOT_COMPARED. Exact and deterministic (mifin.yaml identity_compare):
    the name matches when MIFIN's first name is the applicant's first word and its last name the applicant's last
    word (middle names may differ); the date when both are the same calendar date."""
    out = {"name": "NOT_COMPARED", "dob": "NOT_COMPARED"}
    try:
        details = (result_raw or {})["RESPONSE"]["APPLICANTDETAILS"]
        details = details[0] if isinstance(details, list) else details
    except (KeyError, TypeError, IndexError):
        return out
    if not isinstance(details, dict):
        return out
    ours, first, last = _name_tokens(name), _name_tokens(details.get("FNAME")), _name_tokens(details.get("LNAME"))
    if ours and first and last:
        out["name"] = "MATCH" if (ours[0] == first[0] and ours[-1] == last[-1] and len(ours) >= 2) else "MISMATCH"
    theirs_dob, our_dob = _iso_date(details.get("DOB")), _iso_date(dob)
    if theirs_dob and our_dob:
        out["dob"] = "MATCH" if theirs_dob == our_dob else "MISMATCH"
    return out


def _masked(kind: str, value: Any) -> str | None:
    """PAN / mobile as the page shows it (mifin.yaml privacy.value_mask): the rest as '*'."""
    from app.agents.dedupe import identity

    v = identity.normalise(kind, str(value or "")) or ""
    if not v:
        return None
    spec = ((cfg().get("privacy") or {}).get("value_mask") or {}).get(kind) or {}
    start = min(int(spec.get("keep_start", 0)), len(v))
    end = min(int(spec.get("keep_end", 0)), max(len(v) - start, 0))
    return v[:start] + "*" * max(len(v) - start - end, 0) + (v[-end:] if end else "")


def identity_values(result_raw: dict[str, Any] | None, *, name: str | None, dob: str | None, pan: str | None,
                    searched: str | None = None, searched_value: str | None = None) -> dict[str, Any] | None:
    """BOTH SIDES of the matching table for the officer (mifin.yaml privacy.show_identity_values): what was entered
    and what MIFIN holds -- name and DOB as written, PAN / mobile masked. None when switched off or MIFIN named no
    one. Never logged or stored: it is part of the answer only."""
    if not (cfg().get("privacy") or {}).get("show_identity_values"):
        return None
    try:
        details = (result_raw or {})["RESPONSE"]["APPLICANTDETAILS"]
        details = details[0] if isinstance(details, list) else details
    except (KeyError, TypeError, IndexError):
        return None
    if not isinstance(details, dict):
        return None
    theirs_name = " ".join(str(details.get(k) or "").strip() for k in ("FNAME", "MNAME", "LNAME")
                           if str(details.get(k) or "").strip()) or None
    kind = str(searched or "").upper()
    entered = {"name": (str(name).strip() or None) if name else None, "dob": _iso_date(dob) or (dob or None),
               "pan": _masked("PAN", searched_value if kind == "PAN" else pan)}
    mifin = {"name": theirs_name, "dob": _iso_date(details.get("DOB")), "pan": _masked("PAN", details.get("PAN"))}
    if kind == "MOBILE":
        entered["mobile"], mifin["mobile"] = _masked("MOBILE", searched_value), _masked("MOBILE", details.get("MOBILE"))
    return {"entered": entered, "mifin": mifin}


# ---- the client ------------------------------------------------------------------------------------------------
class MifinClient(Protocol):
    def lookup(self, parameter: str, value: str) -> dict[str, Any]:
        """The raw MIFIN dedupe response for one parameter (PAN / MOBILE / AP_CODE / PR_CODE)."""


class SourceUnavailable(RuntimeError):
    """MIFIN not configured, unreachable, timed out or retried out."""


class _NotConfigured:
    def lookup(self, parameter: str, value: str) -> dict[str, Any]:
        raise SourceUnavailable("MIFIN_NOT_CONFIGURED")


class HttpMifinClient:
    """THE LIVE CALL -- endpoint and token from the environment. The REQUEST BODY is not documented in this repository:
    `MIFIN_DEDUPE_REQUEST_TEMPLATE` (a JSON template with {parameter} / {value}) must be supplied by the MIFIN team."""

    def __init__(self, url: str, token: str | None, template: str):
        self.url, self.token, self.template = url, token, template

    def lookup(self, parameter: str, value: str) -> dict[str, Any]:
        import httpx

        c = cfg()["client"]
        body = json.loads(self.template.replace("{parameter}", json.dumps(parameter)[1:-1])
                          .replace("{value}", json.dumps(value)[1:-1]))
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        last = "MIFIN_FAILED"
        for attempt in range(int(c.get("retries", 0)) + 1):
            try:
                r = httpx.post(self.url, json=body, headers=headers, timeout=float(c.get("timeout_seconds", 8)))
            except httpx.TimeoutException:
                last = "MIFIN_TIMEOUT"
            except httpx.HTTPError:
                last = "MIFIN_UNREACHABLE"
            else:
                if r.status_code >= 500:
                    last = f"MIFIN_HTTP_{r.status_code}"
                elif r.status_code >= 400:
                    raise SourceUnavailable(f"MIFIN_HTTP_{r.status_code}")      # never retried
                else:
                    try:
                        return r.json()
                    except ValueError:
                        return {"_malformed_text": True}
            time.sleep(float(c.get("backoff_seconds", 0.5)) * (attempt + 1))
        raise SourceUnavailable(last)


def client() -> MifinClient:
    provider = (os.getenv("MIFIN_PROVIDER") or "").strip().lower()
    if provider == "demo":
        if (os.getenv("ENVIRONMENT") or "development").strip().lower() in {"production", "prod"}:
            return _NotConfigured()                     # demo data never answers in production
        from app.agents.dedupe.demo_sources import DemoMifin

        return DemoMifin()
    url = (os.getenv("MIFIN_DEDUPE_URL") or "").strip()
    template = (os.getenv("MIFIN_DEDUPE_REQUEST_TEMPLATE") or "").strip()
    if not url or not template:
        return _NotConfigured()
    return HttpMifinClient(url, (os.getenv("MIFIN_DEDUPE_TOKEN") or "").strip() or None, template)


def lookup(parameter: str, value: str, *, source: MifinClient | None = None) -> tuple[MifinResult, dict | None]:
    """(normalised result, raw response for internal use only). Every failure is a result, never an exception."""
    try:
        raw = (source or client()).lookup(parameter, value)
    except SourceUnavailable as exc:
        return MifinResult(UNAVAILABLE, warnings=[str(exc)]), None
    except Exception as exc:  # noqa: BLE001 - a client bug is an unavailable source, logged without the value
        logger.warning("mifin: lookup failed: %s", type(exc).__name__)
        return MifinResult(UNAVAILABLE, warnings=["MIFIN_CLIENT_ERROR"]), None
    if isinstance(raw, dict) and raw.get("_malformed_text"):
        return MifinResult(MALFORMED, warnings=["NOT_JSON"]), None
    result = normalise(raw)
    result.is_demo = bool(getattr(source or client(), "is_demo", False))
    return result, raw if isinstance(raw, dict) else None


__all__ = ["AMBIGUOUS", "CONFIRMED", "MALFORMED", "NO_MATCH", "POTENTIAL", "SOURCE_ERROR", "UNAVAILABLE", "Loan",
           "MifinClient", "MifinResult", "SourceUnavailable", "cfg", "client", "lookup", "mask_ref", "money",
           "normalise", "reload", "same_identifier"]
