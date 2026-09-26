"""
THE EVIDENCE BUILDER -- the one compact packet an answer is composed from.

    tool results (MCP)  ─┐
    stage (resolver)     ├─>  build()  ─>  packet  ─>  Qwen / validator
    case memory          │                  │
    knowledge (RAG)      │                  └─>  answer_basis (provenance)
    JEV notes            ┘

ONE ABSTRACTION, EXTENDING THE ONE THAT EXISTED. `answer._facts_for_model`
already produced the model's view of the tool results -- no identifiers, no
extracted document values. `build` starts from exactly that view and adds
only what the tools do not carry: the authoritative stage, the recorded
problems with their evidence chain, the stage history a "what changed"
question needs, the knowledge passages a mixed question retrieved, and any
JEV notes. Nothing here decides anything; every entry is copied from a
record, a resolver or a retrieval.

WHAT NEVER ENTERS THE PACKET: case, applicant or request ids, tokens, tool
names, raw OCR, stack traces. The one kind of document value that does is
the pair of values in a FAILED identity comparison -- the existing case
memory already publishes those, because "the names differ" cannot be acted
on without the two names.
"""

from __future__ import annotations

from typing import Any

from app.agents.applicant.intents import Intent

#: Problems carried into one packet. More than this is a list, not an answer.
MAX_PROBLEMS = 3


def _stage_block(stage_context: Any) -> dict[str, Any] | None:
    stage = getattr(stage_context, "stage", None)
    if stage is None:
        return None
    from app.agents.applicant import config

    code = getattr(stage, "value", str(stage))
    return {k: v for k, v in {
        "code": code,
        "label": config.stage_label(code),
        "status": getattr(stage_context, "status", None),
    }.items() if v}


def _owner(finding: dict[str, Any],
           documents: Any) -> str | None:
    """
    WHOSE FINDING THIS IS, from the record only: the `party_id` it was
    written with, else the stamped owner of the document it is about.
    Neither -> None, a finding about the case as a whole. Never a guess.
    """
    stamped = str(finding.get("party_id") or "").strip()
    if stamped:
        return stamped
    document_id = finding.get("document_id")
    if document_id:
        for document in documents or ():
            if document.get("document_id") == document_id \
                    and document.get("party_id"):
                return str(document["party_id"])
    return None


def _chain_fields(problem: dict[str, Any], finding: dict[str, Any],
                  owner: str | None, code: str) -> None:
    """
    THE REST OF THE EVIDENCE CHAIN -- scope, source record, when observed,
    and the placeholders Impact and Next Action will fill (Slices 8-9).

    A field the record does not carry is None, never made up: a finding
    written without a document has `document_id: None`, and one read from
    public case memory (no record id) has `record_id: None`.
    """
    from app.agents.applicant.ledger import stable_id

    problem["scope"] = "PARTY" if owner else "CASE"
    problem["source"] = {
        "type": finding.get("source_type") or finding.get("finding_kind"),
        "record_id": finding.get("finding_id"),
        "document_id": finding.get("document_id"),
        "version": finding.get("version"),
    }
    problem["observed_at"] = finding.get("observed_at")
    # STABLE IDENTITY: the same finding read twice is one problem; the same
    # code on two parties, or two documents, is two.
    problem["problem_id"] = stable_id(
        problem.get("finding"), code, owner,
        finding.get("document_id") or finding.get("source_id"))
    problem.setdefault("impact", None)
    problem.setdefault("next_action", None)


def problems(memory: dict[str, Any] | None, *,
             since: str | None = None, parties: Any = (),
             documents: Any = (), limit: int | None = MAX_PROBLEMS,
             ) -> list[dict[str, Any]]:
    """
    The recorded problems, each with its evidence chain:

        type -> message -> [document, field, value] -> finding -> party

    Read from the CURRENT findings only (case_memory already selects
    them). A finding that passed is not a problem. `since` drops what
    an earlier stage recorded (status_facts.during_stage).

    EACH PROBLEM IS ITS PARTY'S. `party_id` / `party_role` are set when the
    record says whose it is (see `_owner`); the same reason code on two
    parties is two problems, never one.
    """
    from app.agents.applicant import case_memory_facts as cm
    from app.agents.applicant import status_facts

    if not memory:
        return []
    roles = {getattr(p, "party_id", None): getattr(getattr(p, "role", None),
                                                   "value", None)
             for p in parties or ()}
    out: list[dict[str, Any]] = []
    for finding in memory.get("findings") or []:
        status = str(finding.get("status") or "").upper()
        if status in {"PASS", "SKIPPED", ""} or not finding.get("reason_codes"):
            continue
        if finding.get("finding_kind") not in cm._EXPLAINING_KINDS:
            continue
        chain = [
            {"source": s.get("document_type"), "field": field.get("field"),
             "value": s.get("value"),
             # The comparison names the document's TYPE, not its record:
             # the id is absent in the source and stays absent here.
             "document_id": s.get("document_id")}
            for field in finding.get("comparisons") or []
            for s in field.get("sources") or []
        ]
        owner = _owner(finding, documents)
        for code in finding["reason_codes"]:
            if any(p["type"] == code and p.get("party_id") == owner
                   for p in out):
                continue
            problem: dict[str, Any] = {"type": code,
                                       "message": cm._readable(code),
                                       "finding": finding.get("finding_kind"),
                                       "status": status}
            if finding.get("document_type"):
                problem["document"] = finding["document_type"]
            if chain:
                problem["evidence"] = chain
            if owner:
                problem["party_id"] = owner
                if roles.get(owner):
                    problem["party_role"] = roles[owner]
            _chain_fields(problem, finding, owner, code)
            out.append(problem)

    # A decision's own reason codes explain a hold no finding recorded.
    decisions = [d for d in memory.get("decisions") or []
                 if status_facts.during_stage(d, since)]
    if decisions:
        latest = decisions[-1]
        for code in latest.get("reason_codes") or []:
            if not any(p["type"] == code for p in out):
                problem = {"type": code, "message": cm._readable(code),
                           "finding": "DECISION",
                           "status": latest.get("decision")}
                _chain_fields(problem, {
                    "source_type": "CASE_DECISION",
                    "finding_id": latest.get("decision_id"),
                    "observed_at": latest.get("recorded_at")}, None, code)
                out.append(problem)
    return out if limit is None else out[:limit]


def history(stage_context: Any) -> list[dict[str, Any]]:
    """The recorded stage history: stage, when, from where, why."""
    rows = []
    for entry in getattr(stage_context, "history", None) or ():
        rows.append({k: v for k, v in {
            "stage": entry.get("stage"), "entered_at": entry.get("started_at"),
            "left_at": entry.get("ended_at"),
            "previous_stage": entry.get("previous_stage"),
            "reason": entry.get("reason")}.items() if v})
    return rows


def build(
    intent: Intent,
    results: dict[str, dict[str, Any]],
    *,
    stage_context: Any = None,
    memory: dict[str, Any] | None = None,
    knowledge: list[dict[str, Any]] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """The packet for this question. Compact: empty parts are left out."""
    from app.agents.applicant.answer import _facts_for_model

    packet = _facts_for_model(intent, results)
    packet["question_intent"] = intent.value

    stage = _stage_block(stage_context)
    if stage:
        packet["stage"] = stage

    found = problems(memory, since=getattr(stage_context, "hold_since", None))
    if found:
        packet["problems"] = found

    if intent in (Intent.APPLICATION_STAGE, Intent.CASE_HISTORY):
        recorded = history(stage_context)
        if len(recorded) > 1:
            packet["stage_history"] = recorded

    if knowledge:
        packet["policy_knowledge"] = [
            {"source": k.get("title") or k.get("source"),
             "text": str(k.get("text") or "")[:600]}
            for k in knowledge[:3]]
    if notes:
        packet["annotations"] = list(notes)

    return {k: v for k, v in packet.items() if v not in (None, [], {}, "")}


def answer_basis(packet: dict[str, Any], *, tools: list[dict[str, Any]],
                 knowledge_sources: list[str] | None = None,
                 semantic_sources: list[str] | None = None,
                 validated: bool, response_source: str) -> dict[str, Any]:
    """
    WHY THIS ANSWER -- structured provenance, safe to publish: which
    governed tools (and over which transport) the case facts came from,
    which knowledge was retrieved, whether a semantic layer contributed,
    and whether the published sentence passed validation.
    """
    return {
        "case_sources": [t["tool"] for t in tools if t.get("ok")],
        "tool_transport": sorted({t.get("transport") or "in_process"
                                  for t in tools}) or [],
        "knowledge_sources": list(knowledge_sources or []),
        "semantic_sources": list(semantic_sources or []),
        "evidence": sorted(k for k in packet if k != "question_intent"),
        "validated": validated,
        "response_source": response_source,
    }


__all__ = ["MAX_PROBLEMS", "answer_basis", "build", "history", "problems"]
