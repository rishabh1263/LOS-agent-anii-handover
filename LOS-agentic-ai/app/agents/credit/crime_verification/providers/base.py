"""
The provider boundary: what the agent sends a crime-check vendor and what it gets back.

A real, authorised vendor adapter implements `CrimeCheckProvider` the same way the mock does; the service, the
database and the screen do not change. `state` is the adapter's own small bookkeeping, stored with the subject and
handed back on `status()` (a real adapter may leave it empty).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

ProviderStatus = Literal["COMPLETED", "PENDING", "FAILED"]


@dataclass(frozen=True)
class ProviderRequest:
    subject_ref: str             # the agent's subject id (the vendor's client reference)
    role: str
    full_name: str
    father_name: str
    date_of_birth: str
    permanent_address: str
    pan: str
    attempt: int = 1             # 1 for the first request of this person on this application
    scenario: str | None = None  # DEMO ONLY: which synthetic answer the mock returns


@dataclass(frozen=True)
class Category:
    group: str
    code: str
    name: str
    coverage_since: int
    cases_found: int


@dataclass(frozen=True)
class ProviderResult:
    risk_level: str
    categories: list[Category]
    clusters: dict[str, int]

    @property
    def total_cases(self) -> int:
        return sum(c.cases_found for c in self.categories)

    @property
    def outcome(self) -> str:
        return "FINDINGS" if self.total_cases else "NO_MATCH"


@dataclass(frozen=True)
class ProviderResponse:
    status: ProviderStatus
    reference: str
    result: ProviderResult | None = None
    error_code: str | None = None
    state: dict[str, Any] = field(default_factory=dict)
    synthetic: bool = False


class ProviderError(Exception):
    """The vendor could not be reached or refused the request (the adapter's own failure, never shown raw)."""


class CrimeCheckProvider(Protocol):
    name: str

    def initiate(self, request: ProviderRequest) -> ProviderResponse: ...

    def status(self, reference: str, state: dict[str, Any]) -> ProviderResponse: ...
