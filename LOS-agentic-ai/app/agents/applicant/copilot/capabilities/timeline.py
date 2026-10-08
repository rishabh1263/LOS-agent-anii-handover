"""
CASE TIMELINE AND TURNAROUND (FOS plan 7.3; flag COPILOT_CASE_TIMELINE, config chatbot.timeline -- ON in dev).

"case ka timeline", "kab kya hua", "kitne din se FOS mein hai", "case kab bana tha", "kal wala verify hua?" -- a
dated timeline from what the store recorded (the case created, each upload and its status, each KYC run, the
recorded stage events), the days in the current stage, and the stage's configured turnaround target. A case past its
target is flagged (here, in the case list row and on open) with its main blocker. Dates are the recorded ones; nothing
is estimated.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

FLAG = "COPILOT_CASE_TIMELINE"
_ON = {"1", "true", "yes", "on"}


def _cfg() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("timeline")


def enabled() -> bool:
    value = os.getenv(FLAG)
    if value is not None and value.strip():
        return value.strip().lower() in _ON
    return bool(_cfg().get("enabled", False))


def _label(key: str, **values: Any) -> str:
    from app.agents.applicant.copilot.answering import language_lock

    defaults = {"heading": "Case timeline -- {case_id}", "created": "Case created", "uploaded": "{document} uploaded "
                "({status})", "kyc": "KYC run ({status})", "event": "{event}",
                "days": "{days} day(s) in {stage}.", "over": "Past the {stage} target of {target} day(s).",
                "within": "Within the {stage} target of {target} day(s).", "blocker": "Main blocker: {blocker}.",
                "day_none": "Nothing was uploaded {when}.", "day_some": "Uploaded {when}: {items}.",
                "created_on": "The case was created on {date}."}
    return language_lock.pick((_cfg().get("labels") or {}).get(key, defaults.get(key, key))).format(**values)


def _readable(value: Any) -> str:
    from app.agents.applicant.copilot.answering.answer import _readable as readable

    return readable(value)


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _status(x: Any) -> str:
    return str(getattr(getattr(x, "status", ""), "value", getattr(x, "status", "")) or "").upper()


def build(case_id: str, repository: Any = None, *, now: datetime | None = None) -> dict[str, Any]:
    from app.agents.los import stages
    from app.store import get_repository

    repository = repository or get_repository()
    now = now or datetime.now(timezone.utc)
    application = repository.get_application(case_id)
    if application is None:
        return {"case_id": case_id, "events": []}
    events: list[dict[str, Any]] = []
    created = _when(getattr(application, "created_at", None))
    if created:
        events.append({"at": created, "kind": "CREATED", "text": _label("created")})
    for d in repository.list_documents(case_id) or []:
        at = _when(getattr(d, "uploaded_at", None))
        if at and _status(d) != "SUPERSEDED":
            events.append({"at": at, "kind": "UPLOAD", "document_type": str(d.document_type).upper(),
                           "status": _status(d), "text": _label("uploaded", document=_readable(d.document_type),
                                                                status=_readable(_status(d)).lower())})
    try:
        for f in repository.get_current_findings(case_id, kind="KYC") or []:
            at = _when(getattr(f, "created_at", None))
            if at:
                events.append({"at": at, "kind": "KYC", "text": _label("kyc", status=_readable(_status(f)).lower())})
        for e in repository.get_case_timeline(case_id) or []:
            at = _when(getattr(e, "created_at", None))
            if at and getattr(e, "event_type", None):
                text = getattr(e, "summary", None) or f"{_readable(e.event_type)}" + (
                    f" -- {e.stage}" if getattr(e, "stage", None) else "")
                events.append({"at": at, "kind": "EVENT", "stage": getattr(e, "stage", None),
                               "text": _label("event", event=text)})
    except Exception:  # noqa: BLE001 - a store without these reads: the dated uploads still stand
        pass
    events.sort(key=lambda e: e["at"])
    stage = getattr(stages.resolve(case_id).stage, "value", None) or "FOS"
    # WHEN THE CURRENT STAGE BEGAN: never left -> the case's own creation; else the first event of this stage
    # after the last event of another one (a re-recorded stage event never resets the clock)
    staged = [e for e in events if e.get("stage")]
    last_other = max((i for i, e in enumerate(staged) if e["stage"] != stage), default=None)
    if last_other is None:
        entered = created
    else:
        entered = next((e["at"] for e in staged[last_other + 1:] if e["stage"] == stage), created)
    days = max(0, (now - entered).days) if entered else None
    target = (_cfg().get("targets_days") or {}).get(stage)
    return {"case_id": case_id, "stage": stage, "events": events, "days_in_stage": days, "target_days": target,
            "over_target": bool(target is not None and days is not None and days > int(target)),
            "created_at": created}


def turnaround_line(case_id: str, repository: Any = None) -> str | None:
    """'5 day(s) in FOS. Past the FOS target of 3 day(s). Main blocker: ...' -- or None within target."""
    if not enabled():
        return None
    t = build(case_id, repository)
    if not t.get("over_target"):
        return None
    line = _label("days", days=t["days_in_stage"], stage=t["stage"]) + " " + \
        _label("over", stage=t["stage"], target=t["target_days"])
    try:
        from app.agents.applicant.copilot.answering import readiness_report

        report = readiness_report.build(case_id, repository)
        first = next((i for g in report.get("groups") or [] for i in g["items"]
                      if i["id"] == (report.get("unblock_order") or [None])[0]), None)
        if first:
            line += " " + _label("blocker", blocker=f"{first['label']} ({first['status'].lower()})")
    except Exception:  # noqa: BLE001
        pass
    return line


def render(t: dict[str, Any]) -> str:
    lines = [f"**{_label('heading', case_id=t['case_id'])}**"]
    for e in t.get("events") or []:
        lines.append(f"- {e['at'].strftime('%d %b %Y, %H:%M')} -- {e['text']}")
    if t.get("days_in_stage") is not None:
        lines += ["", _label("days", days=t["days_in_stage"], stage=t["stage"])]
        if t.get("target_days") is not None:
            lines[-1] += " " + _label("over" if t.get("over_target") else "within", stage=t["stage"],
                                      target=t["target_days"])
    return "\n".join(lines)


def asked(message: str) -> str | None:
    """TIMELINE / DAYS / CREATED / DAY:<offset> -- what a time question asks, from config phrases; else None."""
    said = " " + " ".join(re.findall(r"[\wऀ-ॿ]+", str(message or "").lower())) + " "
    phrases = _cfg().get("phrases") or {}
    for kind in ("timeline", "days", "created"):
        if any(f" {' '.join(str(p).lower().split())} " in said for p in phrases.get(kind) or []):
            return kind.upper()
    for word, offset in (_cfg().get("relative_days") or {}).items():
        if f" {str(word).lower()} " in said and any(f" {w} " in said for w in phrases.get("day_cues") or []):
            return f"DAY:{int(offset)}"
    return None


def answer(case_id: str, kind: str, repository: Any = None, *, today: date | None = None) -> str:
    t = build(case_id, repository)
    if kind == "TIMELINE":
        return render(t)
    if kind == "DAYS":
        text = _label("days", days=t["days_in_stage"], stage=t["stage"])
        if t.get("target_days") is not None:
            text += " " + _label("over" if t["over_target"] else "within", stage=t["stage"], target=t["target_days"])
        return text
    if kind == "CREATED":
        return _label("created_on", date=t["created_at"].strftime("%d %b %Y") if t.get("created_at") else "--")
    offset = int(kind.split(":", 1)[1])
    day = (today or datetime.now(timezone.utc).date()) + timedelta(days=offset)
    when = str((_cfg().get("day_words") or {}).get(str(offset)) or day.strftime("%d %b"))
    items = [f"{_readable(e['document_type'])} ({_readable(e['status']).lower()})" for e in t["events"]
             if e["kind"] == "UPLOAD" and e["at"].date() == day]
    return _label("day_some", when=when, items=", ".join(items)) if items else _label("day_none", when=when)


def attach(published: dict[str, Any], message: str) -> dict[str, Any]:
    if not isinstance(published, dict) or not enabled() or not published.get("case_id"):
        return published
    kind = asked(message)
    if kind is None:
        return published
    from app.agents.applicant.copilot.answering.counts import keep_case_header

    published["answer"] = keep_case_header(published.get("answer"), answer(published["case_id"], kind),
                                           published["case_id"])
    published.pop("answer_markdown", None)
    published["intent"] = "CASE_TIMELINE"
    return published


__all__ = ["FLAG", "answer", "asked", "attach", "build", "enabled", "render", "turnaround_line"]
