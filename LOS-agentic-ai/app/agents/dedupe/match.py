"""
Two profiles -> MATCH / POSSIBLE_MATCH / NO_MATCH, with the parameters that agreed, reason codes and a confidence.

  - PAN equal (keyed HMAC)                                   -> MATCH          (confidence strong_parameter); when both
                                                                                parties have recorded names and they
                                                                                differ, name_conflict (NAME_DIFFERS):
                                                                                one PAN, two people
  - both hold a PAN and it differs                           -> NO_MATCH       (two people; a shared mobile is kept as
                                                                                a shared signal -- a family)
  - no PAN to compare, mobile equal                          -> POSSIBLE_MATCH (confidence supporting_only)
  - nothing in common                                        -> NO_MATCH
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.dedupe.profiles import Profile, names_differ

MATCH, POSSIBLE, NO_MATCH, INSUFFICIENT = "MATCH", "POSSIBLE_MATCH", "NO_MATCH", "INSUFFICIENT_DATA"
_RANK = {MATCH: 3, POSSIBLE: 2, NO_MATCH: 1, INSUFFICIENT: 0}


@dataclass
class Comparison:
    status: str
    matched_fields: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    confidence: str | None = None
    shared_signals: list[str] = field(default_factory=list)
    name_conflict: bool = False


def rank(status: str) -> int:
    return _RANK.get(status, 0)


def compare(current: Profile, other: Profile, matching: dict) -> Comparison:
    conf = matching["confidence"]
    mobile_shared = bool(current.mobile & other.mobile)
    shared = ["MOBILE"] if mobile_shared else []
    if current.pan and other.pan:
        if current.pan & other.pan:
            differ = names_differ(current, other)
            return Comparison(MATCH, ["PAN"] + shared, ["PAN_EXACT"] + (["MOBILE_EXACT"] if mobile_shared else [])
                              + (["NAME_DIFFERS"] if differ else []), conf["strong_parameter"], shared, differ)
        return Comparison(NO_MATCH, [], ["PAN_DIFFERS"] + (["MOBILE_SHARED_DIFFERENT_PAN"] if mobile_shared else []),
                          None, shared)
    if mobile_shared:
        return Comparison(POSSIBLE, ["MOBILE"], ["MOBILE_EXACT", "NO_PAN_TO_COMPARE"], conf["supporting_only"], shared)
    return Comparison(NO_MATCH)


__all__ = ["INSUFFICIENT", "MATCH", "NO_MATCH", "POSSIBLE", "Comparison", "compare", "rank"]
