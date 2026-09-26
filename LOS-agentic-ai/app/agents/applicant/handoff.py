"""
Whether a person should take over, why, and what they need to know.

A FOUNDATION, NOT A DESK. This service has no agent desktop and contacts
nobody. It publishes a structured signal a channel (web, mobile, WhatsApp,
agent desktop) can act on:

    handoff_required   a person MUST take it: a configured manual-review
                       action on the case (Next Best Action), or the user
                       explicitly asked for a person.
    recommended        a person SHOULD look: repeated unresolved turns, or
                       high frustration where configured. Not required.
    handoff_reason     the first trigger, as a code.
    handoff_priority   from `chatbot.handoff.priorities` -- null when none is
                       configured (no priority is invented).
    handoff_summary    what the person picking it up needs, as codes and
                       labels the caller is already authorised to see: case,
                       stage, subject role, finding codes, impact codes, next
                       action, how many history entries, recent intents. No
                       values, no names, no document content, no user text.

NEVER A BUSINESS DECISION. A handoff changes who answers, not what is true:
it does not alter stage, status, verification, KYC, risk, eligibility, the
next action or any fact in the answer. Mild unhappiness never triggers it;
high frustration only RECOMMENDS it, and only when configured.

THE CONVERSATION COUNTER IS UNTRUSTED. `unresolved_turns` comes back from
the client in `context`; it can only raise a recommendation, never a
requirement, and never touches a fact.
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping

USER_REQUEST = "USER_REQUEST"
MANUAL_REVIEW = "CONFIGURED_MANUAL_REVIEW"
REPEATED_UNRESOLVED = "REPEATED_UNRESOLVED"
HIGH_FRUSTRATION = "HIGH_FRUSTRATION"

#: Asking for a person, beyond the English form actions.asks_for_person knows.
_ASKS_PERSON_MORE = re.compile(
    r"\b(customer\s+(care|support|service)|call\s+centre|call\s+center|"
    r"helpline|human\s+agent|real\s+human|talk\s+to\s+(someone|somebody)|"
    r"speak\s+to\s+(someone|somebody)|call\s+me\s+back|callback)\b"
    r"|\b(insaan|kisi\s+(se|insaan))\b[^?]{0,20}\bbaat\b"
    r"|\bbaat\s+karni\s+hai\b|\bbaat\s+karwao\b|\bbaat\s+karao\b",
    re.IGNORECASE)


def _settings() -> dict[str, Any]:
    try:
        from app.agents.applicant import config

        return config.chatbot("handoff")
    except Exception:  # pragma: no cover
        return {}


def enabled() -> bool:
    flag = os.getenv("HANDOFF_ENABLED")
    if flag is not None and flag.strip():
        return flag.strip().lower() in {"1", "true", "yes", "on"}
    return bool(_settings().get("enabled", True))


def asks_for_person(message: str, canonical: str | None = None) -> bool:
    from app.agents.applicant import actions

    return any(actions.asks_for_person(t) or bool(_ASKS_PERSON_MORE.search(t))
               for t in (str(message or ""), str(canonical or "")) if t)


def unresolved(published: Mapping[str, Any]) -> bool:
    """Whether this turn left the question unanswered."""
    from app.knowledge import grounding

    category = str(published.get("category") or "").upper()
    intent = str(published.get("intent") or "").upper()
    answer = str(published.get("answer") or "")
    return (category == "UNSUPPORTED" and intent != "HUMAN_HANDOFF_REQUESTED"
            or intent in {"UNKNOWN", "CLARIFICATION"}
            or answer.strip() == grounding.NO_EVIDENCE
            or str(published.get("status") or "") == "CAPABILITY_UNAVAILABLE")


def _prior_unresolved(context: Mapping[str, Any] | None) -> int:
    try:
        value = int((context or {}).get("unresolved_turns") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(value, 50))


def _summary(published: Mapping[str, Any], context: Mapping[str, Any] | None,
             unresolved_turns: int) -> dict[str, Any]:
    """Codes and labels only -- what the caller is already authorised to see."""
    subject = published.get("subject") or {}
    next_actions = published.get("next_actions") or {}
    primary = (next_actions.get("primary") or {}) if isinstance(next_actions, Mapping) else {}
    problems = published.get("problems") or []
    history = published.get("history") or {}
    changes = history.get("changes") if isinstance(history, Mapping) else None
    recent = [str((context or {}).get("last_intent") or "")] if (context or {}).get("last_intent") else []
    return {
        "case_id": published.get("case_id"),
        "stage": published.get("stage"),
        "subject": (subject.get("kind") if isinstance(subject, Mapping) else None),
        "findings": sorted({str(p.get("type") or p.get("code") or "")
                            for p in problems if isinstance(p, Mapping)} - {""})[:6],
        "impacts": sorted({str((p.get("impact") or {}).get("impact_code") or "")
                           for p in problems if isinstance(p, Mapping)
                           and isinstance(p.get("impact"), Mapping)} - {""})[:6],
        "next_action": primary.get("action_code") or published.get("next_action"),
        "history_entries": len(changes) if isinstance(changes, list) else 0,
        "conversation": {"current_intent": published.get("intent"),
                         "recent_intents": recent,
                         "unresolved_turns": unresolved_turns},
    }


def evaluate(published: Mapping[str, Any], *, message: str,
             canonical: str | None, sentiment_level: str,
             context: Mapping[str, Any] | None) -> dict[str, Any]:
    """
    The handoff block for one response.

    Keeps the Slice 9 keys (`required`, `reason`, `priority`) with their
    meaning unchanged and adds the structured fields beside them.
    """
    existing = dict(published.get("handoff") or {})
    turns = _prior_unresolved(context) + (1 if unresolved(published) else 0)
    if not enabled():
        existing.setdefault("required", bool(existing.get("required")))
        return {**existing, "handoff_required": bool(existing.get("required")),
                "recommended": False, "triggers": [], "unresolved_turns": turns}

    triggers: list[str] = []
    required = False
    if existing.get("required"):
        required = True
        triggers.append(USER_REQUEST if existing.get("reason") == USER_REQUEST
                        else MANUAL_REVIEW)
    if asks_for_person(message, canonical):
        required = True
        if USER_REQUEST not in triggers:
            triggers.insert(0, USER_REQUEST)

    recommended = required
    limit = int(_settings().get("max_unresolved_turns", 3) or 3)
    if turns >= limit:
        recommended = True
        triggers.append(REPEATED_UNRESOLVED)
    if (sentiment_level == "high_frustration"
            and bool(_settings().get("recommend_on_high_frustration", True))):
        recommended = True
        triggers.append(HIGH_FRUSTRATION)

    reason = triggers[0] if triggers else None
    priorities = _settings().get("priorities") or {}
    priority = priorities.get(reason) if reason else None
    legacy_reason = existing.get("reason")
    if required and not legacy_reason:
        legacy_reason = USER_REQUEST if USER_REQUEST in triggers else reason

    block = {
        # Slice 9 contract, unchanged in meaning.
        "required": required,
        "reason": legacy_reason if required else existing.get("reason"),
        "priority": existing.get("priority") or (priority if required else None),
        # Structured handoff fields.
        "handoff_required": required,
        "recommended": recommended,
        "handoff_reason": reason,
        "handoff_priority": priority if (required or recommended) else None,
        "triggers": triggers,
        "unresolved_turns": turns,
        "status": "SIGNAL_ONLY",
    }
    if required or recommended:
        block["handoff_summary"] = _summary(published, context, turns)
    return block


__all__ = ["HIGH_FRUSTRATION", "MANUAL_REVIEW", "REPEATED_UNRESOLVED",
           "USER_REQUEST", "asks_for_person", "enabled", "evaluate", "unresolved"]
