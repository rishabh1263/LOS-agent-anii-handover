"""
Thin, read-only readers over what other agents already RECORDED.

Each adapter reads the case store's current findings and returns an
Observation. None of them computes anything another agent owns: they select,
normalise field names, and grade the quality of what they found (PRESENT /
MISSING / LOW_CONFIDENCE). A missing record is reported as missing -- never
filled with a default.
"""

from __future__ import annotations

from typing import Any


def repository():
    from app.store import get_repository

    return get_repository()


def current_findings(case_id: str, *, kind: str | None = None,
                     party_id: str | None = None) -> list[Any]:
    """The case's CURRENT findings (latest row per logical finding)."""
    return list(repository().get_current_findings(case_id, party_id=party_id, kind=kind))


def kind_of(finding: Any) -> str:
    kind = getattr(finding, "finding_kind", None)
    return str(getattr(kind, "value", kind) or "")


def observed_at(finding: Any) -> str | None:
    stamp = getattr(finding, "updated_at", None) or getattr(finding, "created_at", None)
    return stamp.isoformat() if stamp is not None else None


def record_type(finding: Any) -> str:
    source = getattr(finding, "source_type", None)
    return f"FINDING:{kind_of(finding)}" + (f"/{source}" if source else "")
