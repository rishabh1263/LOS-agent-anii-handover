"""
The vendor boundary (base.py) and its adapters. `provider_for` picks the adapter a vendor is configured with
(config vendors.<id>.provider); a real, authorised vendor adapter is added here -- nothing else changes.
"""

from __future__ import annotations

from app.agents.credit.crime_verification.providers.base import CrimeCheckProvider
from app.agents.credit.crime_verification.providers.crimecheck_mock import MockCrimeCheckProvider

_ADAPTERS = {MockCrimeCheckProvider.name: MockCrimeCheckProvider}


def provider_for(name: str) -> CrimeCheckProvider:
    try:
        return _ADAPTERS[name]()
    except KeyError:
        raise KeyError(f"no provider adapter {name!r}") from None
