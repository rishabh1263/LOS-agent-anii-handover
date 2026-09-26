"""
THE NEXT BEST ACTION ENGINE -- what is authoritatively next, and for whom.

    PRIMARY      the action the system ALREADY establishes for the current
                 stage: at FOS the workflow's own computed next action
                 (workflow.next_action -- its configured table order); at any
                 other stage the next action the pipeline RECORDED with its
                 decision during this stage. Neither -> NOT_CONFIGURED.
    ADDITIONAL   the actions the case's current impacts call for (Impact
                 Engine), each for ITS subject, ordered by the precedence the
                 LOS roll-up already applies (impact_rules.yaml
                 action_precedence). They add attribution; they never
                 replace the primary action.
    HANDOFF      required when an action is one a person must take
                 (`handoff: true`, e.g. MANUAL_REVIEW) or the user asked for
                 a person. NEVER from sentiment.

READ-ONLY. Nothing here performs an action. An action that changes state
(an upload, a handoff, a submission) stays behind the existing propose-and-
confirm flow and its authorisation (agent.confirm_action).

NO MODEL DECIDES AN ACTION, AND NONE PHRASES THIS ANSWER. The answer is built
from codes and the phrasebook and is published as built.
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.applicant import impact as impacts

NOT_CONFIGURED = impacts.NOT_CONFIGURED


def _config() -> dict[str, Any]:
    return impacts.rules()


def _precedence(code: str | None) -> int:
    order = list(_config().get("action_precedence") or [])
    return order.index(code) if code in order else len(order)


def _action_meta(code: str | None) -> dict[str, Any]:
    return (_config().get("actions") or {}).get(code or "", {}) or {}


def _action(code: str | None, *, subject: dict[str, Any],
            source_rule: str, reason_codes: list[str] | None = None,
            document: str | None = None, detail: str | None = None,
            blocking: bool | None = None) -> dict[str, Any]:
    meta = _action_meta(code)
    return {"action_code": code or NOT_CONFIGURED,
            "action_type": "READ_ONLY_GUIDANCE",
            "owner": meta.get("owner"),
            "subject": subject, "document": document,
            "blocking": blocking,
            "priority": _precedence(code),
            "handoff": bool(meta.get("handoff")),
            "source_rule": source_rule,
            "reason_codes": list(reason_codes or []),
            **({"detail": detail} if detail else {})}


_CASE = {"scope": "CASE", "party_id": None, "party_role": None}


def compute(*, stage: str | None, workflow_next: dict[str, Any] | None,
            decisions: list[dict[str, Any]] | None, hold_since: str | None,
            case_impacts: list[dict[str, Any]],
            user_asked_for_person: bool = False) -> dict[str, Any]:
    """The structured next actions for the case, from authoritative inputs."""
    from app.agents.applicant import status_facts

    primary = None
    if stage in (None, "FOS") and isinstance(workflow_next, dict) \
            and workflow_next.get("action"):
        primary = _action(
            workflow_next["action"], subject=_CASE,
            source_rule="workflow.next_action",
            reason_codes=workflow_next.get("reason_codes"),
            document=workflow_next.get("target"),
            detail=workflow_next.get("detail"),
            blocking=workflow_next["action"] != "SUBMIT_TO_CPA")
    if primary is None:
        during = [d for d in decisions or []
                  if status_facts.during_stage(d, hold_since)
                  and d.get("next_action")]
        if during:
            latest = during[-1]
            primary = _action(latest["next_action"], subject=_CASE,
                              source_rule="recorded_decision",
                              reason_codes=latest.get("reason_codes"))
            # WHICH decision -- internal provenance; None if it has no id.
            primary["record_id"] = latest.get("decision_id")
    if primary is None:
        primary = _action(None, subject=_CASE, source_rule="none")

    additional: list[dict[str, Any]] = []
    seen = {(primary["action_code"], primary["subject"]["scope"], None, None)}
    for item in case_impacts:
        code = item.get("action_code")
        if not code:
            continue
        subject = {"scope": item.get("scope") or "CASE",
                   "party_id": item.get("party_id"),
                   "party_role": item.get("party_role")}
        key = (code, subject["scope"], subject["party_id"], item.get("document"))
        if key in seen:
            continue
        seen.add(key)
        additional.append(_action(
            code, subject=subject, source_rule=item.get("source_rule") or "",
            reason_codes=[item.get("finding_code")] if item.get("finding_code")
            else [], document=item.get("document"),
            blocking=item.get("blocking")))
    role_order = {"PRIMARY_APPLICANT": 0, "CO_APPLICANT": 1, None: 2}
    additional.sort(key=lambda a: (a["priority"],
                                   role_order.get(a["subject"]["party_role"], 2)))

    reasons = [a["action_code"] for a in [primary, *additional] if a["handoff"]]
    handoff = {"required": bool(reasons) or user_asked_for_person,
               "reason": ("USER_REQUEST" if user_asked_for_person
                          else reasons[0] if reasons else None),
               # No handoff priority is configured anywhere: none is made up.
               "priority": None}
    return {"primary": primary, "additional": additional, "handoff": handoff}


# ==========================================================================
# WORDS
# ==========================================================================

def _phrase(action: dict[str, Any], language: str = "en") -> str:
    from app.agents.applicant.subjects import _type

    phrases = ((_config().get("phrases") or {}).get(language)
               or (_config().get("phrases") or {}).get("en") or {})
    table = phrases.get("action") or {}
    code = action["action_code"]
    template = table.get(code)
    if template is None:
        # A CONFIGURED ACTION WITHOUT A PHRASE IS NEVER SAID TO BE "NOT
        # CONFIGURED": the step the workflow recorded is used as written.
        if code != NOT_CONFIGURED and action.get("detail"):
            detail = str(action["detail"]).strip().rstrip(".")
            return detail[:1].lower() + detail[1:]
        template = table.get(NOT_CONFIGURED) if code == NOT_CONFIGURED \
            else "complete the recorded step"
    document = action.get("document")
    return str(template).format(
        document=_type(document) if document else "document")


def _for(action: dict[str, Any], multi_party: bool) -> str:
    role = action["subject"].get("party_role")
    if not role or not multi_party:
        return ""
    return (" for the primary applicant" if role == "PRIMARY_APPLICANT"
            else " for the co-applicant")


def answer(nba: dict[str, Any], *, primary_sentence: str | None,
           multi_party: bool) -> str:
    """
    At most two sentences: the primary action, then the additional ones.
    `primary_sentence` is the workflow's own recorded wording when the
    primary action came from it (kept exactly as the agent published it).
    """
    primary = nba["primary"]
    if primary["action_code"] == NOT_CONFIGURED:
        first = "No next step is configured for the current stage."
    elif primary_sentence:
        first = primary_sentence.rstrip()
        if not first.endswith("."):
            first += "."
    else:
        first = f"Your next step is to {_phrase(primary)}."
    extra = [a for a in nba["additional"]
             if a["action_code"] != primary["action_code"]
             or a["subject"]["scope"] != "CASE"][:2]
    if not extra:
        return first
    clauses = [f"{_phrase(a)}{_for(a, multi_party)}" for a in extra]
    return f"{first} Also, {' and '.join(clauses)}."


def blocking_answer(case_impacts: list[dict[str, Any]], nba: dict[str, Any],
                    *, stage_label: str, multi_party: bool) -> str:
    """What blocks the handoff, from the impacts, and what to do next."""
    blockers = [i for i in case_impacts if i.get("blocking") is True]
    if blockers:
        said = "; ".join(
            f"{impacts.text(i)}{_for({'subject': {'party_role': i.get('party_role')}}, multi_party)}"
            for i in blockers[:2])
        first = f"What is blocking the handoff: {said}."
    elif case_impacts and all(i.get("blocking") is None for i in case_impacts):
        first = (f"Blocking conditions are not configured for the "
                 f"{stage_label} stage.")
    elif case_impacts:
        first = ("Nothing recorded blocks the handoff, but "
                 f"{impacts.text(case_impacts[0])}.")
    else:
        first = "Nothing recorded is blocking the handoff."
    second = answer(nba, primary_sentence=None, multi_party=multi_party) \
        .split(". Also,")[0]
    return f"{first} {second if second.endswith('.') else second + '.'}"


def public(nba: dict[str, Any] | None) -> dict[str, Any] | None:
    """What a response may carry: codes, owner, subject role, blocking and
    the words -- never the source rule or a party's record id."""
    if not nba:
        return None

    def one(action: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in {
            "action_code": action["action_code"],
            "owner": action.get("owner"),
            "subject": {"scope": action["subject"]["scope"],
                        "party_role": action["subject"].get("party_role")},
            "document": action.get("document"),
            "blocking": action.get("blocking"),
            "priority": action.get("priority"),
            "handoff": action.get("handoff"),
            "text": _phrase(action),
        }.items() if v is not None or k == "blocking"}

    return {"primary": one(nba["primary"]),
            "additional": [one(a) for a in nba["additional"]],
            "handoff": dict(nba["handoff"])}


_ASKS_PERSON = re.compile(
    r"\b(talk|speak|chat|connect)\b[^?]{0,20}\b(to|with)\b[^?]{0,15}"
    r"\b(a\s+)?(human|person|agent|officer|someone|representative)\b"
    r"|\b(human|real\s+person|live\s+agent)\b[^?]{0,10}\b(please|help)\b",
    re.IGNORECASE)


def asks_for_person(message: str) -> bool:
    return bool(_ASKS_PERSON.search(message or ""))


def asks_what_blocks(message: str) -> bool:
    return bool(re.search(r"\bblock(s|ed|ing)?\b|\bholding\s+(me|us|it)\s+back\b",
                          message or "", re.IGNORECASE))


__all__ = ["NOT_CONFIGURED", "answer", "asks_for_person", "asks_what_blocks",
           "blocking_answer", "compute", "public"]
