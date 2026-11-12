"""
THE MOCK CRIMECHECK PROVIDER -- deterministic synthetic answers, no network, no real search.

The answer comes from the scenario (config crime_verification_demo.yaml `scenarios`): the request's demo scenario, or
the one seeded for that person. The same subject and attempt always give the same reference and the same answer.

    COMPLETED   risk level + cases found per covered source (coverage list) + counts per matter type
    PENDING     accepted, no result yet; `status()` completes it after `pending_polls` refreshes (null: never)
    FAILED      no result (`error`); attempt number > `fail_attempts` gets the `then` scenario instead
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.agents.credit.crime_verification.config import demo
from app.agents.credit.crime_verification.providers.base import (Category, ProviderRequest,
                                                                             ProviderResponse, ProviderResult)


def scenarios() -> dict[str, dict[str, Any]]:
    return dict(demo().get("scenarios") or {})


def _reference(subject_ref: str, attempt: int) -> str:
    digest = hashlib.sha256(f"{subject_ref}:{attempt}".encode()).hexdigest()[:10].upper()
    return str(demo().get("provider_reference_prefix") or "CC-") + digest


def _result(name: str) -> ProviderResult:
    spec = scenarios()[name]
    found = {str(k): int(v) for k, v in (spec.get("findings") or {}).items()}
    categories = [Category(group=str(group["group"]), code=str(item["code"]), name=str(item["name"]),
                           coverage_since=int(item["since"]), cases_found=found.get(str(item["code"]), 0))
                  for group in demo().get("coverage") or [] for item in group.get("items") or []]
    clusters = {str(k): int(v) for k, v in (spec.get("clusters") or {}).items()}
    return ProviderResult(risk_level=str(spec["risk"]), categories=categories, clusters=clusters)


class MockCrimeCheckProvider:
    name = "mock_crimecheck"
    demo_only = True                  # synthetic answers: never in production (verification_service refuses it)

    def initiate(self, request: ProviderRequest) -> ProviderResponse:
        scenario = request.scenario or "NO_RISK"
        spec = scenarios()[scenario]
        reference = _reference(request.subject_ref, request.attempt)
        outcome = str(spec.get("outcome", "COMPLETED"))
        if outcome == "FAILED":
            if request.attempt <= int(spec.get("fail_attempts", 1)) or not spec.get("then"):
                return ProviderResponse("FAILED", reference, error_code=str(spec.get("error", "PROVIDER_UNAVAILABLE")),
                                        state={"scenario": scenario}, synthetic=True)
            scenario = str(spec["then"])
            return ProviderResponse("COMPLETED", reference, _result(scenario), state={"scenario": scenario},
                                    synthetic=True)
        if outcome == "PENDING":
            return ProviderResponse("PENDING", reference, state={"scenario": scenario, "polls": 0}, synthetic=True)
        return ProviderResponse("COMPLETED", reference, _result(scenario), state={"scenario": scenario}, synthetic=True)

    def status(self, reference: str, state: dict[str, Any]) -> ProviderResponse:
        scenario = str(state.get("scenario") or "NO_RISK")
        spec = scenarios()[scenario]
        if str(spec.get("outcome")) != "PENDING":
            return ProviderResponse("COMPLETED", reference, _result(scenario), state=state, synthetic=True)
        polls = int(state.get("polls", 0)) + 1
        limit = spec.get("pending_polls")
        if limit is not None and polls >= int(limit):
            then = str(spec.get("then") or "NO_RISK")
            return ProviderResponse("COMPLETED", reference, _result(then), state={**state, "polls": polls},
                                    synthetic=True)
        return ProviderResponse("PENDING", reference, state={**state, "polls": polls}, synthetic=True)

