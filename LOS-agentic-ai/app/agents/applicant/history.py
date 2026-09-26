"""
"WHAT CHANGED?" -- answered from the case ledger's recorded events, only.

    question -> window (deterministic) -> ledger.events() -> subject filter
             -> structured changes -> one deterministic sentence

NO MODEL DECIDES WHAT CHANGED, AND NONE PHRASES IT. The answer is built here
from recorded events and is published as built (quoted): a composer asked
for two sentences is exactly how "the co-applicant's PAN" becomes "a PAN".

TIME IS DEFINED, NOT GUESSED (`window`). Every window is computed from the
system clock in the configured zone (`chatbot.history.timezone`, default
UTC) and published with the answer, so "since yesterday" always names the
boundary it used:

    today / so far today        00:00 today
    yesterday / since yesterday 00:00 yesterday
    last|past N day(s)          now - N days
    last|past N hour(s)         now - N hours
    this week                   00:00 Monday of this week
    last|past week              now - 7 days
    recently / lately           now - 7 days
    since <STAGE>               when the case last ENTERED that stage
    after <STAGE>               when the case LEFT that stage (next move)
    (none)                      everything recorded

"NOTHING CHANGED" IS EARNED. It is said only when the window was checked
against the recorded event families, and it says "recorded", and names what
the store does not keep as history -- so an absence of records is never
presented as an absence of change. A window that needs timestamps says how
many recorded changes carry none, rather than placing them anywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.agents.applicant import ledger as case_ledger

_I = re.IGNORECASE


@dataclass(frozen=True)
class Window:
    label: str                    # as the answer says it: "yesterday"
    start: datetime | None        # inclusive; None = from the beginning
    kind: str                     # CLOCK | STAGE | ALL

    def public(self) -> dict[str, Any]:
        return {"label": self.label, "kind": self.kind,
                "start": self.start.isoformat() if self.start else None}


def _zone():
    from app.agents.applicant import config

    name = str(config.chatbot("history").get("timezone") or "UTC")
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def _midnight(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def window(message: str, events: list[dict[str, Any]],
           now: datetime | None = None) -> Window | None:
    """
    The window the question asks about, or None for "everything".
    Raises nothing: a stage window the history cannot place returns a
    Window with `start=None` and kind STAGE_UNPLACED (said so, not guessed).
    """
    from app.agents.applicant.intents import stage_in

    text = message or ""
    zone = _zone()
    moment = (now or datetime.now(timezone.utc)).astimezone(zone)

    match = re.search(r"\b(last|past)\s+(\d{1,3})\s+(day|hour)s?\b", text, _I)
    if match:
        count, unit = int(match.group(2)), match.group(3).lower()
        delta = timedelta(days=count) if unit == "day" else timedelta(hours=count)
        return Window(f"the last {count} {unit}{'s' if count != 1 else ''}",
                      moment - delta, "CLOCK")
    if re.search(r"\b(since\s+)?yesterday\b", text, _I):
        return Window("yesterday", _midnight(moment) - timedelta(days=1),
                      "CLOCK")
    if re.search(r"\btoday\b", text, _I):
        return Window("today", _midnight(moment), "CLOCK")
    if re.search(r"\bthis\s+week\b", text, _I):
        return Window("the start of this week",
                      _midnight(moment) - timedelta(days=moment.weekday()),
                      "CLOCK")
    if re.search(r"\b((last|past)\s+week|recently|lately|of\s+late)\b",
                 text, _I):
        return Window("the last 7 days", moment - timedelta(days=7), "CLOCK")

    stage = stage_in(text)
    if stage and re.search(r"\b(since|after)\b", text, _I):
        return _stage_window(stage, bool(re.search(r"\bafter\b", text, _I)),
                             events)
    return None


def _stage_window(stage: str, after: bool,
                  events: list[dict[str, Any]]) -> Window:
    from app.agents.applicant import config

    moves = [e for e in events if e["event_type"] == "STAGE_CHANGED"]
    label = config.stage_label(stage)
    entered = [e for e in moves if e["current"] == stage]
    left = [e for e in moves if e["previous"] == stage]
    # A case STARTS in its first stage without a recorded move into it; a
    # recorded move OUT of a stage is proof it was there.
    if not entered and not left:
        return Window(f"the {label} stage", None, "STAGE_NEVER")
    since = entered[-1]["changed_at"] if entered else None
    if not after:
        if not entered:        # the case began there: all of its history
            return Window(f"the {label} stage", None, "STAGE")
        return Window(f"the {label} stage",
                      _parse(since), "STAGE" if since else "STAGE_UNPLACED")
    later = [e for e in left if (e["changed_at"] or "") >= (since or "")]
    if not later:
        return Window(f"the {label} stage", None, "STAGE_NOT_LEFT")
    start = later[-1]["changed_at"]
    return Window(f"the {label} stage", _parse(start),
                  "STAGE" if start else "STAGE_UNPLACED")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ==========================================================================
# THE ANSWER
# ==========================================================================

_STATE_WORDS = {"PASS": "verified", "VERIFIED": "verified",
                "REVIEW": "under review", "FAIL": "rejected",
                "REJECTED": "rejected", "SKIPPED": "not checked",
                "CONTINUE": "continue", "IN_PROGRESS": "in progress",
                "READY_FOR_HANDOFF": "ready for handoff", "ON_HOLD": "on hold"}


def _state(value: Any) -> str:
    key = str(value or "").upper()
    return _STATE_WORDS.get(key, key.replace("_", " ").lower() or "unknown")


def _when(value: str | None) -> str:
    moment = _parse(value)
    if moment is None:
        return ""
    local = moment.astimezone(_zone())
    return f" on {local.day} {local.strftime('%b %Y')}"


def _whose(event: dict[str, Any], multi_party: bool) -> str:
    role = (event.get("subject") or {}).get("party_role")
    if not multi_party or not role:
        return "the"
    return ("the primary applicant's" if role == "PRIMARY_APPLICANT"
            else "the co-applicant's")


def _doc(event: dict[str, Any]) -> str:
    from app.agents.applicant.subjects import _type

    return _type((event.get("source") or {}).get("document_type"))


def describe(event: dict[str, Any], multi_party: bool) -> str:
    """One recorded change, as a clause. Never more than the record says."""
    from app.agents.applicant import config
    from app.agents.applicant.case_memory_facts import _readable

    kind, when = event["event_type"], _when(event.get("changed_at"))
    whose = _whose(event, multi_party)
    if kind == "STAGE_CHANGED":
        if event.get("previous"):
            return (f"it moved from the {config.stage_label(event['previous'])} "
                    f"stage to the {config.stage_label(event['current'])} "
                    f"stage{when}")
        return f"it entered the {config.stage_label(event['current'])} stage{when}"
    if kind == "STAGE_STATUS_CHANGED":
        return (f"the stage status changed from {_state(event.get('previous'))} "
                f"to {_state(event.get('current'))}{when}")
    if kind == "DOCUMENT_UPLOADED":
        return f"{whose} {_doc(event)} was uploaded{when}"
    if kind == "VERIFICATION_RECORDED":
        return (f"{whose} {_doc(event)} verification was recorded as "
                f"{_state(event.get('current'))}{when}")
    if kind == "VERIFICATION_CHANGED":
        return (f"{whose} {_doc(event)} verification changed from "
                f"{_state(event.get('previous'))} to "
                f"{_state(event.get('current'))}{when}")
    party = ""
    if multi_party and (event.get("subject") or {}).get("party_role"):
        party = (" for the primary applicant"
                 if event["subject"]["party_role"] == "PRIMARY_APPLICANT"
                 else " for the co-applicant")
    if kind == "FINDING_RECORDED":
        reason = ", ".join(_readable(c).rstrip(".")
                           for c in event.get("codes") or []) \
            or _state(event.get("current"))
        return f"a {event['source']['type'].lower()} check recorded: {reason}{party}{when}"
    if kind == "FINDING_CHANGED":
        return (f"a {event['source']['type'].lower()} check changed from "
                f"{_state(event.get('previous'))} to "
                f"{_state(event.get('current'))}{party}{when}")
    if kind == "DECISION_RECORDED":
        return f"a case decision was recorded as {_state(event.get('current'))}{when}"
    if kind == "DECISION_CHANGED":
        return (f"the case decision changed from {_state(event.get('previous'))} "
                f"to {_state(event.get('current'))}{when}")
    if kind == "APPLICATION_CREATED":
        return f"the application was created{when}"
    return f"a {kind.replace('_', ' ').lower()} was recorded{when}"


#: Clauses said in one answer. The structured list carries every one.
MAX_CLAUSES = 3


def _in_scope(event: dict[str, Any], party_ids: set[str] | None) -> bool:
    if party_ids is None:
        return True
    return (event.get("subject") or {}).get("party_id") in party_ids


def changes(ledger: "case_ledger.Ledger", message: str, *,
            party_ids: set[str] | None = None,
            now: datetime | None = None) -> dict[str, Any]:
    """
    The structured truth for a "what changed" question: the window, the
    events in it (in order), how many recorded events had no time, and what
    the store does not record. Party-scoped when `party_ids` is given --
    then only events recorded against those parties.
    """
    events = ledger.events()
    span = window(message, events, now)
    scoped = [e for e in events if _in_scope(e, party_ids)]
    if span is None or span.start is None:
        chosen, undated = scoped, 0
    else:
        chosen = [e for e in scoped
                  if e["changed_at"] and _parse(e["changed_at"]) >= span.start]
        undated = sum(1 for e in scoped if not e["changed_at"])
    return {"window": span, "events": chosen, "undated": undated,
            "completeness": ledger.completeness()}


def answer(truth: dict[str, Any], *, multi_party: bool,
           subject_label: str | None = None) -> str:
    """One or two sentences, deterministically, from `changes()`."""
    span: Window | None = truth["window"]
    events = truth["events"]
    about = f" for {subject_label}" if subject_label else ""

    if span is not None and span.kind == "STAGE_NEVER":
        return f"Your application has not been at {span.label}."
    if span is not None and span.kind == "STAGE_NOT_LEFT":
        return f"Your application has not left {span.label} yet."
    if span is not None and span.kind == "STAGE_UNPLACED":
        return (f"The time your application was at {span.label} is not "
                f"recorded, so changes since then cannot be placed.")

    since = f" since {span.label}" if span is not None else ""
    if not events:
        base = (f"Nothing has been recorded as changing{about}{since}."
                if span is not None or subject_label
                else "No changes have been recorded on your application yet.")
        note = " Checklist and application-detail changes are not kept as history."
        if truth.get("undated"):
            note += (f" {truth['undated']} recorded change(s) have no time and "
                     f"could not be placed in that window.")
        return base + note

    recent = events[-MAX_CLAUSES:]
    clauses = [describe(e, multi_party) for e in recent]
    more = len(events) - len(recent)
    lead = f"Since {span.label}{about}, " if span is not None else (
        f"Recorded changes{about}: " if subject_label else "Recorded changes: ")
    said = "; ".join(clauses)
    tail = f"; plus {more} earlier change(s)" if more > 0 else ""
    sentence = lead + said + tail + "."
    return sentence[0].upper() + sentence[1:]


def public(truth: dict[str, Any]) -> dict[str, Any]:
    """What a response may carry: the changes without record ids."""
    span: Window | None = truth["window"]
    return {
        "window": span.public() if span else None,
        "changes": [case_ledger.public_event(e) for e in truth["events"]],
        "undated": truth.get("undated", 0),
        "not_recorded": list(truth["completeness"]["not_recorded"]),
    }


def asks_what_changed(message: str) -> bool:
    return bool(re.search(r"\bchanged\b|\bwhat\s+(has\s+)?happened\s+"
                          r"(after|since|recently|lately|today|yesterday)\b",
                          message or "", _I))


__all__ = ["MAX_CLAUSES", "Window", "answer", "asks_what_changed", "changes",
           "describe", "public", "window"]
