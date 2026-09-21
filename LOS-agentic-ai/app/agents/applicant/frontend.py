"""
What a screen needs that an answer does not.

A CHAT REPLY IS NOT A UI CONTRACT. The copilot returns prose and the case
data behind it, and a frontend still has to decide what buttons to enable,
what to offer next, and whether it can act at all. Left to the frontend,
those decisions get re-implemented per client and drift from what the
service will actually permit -- a button that is enabled for a case the API
will refuse is worse than no button.

So the service computes them. EVERY FIELD HERE IS DERIVED FROM THE RESPONSE
THE CALLER IS ALREADY GETTING. No suggestion, no action and no state label
comes from a language model, for the same reason no readiness verdict does:
a frontend affordance a model could influence is one nobody can audit, and
an "Upload income proof" button that appears because a model felt it should
is a document request nobody authorised.

IT READS THE PUBLIC SHAPES, NOT THE RECORDS. Everything here takes the
checklist rows, document entries and readiness block exactly as they appear
in the response. That is deliberate: a header that counted from the store
while the detail below it came from the envelope could show two different
answers for the same case, and this way it cannot.

WHAT IS DELIBERATELY NOT HERE. Anything that decides an outcome. These are
navigation aids -- what can be done next, what is worth asking -- and none
of them changes a verdict, a requirement or a readiness answer.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

#: Document states that need somebody to do something.
_NEEDS_ATTENTION = frozenset({"REVIEW", "REJECTED"})

SATISFIED = "SATISFIED"
MISSING = "MISSING"
UNDER_REVIEW = "UNDER_REVIEW"
FAILED = "FAILED"


#: Slot names that are acronyms and read wrongly in sentence case. "Pan" is
#: a cooking vessel; "what can be used as pan?" is not a question a field
#: officer would recognise as their own.
_ACRONYMS = frozenset({"PAN", "ITR", "DL", "KYC", "NOC", "GST", "CPA"})


def _readable(slot: str) -> str:
    """A slot name as it should appear inside a sentence."""
    words = str(slot or "").replace("_", " ").split()
    return " ".join(w if w.upper() in _ACRONYMS else w.lower() for w in words)


def _rows(checklist: Sequence[Mapping[str, Any]] | None) -> list[dict]:
    return [dict(e) for e in (checklist or []) if isinstance(e, Mapping)]


def _mandatory(checklist: Sequence[Mapping[str, Any]] | None) -> list[dict]:
    return [e for e in _rows(checklist) if e.get("mandatory", True)]


def _with_fulfilment(rows: Sequence[Mapping[str, Any]], state: str) -> list[dict]:
    return [dict(e) for e in rows if e.get("fulfilment") == state]


def _collected_ids(
    documents: Sequence[Mapping[str, Any]] | None,
    checklist: Sequence[Mapping[str, Any]] | None,
) -> set[str]:
    """
    Every document this response knows about, from either source.

    BOTH, BECAUSE NEITHER IS ALWAYS THERE. A checklist answer does not
    fetch the document list -- its intent plans one tool and that tool
    returns slots. Counting only `documents` reported zero collected
    documents on a case with three, and the action panel above the
    checklist then offered nothing to do with them.

    Counted by identifier so the two sources cannot double up.
    """
    ids: set[str] = set()
    for document in (documents or []):
        if isinstance(document, Mapping) and document.get("document_id"):
            ids.add(str(document["document_id"]))
    for row in _rows(checklist):
        if row.get("document_id"):
            ids.add(str(row["document_id"]))
    return ids


def _needs_attention(
    documents: Sequence[Mapping[str, Any]] | None,
    checklist: Sequence[Mapping[str, Any]] | None,
) -> bool:
    """
    Whether anything on this case is in a state somebody has to act on.

    Same reason as above: the checklist carries `fulfilment` on every row
    and is present on every case answer, so it is the reliable source.
    `documents` supplements it for anything not bound to a slot.
    """
    if any(str(d.get("status") or "") in _NEEDS_ATTENTION
           for d in (documents or []) if isinstance(d, Mapping)):
        return True
    return any(e.get("fulfilment") in (UNDER_REVIEW, FAILED)
               for e in _rows(checklist))


# ==========================================================================
# CASE STATE -- the compact header a screen renders before anything else
# ==========================================================================


def case_state(
    application: Mapping[str, Any] | None,
    applicant: Mapping[str, Any] | None,
    documents: Sequence[Mapping[str, Any]] | None,
    checklist: Sequence[Mapping[str, Any]] | None,
    readiness: Mapping[str, Any] | None,
    stage: str | None = None,
    case_id: str | None = None,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Where this case stands, in the few fields a header needs.

    A SUMMARY OF FACTS, NOT A NEW FACT. Every number is counted from the
    same checklist the response already carries, so a screen showing the
    header and the detail cannot disagree with itself.

    `case_id` is passed separately because a narrow question does not
    fetch the application record -- a checklist intent plans one tool and
    that tool returns slots. The header then identified itself as case
    `null`, which a client cannot key on. The identifier is on the
    request, so it never has to be looked up.
    """
    required = _mandatory(checklist)
    satisfied = _with_fulfilment(required, SATISFIED)

    return {
        "case_id": (application or {}).get("case_id") or case_id,
        # From the record when the answer read one, otherwise from the
        # policy block -- which names the product it resolved for, and is
        # present on exactly the answers that carry a checklist.
        "product": ((application or {}).get("product")
                    or (policy or {}).get("product")),
        "stage": stage or (application or {}).get("status"),
        "readiness": (readiness or {}).get("status"),
        "documents_collected": len(_collected_ids(documents, checklist)),
        "documents_required": len(required),
        "documents_satisfied": len(satisfied),
        "documents_missing": len(_with_fulfilment(required, MISSING)),
        "documents_under_review": len(_with_fulfilment(required, UNDER_REVIEW)),
        "documents_failed": len(_with_fulfilment(required, FAILED)),
        # A percentage a progress bar can bind to. Over REQUIRED slots only
        # -- counting optional ones would leave a complete case short of
        # 100% and an officer looking for a document nobody needs.
        "collection_progress": (
            round(100 * len(satisfied) / len(required)) if required else 0
        ),
        "applicant_fields_missing": list(
            (applicant or {}).get("missing_fields") or []),
    }


# ==========================================================================
# AVAILABLE ACTIONS -- what this case can actually do next
# ==========================================================================

#: Every action a frontend may offer, in the order a panel lays them out.
#:
#: DISABLED WITH A REASON, NOT HIDDEN. An action that vanishes leaves an
#: officer wondering where it went; one that is visible and disabled with
#: "no document is awaiting re-upload" has answered the question. Hiding
#: also makes the contract unstable -- the list would change shape per case
#: and a client could not lay it out.
_ACTIONS = ("UPLOAD_DOCUMENT", "GET_DOCUMENT_CHECKLIST",
            "GET_VERIFICATION_STATUS", "MARK_FOR_REUPLOAD",
            "CHECK_CPA_READINESS", "GET_CASE_360")

_LABELS = {
    "UPLOAD_DOCUMENT": "Upload a document",
    "GET_DOCUMENT_CHECKLIST": "What is still needed",
    "GET_VERIFICATION_STATUS": "Document verification status",
    "MARK_FOR_REUPLOAD": "Ask for a document again",
    "CHECK_CPA_READINESS": "Check readiness for handoff",
    "GET_CASE_360": "Full case view",
}

#: How an action that is NOT a copilot `action` value is invoked.
#:
#: MARK_FOR_REUPLOAD is real and supported -- as a CUSTOM_QUERY the
#: classifier maps to the write intent, which then goes through the
#: confirmation step every write goes through. It is not a dropdown value,
#: so offering it under `action` alone would have produced a button that
#: 422s. Saying how to call it is the difference between an affordance and
#: a trap.
_INVOKE_VIA = {
    "MARK_FOR_REUPLOAD": {
        "action": "CUSTOM_QUERY",
        "message_template": "Mark the {slot} for re-upload.",
    },
}


def available_actions(
    documents: Sequence[Mapping[str, Any]] | None,
    checklist: Sequence[Mapping[str, Any]] | None,
    readiness: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """
    The actions this case supports right now, each with why or why not.

    THE ENABLED FLAG IS A PREDICTION OF WHAT THE API WILL DO, and it has to
    be a true one. An action shown as enabled that the route then refuses
    teaches an officer to distrust the whole panel.
    """
    outstanding = [e for e in _mandatory(checklist)
                   if e.get("fulfilment") != SATISFIED]
    reviewable = _needs_attention(documents, checklist)
    has_documents = bool(_collected_ids(documents, checklist))
    ready = (readiness or {}).get("status") == "READY"

    conditions: dict[str, tuple[bool, str]] = {
        "UPLOAD_DOCUMENT": (
            bool(outstanding),
            "" if outstanding
            else "Every required document has been collected.",
        ),
        "GET_DOCUMENT_CHECKLIST": (True, ""),
        "GET_VERIFICATION_STATUS": (
            has_documents,
            "" if has_documents else "No document has been uploaded yet.",
        ),
        "MARK_FOR_REUPLOAD": (
            reviewable,
            "" if reviewable
            else "No document is in a state that needs collecting again.",
        ),
        "CHECK_CPA_READINESS": (True, ""),
        "GET_CASE_360": (True, ""),
    }

    out: list[dict[str, Any]] = []
    for action in _ACTIONS:
        enabled, reason = conditions[action]
        entry: dict[str, Any] = {
            "action": action,
            "label": _LABELS[action],
            "enabled": bool(enabled),
        }
        if action in _INVOKE_VIA:
            entry["invoke"] = dict(_INVOKE_VIA[action])
        if not enabled and reason:
            entry["disabled_reason"] = reason
        out.append(entry)

    if ready:
        # Not an API action -- a handoff is a human decision and this
        # service does not perform it. Offered so the panel says what to do
        # next rather than going quiet on a finished case, and flagged
        # `external` so no client tries to POST it.
        out.append({
            "action": "HANDOFF_TO_CPA",
            "label": "Hand this case to CPA",
            "enabled": True,
            "external": True,
        })
    return out


# ==========================================================================
# SUGGESTED QUESTIONS -- what is worth asking about THIS case
# ==========================================================================


def suggested_questions(
    checklist: Sequence[Mapping[str, Any]] | None,
    readiness: Mapping[str, Any] | None,
    policy: Mapping[str, Any] | None = None,
    limit: int = 4,
) -> list[str]:
    """
    Follow-up questions that this case can actually answer.

    THE TEST OF A GOOD SUGGESTION is that clicking it returns something. A
    generic list -- "what documents are required?", "is this ready?" -- is
    identical on every case and half of it is answered by the screen the
    officer is already looking at. These are built from what is actually
    outstanding, most blocking first.

    NEVER A DOWNSTREAM QUESTION. Suggesting "is this applicant
    creditworthy?" would advertise an answer this stage refuses to give,
    and an officer who clicked it would get a routing message instead.
    """
    out: list[str] = []
    rows = _rows(checklist)

    failed = _with_fulfilment(rows, FAILED)
    under_review = _with_fulfilment(rows, UNDER_REVIEW)
    missing = [e for e in _mandatory(rows) if e.get("fulfilment") == MISSING]

    # -- what is blocking, in the order it blocks --------------------------
    if failed:
        out.append(f"Why was the {_readable(failed[0].get('slot'))} rejected?")
    if under_review:
        out.append(
            f"Why is the {_readable(under_review[0].get('slot'))} "
            f"under review?")
    if missing:
        first = missing[0]
        slot = _readable(first.get("slot"))
        # "What can be used as X?" only makes sense where SEVERAL things
        # can. Asked about PAN -- one slot, one accepted type -- the answer
        # is "a PAN", and a suggestion whose answer restates the question
        # is one an officer learns to skip past.
        if len(first.get("accepts") or []) > 1:
            out.append(f"What can be used as {slot}?")
        out.append(f"Why is {slot} required for this application?")

    # -- then the gaps that make the checklist provisional -----------------
    #
    # After the blockers, not before: a document the customer has to bring
    # back today outranks an attribute that would refine tomorrow's list.
    for gap in ((policy or {}).get("unevaluated_rules") or []):
        attributes = gap.get("missing_attributes") or []
        if attributes:
            readable = str(attributes[0]).replace("_", " ")
            out.append(f"Why does the checklist depend on the {readable}?")
            break

    # -- then the state of the case ----------------------------------------
    if (readiness or {}).get("status") == "READY":
        out.append("Is this case ready to hand over?")
    else:
        out.append("What is pending on this case?")

    # De-duplicate while keeping the order they were added in, which is the
    # order they matter in.
    return list(dict.fromkeys(out))[:limit]


# ==========================================================================
# DOCUMENT HIGHLIGHTS -- the one line a card shows
# ==========================================================================

_HIGHLIGHT = {
    "VERIFIED": ("OK", "Verified"),
    "REVIEW": ("ATTENTION", "Needs a person to look at it"),
    "REJECTED": ("BLOCKED", "Rejected - collect this again"),
    "PROCESSING": ("PENDING", "Being checked"),
    "UPLOADED": ("PENDING", "Received, not yet checked"),
}


def document_highlights(
    documents: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """
    One card per document: its state, and the first thing to know about it.

    THE REASON CODES ARE PASSED THROUGH, NOT REPHRASED. They come from
    verification, which is the only thing entitled to say why a document did
    not pass. This picks which one a one-line card shows first; it does not
    write one, and it does not drop the rest.
    """
    out: list[dict[str, Any]] = []
    for document in (documents or []):
        if not isinstance(document, Mapping):
            continue
        status = str(document.get("status") or "")
        severity, headline = _HIGHLIGHT.get(status, ("PENDING", "Received"))
        reasons = list(document.get("reason_codes") or [])
        out.append({
            "document_id": document.get("document_id"),
            "document_type": document.get("document_type"),
            "status": status or None,
            "severity": severity,
            "headline": headline,
            "primary_reason_code": reasons[0] if reasons else None,
            "reason_codes": reasons,
            "needs_attention": status in _NEEDS_ATTENTION,
        })
    return out


# ==========================================================================
# THE WHOLE BLOCK
# ==========================================================================


def contract(
    envelope: Mapping[str, Any],
    *,
    include_state: bool = True,
) -> dict[str, Any]:
    """
    Every frontend field, computed from one response envelope.

    `include_state` is false for an answer that carries no case data -- a
    knowledge answer or a routed one. A case-state header attached to a
    response that never read the case would be stating counts for a case
    the answer did not look at.
    """
    if not include_state:
        return {"case_state": None, "available_actions": [],
                "document_highlights": [], "suggested_questions": []}

    checklist = envelope.get("checklist") or []
    documents = envelope.get("documents") or []
    readiness = envelope.get("readiness") or {}
    policy = envelope.get("policy") or {}

    return {
        "case_state": case_state(
            envelope.get("application"), envelope.get("applicant"),
            documents, checklist, readiness, envelope.get("stage"),
            case_id=envelope.get("case_id"), policy=policy,
        ),
        "available_actions": available_actions(documents, checklist, readiness),
        "document_highlights": document_highlights(documents),
        "suggested_questions": suggested_questions(checklist, readiness, policy),
    }


__all__ = ["available_actions", "case_state", "contract",
           "document_highlights", "suggested_questions"]
