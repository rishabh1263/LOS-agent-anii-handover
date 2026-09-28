"""
DemoBureauProvider -- synthetic fixtures, every report marked is_demo.

Reads app/config/demo/bureau_demo.yaml. A party with no mapped profile gets
None (no bureau record), never a made-up one. Tests may map a party to a
profile at runtime with `assign`, which is process-local and never persisted.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from app.agents.credit.bureau.base import (
    BureauAccount,
    BureauError,
    BureauProvider,
    BureauReport,
    BureauTimeout,
)
from app.agents.credit.schemas import utcnow
from app.core.exceptions import ConfigurationError

#: Runtime party -> profile assignments (tests / demos). Process-local.
_ASSIGNED: dict[str, str] = {}


def assign(party_id: str, profile: str | None) -> None:
    if profile is None:
        _ASSIGNED.pop(party_id, None)
    else:
        _ASSIGNED[party_id] = profile


def clear_assignments() -> None:
    _ASSIGNED.clear()


def load_fixtures(path: str | Path | None = None) -> dict[str, Any]:
    from app.agents.credit import config

    target = Path(path) if path is not None else config.bureau_fixtures_path()
    if not target.exists():
        raise ConfigurationError(f"Demo bureau fixtures not found: {target}")
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Demo bureau fixtures are not valid YAML: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        raise ConfigurationError("Demo bureau fixtures need a `profiles` mapping.")
    return data


class DemoBureauProvider(BureauProvider):
    name = "DEMO_BUREAU"
    is_demo = True

    def __init__(self, fixtures: dict[str, Any] | None = None) -> None:
        self._fixtures = fixtures if fixtures is not None else load_fixtures()
        self.name = str(self._fixtures.get("provider") or self.name)

    def profile_for(self, party_id: str) -> str | None:
        return _ASSIGNED.get(party_id) or (self._fixtures.get("parties") or {}).get(party_id)

    async def fetch(self, party_id: str, *, timeout: float) -> BureauReport | None:
        profile_name = self.profile_for(party_id)
        if not profile_name:
            return None
        profile = (self._fixtures.get("profiles") or {}).get(profile_name)
        if not isinstance(profile, dict):
            return None

        behaviour = str(profile.get("behaviour") or "ok").lower()
        if behaviour == "timeout":
            raise BureauTimeout("Demo bureau simulated a timeout.")
        if behaviour == "error":
            raise BureauError("Demo bureau simulated a provider error.")

        report_id = "DEMO-" + hashlib.sha1(
            f"{profile_name}|{party_id}".encode()).hexdigest()[:10].upper()
        return BureauReport(
            party_id=party_id,
            provider=self.name,
            score=profile.get("score"),
            score_model=self._fixtures.get("score_model"),
            history_months=profile.get("history_months"),
            accounts=[BureauAccount(**a) for a in profile.get("accounts") or []],
            settlements=profile.get("settlements"),
            write_offs=profile.get("write_offs"),
            enquiries_6m=profile.get("enquiries_6m"),
            total_monthly_obligation=profile.get("total_monthly_obligation"),
            report_id=report_id,
            fetched_at=utcnow().isoformat(),
            is_demo=True,
        )


__all__ = ["DemoBureauProvider", "assign", "clear_assignments", "load_fixtures"]
