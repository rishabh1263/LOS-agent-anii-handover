"""
"WHY IS IT DELAYED?" -- answered from records, and only as far as they go.

    current stage (+ since when)      -- the stage record
    what holds it                     -- current findings whose IMPACT holds
                                         the case or blocks its handoff
    what happened in this stage       -- recorded events since it was entered
    what is next                      -- the Next Best Action

NO CAUSE IS INVENTED. Being in a stage is not a delay, and nothing here says
"delayed" as a fact. The case is said to be HELD only when a recorded
finding's configured impact holds it (a review) or blocks its handoff;
otherwise the answer says the available records do not establish a cause.
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.applicant import impact as impacts

#: Effects that hold a case, by the impact rules (impact_rules.yaml).
HOLDING_EFFECTS = frozenset({"CASE_REVIEW", "VERIFICATION_FAIL",
                             "DOCUMENT_REVIEW"})

_DELAY = re.compile(
    r"\b(delay\w*|stuck|not\s+moving|hasn'?t\s+moved|has\s+not\s+moved|"
    r"isn'?t\s+moving|holding\s+(it\s+|my\s+\w+\s+|this\s+)?(up|back)|"
    r"held\s+up|on\s+hold|taking\s+(so\s+)?long)\b", re.IGNORECASE)


def asks_about_delay(message: str) -> bool:
    return bool(_DELAY.search(message or ""))


def explain(*, stage: str | None, since: str | None,
            case_impacts: list[dict[str, Any]], nba: dict[str, Any],
            events: list[dict[str, Any]]) -> dict[str, Any]:
    """The structured truth behind a delay question. No ids, codes only."""
    holding = [i for i in case_impacts
               if i.get("effect") in HOLDING_EFFECTS or i.get("blocking") is True]
    in_stage = [e for e in events
                if not since or (e.get("changed_at") or "") >= since]
    return {
        "current_stage": stage,
        "stage_since": since,
        "cause_established": bool(holding),
        "held_by": [{k: v for k, v in {
            "finding_code": i.get("finding_code"),
            "impact_code": i.get("impact_code"),
            "effect": i.get("effect"), "blocking": i.get("blocking"),
            "subject": i.get("party_role") or "CASE",
            "document": i.get("document")}.items() if v is not None}
            for i in holding],
        "events_in_stage": [e.get("event_type") for e in in_stage],
        "next_action": (nba.get("primary") or {}).get("action_code"),
    }


def answer(truth: dict[str, Any], held: list[dict[str, Any]],
           nba: dict[str, Any], *, multi_party: bool) -> str:
    """At most two sentences; a cause only when the records establish one."""
    from app.agents.applicant import actions, config

    label = config.stage_label(truth["current_stage"] or "FOS")
    if not truth["cause_established"]:
        return (f"Your application is at the {label} stage, and the available "
                f"records do not establish a cause for a delay.")
    reasons = []
    for item in held[:2]:
        whose = ""
        if multi_party and item.get("party_role"):
            whose = (" for the primary applicant"
                     if item["party_role"] == "PRIMARY_APPLICANT"
                     else " for the co-applicant")
        reasons.append(f"{impacts.text(item)}{whose}")
    first = f"The records show your application is held because {'; and '.join(reasons)}."
    second = actions.answer(nba, primary_sentence=None,
                            multi_party=multi_party).split(". Also,")[0]
    return f"{first} {second if second.endswith('.') else second + '.'}"


__all__ = ["HOLDING_EFFECTS", "answer", "asks_about_delay", "explain"]
