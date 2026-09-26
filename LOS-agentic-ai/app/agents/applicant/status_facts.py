"""
Where one application stands, said in one or two sentences.

THE LIVE ANSWER THIS REPLACES. Asked "what is my application status?",
the Copilot replied:

    Application case_9d6ea4703ac149aa9c7ed4023313558b is Basic Document
    Verification. No product has been selected yet. Created 2026-09-24.
    However, your application is under review because there was only one
    document to compare.

An internal identifier, two pieces of metadata nobody asked for, and a
reason code's description ("only one document to compare") where the
officer needed what it means for them: the KYC cross-check had a single
document to work from, so more documents are needed -- and Address Proof,
the one still missing, was never mentioned.

THE ANSWER, IN ORDER:

    1. the stage the application is in              (application record)
    2. whether it is under review, and why           (latest recorded
                                                      decision + reasons)
    3. the required documents still missing, where   (checklist the plan
       they explain the stage                         already fetched)

IT INVENTS NOTHING. Every clause is a recorded status, a recorded reason
code or a checklist slot; there is no model here and no inference. No
identifier, no creation date and no product appear -- the question did not
ask for them, and the response carries them structurally for any caller
that does.
"""

from __future__ import annotations

from typing import Any

from app.agents.applicant import case_memory_facts
from app.agents.applicant.answer import _readable

#: Reason codes that mean "the check needed more documents than it had".
#: Said as what the officer has to do about it, which is what the code
#: means -- not as a description of the comparison that could not run.
NEEDS_DOCUMENTS = frozenset({"INSUFFICIENT_SOURCES"})

NEEDS_DOCUMENTS_CLAUSE = "more documents are needed for verification"

#: Statuses whose own words read badly after "under".
_STAGE_SENTENCE = {
    "APPLICATION_CREATED": "Your application has been created",
    "READY_FOR_CPA": "Your application is ready for CPA",
}

#: A chat answer names the first few; the checklist carries the rest.
_MAX_PENDING = 3

#: A decision that holds the application, and how it is said.
_HELD = {"REVIEW": "is under review", "REJECT": "was declined"}


def stage_sentence(application: dict[str, Any]) -> str:
    """The stage alone, without a full stop. Never an identifier."""
    status = str(application.get("status") or "").upper()
    if not status:
        return "Your application's status has not been recorded"
    return _STAGE_SENTENCE.get(
        status, f"Your application is currently under {_readable(status)}")


def pending_documents(checklist: list[dict[str, Any]] | None) -> list[str]:
    """Required checklist slots with nothing uploaded, in checklist order."""
    return [
        _readable(entry.get("slot"))
        for entry in (checklist or [])
        if entry.get("mandatory", True)
        and str(entry.get("status") or "").upper() == "MISSING"
        and entry.get("slot")
    ]


def _pending_sentence(names: list[str], *, still: bool) -> str:
    shown = names[:_MAX_PENDING]
    more = len(names) - len(shown)
    listed = (", ".join(shown) + f" and {more} more" if more > 0
              else case_memory_facts._and_list(shown))
    verb = "are" if len(names) > 1 else "is"
    return f"{listed} {verb} {'still ' if still else ''}pending."


def _reason(memory: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """
    Why the application is held, as a clause -- and what says so.

    A concrete recorded reason (a failed comparison with both values, or
    any reason code other than "needs more documents") is reported as the
    case-history answer reports it. A reason that only says the check
    lacked documents becomes NEEDS_DOCUMENTS_CLAUSE. Nothing recorded is
    an empty clause, never a guess.
    """
    findings = memory.get("findings") or []
    decisions = memory.get("decisions") or []
    explaining = [
        f for f in findings
        if f.get("reason_codes")
        and f.get("finding_kind") in case_memory_facts._EXPLAINING_KINDS
        and str(f.get("status") or "").upper() not in {"PASS", "SKIPPED"}
    ]
    sources = case_memory_facts._sources(findings=explaining,
                                         decisions=decisions)

    concrete = case_memory_facts._mismatch_detail(findings)
    if concrete:
        return concrete, sources

    codes: list[str] = []
    for finding in explaining:
        for code in finding.get("reason_codes") or []:
            if code not in codes:
                codes.append(code)
    if decisions:
        for code in decisions[-1].get("reason_codes") or []:
            if code not in codes:
                codes.append(code)

    specific = [c for c in codes if c not in NEEDS_DOCUMENTS]
    if specific:
        # Two reasons at most: the answer is one sentence, and the
        # case-history question lists every finding for whoever asks it.
        return (case_memory_facts._and_list(
            [case_memory_facts._readable(c) for c in specific[:2]]), sources)
    if codes:
        return NEEDS_DOCUMENTS_CLAUSE, sources
    return "", sources


def stage_has_case_detail(stage: str | None) -> bool:
    """
    Whether the stage registry gives this stage case capabilities.

    READ FROM THE REGISTRY, never from a stage name: FOS is the only stage
    with case capabilities today, and a stage registered with them later
    gets the full answer with no change here.
    """
    if not stage:
        return True
    from app.agents.los import stage_registry, stages

    parsed = stages.parse(stage)
    if parsed is None:
        return False
    return "case_facts" in stage_registry.capabilities_for(parsed).capabilities


def capabilities(stage: str | None):
    """The stage registry's entry for a stage name (never a guess)."""
    from app.agents.los import stage_registry, stages

    return stage_registry.capabilities_for(stages.parse(stage))


def during_stage(decision: dict[str, Any], since: str | None) -> bool:
    """
    Whether a recorded decision belongs to the CURRENT stage.

    A decision recorded before the case entered its current stage was
    made at an earlier one -- a FOS review is not a CREDIT review -- and is
    never reported as this stage's hold. Without a stage start, or without
    a decision time, nothing can be said to be stale, so it counts.
    """
    recorded = decision.get("recorded_at")
    if not since or not recorded:
        return True
    # STRICTLY AFTER. A decision stamped the same instant the stage began was
    # written with the transition, not made in the new stage -- and on a
    # coarse clock the two routinely share a timestamp.
    return str(recorded) > str(since)


def _at_stage(stage: str, checklist: list[dict[str, Any]] | None,
              memory: dict[str, Any], since: str | None
              ) -> tuple[str, list[dict[str, Any]], bool]:
    """
    A case at a stage WITHOUT FOS-style case facts: what the stage's own
    records support, capability by capability, and nothing else.

        Your application is currently at the CPA stage. Income Proof is
        still pending. Detailed CPA assessment information is not
        currently available.
    """
    from app.agents.applicant import config

    registered = capabilities(stage)
    label = config.stage_label(stage)
    opening = f"Your application is currently at the {label} stage"

    decisions = [d for d in memory.get("decisions") or []
                 if during_stage(d, since)]
    decision = str((decisions[-1] if decisions else {}).get("decision")
                   or "").upper()
    held = _HELD.get(decision) if "case_history" in registered.capabilities else None

    sources: list[dict[str, Any]] = []
    if held:
        reason, sources = _reason({**memory, "decisions": decisions})
        sentences = [f"{opening} and {held}"
                     + (f" because {reason}." if reason else ".")]
    else:
        sentences = [opening + "."]

    if "pending_items" in registered.capabilities:
        pending = pending_documents(checklist)
        sentences.append(_pending_sentence(pending, still=True) if pending
                         else "No documents are pending for this stage.")

    if registered.not_available:
        topic = registered.not_available[0]
        sentences.append(f"{topic[0].upper()}{topic[1:]} "
                         f"{'are' if topic.endswith('s') else 'is'} not "
                         f"currently available.")
    return " ".join(sentences[:3]), sources, bool(held)


def stage_answer(application: dict[str, Any], stage: str | None) -> str:
    """Which stage, and -- where the stage has case detail -- where in it."""
    from app.agents.applicant import config

    status = str(application.get("status") or "").upper()
    if stage and not stage_has_case_detail(stage):
        # The FOS application status (READY_FOR_CPA ...) says nothing about
        # where a case at CREDIT is; the stage is the answer.
        return f"Your application is at the {config.stage_label(stage)} stage."
    if stage and status:
        return (f"Your application is in the {config.stage_label(stage)} "
                f"stage, at {_readable(status)}.")
    if status:
        return f"Your application is at {_readable(status)}."
    return "Your application's stage has not been recorded."


def _when(value: str) -> str:
    """An ISO timestamp as an answer says it."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y, %H:%M UTC")
    except (TypeError, ValueError):
        return str(value)


def stage_history_answer(message: str, context: Any) -> str:
    """
    Where the case HAS BEEN: the stage before one, when it moved, why.

    READ FROM THE RECORDED HISTORY ONLY (stages.resolve -> StageContext.
    history). A reason nobody recorded is reported as not recorded -- never
    a plausible one supplied in its place.
    """
    import re

    from app.agents.applicant import config
    from app.agents.applicant.intents import stage_in

    label = config.stage_label
    history = [dict(e) for e in (getattr(context, "history", None) or ())
               if e.get("stage")]
    current = getattr(getattr(context, "stage", None), "value", None)
    if not history or current is None:
        return "Your application's stage history has not been recorded."

    text = (message or "").lower()
    named = stage_in(message)

    def last_visit(stage: str) -> int | None:
        for index in range(len(history) - 1, -1, -1):
            if history[index]["stage"] == stage:
                return index
        return None

    # WHAT CHANGED: the recorded moves, after the named stage (or all of
    # them), each with when it happened. Findings are not re-derived here.
    # "What happened after FOS?" is the same question in the past tense.
    if re.search(r"\bchanged\b|\bhappened\s+(after|since)\b", text):
        start = 0
        if named:
            visit = last_visit(named)
            if visit is None:
                return (f"Your application has not been at the "
                        f"{label(named)} stage.")
            start = visit
        moves = history[start + 1:]
        if not moves:
            since = label(named or current)
            return (f"Nothing has changed in your application's stage since it "
                    f"entered the {since} stage; it is still at the "
                    f"{label(current)} stage.")
        parts = [f"to the {label(m['stage'])} stage"
                 + (f" on {_when(m['started_at'])}" if m.get("started_at") else "")
                 for m in moves[:3]]
        return (f"Since the {label(history[start]['stage'])} stage, your "
                f"application moved " + ", then ".join(parts) + ".")

    if re.search(r"\bbefore\b|\b(previous|prior|earlier|last)\s+stage\b", text):
        anchor = named or current
        index = last_visit(anchor)
        if index is None:
            return (f"Your application has not been at the "
                    f"{label(anchor)} stage.")
        previous = history[index].get("previous_stage") or (
            history[index - 1]["stage"] if index > 0 else None)
        if not previous:
            return (f"The {label(anchor)} stage is the first stage recorded "
                    f"for your application.")
        return (f"Before the {label(anchor)} stage, your application was at "
                f"the {label(previous)} stage.")

    target = named or current
    index = last_visit(target)
    if index is None:
        return f"Your application has not been at the {label(target)} stage."
    entry = history[index]

    if re.search(r"\bwhy\b", text):
        reason = entry.get("reason")
        if not reason:
            return (f"No reason was recorded for your application's move to "
                    f"the {label(target)} stage.")
        if (re.fullmatch(r"[A-Z0-9]+(_[A-Z0-9]+)+", reason)
                and not config.expose_internal_reason_codes()):
            # A recorded reason CODE (FOS_HANDOFF) is spelled out, not
            # reworded: the same words, readable.
            reason = _readable(reason)
        return (f"Your application moved to the {label(target)} stage with "
                f"the recorded reason: {reason.rstrip('.')}.")

    if re.search(r"\bwhen\b", text):
        started = entry.get("started_at")
        if not started:
            return (f"The time your application entered the {label(target)} "
                    f"stage was not recorded.")
        return (f"Your application moved to the {label(target)} stage on "
                f"{_when(started)}.")

    path = ", then ".join(label(e["stage"]) for e in history)
    return (f"Your application's stages so far: {path}. It is currently at "
            f"the {label(current)} stage.")


def answer(
    application: dict[str, Any],
    checklist: list[dict[str, Any]] | None,
    memory: dict[str, Any] | None,
    stage: str | None = None,
    since: str | None = None,
) -> tuple[str, list[dict[str, Any]], bool]:
    """
    The status answer, the records it cites, and whether a recorded
    decision holds the application (which the caller must not let a model
    rephrase).

        Your application is currently under Basic Document Verification
        and is under review because more documents are needed for
        verification. Address Proof is pending.
    """
    # THE STAGE THE CASE IS IN DECIDES WHAT CAN BE SAID. A case that has
    # left FOS is not described by FOS statuses; its answer is built from
    # what its own stage's capabilities can serve -- the stage-aware
    # checklist, holds recorded during the stage -- and says plainly what
    # is not available. Never a FOS answer.
    if stage and not stage_has_case_detail(stage):
        return _at_stage(stage, checklist, memory or {}, since)

    memory = memory or {}
    decisions = [d for d in memory.get("decisions") or []
                 if during_stage(d, since)]
    decision = str((decisions[-1] if decisions else {}).get("decision")
                   or "").upper()
    pending = pending_documents(checklist)
    opening = stage_sentence(application)

    held = _HELD.get(decision)
    if not held:
        sentences = [opening + "."]
        if pending:
            sentences.append(_pending_sentence(pending, still=True))
        return " ".join(sentences), [], False

    reason, sources = _reason(memory)
    if reason == NEEDS_DOCUMENTS_CLAUSE:
        sentences = [f"{opening} and {held} because {reason}."]
        if pending:
            sentences.append(_pending_sentence(pending, still=False))
    elif reason:
        # A concrete reason explains the hold by itself; a missing
        # document beside it would read as a second cause.
        sentences = [f"{opening} and {held} because {reason}."]
    else:
        sentences = [f"{opening} and {held}."]
        if pending:
            sentences.append(_pending_sentence(pending, still=True))
    return " ".join(sentences), sources, True


def names_an_identifier(text: str, *identifiers: str | None) -> bool:
    """Whether a sentence exposes one of the case's internal identifiers."""
    lowered = (text or "").lower()
    return any(i and str(i).lower() in lowered for i in identifiers)


__all__ = ["NEEDS_DOCUMENTS", "NEEDS_DOCUMENTS_CLAUSE", "answer",
           "capabilities", "during_stage",
           "names_an_identifier", "pending_documents", "stage_answer",
           "stage_history_answer",
           "stage_has_case_detail", "stage_sentence"]
