"""
QUERIES IN THE CONVERSATION -- "query raise kar do", "raise a query for this",
"which queries are open?", "any deviation?".

The chat never creates a query by itself: it finds WHAT the query should be
about from the live records (the document that needs a reviewer, the KYC under
review, the gate blocker) and returns the SAME structured RAISE_QUERY action
the frontend's button posts (app/agents/los/queries.raise_action). The query is
created when that action is confirmed, through the one service both use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

RAISE, LIST = "RAISE", "LIST"

_Q = r"quer(?:y|ies)"
_RAISE = re.compile(
    # "open" is a verb only as "open a query" -- "any open queries?" asks for a list
    rf"\b(raise|create|log|file|add|daal\w*|dal\w*|bana\w*|uthao|uthana|lagao|kar\s*do|karo|kardo)\b"
    rf"[^?.]{{0,25}}\b{_Q}\b|\bopen\s+(a|an|one|new)\s+([a-z]+\s+)?{_Q}\b|\b{_Q}\b[^?.]{{0,25}}\b(raise|daal\w*|dal\w*|bana\w*|uthao|lagao|kar\s*do|karo|kardo|"
    rf"create|open\s+(it|karo|kar\s*do))\b|क्वेरी\s*(उठा|डाल|बना|रेज़|raise)", re.I)
_LIST = re.compile(
    rf"\b(open|pending|outstanding|kaun\s*si|kitni|list|show|any|koi)\b[^?.]{{0,25}}\b({_Q}|deviations?)\b"
    rf"|\b({_Q}|deviations?)\b[^?.]{{0,25}}\b(status|pending|open|kya|hai|hain|outstanding|resolved?)\b", re.I)


@dataclass(frozen=True)
class Request:
    kind: str


def request(message: str) -> Request | None:
    text = " ".join(str(message or "").split())
    if _RAISE.search(text):
        return Request(RAISE)
    if _LIST.search(text):
        return Request(LIST)
    return None


def _subject(case_id: str, message: str, documents: list[dict[str, Any]]) -> dict[str, Any]:
    """What the query is about: the named document, else the first thing that needs someone."""
    from app.agents.applicant.copilot.answering import structured
    from app.agents.applicant.copilot.semantics import intents

    named = intents._document_type(message)
    cards = structured.verification_block(documents)["documents"]
    if named:
        cards = [c for c in cards if str(c.get("document_type") or "").upper() == named.upper()] or cards
    for card in cards:
        if card["verdict"] in ("FAIL", "REVIEW") or (named and card.get("document_type") == named):
            reason = (card.get("reason") or "").rstrip(".")
            issue = "did not pass verification" if card["verdict"] == "FAIL" else (
                "needs a reviewer" if card["verdict"] == "REVIEW" else f"is {str(card['verdict']).lower()}")
            return {"target_type": "DOCUMENT", "target_id": card.get("document_id"),
                    "query_type": "VERIFICATION_ISSUE" if card["verdict"] in ("FAIL", "REVIEW") else "CLARIFICATION",
                    "party_id": None, "label": card["label"],
                    "prefill": f"Please review the {card['label']}: it {issue}" + (f" ({reason.lower()})." if reason else "."),
                    "evidence_refs": [{"type": "DOCUMENT", "document_id": card.get("document_id"),
                                       "reason_codes": card.get("reason_codes") or []}]}
    # the gate blocker, from the records
    try:
        from app.agents.los import stage_gate, stages

        stage = stages.resolve(case_id).stage
        gate = stage_gate.public(stage_gate.evaluate_live(case_id, getattr(stage, "value", stage)))
        blocker = next((b for b in gate.get("blockers") or [] if b.get("id") not in ("OPEN_QUERIES",)), None)
        if blocker:
            return {"target_type": "STAGE", "target_id": gate.get("stage"), "query_type": "CLARIFICATION",
                    "party_id": None, "label": blocker.get("label"),
                    "prefill": f"Please clarify what is needed for: {blocker.get('label')}.",
                    "evidence_refs": [{"type": "GATE_CHECK", "id": blocker.get("id"),
                                       "reason_code": blocker.get("reason_code")}]}
    except Exception:  # noqa: BLE001 - no gate read: a case-level query
        pass
    return {"target_type": "CASE", "target_id": case_id, "query_type": "CLARIFICATION", "party_id": None,
            "label": "this case", "prefill": "", "evidence_refs": []}


_STAGES = r"(fos|cpa|credit|rcu|bops|hops|disbursement)"
#: "to CPA", "CPA ko", "CPA ke liye", "for CPA" -- the stage the query is sent to
_TARGET = re.compile(rf"\b(?:to|for)\s+(?:the\s+)?{_STAGES}\b|\b{_STAGES}\s+(?:ko|ke\s+liye|ki\s+taraf|team)\b", re.I)
#: what it is about: "... for the income mismatch", "income mismatch ke liye", "about X", "regarding X"
_ABOUT = re.compile(r"\b(?:about|regarding|on)\s+(?:the\s+)?(.{3,80}?)\s*[?.!]*$"
                    r"|\b(?:for|ke\s+liye)\s+(?:the\s+)?(?!" + _STAGES + r"\b)(.{3,80}?)\s*(?:ke\s+liye)?\s*[?.!]*$"
                    r"|(?:hai|karni\s+hai|chahiye)\s+(.{3,80}?)\s+ke\s+liye\b", re.I)


def _target_and_subject(message: str) -> tuple[str | None, str | None]:
    text = " ".join(str(message or "").split())
    found = _TARGET.search(text)
    target = (found.group(1) or found.group(2)).upper() if found else None
    about = _ABOUT.search(text)
    subject = next((g for g in (about.groups() if about else ()) if g), None)
    if subject:
        subject = re.sub(r"\b(query|raise|kar\w*|karo|do|please|ke\s+liye|ko)\b", " ", subject, flags=re.I)
        subject = " ".join(subject.split()).strip(" .") or None
        if subject and (re.fullmatch(_STAGES, subject, re.I) or re.fullmatch(
                r"(this|that|it|this one|iska|iske|isko|uska|ye|yeh)", subject, re.I)):
            subject = None                  # a pronoun is the live blocker's job, not a subject
    return target, subject


def run(req: Request, *, case_id: str, message: str, documents: list[dict[str, Any]]) -> dict[str, Any]:
    from app.agents.los import queries, stages

    items = queries.open_items(case_id)
    if req.kind == LIST:
        qs, ds = items["queries"], items["deviations"]
        lines = [f"{q['query_id']} -- {str(q['status']).lower()}"
                 + (f", sent to {q['target_stage']}" if q.get("target_stage") else "")
                 + (f", answered by {q['responded_by']}" if q.get("responded_by") else
                    (", no response yet" if q.get("target_stage") else ""))
                 + f" (raised by {q.get('raised_by')}): {q.get('text')}" for q in qs]
        head = (f"{len(qs)} open quer{'y' if len(qs) == 1 else 'ies'} on this case:\n" + "\n".join(f"- {x}" for x in lines)
                if qs else "No queries are open on this case.")
        if ds:
            said = f"{len(ds)} deviation(s) are waiting for the approving authority."
        elif queries.deviation_rules_status() == "CONFIGURATION_GAP":
            said = ("No deviation is recorded on this case. No deviation rules are configured yet, so none "
                    "can be raised or tracked automatically.")
        else:
            said = "No deviation is pending on this case."
        # asked about deviations: that answer first, the queries after it
        if re.search(r"\bdeviations?\b", message, re.I):
            head = said + ("\n" + head if qs else "")
        else:
            head += "\n" + said if ds else ""
        return {"answer": head, "response_type": "CASE_QUERIES", "queries": qs, "deviations": ds,
                "deviation_rules": queries.deviation_rules_status(), "actions": []}

    stage = getattr(stages.resolve(case_id).stage, "value", None)
    target_stage, about = _target_and_subject(message)
    if target_stage:
        allowed = [str(s).upper() for s in ((queries.config("queries").get("routes") or {}).get(stage or "") or [])]
        if target_stage not in allowed:
            return {"answer": (f"A query can't be sent to {target_stage} from {stage or 'this stage'}. "
                               + (f"It can go to: {', '.join(allowed)}." if allowed else
                                  "No stage route is configured from here.")),
                    "response_type": "QUERY_NOT_ROUTABLE", "queries": items["queries"], "actions": []}
    if about:
        # WHAT THE OFFICER NAMED is the subject; the live blocker is not guessed over it
        kind = ("DOCUMENT_DISCREPANCY" if re.search(r"mismatch|discrepan|differ|match\s+nahi", about, re.I)
                else "MISSING_INFORMATION" if re.search(r"missing|pending|baaki|nahi\s+(mila|hai)", about, re.I)
                else "CLARIFICATION")
        subject = {"target_type": "CASE", "target_id": case_id, "query_type": kind, "party_id": None,
                   "label": about, "prefill": f"Please clarify the {about}.", "evidence_refs": []}
    else:
        subject = _subject(case_id, message, documents)
    existing = next((q for q in items["queries"] if q.get("target_type") == subject["target_type"]
                     and q.get("target_id") == subject["target_id"]
                     and q.get("target_stage") == target_stage
                     and (not about or (q.get("subject") or "").lower() == about.lower())), None)
    if existing:
        return {"answer": (f"A query on {subject['label']} is already open ({existing['query_id']}, "
                           f"{str(existing['status']).lower()}): \"{existing.get('text')}\". I haven't raised a "
                           f"second one."),
                "response_type": "QUERY_EXISTS", "queries": [existing], "actions": []}
    action = queries.raise_action(case_id=case_id, stage=stage, target_type=subject["target_type"],
                                  target_id=subject["target_id"], query_type=subject["query_type"],
                                  prefill=subject["prefill"], party_id=subject["party_id"],
                                  evidence_refs=subject["evidence_refs"], target_stage=target_stage,
                                  subject=about)
    if about:
        answer = (f"I can prepare a query{' to ' + target_stage if target_stage else ''} about the {about}: "
                  f"\"{subject['prefill']}\" Check the wording and confirm with "
                  f"{action['label']} -- nothing is sent until you do.")
    elif subject["target_type"] == "CASE":
        answer = ("Nothing on this case is waiting on a reviewer right now. If you still want to raise a query, "
                  "write what you need answered and confirm it with Raise Query.")
    else:
        answer = (f"I can raise a query{' to ' + target_stage if target_stage else ''} on {subject['label']}: "
                  f"\"{subject['prefill']}\" Check the wording and confirm with {action['label']} -- "
                  f"nothing is sent until you do.")
    return {"answer": answer, "response_type": "QUERY_PROPOSED", "queries": items["queries"],
            "actions": [action], "raise_query_action": action}


__all__ = ["LIST", "RAISE", "Request", "request", "run"]
