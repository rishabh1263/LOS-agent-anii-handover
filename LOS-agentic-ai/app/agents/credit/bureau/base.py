"""
The bureau provider contract and the normalised report it returns.

A provider answers ONE question -- "what does the bureau hold for this party"
-- and says so in a fixed, normalised shape, whatever the vendor's own format.
It never scores, never decides, and never invents a record: a party the
bureau has nothing on is `None`, which underwriting reports as a data gap.

Failures are typed so the graph can apply its one rule: a TRANSIENT failure
(BureauTimeout) is retried at most once; any other failure (BureauError) is
not retried.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class BureauError(Exception):
    """A non-transient provider failure. Not retried."""


class BureauTimeout(BureauError):
    """A transient failure (timeout / temporarily unavailable). Retried once."""


class BureauAccount(BaseModel):
    type: str
    status: str
    dpd_max_12m: int | None = None


class BureauReport(BaseModel):
    """The normalised bureau view of one party."""

    party_id: str
    provider: str
    score: int | None = None
    score_model: str | None = None
    history_months: int | None = None
    accounts: list[BureauAccount] = Field(default_factory=list)
    settlements: int | None = None
    write_offs: int | None = None
    enquiries_6m: int | None = None
    total_monthly_obligation: float | None = None
    report_id: str | None = None
    fetched_at: str | None = None
    is_demo: bool = False

    @property
    def max_dpd_12m(self) -> int | None:
        """The worst reported delay across accounts -- a selection, not a score."""
        values = [a.dpd_max_12m for a in self.accounts if a.dpd_max_12m is not None]
        return max(values) if values else None

    def signals(self) -> dict[str, Any]:
        """The fields the underwriting policy may read, by name."""
        return {
            "score": self.score,
            "history_months": self.history_months,
            "max_dpd_12m": self.max_dpd_12m,
            "settlements": self.settlements,
            "write_offs": self.write_offs,
            "enquiries_6m": self.enquiries_6m,
            "total_monthly_obligation": self.total_monthly_obligation,
            "account_count": len(self.accounts),
        }


class BureauProvider(ABC):
    name: str = "UNKNOWN"
    is_demo: bool = False

    @abstractmethod
    async def fetch(self, party_id: str, *, timeout: float) -> BureauReport | None:
        """The party's report, None when the bureau holds nothing on them.

        Raises BureauTimeout on a transient failure, BureauError otherwise.
        """


__all__ = ["BureauAccount", "BureauError", "BureauProvider", "BureauReport", "BureauTimeout"]
