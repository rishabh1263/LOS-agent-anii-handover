"""
THE CANONICAL RESPONSE CONTRACT (2026-10-05, extended 2026-10-06).

One block, on EVERY route (typed question, dropdown read, upload), that a frontend
renders without parsing prose:

    {"message", "intent", "status", "next_step", "case_context",
     "sections":   [{"title", "items": [{"icon", "label", "status"}]}],
     "documents":  [{"party", "label", "document_type", "icon", "status", "state",
                     "reason", "action", "action_required"}],
     "document_groups": [{"party", "documents": [...]}],
     "actions":    [{"label", "action"}],          (alias: next_actions)
     "knowledge_sources": [{"title", "type", "version", "effective_date"}],
     "citations":  [...]                           (same list, kept for older clients)
     "semantic_decisions": [{"type", "severity", "subject", "confidence",
                             "recommended_action", "source", "acted_upon"}],
     "metadata":   {"request_id", "language", "response_source"}}

DERIVED, never authored: every value is read from the envelope the copilot already
built. Nothing here decides anything or calls a model. NO INTERNALS: no document
ids, reason codes, scores, hashes or provider names -- a reason is the catalogue's
sentence, provenance lives in `knowledge_sources`, the request id in `metadata`.
SUPERSEDED documents are never listed (they are history, not the case).
"""

from __future__ import annotations

from typing import Any

from app.agents.applicant.copilot.answering.answer import _STATE_ICON, _doc_label, _explained

_ICONS = ("✓", "⚠", "✗", "⏳", "•", "○")

#: stored status -> (canonical state, action the user can take or None)
_DOC_STATE = {"VERIFIED": ("VERIFIED", None), "PASS": ("VERIFIED", None),
              "REVIEW": ("REVIEW", "Check the document, or upload a clearer copy"),
              "REJECTED": ("REJECTED", "Upload the correct document"),
              "FAIL": ("REJECTED", "Upload the correct document"),
              "UPLOADED": ("PROCESSING", None), "PROCESSING": ("PROCESSING", None),
              "PENDING": ("PROCESSING", None)}
_PARTY = {"CO_APPLICANT": "Co-applicant", "PRIMARY_APPLICANT": "Applicant"}


def _item(line: str) -> dict[str, Any]:
    icon, rest = line[0], line[1:].strip()
    label, _, status = rest.partition(" — ")
    return {"icon": icon, "label": label.strip(), "status": status.strip() or None}


def _sections(answer: str) -> tuple[str, list[dict[str, Any]]]:
    """Heading lines ('Pending:') open a section; icon lines are its items."""
    message: list[str] = []
    sections: list[dict[str, Any]] = []
    for raw in (answer or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(_ICONS):
            if not sections:
                sections.append({"title": None, "items": []})
            sections[-1]["items"].append(_item(line))
        elif line.endswith(":"):
            sections.append({"title": line[:-1].strip(), "items": []})
        else:
            message.append(line)
    return " ".join(message), [s for s in sections if s["items"]]


def _documents(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        stored = str(row.get("status") or row.get("verification_status") or "").upper()
        if stored == "SUPERSEDED":
            continue                                   # history, not an active document
        state, action = _DOC_STATE.get(stored, (stored or "UNKNOWN", None))
        icon, words = _STATE_ICON.get(stored, ("•", stored.replace("_", " ").capitalize() or None))
        if state == "PROCESSING":
            icon = "○"                                 # still running: never shown as a failure
        reasons = [r for r in (_explained(c) for c in (row.get("reason_codes") or [])) if r]
        out.append({"party": _PARTY.get(str(row.get("party_role") or "").upper(), "Applicant"),
                    "label": _doc_label(row.get("document_type"), row.get("party_role")),
                    "document_type": row.get("document_type"), "icon": icon, "status": words, "state": state,
                    "reason": reasons[0] if reasons and state != "VERIFIED" else None,
                    "action": action, "action_required": action is not None})
    return out


def _groups(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for d in documents:
        groups.setdefault(d["party"], []).append(d)
    return [{"party": party, "documents": docs} for party, docs in groups.items()]


_ACRONYMS = {"cpa", "fos", "kyc", "rcu", "pan", "itr", "dl", "emi"}


def _readable_action(code: Any) -> str:
    """An action code a user can read: SUBMIT_TO_CPA -> 'Submit to CPA' (never the raw enum)."""
    words = str(code or "").replace("_", " ").lower().split()
    out = [w.upper() if w in _ACRONYMS else w for w in words]
    return (out[0][:1].upper() + out[0][1:] + (" " + " ".join(out[1:]) if out[1:] else "")) if out else ""


def _next_actions(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    nxt = envelope.get("next_action")
    if isinstance(nxt, dict) and (nxt.get("label") or nxt.get("message") or nxt.get("action")):
        actions.append({"label": nxt.get("label") or nxt.get("message") or _readable_action(nxt.get("action")),
                        "action": nxt.get("action")})
    for row in envelope.get("available_actions") or []:
        if isinstance(row, dict) and row.get("label") and row.get("label") not in {a["label"] for a in actions}:
            actions.append({"label": row["label"], "action": row.get("action")})
    return actions[:5]


def _knowledge_sources(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    knowledge = envelope.get("knowledge") or {}
    sources = knowledge.get("sources") if isinstance(knowledge, dict) else None
    out = []
    for src in sources or []:
        if isinstance(src, dict):
            out.append({"title": src.get("title") or src.get("section") or src.get("source"),
                        "source": src.get("source") or src.get("document"), "type": src.get("type") or "OKF",
                        "version": src.get("version"), "effective_date": src.get("effective_date")})
        elif src:
            out.append({"title": str(src), "source": str(src), "type": "OKF", "version": None,
                        "effective_date": None})
    return out


def _semantic(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """JEV's typed decisions as advisory items. `acted_upon` is false unless an action EXECUTED."""
    view = envelope.get("semantic_decisions")
    if not isinstance(view, dict):
        return []
    provider = view.get("provider")
    return [{"type": d.get("decision_type"), "decision": d.get("answer"), "severity": d.get("severity"),
             "subject": d.get("target"), "confidence": d.get("confidence"),
             "confidence_band": d.get("confidence_band"), "recommended_action": d.get("recommended_action"),
             "affected_documents": list(d.get("affected_documents") or []),
             "evidence_refs": [r for r in [view.get("evidence_version")] if r],
             "source": "JEV", "provider": provider.get("name") if isinstance(provider, dict) else provider,
             "model": provider.get("model") if isinstance(provider, dict) else None,
             "timestamp": view.get("evaluated_at"), "acted_upon": d.get("status") == "OPEN"}
            for d in view.get("semantic_decisions") or [] if isinstance(d, dict)]


def _status(envelope: dict[str, Any], documents: list[dict[str, Any]]) -> str:
    """One word for the screen header: what most needs the reader's attention."""
    states = {d["state"] for d in documents}
    if "REJECTED" in states or any(i.get("code") == "DOCUMENT_MISSING" for i in envelope.get("pending_items") or []
                                   if isinstance(i, dict)):
        return "ACTION_REQUIRED"
    if "PROCESSING" in states:
        return "PROCESSING"
    kyc = str((envelope.get("kyc") or {}).get("status") or "").upper() if isinstance(envelope.get("kyc"), dict) else ""
    if "REVIEW" in states or kyc in {"REVIEW", "FAIL"}:
        return "REVIEW"
    return "OK" if documents else "INFO"


#: intent -> the response type a renderer picks (brief: STATUS / RESULT / REFUSAL ...)
_RESPONSE_TYPE = {
    "GREETING": "ANSWER", "THANKS": "ANSWER", "ACKNOWLEDGEMENT": "ANSWER",
    "GUARDRAIL_BLOCKED": "REFUSAL", "UNKNOWN": "CLARIFICATION",
    "DOCUMENTS_UPLOADED": "STATUS", "DOCUMENT_VERIFICATION": "STATUS", "APPLICATION_STATUS": "STATUS",
    "KYC_RESULT": "RESULT", "ELIGIBILITY": "RESULT", "DOCUMENT_DETAILS": "RESULT",
    "PENDING_ITEMS": "STATUS", "DOCUMENTS_PENDING": "STATUS", "DOCUMENTS_MISSING": "STATUS",
    "NEXT_ACTION": "ACTION", "READINESS": "STATUS", "CASE_HISTORY": "SUMMARY",
    "FOS_KNOWLEDGE": "ANSWER", "STAGE_PROCESS": "ANSWER",
}
_DETAILED = ("explain properly", "explain in detail", "in detail", "detail mein", "vistar", "sab batao",
             "show everything", "everything", "full details", "poora", "विस्तार", "savistar")
_BRIEF_TYPES = {"STATUS", "RESULT"}


def _response_type(envelope: dict[str, Any], status: str) -> str:
    if envelope.get("clarification_required"):
        return "CLARIFICATION"
    if envelope.get("route_to"):
        return "HANDOFF"
    kind = _RESPONSE_TYPE.get(str(envelope.get("intent") or ""), "ANSWER")
    return "REVIEW" if kind == "STATUS" and status == "REVIEW" else kind


def _depth(envelope: dict[str, Any], kind: str) -> str:
    """BRIEF / NORMAL / DETAILED: how much the user asked for (a presentation hint, never a fact)."""
    asked = str((envelope.get("language_contract") or {}).get("normalized_text") or "").lower() \
        if isinstance(envelope.get("language_contract"), dict) else ""
    if any(w in asked for w in _DETAILED):
        return "DETAILED"
    if asked.startswith(("why", "kyun", "kyu", "ka ", "kasa")):
        return "NORMAL"
    return "BRIEF" if kind in _BRIEF_TYPES else "NORMAL"


#: actions that ask the user to upload -- only offered when something is actually uploadable
_UPLOAD_ACTIONS = {"UPLOAD_DOCUMENT", "MARK_FOR_REUPLOAD", "UPLOAD", "REUPLOAD"}


def _missing_slots(envelope: dict[str, Any]) -> list[str]:
    return [str(i["slot"]) for i in envelope.get("pending_items") or []
            if isinstance(i, dict) and i.get("code") == "DOCUMENT_MISSING" and i.get("slot")]


def _supported(actions: list[dict[str, Any]], envelope: dict[str, Any],
               documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    THE ACTIONS THE CURRENT STATE SUPPORTS. A generic "Upload a document" was
    offered on a case with nothing to upload (2026-10-06): an upload action now
    survives only when a document is missing or must be re-uploaded.
    """
    uploadable = bool(_missing_slots(envelope)) or any(d["state"] == "REJECTED" for d in documents)
    return [a for a in actions if uploadable or str(a.get("action") or "").upper() not in _UPLOAD_ACTIONS]


def _next_action(envelope: dict[str, Any], documents: list[dict[str, Any]],
                 actions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """ONE structured next step the UI can render as a button -- only an action the state supports."""
    from app.agents.applicant.copilot.answering.answer import _readable

    for d in documents:
        if d["state"] == "REJECTED":
            return {"type": "UPLOAD_DOCUMENT", "document_type": d["document_type"],
                    "label": f"Re-upload {d['label']}"}
    missing = _missing_slots(envelope)
    if missing:
        return {"type": "UPLOAD_DOCUMENT", "document_type": missing[0], "label": f"Upload {_readable(missing[0])}"}
    for d in documents:
        if d["state"] == "REVIEW":
            return {"type": "REVIEW_DOCUMENT", "document_type": d["document_type"],
                    "label": f"Review {d['label']}"}
    if actions:
        return {"type": actions[0].get("action") or "ACTION", "document_type": None, "label": actions[0]["label"]}
    return None


def build(envelope: dict[str, Any]) -> dict[str, Any]:
    from app.tts import service as _tts

    message, sections = _sections(str(envelope.get("answer") or ""))
    documents = _documents(envelope.get("documents") or [])
    actions = _supported(_next_actions(envelope), envelope, documents)
    sources = _knowledge_sources(envelope)
    contract = envelope.get("language_contract") if isinstance(envelope.get("language_contract"), dict) else {}
    status = _status(envelope, documents)
    kind = _response_type(envelope, status)
    language = (contract or {}).get("reply_language") or envelope.get("language")
    next_action = _next_action(envelope, documents, actions)
    return {
        "message": message or None,
        "intent": envelope.get("intent"),
        "response_type": kind,
        "response_depth": _depth(envelope, kind),
        "language": language,
        "status": status,
        "next_step": next_action["label"] if next_action else None,
        "next_action": next_action,
        # the SAME text the screen shows, speakable, in the response language's voice
        "audio": _tts.audio_block(str(envelope.get("answer") or ""), language),
        "case_context": {"case_id": envelope.get("case_id"), "applicant_id": envelope.get("applicant_id"),
                         "stage": envelope.get("stage")},
        "sections": sections,
        "documents": documents,
        "document_groups": _groups(documents),
        "actions": actions,
        "next_actions": actions,
        "knowledge_sources": sources,
        "citations": sources,
        "semantic_decisions": _semantic(envelope),
        "metadata": {"request_id": envelope.get("request_id"),
                     "language": (contract or {}).get("reply_language") or envelope.get("language"),
                     "response_source": envelope.get("response_source")},
    }
