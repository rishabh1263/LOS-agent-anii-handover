"""
LMS (loan management / core) -- the loan-level record the LOS needs, by PR code (Phase B, 2026-10-22).

NO LMS CONTRACT EXISTS IN THIS REPOSITORY. This module defines the LOS-SIDE TYPED RECORD (what loan history needs) and
the client interface. A real LMS adapter must map its own response into `LmsLoan`; until one exists:
  LMS_PROVIDER unset  -> every lookup UNAVAILABLE
  LMS_PROVIDER=demo   -> the Leviosa demo (same as leviosa-demo; development / test only; refused in production)
  LMS_PROVIDER=leviosa -> Leviosa, the live LMS (leviosa.py, app/config/leviosa_lms.yaml; owner 2026-10-23)
  LMS_PROVIDER=leviosa-demo -> synthetic Leviosa replies read by the real Leviosa mapping (development only)

FIELD MEANINGS (the contract the real adapter must honour):
  status                 ACTIVE | CLOSED | SETTLED | WRITTEN_OFF | CANCELED | UNKNOWN  (the LMS's loan state)
  principal_outstanding  principal still owed today, rupees
  emi                    the current monthly instalment, rupees
  dpd_current / dpd_max_12m   days past due now / worst in the last 12 months
  overdue_amount         amount overdue today, rupees
  disbursed_on / closed_on    ISO dates
  settled / written_off  booleans
A field the LMS did not return is UNAVAILABLE -- never zero.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from app.agents.dedupe.mifin import money

STATUSES = ("ACTIVE", "CLOSED", "SETTLED", "WRITTEN_OFF", "CANCELED", "UNKNOWN")
FIELDS = ("status", "principal_outstanding", "emi", "dpd_current", "dpd_max_12m", "overdue_amount", "disbursed_on",
          "closed_on", "settled", "written_off", "tenure_months", "emi_bounces_12m")
_MONEY = ("principal_outstanding", "emi", "overdue_amount")
_INTS = ("dpd_current", "dpd_max_12m", "tenure_months", "emi_bounces_12m")


class LmsUnavailable(RuntimeError):
    """LMS not configured, unreachable or failed."""


@dataclass
class LmsLoan:
    prospect_code: str
    values: dict[str, Any] = field(default_factory=dict)       # only what the LMS returned, typed
    problems: list[str] = field(default_factory=list)
    is_demo: bool = False

    @property
    def status(self) -> str:
        return self.values.get("status") or "UNKNOWN"

    @property
    def unavailable(self) -> list[str]:
        return [f for f in FIELDS if f not in self.values]


def parse(prospect_code: str, raw: dict[str, Any], *, is_demo: bool = False) -> LmsLoan:
    """The LOS-side record from an LMS answer already mapped to FIELDS; bad values are dropped and reported."""
    loan = LmsLoan(prospect_code, is_demo=is_demo)
    loan.problems.extend(str(x) for x in (raw.get("_problems") or []))       # what the adapter could not read
    for name in FIELDS:
        if name not in raw or raw[name] in (None, ""):
            continue
        value = raw[name]
        if name == "status":
            status = str(value).strip().upper()
            loan.values[name] = status if status in STATUSES else "UNKNOWN"
            if status not in STATUSES:
                loan.problems.append("STATUS_UNRECOGNISED")
        elif name in _MONEY:
            amount, problem = money(value)
            if amount is not None and problem != "NEGATIVE":
                loan.values[name] = amount
            else:
                loan.problems.append(f"{name.upper()}_{problem or 'INVALID'}")
        elif name in _INTS:
            try:
                number = int(value)
            except (TypeError, ValueError):
                loan.problems.append(f"{name.upper()}_INVALID")
                continue
            if number < 0:
                loan.problems.append(f"{name.upper()}_NEGATIVE")
                continue
            loan.values[name] = number
        elif name in ("settled", "written_off"):
            if isinstance(value, bool):
                loan.values[name] = value
            else:
                loan.problems.append(f"{name.upper()}_INVALID")
        else:
            loan.values[name] = str(value)
    return loan


class LmsClient(Protocol):
    def loan(self, prospect_code: str) -> dict[str, Any] | None:
        """The loan's record mapped to FIELDS, or None when the LMS holds no such loan."""


class _NotConfigured:
    name = None

    def loan(self, prospect_code: str) -> dict[str, Any] | None:
        raise LmsUnavailable("LMS_NOT_CONFIGURED")


class DemoLms:
    is_demo = True
    name = "DEMO"

    def __init__(self, records: dict[str, Any]):
        self.records = {str(k).upper(): v for k, v in (records or {}).items()}

    def loan(self, prospect_code: str) -> dict[str, Any] | None:
        return self.records.get(str(prospect_code).upper())


def _production() -> bool:
    return (os.getenv("ENVIRONMENT") or "development").strip().lower() in {"production", "prod"}


def client() -> LmsClient:
    provider = (os.getenv("LMS_PROVIDER") or "").strip().lower()
    if provider in ("demo", "leviosa-demo") and not _production():
        # the LMS IS Leviosa (owner 2026-10-23): the demo answers as Leviosa, through the real Leviosa mapping
        from app.agents.dedupe import demo_sources, leviosa

        return leviosa.DemoLeviosa(demo_sources.data().get("leviosa") or {})
    if provider == "leviosa":
        from app.agents.dedupe import leviosa

        return leviosa.client() or _NotConfigured()
    return _NotConfigured()


__all__ = ["FIELDS", "STATUSES", "DemoLms", "LmsClient", "LmsLoan", "LmsUnavailable", "client", "parse"]
