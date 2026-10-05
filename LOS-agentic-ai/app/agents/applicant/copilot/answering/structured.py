"""
THE FRONTEND-READY RESPONSE: what kind of answer this is, whose it is, in
which language, and the structured blocks a UI renders as cards, badges and
buttons -- built from the SAME records the prose was built from.

    response_type   CONVERSATION | REFUSAL | CLARIFICATION | KNOWLEDGE_ANSWER |
                    CASE_FACT | CASE_STATUS | DOCUMENT_CHECKLIST |
                    DOCUMENT_STATUS | DOCUMENT_VERIFICATION_SELECTION |
                    DOCUMENT_VERIFICATION_RESULT | KYC_RESULT | NEXT_ACTION |
                    ROUTED | HANDOFF
    subject         {"party": PRIMARY_APPLICANT | CO_APPLICANT | BOTH | null}
    language        the language the question was asked in
    verification    {"documents": [...], "summary": {...}} -- per document:
                    status, verdict, score (only when RECORDED), reason codes,
                    a readable reason, the next action and the actions a UI
                    may offer (VERIFY_DOCUMENT / UPLOAD_DOCUMENT)
    kyc             {"parties": [...]} -- per person: status, recorded score,
                    checks passed / in review / failed, reasons, next action,
                    and the KYC policy's version and sign-off state

NOTHING HERE DECIDES A FACT. Every value is copied from a record a tool
returned under the caller's authorization; a status is the recorded status,
a score is the recorded score or null (never computed, never guessed), a
reason is the reason code in words. Nothing is read from the store here.
"""

from __future__ import annotations

import contextvars
from typing import Any

#: Structured blocks gathered while ONE answer is built (a request-scoped
#: collector, like field_state.EVIDENCE): kind -> party role -> block.
BLOCKS: contextvars.ContextVar[dict[str, dict[str, Any]] | None] = contextvars.ContextVar(
    "copilot_structured_blocks", default=None)


def put(kind: str, party: str | None, block: dict[str, Any]) -> None:
    gathered = BLOCKS.get()
    if gathered is not None:
        gathered.setdefault(kind, {})[party or "PRIMARY_APPLICANT"] = block


_TYPES = {
    "GREETING": "CONVERSATION", "THANKS": "CONVERSATION", "ACKNOWLEDGEMENT": "CONVERSATION",
    "GOODBYE": "CONVERSATION", "SMALL_TALK": "CONVERSATION", "HELP": "CONVERSATION",
    "FRUSTRATION": "CONVERSATION", "OFF_TOPIC": "CONVERSATION", "CAPABILITIES": "CONVERSATION",
    "CONVERSATION_HISTORY": "CONVERSATION",
    "GUARDRAIL_BLOCKED": "REFUSAL",
    "FOS_KNOWLEDGE": "KNOWLEDGE_ANSWER", "STAGE_PROCESS": "KNOWLEDGE_ANSWER",
    "APPLICANT_PROFILE": "CASE_FACT", "APPLICANT_DETAILS": "CASE_FACT", "ELIGIBILITY": "CASE_FACT",
    "APPLICATION_STATUS": "CASE_STATUS", "APPLICATION_STAGE": "CASE_STATUS", "CASE_HISTORY": "CASE_STATUS",
    "FULL_SUMMARY": "CASE_STATUS", "READINESS": "CASE_STATUS", "CASE_FINDINGS": "CASE_STATUS",
    "DOCUMENTS_REQUIRED": "DOCUMENT_CHECKLIST", "DOCUMENTS_MISSING": "DOCUMENT_CHECKLIST",
    "DOCUMENTS_PENDING": "DOCUMENT_CHECKLIST",
    "DOCUMENTS_UPLOADED": "DOCUMENT_STATUS", "DOCUMENT_VERIFICATION": "DOCUMENT_STATUS",
    "DOCUMENT_DETAILS": "DOCUMENT_STATUS",
    "KYC_RESULT": "KYC_RESULT",
    "NEXT_ACTION": "NEXT_ACTION", "PENDING_ITEMS": "NEXT_ACTION",
    "OUT_OF_SCOPE": "ROUTED", "HUMAN_HANDOFF_REQUESTED": "HANDOFF",
}
_PARTY = {"CO_APPLICANT": "CO_APPLICANT", "CO": "CO_APPLICANT", "BOTH": "BOTH",
          "SELF": "PRIMARY_APPLICANT", "PRIMARY": "PRIMARY_APPLICANT",
          "PRIMARY_APPLICANT": "PRIMARY_APPLICANT"}


def response_type(response: dict[str, Any]) -> str:
    if response.get("response_type"):
        return str(response["response_type"])
    if _TYPES.get(str(response.get("intent") or "")) == "CONVERSATION":
        return "CONVERSATION"         # an off-topic reply carries options, still a chat reply
    if response.get("clarification_required"):
        return "CLARIFICATION"
    if response.get("guardrail") and str(response.get("intent") or "") == "GUARDRAIL_BLOCKED":
        return "REFUSAL"
    return _TYPES.get(str(response.get("intent") or ""), "ANSWER")


# ---- documents ----------------------------------------------------------------
_VERDICT = {"VERIFIED": "PASS", "PASS": "PASS", "REVIEW": "REVIEW", "REJECTED": "FAIL", "FAIL": "FAIL",
            "UPLOADED": "PENDING", "PROCESSING": "PROCESSING", "MISSING": "MISSING"}


def _readable_type(document_type: Any) -> str:
    from app.agents.applicant.copilot.answering.answer import _readable

    if str(document_type or "").upper() in ("", "UNKNOWN"):
        return "unidentified document"
    return _readable(str(document_type or ""))


def _reason(codes: list[str]) -> str | None:
    if not codes:
        return None
    from app.agents.applicant import case_memory_facts
    from app.agents.verification.reasons import POSITIVE_CODES

    # a positive code ("signature present") is not a reason something failed
    codes = [c for c in codes if str(c).upper() not in POSITIVE_CODES] or codes
    said = list(dict.fromkeys(s for s in (case_memory_facts._readable(str(c)) for c in codes) if s))
    # the leading reasons, then how many more -- every code stays in reason_codes
    text = "; ".join(said[:2]) + (f" (+{len(said) - 2} more)" if len(said) > 2 else "")
    return text[:1].upper() + text[1:] if text else None


def next_step(verdict: str, document_type: Any, accepts: list[str] | None = None) -> dict[str, Any] | None:
    """What to do next for ONE document, from its recorded verdict."""
    name = _readable_type(document_type)
    if verdict == "FAIL":
        return {"action": "UPLOAD_DOCUMENT", "document_type": document_type,
                "accepted_types": accepts or [document_type], "label": f"Upload a correct {name}"}
    if verdict == "REVIEW":
        return {"action": "AWAIT_REVIEW", "label": f"A reviewer needs to check the {name}"}
    if verdict == "MISSING":
        return {"action": "UPLOAD_DOCUMENT", "document_type": document_type,
                "accepted_types": accepts or [document_type], "label": f"Upload the {name}"}
    if verdict in ("PENDING", "PROCESSING"):
        return {"action": "AWAIT_VERIFICATION", "label": f"The {name} is being verified"}
    return None


def document_entry(document: dict[str, Any], *, score: Any = None, confidence: Any = None,
                   accepts: list[str] | None = None, source: str = "RECORDED") -> dict[str, Any]:
    """One uploaded document as a card: recorded values only."""
    status = str(document.get("status") or "").upper()
    verdict = _VERDICT.get(str(document.get("verification_status") or "").upper()) \
        or _VERDICT.get(status, "PENDING")
    codes = [str(c) for c in document.get("reason_codes") or []]
    # THE PERSISTED NUMBERS (documents.get carries the latest recorded
    # verification finding's), else what the caller passed; null otherwise
    if score is None:
        score = document.get("verification_score")
    if confidence is None:
        confidence = document.get("verification_confidence")
    entry = {
        "document_id": document.get("document_id"),
        "document_type": document.get("document_type"),
        "label": _readable_type(document.get("document_type")),
        "party_role": document.get("party_role") or "PRIMARY_APPLICANT",
        "status": status or None,
        # the recorded pipeline verdict as stored (kept for existing clients)
        "verification": document.get("verification_status"),
        "verdict": verdict,
        "score": score,
        "confidence": confidence,
        "score_recorded": score is not None,
        "reason_codes": codes,
        "reason_code": codes[0] if codes else None,
        "reason": _reason(codes),
        "next_action": next_step(verdict, document.get("document_type"), accepts),
        "source": source,
    }
    actions = [{"action": "VERIFY_DOCUMENT", "document_id": document.get("document_id"),
                "enabled": verdict in ("PENDING",),
                **({} if verdict == "PENDING" else {"disabled_reason": "Already verified -- result shown."})}]
    if verdict == "FAIL":
        actions.append(entry["next_action"])
    if verdict in ("FAIL", "REVIEW") and document.get("document_id"):
        # THE RAISE QUERY BUTTON, structured -- the same action the chat offers
        # (app/agents/los/queries.raise_action); posting it creates the query
        try:
            from app.agents.los import queries as _queries

            reason = (entry["reason"] or "").rstrip(".")
            actions.append(_queries.raise_action(
                case_id=str(document.get("case_id") or "{case_id}"), stage=None, target_type="DOCUMENT",
                target_id=document.get("document_id"), query_type="VERIFICATION_ISSUE",
                prefill=(f"Please review the {entry['label']}: it "
                         f"{'did not pass verification' if verdict == 'FAIL' else 'needs a reviewer'}"
                         + (f" ({reason.lower()})." if reason else ".")),
                evidence_refs=[{"type": "DOCUMENT", "document_id": document.get("document_id"),
                                "reason_codes": codes}]))
        except Exception:  # noqa: BLE001 - no query config: no button, never a broken card
            pass
    entry["actions"] = actions
    return entry


def missing_entry(row: dict[str, Any]) -> dict[str, Any]:
    """A checklist slot with nothing uploaded: what may be uploaded for it."""
    accepts = [str(a) for a in row.get("accepts") or []]
    step = next_step("MISSING", row.get("slot"), accepts)
    return {"document_id": None, "document_type": row.get("slot"), "label": _readable_type(row.get("slot")),
            "party_role": None, "status": "MISSING", "verdict": "MISSING", "score": None,
            "score_recorded": False, "reason_codes": [], "reason": None,
            "requirement": row.get("requirement") or ("REQUIRED" if row.get("mandatory", True) else "OPTIONAL"),
            "accepted_types": accepts, "next_action": step, "actions": [step], "source": "CHECKLIST"}


def verification_block(documents: list[dict[str, Any]], checklist: list[dict[str, Any]] | None = None,
                       *, scores: dict[str, Any] | None = None, include_missing: bool = False,
                       sources: dict[str, str] | None = None) -> dict[str, Any]:
    scores, sources = scores or {}, sources or {}
    accepts_for = {}
    for row in checklist or []:
        if isinstance(row, dict):
            for a in row.get("accepts") or []:
                accepts_for[str(a).upper()] = [str(x) for x in row.get("accepts") or []]
    entries = [document_entry(d, score=scores.get(str(d.get("document_id"))),
                              accepts=accepts_for.get(str(d.get("document_type") or "").upper()),
                              source=sources.get(str(d.get("document_id")), "RECORDED"))
               for d in documents if isinstance(d, dict)]
    if include_missing:
        entries += [missing_entry(r) for r in checklist or []
                    if isinstance(r, dict) and str(r.get("status") or "").upper() == "MISSING"]
    summary: dict[str, int] = {}
    for e in entries:
        summary[e["verdict"]] = summary.get(e["verdict"], 0) + 1
    return {"documents": entries, "summary": summary,
            "needs_attention": [e["document_type"] for e in entries if e["verdict"] in ("REVIEW", "FAIL")]}


def checklist_row(row: dict[str, Any]) -> dict[str, Any]:
    """
    ONE CONFIGURED REQUIREMENT, renderable as it stands: its category, the
    accepted types, whether it is required, its status and the action it
    allows. ADDITIVE -- every field the policy engine wrote stays as written;
    the requirement semantics (ONE_OF accepts, CONDITIONAL, OPTIONAL) are the
    engine's, copied, never re-derived.
    """
    status = str(row.get("status") or "").upper()
    accepts = [str(a) for a in row.get("accepts") or []]
    required = bool(row.get("mandatory", True))
    row.setdefault("category", row.get("slot"))
    row.setdefault("label", _readable_type(row.get("slot")))
    row.setdefault("accepted_types", accepts)
    row.setdefault("accepts_any_one_of", len(accepts) > 1)
    row.setdefault("required", required)
    actions = []
    if status in ("MISSING", "REJECTED", ""):
        actions.append({"action": "UPLOAD_DOCUMENT", "document_type": row.get("slot"),
                        "accepted_types": accepts, "enabled": True})
    if status in ("UPLOADED", "PROCESSING") and row.get("document_id"):
        actions.append({"action": "VERIFY_DOCUMENT", "document_id": row.get("document_id"), "enabled": True})
    if status in ("VERIFIED", "REVIEW") and row.get("document_id"):
        actions.append({"action": "VIEW_VERIFICATION", "document_id": row.get("document_id"), "enabled": True})
    row.setdefault("actions", actions)
    return row


# ---- KYC --------------------------------------------------------------------------
def _kyc_policy() -> dict[str, Any]:
    try:
        import pathlib
        import yaml

        path = pathlib.Path(__file__).resolve().parents[4] / "config" / "kyc_policies.yaml"
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {"version": str(loaded.get("policy_version") or ""),
                "signed_off": bool(loaded.get("signed_off", False))}
    except Exception:  # noqa: BLE001
        return {}


def kyc_block(latest: dict[str, Any] | None, *, party_role: str | None) -> dict[str, Any]:
    """ONE person's recorded KYC result, as structured data."""
    if not latest:
        return {"party_role": party_role or "PRIMARY_APPLICANT", "status": "NOT_RECORDED", "state": "PENDING",
                "score": None,
                "score_recorded": False, "checks": [], "passed": [], "review": [], "failed": [],
                "reason_codes": [], "reason": None, "next_action": None}
    status = str(latest.get("status") or "").upper()
    checked = latest.get("checked") or []
    comparisons = latest.get("comparisons") or []
    checks = [{"field": str(c.get("field") or "").upper(), "status": str(c.get("status") or "").upper()}
              for c in checked] or [{"field": str(c.get("field") or "").upper(), "status": "FAIL"}
                                    for c in comparisons]
    passed = [c["field"] for c in checks if c["status"] in ("PASS", "MATCH")]
    review = [c["field"] for c in checks if c["status"] in ("REVIEW", "PARTIAL")]
    failed = [c["field"] for c in checks if c["status"] in ("FAIL", "MISMATCH")]
    codes = [str(c) for c in latest.get("reason_codes") or []]
    score = latest.get("score")
    policy = _kyc_policy()
    # THE KYC STATE, read from the agent's recorded status -- never re-decided.
    # SKIPPED splits by WHY: switched off / no policy is a configuration gap;
    # nothing released to compare yet is pending.
    if status in ("PASS", "PARTIAL", "REVIEW", "FAIL"):
        state = status
    elif status == "SKIPPED" and set(codes) & {"KYC_DISABLED", "POLICY_UNAVAILABLE", "KYC_POLICY_UNAVAILABLE"}:
        state = "CONFIGURATION_GAP"
    else:
        state = "PENDING"
    block = {
        "party_role": party_role or "PRIMARY_APPLICANT",
        "status": status or None,
        "state": state,
        "score": score if score not in ("",) else None,
        "score_recorded": score not in (None, ""),
        "checks": checks, "passed": passed, "review": review, "failed": failed,
        "reason_codes": codes, "reason": _reason(codes),
        "next_action": ({"action": "AWAIT_REVIEW", "label": "A reviewer resolves the KYC mismatch"}
                        if status in ("REVIEW",) else
                        {"action": "UPLOAD_DOCUMENT", "label": "Provide consistent identity documents"}
                        if status in ("FAIL",) else None),
        "policy": {"version": policy.get("version"), "signed_off": policy.get("signed_off"),
                   "note": (None if policy.get("signed_off")
                            else "The KYC scoring policy is not yet signed off by the business.")},
    }
    return block


# ---- the whole response -------------------------------------------------------
#: WHAT THE ANSWER IS ABOUT: one case (an application), the applicant across
#: every case they may see, or nothing on record (a chat reply, a handbook
#: answer, a refusal -- a refusal never says what exists).
_APPLICANT_INTENTS = {"APPLICANT_PROFILE", "APPLICANT_DETAILS"}
_NO_RECORD_TYPES = {"CONVERSATION", "KNOWLEDGE_ANSWER", "REFUSAL", "CLARIFICATION", "ROUTED", "HANDOFF"}


def scope_of(response: dict[str, Any]) -> str:
    kind = str(response.get("response_type") or "")
    if kind == "CASE_PORTFOLIO":
        return "APPLICANT_CASES"
    if kind in _NO_RECORD_TYPES:
        return "NONE"
    if str(response.get("intent") or "") in _APPLICANT_INTENTS:
        return "APPLICANT"
    return "CASE"


def enrich(response: dict[str, Any]) -> dict[str, Any]:
    """Attach response_type, subject, language and the structured blocks."""
    understanding = response.get("understanding") if isinstance(response.get("understanding"), dict) else {}
    frame = (understanding or {}).get("frame") or {}
    frame = frame if isinstance(frame, dict) else {}
    response["response_type"] = response_type(response)
    party = _PARTY.get(str(frame.get("party") or "").upper())
    subject = response.get("subject")
    if isinstance(subject, dict):
        # the party-resolution subject ({"kind", "parties"}) gains the same key
        subject.setdefault("party", _PARTY.get(str(subject.get("kind") or "").upper(), party))
        party = subject.get("party")
    # WHOSE ANSWER, for the FOS contract's `subject` (fos_api._from_agent). Kept
    # apart from `subject`, which the Universal Copilot publishes only for a
    # resolved party -- a case-level answer there carries none.
    response["subject_party"] = party
    response.setdefault("language", frame.get("language") or response.get("_presented_language"))
    gathered = BLOCKS.get() or {}
    intent = str(response.get("intent") or "")
    for row in response.get("checklist") or []:
        if isinstance(row, dict):
            checklist_row(row)
    if gathered.get("portfolio"):
        response["portfolio"] = next(iter(gathered["portfolio"].values()))
        response["response_type"] = "CASE_PORTFOLIO"
    if response.get("eligibility") is None and gathered.get("eligibility"):
        response["eligibility"] = next(iter(gathered["eligibility"].values()))
    if response.get("kyc") is None and gathered.get("kyc"):
        response["kyc"] = {"parties": list(gathered["kyc"].values())}
    if response.get("verification") is None and intent in ("DOCUMENT_VERIFICATION", "DOCUMENTS_UPLOADED") \
            and response.get("documents"):
        documents = response["documents"]
        if party in ("PRIMARY_APPLICANT", "CO_APPLICANT"):
            # only the person the answer is about (the prose is scoped the same way)
            documents = [d for d in documents
                         if str(d.get("party_role") or "PRIMARY_APPLICANT") == party] or documents
        response["verification"] = verification_block(documents, response.get("checklist"))
    response["scope"] = scope_of(response)
    return response


__all__ = ["BLOCKS", "document_entry", "enrich", "kyc_block", "missing_entry", "next_step", "put",
           "response_type", "scope_of", "verification_block"]
