"""
The Applicant Agent.

THE ORDER MATTERS, and it is the whole design:

    classify  ->  route out of scope  ->  check capability  ->  check ownership
              ->  plan  ->  call tools  ->  deterministic rules
              ->  phrase (model, validated)  ->  respond  ->  audit

Authorisation happens BEFORE any tool runs, and the business answer is
computed BEFORE the model is consulted. A model that is off, slow or wrong
costs the phrasing of the answer and nothing else -- never its content, never
what was read, never whether it was allowed.

Write intents never execute on the first pass. They come back as a proposed
action the FOS has to confirm, and only the confirmation carries out the work.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import time
import uuid
from typing import Any

from app.agents.applicant import audit, case_memory_facts, config, counters, facts, eligibility_facts, grounding, income_facts, knowledge_answer, permissions, routing, status_facts, history, ledger, actions, delay
from app.agents.applicant.copilot.facts import document_facts
from app.agents.applicant.copilot.routing import subjects
from app.agents.applicant.copilot.answering.answer import (
    NOTHING_AVAILABLE,
    deterministic_answer,
    generate_answer,
)
from app.agents.applicant.copilot.conversation import followup
from app.agents.applicant.query_types import QueryType, clarification_for, type_for
from app.agents.applicant.copilot.semantics.intents import (
    looks_like_knowledge,
    Classification,
    Intent,
    SIMPLE_INTENTS,
    WRITE_INTENTS,
    asks_about_own_case,
    classify,
    plan_for,
    understand,
)
from app.agents.applicant.permissions import Caller, PermissionDenied

#: The intent a request refused by the input guardrail is published under.
#: Not an Intent member: nothing is classified, planned or answered.
GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"

logger = logging.getLogger(__name__)


class AgentError(Exception):
    """A failure that should reach the caller as a clean structured error."""

    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _executed(step: dict[str, Any],
              caller: "permissions.Caller | None") -> bool:
    """
    Whether a traced tool actually ran.

    A tool the caller's scope did not cover was refused BEFORE running and
    is traced as a failure. It is told apart by asking the same check that
    refused it, so the trace itself keeps its shape.
    """
    if step.get("ok") or caller is None:
        return True
    try:
        permissions.check_tool(caller, step["tool"])
    except permissions.PermissionDenied:
        return False
    return True


async def _call_tools(
    plan: tuple[str, ...],
    *,
    applicant_id: str | None,
    case_id: str | None,
    document_type: str | None,
    # WHO IS ASKING, so each capability can be checked against the scope
    # its own contract declares. Optional so an internal caller that has
    # already established authorisation another way is unaffected; every
    # request path supplies it.
    caller: "permissions.Caller | None" = None,
    # Carried on each MCP call's trace; never decides anything.
    request_id: str | None = None,
    stage: str | None = None,
    intent: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    """
    Run the planned tools.

    Returns (results, trace, errors). A tool that fails does not take the
    request down: its failure is recorded and the answer is built from what
    did come back, because a partial answer with a named gap is more useful to
    a FOS than a 500.

    A TOOL THE CALLER MAY NOT USE IS REFUSED, NOT RUN, and refused the
    same way a failure is handled: recorded and skipped. The request has
    already passed the capability check for the KIND of work it is; a
    plan that reaches one capability beyond the caller's scopes should
    cost that capability, not the whole answer.
    """
    from app.mcp import applicant as tools
    from app.mcp import runtime as mcp_runtime

    results: dict[str, dict[str, Any]] = {}
    trace: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    # PROTOCOL MODE: the read tools cross the MCP protocol (app/mcp/runtime),
    # where the server re-authorises the caller. They are independent reads,
    # so they are sent concurrently and collected in plan order.
    if (mcp_runtime.mode() == "protocol" and caller is not None
            and all(c in tools.READ_TOOLS for c in plan)):
        allowed: list[str] = []
        for capability in plan:
            try:
                permissions.check_tool(caller, capability)
                allowed.append(capability)
            except permissions.PermissionDenied as denied:
                errors.append({"code": denied.code, "message": denied.message})
                trace.append({"tool": capability, "ok": False,
                              "processing_ms": 0.0})
        calls = await asyncio.gather(*(
            mcp_runtime.call(capability, applicant_id=applicant_id,
                             case_id=case_id, document_type=document_type,
                             caller=caller, request_id=request_id,
                             stage=stage, intent=intent)
            for capability in allowed))
        for capability, (envelope, call_trace) in zip(allowed, calls):
            trace.append({"tool": capability, "ok": envelope.ok,
                          "processing_ms": envelope.processing_ms,
                          "transport": call_trace["transport"],
                          "provider": call_trace["provider"],
                          "duration_ms": call_trace["duration_ms"],
                          **{k: call_trace[k] for k in
                             ("server_ms", "transport_ms", "timed_out",
                              "fallback") if k in call_trace}})
            if envelope.ok and envelope.result is not None:
                results[capability] = envelope.result
            elif envelope.error is not None:
                errors.append({"code": envelope.error.code,
                               "message": envelope.error.message})
            # THE SERVER REFUSED THE CALLER. Not "no data": the MCP server
            # authenticated and authorised this call itself and said no, and
            # that is published as a refusal, never as an empty answer.
            if not call_trace.get("authorized", True):
                raise AgentError("CASE_NOT_ACCESSIBLE",
                                 "You are not authorized to access this case.",
                                 http_status=403)
        return results, trace, errors

    for capability in plan:
        handler = tools.ALL_TOOLS.get(capability)
        if handler is None:
            errors.append({"code": "UNKNOWN_TOOL",
                           "message": f"No such capability: {capability}."})
            continue

        if caller is not None:
            try:
                permissions.check_tool(caller, capability)
            except permissions.PermissionDenied as denied:
                errors.append({"code": denied.code, "message": denied.message})
                trace.append({"tool": capability, "ok": False,
                              "processing_ms": 0.0})
                continue

        if capability == "applicant.get":
            envelope = await handler(applicant_id)
        elif capability == "applications.list":
            envelope = await handler(applicant_id)
        elif capability == "documents.verification":
            envelope = await handler(case_id, document_type or "")
        else:
            envelope = await handler(case_id)

        trace.append({
            "tool": capability,
            "ok": envelope.ok,
            "processing_ms": envelope.processing_ms,
        })

        if envelope.ok and envelope.result is not None:
            results[capability] = envelope.result
        elif envelope.error is not None:
            errors.append({"code": envelope.error.code,
                           "message": envelope.error.message})

    return results, trace, errors


def _trace_entry(step: dict[str, Any]) -> dict[str, Any]:
    """
    How one tool was reached and what it cost: transport, provider, total
    time and -- over the MCP protocol -- the server's share and the
    transport's. Internal: published only as provenance and timings.
    """
    entry = {"tool": step["tool"], "ok": step.get("ok"),
             "transport": step.get("transport", "in_process"),
             "provider": step.get("provider") or _provider(step["tool"]),
             "duration_ms": step.get("duration_ms", step.get("processing_ms"))}
    for key in ("server_ms", "transport_ms", "timed_out", "fallback"):
        if key in step:
            entry[key] = step[key]
    return entry


def _provider(tool: str) -> str | None:
    from app.mcp.contracts import CONTRACTS

    contract = CONTRACTS.get(tool)
    return contract.provider if contract else None


#: Write intent -> the ONE tool that may carry it out. The proposal names it
#: and the confirmation is refused if the caller sends any other.
_WRITE_TOOL_FOR = {
    Intent.CREATE_APPLICANT: "applicant.create",
    Intent.CREATE_APPLICATION: "application.create",
    Intent.UPDATE_APPLICANT: "applicant.update",
    Intent.MARK_FOR_REUPLOAD: "documents.mark_for_reupload",
}


def _proposed_action(
    classification,
    applicant_id: str | None,
    case_id: str | None,
) -> dict[str, Any]:
    """
    A write, described but not performed.

    The FOS sees exactly what would change and confirms it by action_id. The
    model is not involved in either half: it does not decide that a write is
    wanted, and it cannot carry one out.
    """
    intent = classification.intent
    fields = dict(classification.fields or {})

    summaries = {
        Intent.CREATE_APPLICANT: "Create a new applicant record.",
        Intent.CREATE_APPLICATION: "Create a new application for this applicant.",
        Intent.UPDATE_APPLICANT: (
            "Update " + ", ".join(f"{k} to {v}" for k, v in fields.items())
            if fields else "Update the applicant's details."
        ),
        Intent.MARK_FOR_REUPLOAD: (
            f"Mark {(classification.document_type or 'the document').replace('_', ' ').title()} "
            "for re-upload."
        ),
    }

    tool = _WRITE_TOOL_FOR[intent]

    arguments: dict[str, Any] = dict(fields)
    if intent in (Intent.UPDATE_APPLICANT,):
        arguments["applicant_id"] = applicant_id
    if intent is Intent.CREATE_APPLICATION:
        arguments["applicant_id"] = applicant_id
    if intent is Intent.MARK_FOR_REUPLOAD:
        arguments["case_id"] = case_id
        arguments["document_type"] = classification.document_type

    return {
        "action_id": f"act_{uuid.uuid4().hex[:12]}",
        "type": intent.value,
        "tool": tool,
        "arguments": arguments,
        "requires_confirmation": True,
        "summary": summaries.get(intent, "Perform this change."),
    }


def _case_qualifier(case_id: str | None, party_id: str | None,
                    since: str | None = None) -> str:
    """
    What the CASE concluded, when that differs from the documents.

    Empty when nothing was recorded, and empty when the recorded
    decision is not a review -- a case that is progressing needs no
    caveat, and adding one to every answer would train a reader to
    skip the sentence that matters.
    """
    if not case_id:
        return ""

    try:
        memory = case_memory_facts.case_memory(case_id, party_id)
        # ONLY THIS STAGE'S HOLD. A review decided at FOS is not the hold on
        # a case now at CREDIT; `since` is when the current stage began.
        decisions = [d for d in memory.get("decisions") or []
                     if status_facts.during_stage(d, since)]
        if not decisions:
            return ""
        memory = {**memory, "decisions": decisions}

        latest = decisions[-1]
        decision = str(latest.get("decision") or "").upper()
        if decision not in {"REVIEW", "REJECT"}:
            return ""

        # THE REASON, NOT THE CASE-HISTORY SENTENCE.
        #
        # This used to borrow `explain`, whose first sentence names the
        # processing status as well -- "recorded as PARTIAL with a
        # REVIEW decision" -- and a reader given both took PARTIAL for
        # the decision. PARTIAL is how far processing got; REVIEW is
        # what was decided. Here only the decision and its reason are
        # wanted, because the sentence in front of this one has already
        # said what the documents did.
        clause, _ = case_memory_facts.review_reason(memory)
        if not clause:
            return ""

        verb = ("was declined" if decision == "REJECT"
                else "is under review")
        return f"However, your application {verb} because {clause}."
    except Exception:
        return ""


#: The second question in a mixed message, and what it asks.
_SECOND_HALF = re.compile(
    r"\b(?:and|also|plus)\s+(what|which|how|who|can|could|should|tell\s+me|"
    r"explain)\b|,\s*(?:and\s+)?(what|which|how)\b", re.IGNORECASE)
_WHAT_TO_DO = re.compile(
    r"\b(upload|submit|provide|collect|send|pending|missing|outstanding|"
    r"need|needed|required|do\s+next|should\s+i\s+do|next\s+step|"
    r"what\s+(do|should)\s+i\s+do)\b", re.IGNORECASE)


def _second_half(message: str) -> str:
    match = _SECOND_HALF.search(message or "")
    return message[match.start():] if match else ""


def _asks_what_to_do(text: str) -> bool:
    return bool(text) and bool(_WHAT_TO_DO.search(text))


def _pending_and_next(results: dict[str, Any]) -> str:
    """What this case still needs, from its own checklist and next action."""
    view = results.get("applicant.360") or {}
    pending = status_facts.pending_documents(view.get("checklist"))
    if pending:
        listed = case_memory_facts._and_list(pending)
        one = len(pending) == 1
        return (f"{listed} {'is' if one else 'are'} also pending; please "
                f"upload {'it' if one else 'them'} to continue.")
    detail = (view.get("next_action") or {}).get("detail")
    return f"Next step: {detail}" if detail else ""


def _slot_requirement(document_type: str, results: dict[str, Any]) -> str:
    """Whether ONE named document is required here, and where it stands (the checklist)."""
    from app.agents.applicant.copilot.answering.answer import _readable

    wanted = str(document_type or "").upper()
    checklist = (results.get("documents.checklist") or {}).get("checklist") or []
    for row in checklist:
        if not isinstance(row, dict):
            continue
        accepts = [str(a).upper() for a in row.get("accepts") or []]
        if str(row.get("slot") or "").upper() != wanted and wanted not in accepts:
            continue
        name = _readable(wanted)
        need = "required" if row.get("mandatory", True) else "optional"
        status = str(row.get("status") or "").upper()
        state = {"MISSING": "it is still missing (not uploaded yet)", "VERIFIED": "it is already verified",
                 "REVIEW": "it is under review", "REJECTED": "it was rejected",
                 "UPLOADED": "it has been uploaded"}.get(status, "")
        said = f"{name} is {need} for this application"
        return said + (f", and {state}." if state else ".")
    return ""


def _named_pending(document_type: str, results: dict[str, Any]) -> str:
    """
    Whether ONE named document is pending, from the checklist -- then what
    else is still to collect. Empty when the checklist does not name it.

    "addr proof pending?" was answered "Pending -- not yet collected: Bank
    Statement.": true, and silent on the one document that was asked about.
    """
    from app.agents.applicant.copilot.answering.answer import _readable

    checklist = (results.get("documents.checklist") or {}).get("checklist") or []
    wanted = str(document_type).upper()
    entry = next((e for e in checklist if isinstance(e, dict) and (
        str(e.get("slot") or "").upper() == wanted
        or wanted in {str(a).upper() for a in e.get("accepts") or []})), None)
    if entry is None:
        return ""

    name = _readable(entry.get("slot"))
    status = str(entry.get("status") or "").upper()
    if status == "MISSING":
        said = f"{name} is still pending."
    elif status == "VERIFIED":
        said = f"{name} is not pending: it has been received and verified."
    else:
        said = f"{name} has been received and is {_readable(status).lower()}."

    others = [_readable(e.get("slot")) for e in _missing_required(checklist)
              if e is not entry]
    if others:
        said += f" Still to collect: {case_memory_facts._and_list(others)}."
    return said


#: A document status filter, as the answer says it.
_STATUS_PHRASE = {"VERIFIED": "verified", "REVIEW": "under review",
                  "REJECTED": "rejected"}


def _one_document_upload(document_type: str, results: dict[str, Any]) -> str:
    """Whether ONE named document has come in, and in what state it is."""
    from app.agents.applicant.copilot.answering.answer import _readable

    wanted = str(document_type or "").upper()
    name = _readable(wanted)
    documents = (results.get("documents.get") or {}).get("documents") or []
    states = {"VERIFIED": "verified", "PASS": "verified", "REVIEW": "under review",
              "REJECTED": "rejected", "FAIL": "rejected", "UPLOADED": "uploaded",
              "PROCESSING": "being processed"}
    mine = [d for d in documents if str(d.get("document_type") or "").upper() == wanted]
    if mine:
        parts = []
        for d in mine:
            owner = ("the co-applicant's" if str(d.get("party_role") or "").upper()
                     == "CO_APPLICANT" else "your")
            state = states.get(str(d.get("status") or "").upper(),
                               str(d.get("status") or "recorded").replace("_", " ").lower())
            parts.append(f"{owner} {name} is uploaded and {state}"
                         if state not in ("uploaded", "being processed")
                         else f"{owner} {name} is {state}")
        said = case_memory_facts._and_list(parts) + "."
        return said[0].upper() + said[1:]
    checklist = (results.get("documents.checklist") or {}).get("checklist") or []
    listed = [e for e in checklist if isinstance(e, dict)
              and (str(e.get("slot") or "").upper() == wanted
                   or wanted in [str(a).upper() for a in (e.get("accepts") or [])])]
    if listed:
        status = str(listed[0].get("status") or "").upper()
        if status == "MISSING":
            return f"Your {name} is still missing -- it hasn't been uploaded yet."
        return f"Your {name} is recorded as {status.replace('_', ' ').lower()} on the checklist."
    return f"No {name} has been uploaded on this application."


def _documents_in_status(status: str, results: dict[str, Any]) -> str:
    """The uploaded documents in ONE status, read from documents.get."""
    from app.agents.applicant.copilot.answering.answer import _readable

    documents = (results.get("documents.get") or {}).get("documents") or []
    phrase = _STATUS_PHRASE.get(status, status.lower())
    matching = [d for d in documents
                if str(d.get("status") or "").upper() == status]
    if not documents:
        return "No documents have been uploaded for this case yet."
    if not matching:
        return f"No documents are {phrase}."
    kinds = list(dict.fromkeys(_readable(d.get("document_type")) for d in matching))
    verb = "is" if len(kinds) == 1 else "are"
    return f"{case_memory_facts._and_list(kinds)} {verb} {phrase}."


def _missing_required(checklist: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Required checklist entries with nothing uploaded."""
    return [e for e in checklist if isinstance(e, dict)
            and e.get("mandatory", True)
            and str(e.get("status") or "").upper() == "MISSING"]


def _plain_knowledge(text: str) -> str:
    """A handbook passage fit for a sentence: codes in words, no tables."""
    from app.agents.applicant.copilot.answering.validate import _CODE
    from app.knowledge.grounding import _in_words

    said = _in_words(text or "")
    sentences = re.split(r"(?<=[.!?])\s+|\n+", said)
    kept = [s.strip() for s in sentences
            if s.strip() and "|" not in s and not _CODE.search(s)
            # A LIST HEADING OR MARKER IS NOT A SENTENCE: "Concretely:" and
            # "1." survived as the whole general half of a live answer.
            and not s.strip().endswith(":")
            and not re.fullmatch(r"(\d+|[a-z])[.)]", s.strip(), re.I)
            and len(s.split()) >= 4]
    return " ".join(kept).strip()


def _stage_ctx(stage_context: Any, case_id: str | None) -> Any:
    """The caller's stage context, else the case record's (stages.resolve)."""
    if stage_context is not None or not case_id:
        return stage_context
    try:
        from app.agents.los import stages

        return stages.resolve(case_id)
    except Exception:
        return None


def _stage_after(stage: str | None) -> str | None:
    """
    The stage that follows `stage` in the LOS order (stages.ORDER), or None
    for the last one. READ-ONLY VOCABULARY: the Copilot names the next stage,
    it never reaches the transition service that moves a case.
    """
    from app.agents.los import stages as los_stages

    try:
        current = los_stages.LosStage(str(stage or "").upper())
    except ValueError:
        return None
    order = list(los_stages.ORDER)
    position = order.index(current)
    return order[position + 1].value if position + 1 < len(order) else None


def _stage_of(stage_context: Any, case_id: str | None) -> str | None:
    """The stage name the case record establishes, or None."""
    stage = getattr(_stage_ctx(stage_context, case_id), "stage", None)
    return getattr(stage, "value", stage)


def _portfolio_answer(results: dict[str, Any]) -> str:
    """
    The applicant's applications, counted and listed separately.

    NEVER MERGED. Two cases are two answers; a sentence that blended
    them would report a status no single case holds -- and a reviewer
    acting on "the application is in review" would not know which one.
    The count leads, then each case is named with its own status.

    Deterministic: the rows come from `applications.list`, which is
    scoped to one applicant_id at the tool, and nothing here is
    inferred or summarised by a model.
    """
    payload = (results.get("applications.list") or {})
    applications = payload.get("applications") or []
    total = int(payload.get("count") or len(applications))

    if not total:
        return "No applications are on file for this applicant."

    # NO CASE IDS IN THE SENTENCE. Each case is told apart by position,
    # product and status; the ids stay in the structured `applications`
    # list for any caller that needs to act on one.
    from app.agents.applicant.copilot.answering.answer import _readable

    lines = []
    for number, record in enumerate(applications, start=1):
        status = _readable(str(record.get("status") or "UNKNOWN"))
        product = record.get("product")
        what = (f"{_readable(str(product))} application" if product
                else "application")
        lines.append(f"{number}) {what}, {status}")

    noun = "case" if total == 1 else "cases"
    return f"Across {total} {noun}: " + "; ".join(lines) + "."


async def answer_question(
    *,
    message: str,
    applicant_id: str | None,
    case_id: str | None,
    claims: dict[str, Any],
    request_id: str | None = None,
    # Which party on the case the question is about. Optional: without
    # it a case question answers across the whole case, which is what
    # every existing caller already gets.
    party_id: str | None = None,
    concise: bool = True,
    context: dict[str, Any] | None = None,
    # WHEN THE CALLER ALREADY KNOWS WHAT WAS ASKED.
    #
    # A dropdown action is not a question: the client picked
    # "Document checklist" from a list this service published, so the
    # intent is already decided and classifying an English sentence back
    # into it can only lose. It did, repeatedly -- every routing defect
    # found in review was a pattern-ordering bug in a regex ladder that
    # a named action never needed to enter.
    #
    # EVERYTHING ELSE IS UNCHANGED. Capability check, ownership check,
    # the tool plan, the deterministic answer, the audit record and the
    # response shape are the ones a typed question gets. This skips the
    # inference step and nothing else.
    intent_override: Intent | None = None,
    # WHICH LOS STAGE THE CASE IS IN, as the caller already resolved it
    # (stages.StageContext). Optional: without it the stage is read from
    # the case record when an answer needs it. Never taken from the words
    # of the question.
    stage_context: Any = None,
    # ONE MODEL CALL PER QUESTION. The Universal Copilot phrases the answer
    # itself from the evidence packet; it passes False so the agent does
    # not spend a second model call on the same answer.
    compose_with_model: bool = True,
) -> dict[str, Any]:
    """
    One FOS question, answered.

    Never raises for an ordinary refusal -- an unauthorised, unknown or
    out-of-scope request is a structured response, not an exception. AgentError
    is reserved for conditions the route turns into an HTTP status.
    """
    started = time.perf_counter()
    request_id = request_id or f"aa_{uuid.uuid4().hex}"
    caller = Caller.from_claims(claims)

    def elapsed() -> float:
        return round((time.perf_counter() - started) * 1000, 2)

    _routing_ms: list[float | None] = [None]
    #: HOW THE QUESTION WAS UNDERSTOOD (semantic_frame): published on every
    #: envelope so a caller -- and an eval -- can see the frame, the
    #: referents it resolved and which layer decided, not only the answer.
    _understanding: list[dict[str, Any] | None] = [None]

    def envelope(**overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "request_id": request_id,
            "applicant_id": applicant_id,
            "case_id": case_id,
            "intent": Intent.UNKNOWN.value,
            "answer": "",
            "applicant": None,
            "application": None,
            "stage": None,
            "documents": [],
            "checklist": [],
            # Which policy produced the checklist, and which rules fired.
            # Always alongside it, never instead of it.
            "policy": None,
            "pending_items": [],
            "next_action": None,
            "readiness": None,
            "actions": [],
            # Where a knowledge-grounded answer came from. Absent on a pure
            # case answer, because no knowledge was consulted and saying
            # otherwise would imply a source the facts did not have.
            "knowledge": None,
            # Recorded findings, for a case-history question only.
            "case_memory": None,
            "sources": [],
            # Which of the four kinds of question this was, so a caller can
            # tell "the store answered it" from "the handbook did" without
            # inferring it from which fields happen to be populated.
            "category": routing.QueryCategory.CASE_ONLY.value,
            # WHAT WAS ASKED FOR, as opposed to what was consulted. See
            # app/agents/applicant/query_types.py for why both exist.
            "query_type": QueryType.CASE_FACT.value,
            # -- the frontend contract. Derived, never phrased by a model.
            "case_state": None,
            "suggested_questions": [],
            "available_actions": [],
            "document_highlights": [],
            # Present ONLY when the service declined to guess. A null here
            # is a claim that the request was understood.
            "clarification_required": None,
            # What a bare follow-up was taken to mean, when one was
            # resolved. Reported so a misunderstanding is visible rather
            # than leaving an officer wondering why the answer does not
            # match the question.
            "followed_up": None,
            # The block the caller echoes back on the next question. This
            # service holds no conversation state.
            "context": None,
            "base_intent": None,
            "route_to": None,
            "response_source": routing.ResponseSource.STRUCTURED.value,
            "processing_ms": 0.0,
            "errors": [],
            "understanding": _understanding[0],
        }
        # PER-STEP TIMINGS: routing always, plus whatever the path adds.
        timings = {"routing_ms": _routing_ms[0]} if _routing_ms[0] is not None else {}
        timings.update(overrides.pop("_timings", None) or {})
        if timings:
            base["_timings"] = timings
        base.update(overrides)
        base["processing_ms"] = elapsed()
        return base

    if not config.enabled():
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent="DISABLED", tools=[], status="DISABLED")
        return envelope(
            intent="DISABLED",
            answer="The Applicant Agent is switched off in this deployment.",
            errors=[{"code": "AGENT_DISABLED",
                     "message": "The Applicant Agent is disabled."}],
        )

    # THE INPUT GUARDRAIL, BEFORE ANYTHING RUNS. A request for the system's
    # own code, files, secrets, prompts or tool payloads -- or an attempt to
    # talk it out of its rules -- is refused here: no follow-up is resolved,
    # nothing is classified, no tool runs and no record is read. The refusal
    # is the same whoever asks and whatever case they name, so it discloses
    # nothing about the case either. See app/security/guardrails.py.
    from app.security import guardrails

    # THE IDS THIS REQUEST IS AUTHORISED FOR. Any other case / applicant /
    # party id named in the message is somebody else's (request_policy).
    screened = guardrails.check_input(
        message, allowed_ids=(case_id, applicant_id, party_id))
    if not screened.allowed:
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=GUARDRAIL_BLOCKED, tools=[], status="BLOCKED",
                     message=message, detail=screened.category.value)
        return envelope(
            intent=GUARDRAIL_BLOCKED,
            category=routing.QueryCategory.UNSUPPORTED.value,
            query_type=QueryType.CLARIFICATION.value,
            answer=guardrails.refusal(screened.category),
            guardrail={"stage": "input", "action": "BLOCKED",
                       "category": screened.category.value},
            errors=[{"code": "REQUEST_NOT_ALLOWED",
                     "message": "This request cannot be answered here."}],
        )

    # SMALL TALK AND "WHAT CAN YOU DO", ANSWERED FROM NOTHING. A greeting,
    # thanks, goodbye or request for help -- or a question about what the
    # Copilot can access, or about past conversations -- reads no record,
    # runs no tool, retrieves nothing and calls no model (conversation.py).
    # A request for a person is NOT small talk: it keeps its handoff path.
    from app.agents.applicant import conversation as conversations
    from app.agents.applicant import handoff as _handoffs
    from app.security import request_policy

    turn = None
    if not _handoffs.asks_for_person(message):
        turn = conversations.classify(message)
        if turn is None and request_policy.asks_capability(message) \
                and subjects.mentioned(message) not in (subjects.Kind.CO, subjects.Kind.BOTH):
            turn = conversations.Turn(conversations.CAPABILITIES)
        if turn is None and request_policy.asks_own_history(message):
            turn = conversations.Turn(conversations.HISTORY)
    if turn is not None:
        reply, reply_language = conversations.reply(turn.kind, turn.language)
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=turn.kind, tools=[], status="CONVERSATION")
        return envelope(
            intent=turn.kind,
            category=routing.QueryCategory.CONVERSATION.value,
            query_type=QueryType.CLARIFICATION.value,
            answer=reply,
            response_source=routing.ResponseSource.CONVERSATION.value,
            suggested_questions=list(conversations.SUGGESTIONS),
            _presented_language=reply_language,
        )

    # A GREETING IN FRONT OF A QUESTION is courtesy, not a second clause.
    message = conversations.without_greeting(message)

    # A BARE FOLLOW-UP BECOMES A WHOLE QUESTION FIRST.
    #
    # The rewrite produces a MESSAGE, which is then classified by exactly
    # the same patterns as anything typed by a person. It selects no
    # intent, reaches no tool and skips no check -- see
    # app/agents/applicant/copilot/conversation/followup.py for why that boundary is where it
    # is, given the context comes from the caller.
    routing_started = time.perf_counter()
    from app.observability.tracing import annotate as _annotate
    from app.observability.tracing import span as _span

    with _span("copilot.routing") as _routing_span:
        resolution = followup.resolve(
            message, followup.Context.from_payload(context))
        message = resolution.message

        named_subject = None
        if intent_override is not None:
            # No follow-up resolution either: a named action carries no
            # pronoun to resolve and no previous turn to resolve it against.
            classification = Classification(
                intent_override, confidence="high", matched_on="action")
        else:
            # NORMALISED, CLASSIFIED, AND ONLY THEN THE SEMANTIC FALLBACK --
            # see intents.understand. A WRITE is classified on the words as
            # typed: normalisation rewrites short words, and a name or an
            # address being saved must reach the store exactly as given.
            classification = classify(message)
            if classification.intent not in WRITE_INTENTS:
                # WHO THE QUESTION IS ABOUT, read from its words (a role only --
                # the person is read from the case record below). When a role is
                # named, the question is classified with the subject phrase
                # neutralised, so the ordinary rules decide WHAT is being asked
                # (app/agents/applicant/copilot/routing/subjects.py).
                from app.agents.applicant import normalize

                said = normalize.normalise(message).text or message
                named_subject = (subjects.mentioned(said)
                                 or subjects.mentioned(message))
                # NEUTRALISED AS TYPED: normalisation's typo pass reads
                # "co-applicant's" as "co-applicants" and loses the possessive
                # that says a document noun follows. `understand` normalises
                # the neutral text itself.
                classification = understand(
                    subjects.neutral(message)
                    if named_subject in (subjects.Kind.CO, subjects.Kind.BOTH) else message,
                    has_case=bool(case_id))
                if named_subject in (subjects.Kind.CO, subjects.Kind.BOTH):
                    # A PERSON'S DETAILS OR KYC, asked of the co-applicant: the
                    # party's own record answers it (subjects.party_question).
                    asked_of_party = subjects.party_question(said, classification)
                    if asked_of_party is not None:
                        classification = asked_of_party
                if named_subject:
                    message = said
                    # THE FRAME KEEPS THE PARTY the neutral text dropped, so a
                    # clarification asks about the co-applicant, not "my documents".
                    from app.agents.applicant.copilot.semantics import semantic_frame as _frames

                    frame_ = getattr(classification, "frame", None)
                    if frame_ is not None and named_subject in (subjects.Kind.CO,
                                                                subjects.Kind.BOTH):
                        frame_.party = _frames.Party.CO_APPLICANT
                elif classification.normalized:
                    message = classification.normalized

        _annotate(_routing_span, intent=classification.intent.value,
                  matched_on=(classification.matched_on or "")[:40],
                  confidence=classification.confidence,
                  normalized=bool(classification.normalized),
                  followed_up=bool(getattr(resolution, "rewritten", False)))
    intent = classification.intent
    _routing_ms[0] = round((time.perf_counter() - routing_started) * 1000, 2)

    # ------------------------------------------------------------------
    # UNDERSTANDING, BEYOND THE RULES (semantic_frame.py).
    #
    # 1. QWEN AS A BOUNDED FALLBACK FOR MEANING. Only when nothing above
    #    understood the question, and it is not a bare follow-up: ONE call
    #    proposes a frame in the closed enums, validated like any other.
    #    It chooses no tool and states no fact; a timeout or an invalid
    #    answer leaves the question UNKNOWN. Never called when the parser
    #    or the rules were confident.
    # 2. REFERENTS. "this stage" is the case's recorded stage; "that
    #    document" the document the previous answer was about. One that
    #    cannot be resolved safely is asked back, by name.
    # ------------------------------------------------------------------
    from app.agents.applicant.copilot.semantics import semantic_frame
    from app.agents.applicant.copilot.semantics import short_query as short_queries

    understanding_trace: dict[str, Any] = {"llm": {"consulted": False, "status": "NOT_NEEDED"}}
    frame = getattr(classification, "frame", None)

    # A ONE-WORD QUESTION IS NOT A GUESSING GAME. "name", "status",
    # "documents", "PAN" each mean several things. The previous turn
    # settles it when it can (then the settled question is understood
    # exactly as if typed); otherwise the reply names the choices, and no
    # tool, retrieval or model runs for it (short_query.py).
    short = None
    if intent_override is None:
        short = short_queries.short_query(message, followup.Context.from_payload(context))
        if short is None and intent is Intent.UNKNOWN:
            # Nothing understood it, and its only content word is ambiguous.
            short = short_queries.short_query(
                message, followup.Context.from_payload(context), loose=True)
    if short is not None and short.resolved_to:
        message = short.resolved_to
        classification = understand(message, has_case=bool(case_id))
        intent = classification.intent
        frame = getattr(classification, "frame", None)
        understanding_trace["short_query"] = {"head": short.head, "resolved_to": message,
                                              "resolved_by": short.resolved_by}
    elif short is not None and short.clarification and not (
            intent is not Intent.UNKNOWN and len(short_queries._tokens(message)) > 1):
        # A BARE WORD is asked back; a word with a possessive or a conjunction
        # ("my number?", "and stage?") that the rules already read is answered.
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=Intent.UNKNOWN.value, tools=[], status="CLARIFICATION",
                     message=message)
        _understanding[0] = {"frame": None, "decided_by": "SHORT_QUERY",
                             "referents": {}, "short_query": {"head": short.head},
                             "llm": understanding_trace["llm"], "parse_ms": 0.0,
                             "case_stage": None}
        return envelope(
            intent=Intent.UNKNOWN.value,
            category=routing.QueryCategory.UNSUPPORTED.value,
            query_type=QueryType.CLARIFICATION.value,
            clarification_required=short.clarification,
            suggested_questions=list(short.clarification["options"]),
            answer=short.clarification["question"],
        )
    if (intent is Intent.UNKNOWN and intent_override is None
            and not followup.is_bare(message)
            and resolution.reason != followup.EXPLAIN_REASON):
        proposed, llm_trace = await semantic_frame.llm_frame(message)
        understanding_trace["llm"] = llm_trace
        if proposed is not None:
            routed = semantic_frame.route(proposed)
            if routed and Intent(routed) not in WRITE_INTENTS:
                classification = Classification(
                    Intent(routed), confidence="medium",
                    document_type=proposed.document_type or classification.document_type,
                    matched_on=f"llm_frame:{proposed.task.value}/{proposed.object.value}",
                    frame=proposed, understanding="LLM")
                intent = classification.intent
                frame = proposed
            else:
                frame = proposed

    # A follow-up whose pronoun the previous turn cannot settle ("why is it
    # still pending?" after two pending documents) is asked back, not guessed.
    asked_back = followup.pronoun_clarification(message, followup.Context.from_payload(context))
    referent_clarification = asked_back[0] if asked_back else None
    referent_options = list(asked_back[1]) if asked_back else []
    if frame is not None:
        # BEFORE OWNERSHIP IS CHECKED nothing is read from the store: only a
        # stage the route already resolved is used here; "this stage" is
        # bound to the case record after the ownership check below.
        resolved = semantic_frame.resolve_referents(
            frame, followup.Context.from_payload(context),
            case_stage=getattr(getattr(stage_context, "stage", None), "value", None))
        understanding_trace["referents"] = resolved.resolutions
        referent_clarification = resolved.clarification or referent_clarification
        if frame.document_type and not classification.document_type:
            classification.document_type = frame.document_type
    _understanding[0] = {
        "frame": frame.public() if frame is not None else None,
        "decided_by": getattr(classification, "understanding", None),
        "referents": understanding_trace.get("referents") or {},
        "short_query": understanding_trace.get("short_query"),
        "llm": understanding_trace["llm"],
        "parse_ms": getattr(frame, "parse_ms", 0.0) if frame is not None else 0.0,
        # THE CASE'S RECORDED LOS STAGE -- what "this stage" means here.
        # Filled in after the ownership check (below); a refusal never reads it.
        "case_stage": getattr(getattr(stage_context, "stage", None), "value", None),
    }

    if referent_clarification and intent is not Intent.UNKNOWN:
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=intent.value, tools=[], status="CLARIFICATION",
                     message=message)
        return envelope(
            intent=intent.value,
            category=routing.QueryCategory.UNSUPPORTED.value,
            query_type=QueryType.CLARIFICATION.value,
            clarification_required={"reason": "REFERENT_UNRESOLVED",
                                    "question": referent_clarification,
                                    "options": referent_options,
                                    "original_message": message[:200]},
            followed_up=resolution.public(),
            answer=referent_clarification,
        )

    # "WHAT IS THIS BASED ON?" WITH NOTHING TO EXPLAIN: there was no previous
    # answer in this conversation, so the honest reply says so (read nothing).
    if intent is Intent.UNKNOWN and (
            resolution.reason == followup.EXPLAIN_REASON
            or followup._WHY_ANSWER.match(message or "")):
        from app.agents.applicant import provenance as _chain

        return envelope(
            intent="ANSWER_BASIS",
            category=routing.QueryCategory.CONVERSATION.value,
            query_type=QueryType.CLARIFICATION.value,
            answer=_chain.explain(None),
            response_source=routing.ResponseSource.CONVERSATION.value,
            followed_up=resolution.public())

    # ---- out of scope, before anything is read -------------------------
    if intent is Intent.OUT_OF_SCOPE:
        route = config.routing_table().get(classification.route_to or "", {})
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=intent.value, tools=[], status="ROUTED",
                     message=message, detail=classification.route_to)
        routed = route.get("message", "That question is handled by a downstream process.")
        # "IS MY LOAN APPROVED YET?" -- the decision is downstream, but where
        # THIS application stands is recorded: said first, from the case
        # record, only after the caller's ownership is proven.
        if (case_id and route.get("route_to") == "DECISION_AGENT"
                and re.search(r"\b(my|mera|meri|mere|our|apna|apni)\b|मेरा|मेरी|माझ", message, re.I)):
            try:
                permissions.check_ownership(applicant_id or "", case_id, caller=caller)
                standing, _src = case_memory_facts.explain(case_memory_facts.case_memory(case_id))
                if standing:
                    routed = f"Not yet. {standing} {routed}"
            except Exception:  # noqa: BLE001 - not provable: the routed answer alone
                pass
        return envelope(
            intent=intent.value,
            category=routing.QueryCategory.DOWNSTREAM.value,
            query_type=QueryType.DOWNSTREAM.value,
            followed_up=resolution.public(),
            response_source=routing.ResponseSource.ROUTED.value,
            route_to=route.get("route_to", classification.route_to),
            answer=routed,
        )

    # ---- a question about the rules, not about this case ---------------
    #
    # Answered from the FOS knowledge base. No tool runs and no record is
    # read: this path cannot reach case data, which is what stops a policy
    # answer from ever appearing to be a statement about an applicant.
    if intent is Intent.FOS_KNOWLEDGE:
        # ONE MODEL CALL PER QUESTION: when the caller composes (the
        # Universal Copilot), the passage is not phrased here first.
        text, source, detail = await _knowledge_reply(
            message, allow_model=compose_with_model)
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=intent.value, tools=["knowledge.fos"],
                     status="OK" if detail["confident"] else "NO_KNOWLEDGE",
                     message=message)
        return envelope(intent=intent.value, answer=text,
                        category=routing.QueryCategory.KNOWLEDGE_ONLY.value,
                        query_type=QueryType.PROCESS_KNOWLEDGE.value,
                        followed_up=resolution.public(),
                        response_source=source,
                        knowledge=_public_knowledge(detail))

    from app.agents.applicant import handoff as handoffs

    # ANY LANGUAGE the handoff layer recognises ("talk to a human", "customer
    # care", "kisi insaan se baat karni hai"), checked on the canonical words.
    asks_person_only = handoffs.asks_for_person(message) and len(message.split()) <= 6
    if (intent is Intent.UNKNOWN or asks_person_only) and handoffs.asks_for_person(message):
        # AN EXPLICIT REQUEST FOR A PERSON. Reported as a handoff signal a
        # channel can act on -- no human desk exists in this service, so the
        # answer does not claim anyone has been contacted. No case data.
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent="HUMAN_HANDOFF_REQUESTED", tools=[],
                     status="HANDOFF_SIGNAL", message=message)
        return envelope(
            intent="HUMAN_HANDOFF_REQUESTED",
            category=routing.QueryCategory.UNSUPPORTED.value,
            query_type=QueryType.CLARIFICATION.value,
            followed_up=resolution.public(),
            answer=("I've marked that you'd like help from a person. Until "
                    "then, I can answer questions about this application's "
                    "documents, status and next steps."),
            next_actions={"primary": None, "additional": [],
                          "handoff": {"required": True,
                                      "reason": "USER_REQUEST",
                                      "priority": None}},
        )

    if intent is Intent.UNKNOWN:
        # Not understood as a case question -- but the knowledge base gets a
        # chance before the agent gives up, and its own confidence threshold
        # decides. A phrase list can always be out of date; retrieval scoring
        # degrades gracefully instead of failing at an exact wording.
        #
        # EXCEPT FOR AN UNRESOLVED FOLLOW-UP. "why?" is not a question about
        # the FOS handbook, but retrieval scored it confident and answered
        # it with a paragraph about document states. A message that only
        # means something in context, and whose context did not resolve,
        # goes straight to the clarification.
        #
        # NOR A QUESTION ABOUT THIS CASE. "Where does my application
        # stand" asked against a case is about that case; the handbook
        # has nothing to say about it, and retrieval scoring it
        # confident published a paragraph of policy as FOS_KNOWLEDGE,
        # with no tool run, in place of the case's recorded status. A
        # case question this service did not understand gets the
        # clarification, never policy text.
        bare = followup.is_bare(message)
        own_case = bool(case_id) and asks_about_own_case(message)
        # RAG ONLY FOR A GENUINE KNOWLEDGE QUESTION. Retrieval used to run on
        # every unrecognised message and, when it scored a passage
        # confident, published policy text in place of understanding. It
        # runs now only when the frame says the question is about the
        # rules (a definition, a general or product question) -- never as
        # a stand-in for a case question the service did not understand.
        knowledge_like = (frame is not None and semantic_frame.is_knowledge(frame)) \
            or (frame is None and looks_like_knowledge(message))
        text, source, detail = (
            ("", "", {"confident": False}) if bare or own_case or not knowledge_like
            else await _knowledge_reply(message, allow_model=compose_with_model)
        )
        if detail["confident"]:
            audit.record(request_id=request_id, subject=caller.subject,
                         applicant_id=applicant_id, case_id=case_id,
                         intent=Intent.FOS_KNOWLEDGE.value,
                         tools=["knowledge.fos"], status="OK", message=message)
            return envelope(intent=Intent.FOS_KNOWLEDGE.value, answer=text,
                            category=routing.QueryCategory.KNOWLEDGE_ONLY.value,
                            query_type=QueryType.PROCESS_KNOWLEDGE.value,
                            followed_up=resolution.public(),
                            response_source=source,
                            knowledge=_public_knowledge(detail))

        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=intent.value, tools=[], status="UNSUPPORTED",
                     message=message)
        # A QUESTION BACK, NOT A DEAD END. The request was not understood
        # and the service will not guess -- but "could you rephrase?" puts
        # the work back on the officer with no help. The clarification
        # carries what this desk can answer, so the next click succeeds.
        #
        # The error stays. A caller distinguishing "answered" from "not
        # answered" reads `errors`, and dropping it to make the response
        # look friendlier would make an unanswered question look answered.
        # A TARGETED QUESTION FIRST: what the frame did understand names
        # the missing piece. The generic offer only when nothing at all
        # was understood.
        clarification = (
            (semantic_frame.clarification(frame, has_case=bool(case_id)) or {})
            if frame is not None else {})
        if referent_clarification:
            clarification = {"reason": "REFERENT_UNRESOLVED",
                             "question": referent_clarification, "options": []}
        if clarification:
            clarification["original_message"] = (message or "").strip()[:200]
        else:
            clarification = clarification_for(message, has_case=bool(case_id))
        return envelope(
            intent=intent.value,
            category=routing.QueryCategory.UNSUPPORTED.value,
            query_type=QueryType.CLARIFICATION.value,
            clarification_required=clarification,
            followed_up=resolution.public(),
            suggested_questions=list(clarification["options"]),
            answer=clarification["question"],
            errors=[{"code": "UNSUPPORTED_REQUEST",
                     "message": "The request was not understood."}],
        )

    # ---- authorisation, before any tool runs ---------------------------
    try:
        permissions.check_capability(caller, intent)
        if intent is not Intent.CREATE_APPLICANT:
            # THE CALLER, not just the pair of ids it sent: see
            # app/security/access.py for the ownership model.
            permissions.check_ownership(applicant_id or "", case_id,
                                        caller=caller,
                                        write=intent in WRITE_INTENTS)
            # THE CUSTOMER-FACING ACCESS POLICY, for every conversation surface
            # (Universal Copilot and the FOS copilot route alike).
            from app.security import access as _access

            try:
                _access.authorize_conversation(caller.subject, caller.scopes,
                                               applicant_id=applicant_id,
                                               case_id=case_id)
            except _access.AccessDenied:
                raise PermissionDenied("CASE_NOT_ACCESSIBLE",
                                       "Not the caller's case.") from None
        # OWNERSHIP PASSED: "this stage" now means the case's recorded stage.
        # RESOLVED ONCE: the case record is read here and every later step
        # (tools, the stage answer, the next-action intelligence) reuses it
        # instead of resolving the stage again -- the same context the
        # Universal Copilot route hands in.
        stage_context = _stage_ctx(stage_context, case_id)
        if case_id and _understanding[0] is not None:
            recorded = _stage_of(stage_context, case_id)
            _understanding[0]["case_stage"] = recorded
            if frame is not None and recorded and frame.referents.get("stage") == "CURRENT":
                frame.stage = str(recorded).upper()
                _understanding[0]["frame"] = frame.public()
                _understanding[0]["referents"]["stage"] = f"CURRENT -> {frame.stage} (case record)"
            elif frame is not None and recorded and frame.referents.get("stage") == "NEXT":
                # "after this step": the stage the configured lifecycle names
                # after the case's recorded stage (never advanced, only named).
                frame.stage = _stage_after(recorded)
                _understanding[0]["frame"] = frame.public()
                _understanding[0]["referents"]["stage"] = (
                    f"NEXT -> {frame.stage or 'none (final stage)'} (lifecycle after {recorded})")
    except PermissionDenied as exc:
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=intent.value, tools=[], status="DENIED",
                     message=message, detail=exc.code)
        raise AgentError(exc.code, exc.message, http_status=403) from exc

    # ---- which party the question is about -----------------------------
    #
    # AFTER OWNERSHIP, and from the RECORD. The caller has just been cleared
    # for this case; the parties are read from its application, never from
    # the question or the conversation. A `party_id` the case does not have
    # is refused exactly as a case the caller does not hold is: whether it
    # belongs to another case is not something this caller may learn.
    parties_on_case = subjects.parties_of(case_id) if case_id else []
    if party_id and case_id and not subjects.belongs(case_id, party_id):
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=intent.value, tools=[], status="DENIED",
                     message=message, detail="PARTY_NOT_ON_CASE")
        raise AgentError("CASE_NOT_ACCESSIBLE",
                         "You are not authorized to access this case.",
                         http_status=403)

    subject = subjects.resolve(named_subject, parties_on_case)
    if intent is Intent.APPLICANT_PROFILE and named_subject is subjects.Kind.PRIMARY:
        subject = None      # the profile IS the primary applicant's own record
    # A DOCUMENT QUESTION ON A TWO-PARTY CASE IS ANSWERED FOR EACH PARTY.
    # "Is the PAN verified?" was answered from the latest PAN on the case,
    # whoever it belonged to; with two people, that is one person's
    # document reported as the other's.
    if (subject is None and not party_id and len(parties_on_case) > 1
            and intent is Intent.DOCUMENT_VERIFICATION):
        # "is MY PAN verified?" / "mera PAN" is the primary applicant's own:
        # only a question naming no one is answered for each party.
        own = re.search(r"\b(my|mine|mera|meri|mere|mujhe|apna|apni|apne)\b|मेरा|मेरी|मेरे|माझा|माझी|माझे",
                        message, re.IGNORECASE)
        subject = subjects.resolve(subjects.Kind.PRIMARY if own else subjects.Kind.BOTH,
                                   parties_on_case)

    if subject is not None and case_id:
        answered = await _answer_for_subject(
            subject, parties_on_case, classification, message,
            applicant_id=applicant_id, case_id=case_id, caller=caller,
            request_id=request_id, stage=_stage_of(stage_context, case_id))
        if answered is None:
            # ONE DOCUMENT'S RECORDED VALUES: the existing party-scoped path
            # answers it, for the one party named.
            if len(subject.parties) == 1:
                party_id = subject.parties[0].party_id
        else:
            answer, results, trace, errors, memory = answered
            audit.record(request_id=request_id, subject=caller.subject,
                         applicant_id=applicant_id, case_id=case_id,
                         intent=intent.value,
                         tools=[t["tool"] for t in trace], status="OK",
                         message=message, detail=subject.kind.value)
            return envelope(
                intent=intent.value,
                query_type=type_for(intent).value,
                followed_up=resolution.public(),
                answer=answer,
                subject=subject.public(),
                documents=(results.get("documents.get") or {}).get(
                    "documents") or [],
                **({"case_memory": memory} if memory else {}),
                **({"history": {"per_party": [
                    {k: v for k, v in b.items() if k != "_events"}
                    for b in results["_history"]["per_party"]]},
                    "_history_events": [e for b in results["_history"]["per_party"]
                                        for e in b["_events"]]}
                   if results.get("_history") else {}),
                tools_invoked=[step["tool"] for step in trace
                               if _executed(step, caller)],
                tool_trace=[_trace_entry(step) for step in trace],
                # PER-PARTY ANSWERS ARE REPORTED, NEVER REPHRASED: a composer
                # asked for two sentences is exactly how two people become
                # "the applicant".
                answer_is_quoted=True,
                errors=errors,
            )

    # ---- how a named stage works ---------------------------------------
    #
    # RETURNS BEFORE THE READ PATH, and must. No MCP tool answers "what
    # does RCU check", so `plan_for` returns nothing for it -- and the
    # read path treats an empty plan as an empty RESULT, replying "No
    # data is available for this request." with the default CASE_ONLY
    # category. A process question is not a case question that failed.
    #
    # AFTER THE OWNERSHIP CHECK, THOUGH IT READS NO CASE. A stage
    # guide describes a desk and nothing on this path can reach an
    # applicant record -- but the request still names a case, and a
    # caller who does not hold that case is refused before anything
    # answers them. Ownership is a property of the request, not a
    # question about which branch happens to need the data.
    #
    # THE ANSWER IS LEFT EMPTY ON PURPOSE. The text comes from the
    # indexed stage guide, retrieved and phrased by the caller that has
    # a vector store -- this module has no retrieval of its own, and a
    # deterministic sentence invented here would be a second answer
    # competing with the real one.
    if intent is Intent.STAGE_PROCESS:
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=intent.value, tools=[], status="OK",
                     message=message)
        # "WHAT HAPPENS AFTER THIS STEP?" names the next stage from the
        # configured lifecycle -- a recorded fact, stated; the stage guide
        # (retrieved by the caller) may describe it. Nothing is invented.
        next_stage_answer = ""
        if frame is not None and frame.referents.get("stage") == "NEXT" and case_id:
            current = _stage_of(stage_context, case_id)
            if current and frame.stage:
                next_stage_answer = (
                    f"Your application is at the {config.stage_label(current)} stage. "
                    f"The next stage in the process is {config.stage_label(frame.stage)}.")
            elif current:
                next_stage_answer = (
                    f"Your application is at the {config.stage_label(current)} stage, "
                    f"which is the final stage; there is no stage after it.")
        return envelope(
            intent=intent.value,
            answer=next_stage_answer,
            category=routing.QueryCategory.PROCESS_KNOWLEDGE.value,
            query_type=QueryType.PROCESS_KNOWLEDGE.value,
            followed_up=resolution.public(),
            response_source=routing.ResponseSource.STRUCTURED.value,
        )

    # ---- writes are proposed, never performed here ---------------------
    if intent in WRITE_INTENTS:
        action = _proposed_action(classification, applicant_id, case_id)
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=intent.value, tools=[], write=True,
                     confirmed=False, status="PROPOSED", message=message)
        return envelope(
            intent=intent.value,
            query_type=QueryType.ACTION_REQUEST.value,
            followed_up=resolution.public(),
            answer=f"{action['summary'].rstrip('.')}. Please confirm and I will apply it.",
            actions=[action],
        )

    # ---- read path -----------------------------------------------------
    plan = plan_for(classification, has_case=bool(case_id))
    # "WHAT IS BLOCKING ME?" is answered from the pending items AND the next
    # action (Slices 8-9), so both are read -- once, through the same tools.
    asks_blocking = bool(case_id) and actions.asks_what_blocks(message) and         intent in (Intent.PENDING_ITEMS, Intent.READINESS, Intent.COMPLETENESS,
                   Intent.NEXT_ACTION)
    # "WHY IS IT DELAYED?" reads the same: findings, impacts, the next action.
    asks_delay = bool(case_id) and intent is Intent.CASE_HISTORY and         delay.asks_about_delay(message)
    if asks_blocking or asks_delay:
        plan = tuple(dict.fromkeys(
            plan + ("workflow.pending_items", "workflow.next_action")))
    results, trace, errors = await _call_tools(
        plan,
        applicant_id=applicant_id,
        case_id=case_id,
        document_type=classification.document_type,
        # Each capability is checked against the scope its own contract
        # declares, in addition to the capability check this request has
        # already passed.
        caller=caller,
        request_id=request_id,
        stage=_stage_of(stage_context, case_id),
        intent=intent.value,
    )

    # an identity number or a KYC result is read from the case's findings:
    # no tool result is not "no data"
    identity_only = (intent in (Intent.APPLICANT_PROFILE, Intent.KYC_RESULT)
                     and not plan and not errors)
    if not results and not identity_only:
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=intent.value, tools=[t["tool"] for t in trace],
                     status="NO_DATA", message=message)
        return envelope(
            intent=intent.value,
            answer=(errors[0]["message"] if errors
                    else "No data is available for this request."),
            errors=errors or [{"code": "NO_DATA",
                               "message": "No data was returned."}],
        )

    # ---- phrasing ------------------------------------------------------
    #
    # A MIXED question is answered from the case FIRST and always
    # deterministically. The facts half of "why is this not ready and what
    # should I collect?" is the same computation as the question asked alone,
    # and it must not become a model's paraphrase because a second clause was
    # attached to it.
    answering_intent = (classification.base_intent or Intent.FULL_SUMMARY
                        if intent is Intent.MIXED else intent)

    # CASE HISTORY IS ANSWERED FROM WHAT WAS RECORDED, AND ONLY THAT.
    #
    # Reached here and not earlier because ownership has now been checked
    # -- case memory must never be read for a case the caller has not
    # been cleared for, and the check above is the one that clears it.
    #
    # No model on this path at any setting. The answer is a list of
    # reason codes the pipeline wrote down; a paraphrase can drop one,
    # and a dropped finding reads as a finding that did not happen.
    case_memory_block = None
    case_sources: list[dict[str, Any]] = []

    # WHAT THE CASE ITSELF RECORDED, read once before anything phrases
    # an answer, because its presence decides who does the phrasing.
    recorded = (_case_qualifier(
                    case_id, party_id,
                    since=getattr(_stage_ctx(stage_context, case_id),
                                  "hold_since", None))
                if intent in (Intent.DOCUMENT_VERIFICATION,
                              Intent.APPLICATION_STATUS)
                else "")
    # WHETHER THE ANSWER QUOTES A RECORDED VALUE -- a recorded reason, a
    # hold, the names in a mismatch. Such an answer is reported, never
    # rephrased, by the agent or by any composer downstream of it.
    quoted = bool(recorded)
    # "What changed": the structured changes behind the answer, when asked.
    history_block = None
    history_events = None

    intelligence = None
    delay_block = None
    if case_id and (asks_blocking or asks_delay
                    or intent is Intent.NEXT_ACTION):
        # FACT -> EVIDENCE -> IMPACT -> NEXT BEST ACTION, all deterministic,
        # all from the case's records; reported, never rephrased.
        started_nba = time.perf_counter()
        intelligence = _intelligence(case_id, results, stage_context, message)
        nba_ms = round((time.perf_counter() - started_nba) * 1000, 2)

    if intelligence is not None:
        book, found, case_impacts, nba = intelligence
        multi = len(book.parties) > 1
        if asks_delay:
            ctx = _stage_ctx(stage_context, case_id)
            delay_block = delay.explain(
                stage=_stage_of(stage_context, case_id),
                since=getattr(ctx, "since", None), case_impacts=case_impacts,
                nba=nba, events=book.events())
            held = [i for i in case_impacts
                    if i.get("effect") in delay.HOLDING_EFFECTS
                    or i.get("blocking") is True]
            answer = delay.answer(delay_block, held, nba, multi_party=multi)
        elif asks_blocking:
            from app.agents.applicant import config as _config

            stage_code = _stage_of(stage_context, case_id)
            answer = actions.blocking_answer(
                case_impacts, nba, multi_party=multi,
                stage_label=_config.stage_label(stage_code or "FOS"))
        else:
            from_workflow = nba["primary"]["source_rule"] == "workflow.next_action"
            answer = actions.answer(
                nba, multi_party=multi,
                primary_sentence=(deterministic_answer(Intent.NEXT_ACTION, results)
                                  if from_workflow else None))
        source, llm_ms = "deterministic", 0.0
        quoted = True

    elif intent is Intent.CASE_PORTFOLIO:
        answer = _portfolio_answer(results)
        source, llm_ms = "deterministic", 0.0
        # EACH CASE WITH ITS RECORDED STATUS -- a list of recorded values,
        # never rephrased: a composed "a total of 3 cases" dropped them all.
        quoted = True

    elif intent is Intent.ELIGIBILITY:
        # READ, NOT COMPUTED, and no model on this path at any setting.
        # A FOIR is the one figure in this system most likely to be
        # repeated back confidently and wrongly: it is a percentage, it
        # sounds like arithmetic anyone could redo, and redoing it from
        # a chat turn's context would produce a second answer for one
        # applicant.
        memory = case_memory_facts.case_memory(case_id or "", party_id)
        answer, case_sources = eligibility_facts.answer(memory)
        case_memory_block = memory
        source, llm_ms = "deterministic", 0.0

    elif intent is Intent.DOCUMENT_DETAILS:
        # QUOTED FROM THE RECORD, for the applicant who was asked about.
        # Scoped to that party -- in a two-party case "my PAN" is the
        # applicant's own, never the co-applicant's -- and to the case,
        # whose ownership the check above has already cleared.
        answer, case_sources = document_facts.answer(
            case_id or "", party_id or applicant_id, message)
        source, llm_ms = "deterministic", 0.0

    elif intent is Intent.INCOME_EVIDENCE:
        # THE SAME PLACE THE REST OF THE CASE'S CONCLUSIONS COME FROM,
        # and no model on this path at any setting. Income is the one
        # subject where a plausible sentence is most tempting and least
        # acceptable: "your salary is 50,000" reads perfectly and is a
        # figure nobody recorded.
        memory = case_memory_facts.case_memory(case_id or "", party_id)
        answer, case_sources = income_facts.answer(memory)
        case_memory_block = memory
        source, llm_ms = "deterministic", 0.0

    elif intent in (Intent.CASE_HISTORY, Intent.CASE_FINDINGS, Intent.KYC_RESULT):
        memory = case_memory_facts.case_memory(case_id or "", party_id)
        if intent is Intent.CASE_FINDINGS:
            answer, case_sources = case_memory_facts.findings_summary(memory)
        elif intent is Intent.KYC_RESULT:
            # THE CALLER'S OWN CHECK: on a two-party case the co-applicant's
            # KYC is theirs, never reported as "your KYC".
            _parties = subjects.parties_of(case_id) if case_id else []
            _primary = next((p.party_id for p in _parties
                             if p.role is subjects.Kind.PRIMARY), None)
            answer, case_sources = case_memory_facts.kyc_answer(
                memory, want=(classification.fields or {}).get("want") or "result",
                party_id=_primary if len(_parties) > 1 else None, primary_id=_primary)
        else:
            answer, case_sources = case_memory_facts.explain(memory)
        case_memory_block = memory
        source, llm_ms = "deterministic", 0.0

    elif intent is Intent.MIXED:
        # THE CASE HALF IS ANSWERED THE WAY THAT HALF IS ANSWERED.
        #
        # A mixed question whose case half is a history question --
        # "why is this case not ready AND what does the bank statement
        # show" -- was sent to `deterministic_answer`, which has no
        # case-history branch, so it returned "No answer is available
        # for this request." and the handbook paragraph was appended
        # underneath it. The explanation was in case memory all along,
        # which is where the same question asked alone reads it from.
        if answering_intent is Intent.CASE_HISTORY:
            memory = case_memory_facts.case_memory(case_id or "", party_id)
            answer, case_sources = case_memory_facts.explain(memory)
            case_memory_block = memory
        else:
            answer = deterministic_answer(answering_intent, results)
        source, llm_ms = "deterministic", 0.0
    else:
        # WHO WORDS THE ANSWER (model_routing.py): the policy decides from the
        # intent and the turn -- FAST is worded deterministically, MODEL may
        # be reworded by the model (one call, validated), NEVER is quoted.
        # A question typed in another language keeps its deterministic,
        # localizable wording: the model phrases English only.
        from app.agents.applicant.copilot.conversation import state as _conv
        from app.agents.applicant.copilot.routing import model_routing as _routing

        typed_language = getattr(frame, "language", None) or "en"
        decision = _routing.decide(
            answering_intent.value, turn_type=_conv.CURRENT_TURN.get(),
            language=typed_language, model_reachable=config.llm_enabled())
        use_llm = compose_with_model and config.llm_enabled() and typed_language == "en" and (
            decision.route is _routing.Route.MODEL
            or (config.llm_for_simple_intents() and decision.route is not _routing.Route.NEVER)
        )
        if _understanding[0] is not None:
            _understanding[0]["model_routing"] = {
                "route": decision.route.value, "reason": decision.reason,
                "phrased_by_model": bool(use_llm)}

        # AND WHERE THERE IS ONE, NOTHING PARAPHRASES IT.
        #
        # Asked for the status of a case recorded as REVIEW with
        # NAME_MISMATCH, the model wrote "there wasn't enough matching
        # information to make a decision" -- a generalisation of a
        # finding that names two people. The reason is a recorded
        # fact, and a fact is reported rather than rewritten. The
        # model keeps every answer where the case recorded nothing
        # specific, which is most of them.
        if recorded:
            use_llm = False

        # A STATUS QUESTION IS ANSWERED FROM THE STATUS, IN ONE OR TWO
        # SENTENCES: the stage, whether a recorded decision holds it and
        # why, and the required documents still missing. No identifier,
        # no creation date, no product. See status_facts.py.
        status_view = None
        if intent is Intent.APPLICATION_STATUS:
            status_view = status_facts.answer(
                (results.get("application.get") or {}).get("application") or {},
                (results.get("documents.checklist") or {}).get("checklist"),
                case_memory_facts.case_memory(case_id, party_id)
                if case_id else {},
                stage=_stage_of(stage_context, case_id),
                since=getattr(_stage_ctx(stage_context, case_id),
                              "hold_since", None),
            )
            if status_view[2]:
                # A recorded decision holds the application; its reason
                # is reported, never rephrased.
                use_llm = False
                quoted = True

        if use_llm:
            answer, source, llm_ms = await generate_answer(
                message, answering_intent, results,
                identifiers=(case_id, applicant_id),
                structured=status_view[0] if status_view is not None else None,
                stage_context=_stage_ctx(stage_context, case_id),
            )
            if status_view is not None and status_facts.names_an_identifier(
                    answer, case_id, applicant_id):
                answer, source = status_view[0], "deterministic"
        elif status_view is not None:
            answer, case_sources = status_view[0], status_view[1]
            source, llm_ms = "deterministic", 0.0
        elif intent is Intent.APPLICATION_STAGE:
            # The stage the case record establishes, and where in it.
            view = results.get("applicant.360") or {}
            if (classification.matched_on == "stage_history" and case_id
                    and history.asks_what_changed(message)):
                # WHAT CHANGED: every recorded change -- stage, uploads,
                # verification, findings, decisions -- from the case ledger,
                # in order, in a defined window. Reported, never rephrased.
                book = ledger.load(case_id)
                truth = history.changes(book, message)
                answer = history.answer(truth,
                                        multi_party=len(book.parties) > 1)
                history_block = history.public(truth)
                history_events = truth["events"]
                quoted = True
            elif classification.matched_on == "stage_history":
                # Where it HAS BEEN -- the recorded history, never inferred.
                answer = status_facts.stage_history_answer(
                    message, _stage_ctx(stage_context, case_id))
            else:
                answer = status_facts.stage_answer(
                    view.get("application") or {"status": view.get("stage")},
                    _stage_of(stage_context, case_id))
            source, llm_ms = "deterministic", 0.0
        elif (intent in (Intent.DOCUMENTS_REQUIRED, Intent.DOCUMENTS_MISSING)
              and classification.document_type
              and _slot_requirement(classification.document_type, results)):
            answer = _slot_requirement(classification.document_type, results)
            source, llm_ms = "deterministic", 0.0
        elif (intent is Intent.DOCUMENTS_PENDING
              and classification.document_type
              and _named_pending(classification.document_type, results)):
            # "Address proof pending?" is answered about Address Proof.
            answer = _named_pending(classification.document_type, results)
            source, llm_ms = "deterministic", 0.0
        elif intent is Intent.APPLICANT_PROFILE:
            # ONE RECORDED DETAIL, from the applicant / application record the
            # tools just read for this case (profile.py). A co-applicant's
            # form details are not served here: said so, never substituted.
            from app.agents.applicant.copilot.answering import profile as profiles

            if named_subject in (subjects.Kind.CO, subjects.Kind.BOTH):
                answer = ("I can only show the primary applicant's recorded details here. "
                          "Ask me about your co-applicant's documents or verification instead.")
            else:
                from app.agents.applicant import language as _languages

                answer = profiles.answer(
                    profiles.Question(classification.fields.get("field") or profiles.ALL),
                    results, case_id=case_id, party_id=party_id or applicant_id,
                    # the language the question was TYPED in (the frame read the
                    # raw text); the message may have been canonicalised since
                    language=(getattr(frame, "language", None)
                              or _languages.detect(message).code))
            source, llm_ms = "deterministic", 0.0
        elif intent is Intent.DOCUMENTS_UPLOADED and classification.document_type \
                and not classification.status_filter:
            answer = _one_document_upload(classification.document_type, results)
            source, llm_ms = "deterministic", 0.0
        elif (intent in (Intent.DOCUMENTS_UPLOADED, Intent.DOCUMENTS_PENDING)
              and classification.status_filter):
            # "Which documents are verified / under review / rejected?"
            answer = _documents_in_status(classification.status_filter,
                                          results)
            source, llm_ms = "deterministic", 0.0
        else:
            answer = deterministic_answer(answering_intent, results)
            source, llm_ms = "deterministic", 0.0

        # NO INTERNAL IDENTIFIER IN A SENTENCE. A model answer that names
        # the case or applicant id is replaced by the deterministic one,
        # whatever else it got right. The ids stay in the structured
        # fields, where a caller that needs them reads them.
        if (source == "llm" and not config.expose_internal_ids()
                and status_facts.names_an_identifier(answer, case_id,
                                                     applicant_id)):
            answer = (status_view[0] if status_view is not None
                      else deterministic_answer(answering_intent, results))
            source = "deterministic"

    # A DOCUMENT VERDICT IS NOT THE CASE'S VERDICT.
    #
    # "No documents currently have verification issues" is true of the
    # documents and misleading about the application: this case had
    # both documents passing and a recorded REVIEW, because the name
    # on the PAN and the name on the bank account belong to different
    # people. A reader told only the first half walks away believing
    # the case is clear.
    #
    # THE QUALIFIER IS RECORDED, NOT REASONED. It is the decision and
    # the reason codes the pipeline wrote down, phrased by the same
    # function the case-history answer uses. Nothing new is concluded
    # here.
    # A status answer states its own hold (status_facts), so the
    # qualifier is appended to the document-verification answer only.
    if intent is Intent.DOCUMENT_VERIFICATION:
        if recorded:
            answer = answer.rstrip() + " " + recorded

    # "WHAT IS PENDING, AND WHY?" -- the recorded reason of each pending item
    # (workflow.pending_items `detail`), never an explanation composed here.
    if frame is not None and "WHY" in [getattr(q, "value", q) for q in frame.qualifiers] \
            and intent in (Intent.DOCUMENTS_PENDING, Intent.DOCUMENTS_MISSING,
                           Intent.PENDING_ITEMS):
        payload = results.get("workflow.pending_items") or {}
        items = (payload.get("pending_items") if isinstance(payload, dict) else None) or []
        details = [str(i.get("detail")).strip() for i in items
                   if isinstance(i, dict) and i.get("detail")]
        if details and not all(d in answer for d in details):
            answer = answer.rstrip() + " " + " ".join(d for d in details if d not in answer)

    knowledge_block = None
    category = routing.category_for(intent)

    # SOURCE IS DECIDED BY WHAT ACTUALLY CONTRIBUTED, not by the branch.
    #
    # A mixed question whose retrieval came back unconfident produced only a
    # structured answer, and reporting MIXED for it would claim the handbook
    # had a say when it did not.
    if source == "llm":
        response_source = routing.ResponseSource.LLM.value
    else:
        response_source = routing.ResponseSource.STRUCTURED.value

    if intent is Intent.MIXED:
        # NO HALF ANSWERS. "What is wrong with my application AND what
        # should I upload?" was answered with the problem and a handbook
        # table of reason codes -- while the case record already said
        # Address Proof was missing. What to upload, what is pending and
        # what to do next are facts about THIS case, so the case half
        # answers them from its records; the handbook half below adds the
        # general rule, labelled as such.
        second = _second_half(message)
        if _asks_what_to_do(second):
            todo = _pending_and_next(results)
            if todo:
                answer = f"{answer.rstrip()} {todo}".strip()

        # The knowledge half, appended -- never substituted. If retrieval is
        # not confident the case answer still stands on its own; a question
        # the handbook cannot help with is not a question the case facts
        # failed to answer.
        # NOT PHRASED. The answer already has a computed half; phrasing the
        # other one costs the model budget and adds a hallucination surface
        # for a sentence nobody reads differently. Trimmed too: the handbook
        # section behind it is a numbered list, and a chat reply is not the
        # place for it.
        # THE HANDBOOK IS STILL CONSULTED for the general half -- a mixed
        # question is answered from both, and says which part is which.
        # RETRIEVED FOR THE CLAUSE THAT ASKED. The whole message carries the
        # case half's words too: "why is my application under review and
        # what does KYC mean?" retrieved the CPA-readiness section on
        # "application" and "review". The knowledge clause is asked first;
        # the whole message only when that clause alone finds nothing.
        text, knowledge_source, detail = await _knowledge_reply(
            second or message, allow_model=False,
        )
        if second and not detail.get("confident"):
            text, knowledge_source, detail = await _knowledge_reply(
                message, allow_model=False,
            )
        # CLEAN, AND LABELLED AS GENERAL. Codes become words, a table or a
        # code list is dropped, and what remains is marked as general so it
        # cannot be read as a fact about this case.
        # CLEANED BEFORE IT IS TRIMMED. Trimmed first, a section opening
        # "Concretely: 1." spent both sentences on a heading and a list
        # marker, the clean-up then removed both, and a confident handbook
        # half was reported as no half at all.
        text = " ".join(
            re.split(r"(?<=[.!?])\s+", _plain_knowledge(text))[:2]).strip()
        if not text:
            detail = {**detail, "confident": False}
        elif detail["confident"]:
            text = f"In general: {text}"
        knowledge_block = _public_knowledge(detail)
        if detail["confident"]:
            # NEVER A REFUSAL IN FRONT OF AN ANSWER. Where the case
            # half genuinely has nothing to say, prefixing the
            # knowledge half with "No answer is available" tells the
            # reader the service failed at the moment it succeeded.
            settled = answer.rstrip()
            if settled == NOTHING_AVAILABLE:
                settled = ""
            joined = "\n\n".join(p for p in (settled, text) if p)
            answer = joined
            response_source = routing.ResponseSource.MIXED.value
        else:
            # Only the store contributed, so say so.
            category = routing.QueryCategory.CASE_ONLY

    payload = _shape(results)

    # WHAT IS STILL BEING WORKED ON. A document queued for background
    # reading is neither finished nor forgotten, and an officer asking
    # about the case has no way to tell those apart unless the answer
    # says so.
    if case_id:
        try:
            from app.store import get_repository, ocr_queue

            queued = ocr_queue.jobs_for_case(get_repository(), case_id)
            if queued:
                payload["processing_queue"] = queued
        except Exception:
            pass
    response = envelope(
        intent=intent.value,
        # The kind of request, resolved from the intent the classifier
        # settled on -- including MIXED, whose case half decides nothing
        # here: a mixed question is its own kind because a UI renders the
        # two halves differently.
        query_type=type_for(intent).value,
        followed_up=resolution.public(),
        # The case intent under a MIXED answer. The public boundary prunes
        # with it, because a mixed answer is about whatever its case half was
        # about.
        base_intent=(classification.base_intent.value
                     if classification.base_intent else None),
        answer=answer,
        category=category.value,
        response_source=response_source,
        knowledge=knowledge_block,
        # What the case actually recorded, and the findings the answer
        # rests on. Both absent unless case history was asked for, so no
        # existing response grows a key.
        **({"case_memory": case_memory_block} if case_memory_block else {}),
        **({"sources": case_sources} if case_sources else {}),
        # THE TOOLS THAT ACTUALLY RAN, in order. A refused tool never ran
        # and is absent. Case-memory reads are not tools and are not
        # listed as if they were.
        tools_invoked=[step["tool"] for step in trace
                       if _executed(step, caller)],
        # HOW EACH TOOL WAS REACHED -- in process, or over the MCP protocol
        # and which transport -- and what answered it. Internal: the public
        # surfaces publish it only as provenance (`answer_basis`).
        tool_trace=[_trace_entry(step) for step in trace],
        llm_ms=llm_ms,
        answer_is_quoted=quoted,
        **({"history": history_block} if history_block else {}),
        **({"next_actions": actions.public(intelligence[3]),
            # INTERNAL: the full result, for provenance (never published).
            "_nba_internal": intelligence[3],
            "_timings": {"nba_ms": nba_ms}} if intelligence else {}),
        **({"delay": delay_block} if delay_block else {}),
        **({"_history_events": history_events} if history_events else {}),
        errors=errors,
        **payload,
    )

    # CARRY ONLY WHAT THIS ANSWER IS ABOUT.
    #
    # Every action used to return the whole case -- the applicant record,
    # every document, the full checklist -- for a question like "what is the
    # next action?". That is a payload a frontend has to ignore, and it puts
    # extracted applicant details into a chat reply with no reason to hold
    # them. The shape is unchanged; the fields this answer is not about come
    # back empty.
    # NOT PRUNED HERE.
    #
    # Conciseness is a property of the PUBLIC contract, so it is applied at
    # the FOS boundary in app/api/routes/fos_api.py and nowhere else. This
    # envelope stays complete for its internal consumers -- the deprecated
    # applicant-agent routes and the audit record among them.
    #
    # Pruning in both places also pruned twice, and the second pass could
    # only remove what the first had already left.

    logger.info(
        "applicant_agent request_id=%s subject=%s applicant=%s case=%s "
        "intent=%s tools=%s source=%s llm_ms=%.1f total_ms=%.1f",
        request_id, caller.subject, applicant_id, case_id, intent.value,
        ",".join(t["tool"] for t in trace) or "-", source, llm_ms,
        response["processing_ms"],
    )
    audit.record(request_id=request_id, subject=caller.subject,
                 applicant_id=applicant_id, case_id=case_id,
                 intent=intent.value, tools=[t["tool"] for t in trace],
                 status="OK", message=message)

    return response


def _intelligence(case_id: str, results: dict[str, Any], stage_context: Any,
                  message: str):
    """
    Evidence -> Impact -> Next Best Action for one case, from ONE ledger
    read: (ledger, problems, impacts, next_actions). Impacts cover the
    recorded problems (each its subject's) and the FOS workflow's pending
    items (the case's). Nothing here calls a model.
    """
    from app.agents.applicant import impact

    stage = _stage_of(stage_context, case_id)
    hold_since = getattr(_stage_ctx(stage_context, case_id), "hold_since", None)
    book = ledger.load(case_id)
    found = book.evidence(since=hold_since, stage=stage)
    pending = (results.get("workflow.pending_items") or {}).get("pending_items")
    case_impacts = [p["impact"] for p in found] + impact.for_pending(pending, stage)
    nba = actions.compute(
        stage=stage,
        workflow_next=(results.get("workflow.next_action") or {}).get("next_action"),
        decisions=book.memory()["decisions"], hold_since=hold_since,
        case_impacts=case_impacts,
        user_asked_for_person=actions.asks_for_person(message))
    return book, found, case_impacts, nba


#: The question says it is the caller's own ("my PAN", "mera case").
_OWN_WORDS = re.compile(r"\b(my|mine|mera|meri|mere|mujhe|apna|apni|apne)\b|मेरा|मेरी|मेरे|माझा|माझी|माझे",
                        re.IGNORECASE)


async def _answer_for_subject(
    subject: "subjects.Subject",
    parties_on_case: list["subjects.Party"],
    classification: Classification,
    message: str,
    *,
    applicant_id: str | None,
    case_id: str,
    caller: Caller,
    request_id: str | None,
    stage: str | None,
) -> tuple[str, dict[str, Any], list[dict[str, Any]],
           list[dict[str, str]], dict[str, Any] | None] | None:
    """
    A question about one party, or about each of them.

    Returns (answer, results, trace, errors, case_memory), or None when the
    question is about ONE DOCUMENT'S RECORDED VALUES -- which the existing
    party-scoped path answers once it is given the party.

    Every fact is the party's own: its stamped documents (governed tools)
    and its recorded findings (case memory, whose ownership was cleared
    before this runs). What the system keeps only for the application --
    the checklist, readiness -- is said to be the application's.
    """
    intent = classification.intent
    capability = subjects.capability_for(intent, message,
                                         classification.document_type)

    if subject.missing and subject.kind is subjects.Kind.CO:
        return subject.missing, {}, [], [], None
    frame = getattr(classification, "frame", None)
    typed_in = getattr(frame, "language", None)
    if capability is subjects.Capability.PROFILE:
        # EACH PARTY'S OWN RECORD, read by the party id the CASE RECORD names
        # -- never an id the user typed; the case's ownership was cleared above.
        from app.agents.applicant.copilot.answering import profile as _profiles

        wanted = (classification.fields or {}).get("field") or _profiles.ALL_APPLICANT
        answer = subjects.profile_answer(subject, wanted, case_id, language=typed_in)
        if subject.missing:
            answer = f"{subject.missing} {answer}"
        return answer, {}, [], [], None
    if capability is subjects.Capability.KYC:
        memory = case_memory_facts.case_memory(case_id)
        primary = next((p.party_id for p in parties_on_case
                        if p.role is subjects.Kind.PRIMARY), None)
        want = (classification.fields or {}).get("want") or "result"
        lines = [case_memory_facts.kyc_answer(
                     memory, want=want, party_id=party.party_id, primary_id=primary,
                     who=party.label)[0]
                 for party in subject.parties]
        answer = " ".join(lines)
        if subject.missing:
            answer = f"{subject.missing} {answer}"
        return answer, {}, [], [], memory
    if capability is subjects.Capability.DETAILS:
        if len(subject.parties) == 1:
            return None
        lines = []
        for party in subject.parties:
            said, _ = document_facts.answer(case_id, party.party_id, message)
            lines.append(f"For {party.label}: {said}")
        return " ".join(lines), {}, [], [], None
    if capability is None:
        return subjects.clarification(subject), {}, [], [], None
    if capability is subjects.Capability.HISTORY:
        # EACH PARTY'S OWN RECORDED CHANGES -- never the other party's, and
        # never the case's attributed to a person.
        book = ledger.load(case_id)
        lines, blocks = [], []
        for party in subject.parties:
            truth = history.changes(book, message, party_ids={party.party_id})
            lines.append(history.answer(truth, multi_party=False,
                                        subject_label=party.label))
            blocks.append({"party_role": party.role.value,
                           **history.public(truth),
                           "_events": truth["events"]})
        answer = " ".join(lines)
        if subject.missing:
            answer = f"{subject.missing} {answer}"
        return answer, {"_history": {"per_party": blocks}}, [], [], None

    results, trace, errors = await _call_tools(
        subjects.PLANS[capability], applicant_id=applicant_id,
        case_id=case_id, document_type=classification.document_type,
        caller=caller, request_id=request_id, stage=stage,
        intent=intent.value)
    documents = (results.get("documents.get") or {}).get("documents") or []
    memory = None

    if capability is subjects.Capability.VERIFICATION:
        answer = subjects.verification(subject, documents,
                                       classification.document_type)
        if _OWN_WORDS.search(message):
            answer = subjects._to_caller(subject, answer)
        if classification.matched_on == "document_rejected":
            # WHY IT WAS REJECTED: the recorded reason on that document, per
            # party -- never guessed; said plainly when none was recorded.
            memory = case_memory_facts.case_memory(case_id)
            reasons = []
            for party in subject.parties:
                found = [p for p in subjects._problems_for(party, memory, documents,
                                                           tuple(parties_on_case))
                         if not classification.document_type
                         or str(p.get("document") or "").upper()
                         == str(classification.document_type).upper()]
                reasons += [subjects._problem_words(p) for p in found]
            reasons = list(dict.fromkeys(reasons))
            if reasons:
                answer += " The recorded reason: " + case_memory_facts._and_list(reasons) + "."
            elif "rejected" in answer:
                answer += " No reason was recorded for the rejection."
    elif capability is subjects.Capability.PENDING:
        answer = subjects.pending(
            subject, documents,
            (results.get("documents.checklist") or {}).get("checklist"))
    elif capability is subjects.Capability.READINESS:
        answer = subjects.readiness(
            subject, documents,
            deterministic_answer(Intent.READINESS, results))
    else:
        memory = case_memory_facts.case_memory(case_id)
        answer = subjects.issues(subject, memory, documents, parties_on_case)
        if _OWN_WORDS.search(message):
            answer = subjects._to_caller(subject, answer)

    if subject.missing:
        answer = f"{subject.missing} {answer}"
    return answer, results, trace, errors, memory


_answer_unguarded = answer_question


async def _named_people(message: str, kwargs: dict[str, Any],
                        claims: dict[str, Any]) -> dict[str, Any]:
    """
    A PERSON NAMED BY NAME ("Priya ka mobile", "Zara's PAN"). Only when the
    message carries a token no vocabulary knows, and only AFTER the caller's
    ownership of the case is proven with the pipeline's own checks: then the
    case record's parties are read and the name is matched against them.
    Returns {"message": rewritten} / {"ask", "options"} / {"refuse": True} /
    {} (nothing named, or not provable here -- the pipeline decides as before).
    """
    case_id = kwargs.get("case_id")
    applicant_id = kwargs.get("applicant_id")
    if not case_id or not subjects.names_a_person(message):
        return {}
    try:
        caller = Caller.from_claims(claims)
        permissions.check_ownership(applicant_id or "", case_id, caller=caller)
        from app.security import access as _access

        _access.authorize_conversation(caller.subject, caller.scopes,
                                       applicant_id=applicant_id, case_id=case_id)
        from app.store import get_repository, request_cache as _cache

        parties = subjects.parties_of(case_id)
        repo = get_repository()
        names = {}
        for party in parties:
            record = _cache.read(repo, "get_applicant", party.party_id)
            names[party.party_id] = getattr(record, "full_name", None) or ""
    except Exception:  # noqa: BLE001 - not provable here: the pipeline refuses as usual
        return {}
    found = subjects.resolve_names(message, parties, names)
    role = found.get("role")
    if role is subjects.Kind.CO:
        return {"message": found["as_co"]}
    if role is subjects.Kind.PRIMARY:
        return {"message": found["as_self"]}
    if role == "AMBIGUOUS":
        return {"ask": "Do you mean you (the primary applicant) or the co-applicant?",
                "options": [found["as_self"], found["as_co"]]}
    if role == "OTHER":
        if _PARTY_CLAIM.search(message) and "?" not in message:
            return {"reply": "I can only go by the people recorded on this application, so "
                             "nothing has been changed. That name isn't one of them."}
        return {"refuse": True}
    return {}


#: A statement ABOUT who is on the case ("X is my co-applicant", "remember
#: that ..."): never adopted -- the case record decides who is on it.
_PARTY_CLAIM = re.compile(
    r"\b(remember|note|assume|consider|treat|save|store|yaad\s+rakh\w*)\b"
    r"|\bis\s+(my|our|the)\s+(co-?\s?applicant|applicant|guarantor|spouse|wife|husband)\b"
    r"|\b(mera|meri|hamara)\s+(co-?\s?applicant|guarantor)\s+(hai|he|h)\b", re.IGNORECASE)


async def _conversational(**kwargs: Any) -> dict[str, Any]:
    """
    THE CONVERSATION STATE LAYER around one question (conversation_state.py).

    ORDER: the input guardrail first -- a refused request touches no state
    and is refused exactly as before. Then the turn is read RELATIVE TO THE
    CONVERSATION (a pending clarification, a cancellation, a correction, an
    acknowledgement, "again"), and the message that results is answered by
    the ordinary pipeline with the conversation's context merged under any
    context the caller sent. Afterwards the state records what the answer
    was about. State is keyed by the authenticated subject; it never grants
    ownership and never carries case values.
    """
    from app.agents.applicant.copilot.conversation import state as conv
    from app.security import guardrails

    message = str(kwargs.get("message") or "")
    claims = kwargs.get("claims") or {}
    case_id = kwargs.get("case_id")
    context = kwargs.get("context")
    if not conv.enabled() or kwargs.get("intent_override") is not None:
        return await _answer_unguarded(**kwargs)
    security_started = time.perf_counter()
    screened = guardrails.check_input(
        message, allowed_ids=(case_id, kwargs.get("applicant_id"), kwargs.get("party_id")))
    security_ms = round((time.perf_counter() - security_started) * 1000, 2)
    if not screened.allowed:
        refused = await _answer_unguarded(**kwargs)
        refused.setdefault("_timings", {})["security_ms"] = security_ms
        try:
            # THE CONVERSATION LEARNS WHICH KIND OF TURN WAS REFUSED (a code):
            # "and his mobile?" next points at the refused person.
            _subject = str(Caller.from_claims(claims).subject or "anonymous")
            _cid = (context or {}).get("conversation_id") if isinstance(context, dict) else None
            _state = conv.STORE.get(_subject, _cid)
            if _state is None or (_state.case_id or None) != (case_id or None):
                _state = conv.STORE.new(_subject, case_id)
            _state.last_refusal = getattr(screened.category, "value", None)
            conv.STORE.put(_state)
            # the conversation continues from here: its id, and nothing else
            if not isinstance(refused.get("context"), dict):
                refused["context"] = {"conversation_id": _state.conversation_id}
            _understood = refused.get("understanding")
            if not isinstance(_understood, dict):
                _understood = refused["understanding"] = {}
            _understood.setdefault("conversation", {"conversation_id": _state.conversation_id,
                                                    "turn_id": _state.turn_id + 1,
                                                    "outcome": "REFUSED", "turn_type": "NEW_REQUEST"})
        except Exception:  # noqa: BLE001 - state never blocks a refusal
            pass
        return refused
    turn_started = time.perf_counter()

    subject = str(Caller.from_claims(claims).subject or "anonymous")
    conversation_id = (context or {}).get("conversation_id") if isinstance(context, dict) else None
    state = conv.STORE.get(subject, conversation_id)
    if state is not None and (state.case_id or None) != (case_id or None):
        state = None                      # another case: a new conversation
    if state is None:
        state = conv.STORE.new(subject, case_id)
    if isinstance(context, dict) and str(context.get("conversation_id") or "") == \
            state.conversation_id:
        # THE REFERENTS THE CALLER'S CONTEXT CARRIES (labels, never authority):
        # a route that builds its reply context after the agent returns
        # leaves the stored state without the document "it" points at.
        if not state.last_slot and context.get("last_slot"):
            state.last_slot = str(context.get("last_slot"))
        if not state.last_document and context.get("last_document"):
            state.last_document = str(context.get("last_document"))
        if not state.last_documents and isinstance(context.get("last_documents"), list):
            state.last_documents = [str(d) for d in context["last_documents"]][:6]
    named = await _named_people(message, kwargs, claims)
    if named.get("refuse"):
        reading = conv.Reading(conv.REFUSED, message, reply="", note="CROSS_CUSTOMER_DATA")
    elif named.get("ask"):
        reading = conv.Reading(conv.ASKED, message, reply=named["ask"], options=named["options"],
                               note="which of the two people on the case")
    elif named.get("reply"):
        reading = conv.Reading(conv.NEW_TOPIC, message, reply=named["reply"],
                               note="a claim about the case's parties, not adopted")
    else:
        reading = conv.read_turn(named.get("message") or message, state)
    conversation_ms = round((time.perf_counter() - turn_started) * 1000, 2)
    from app.agents.applicant.copilot.answering import phrasing as _phrasing

    _phrasing.TURN_SEED.set(_phrasing.seed_for(state.conversation_id, state.turn_id + 1))
    conv.CURRENT_TURN.set(conv._TURN_TYPES.get(reading.outcome, conv.NEW_REQUEST))

    trace = {"conversation_id": state.conversation_id, "turn_id": state.turn_id + 1,
             "outcome": reading.outcome, "note": reading.note,
             "pending_before": (state.pending_clarification.public()
                                if state.pending_clarification else None)}
    merged = dict(state.as_context())
    if isinstance(context, dict):
        merged.update({k: v for k, v in context.items() if v not in (None, "", [])
                       and k not in merged or merged.get(k) in (None, "", [])})
    merged["conversation_id"] = state.conversation_id

    if reading.reply is not None:
        # ANSWERED FROM THE CONVERSATION ALONE: nothing read, no tool, no model.
        clarification = None
        if reading.options:
            clarification = {"reason": "CLARIFICATION_PENDING", "question": reading.reply,
                             "options": list(reading.options), "original_message": message[:200]}
        response = {
            "request_id": kwargs.get("request_id"), "applicant_id": kwargs.get("applicant_id"),
            "case_id": case_id, "intent": Intent.UNKNOWN.value, "answer": reading.reply,
            "applicant": None, "application": None, "stage": None, "documents": [],
            "checklist": [], "policy": None, "pending_items": [], "next_action": None,
            "readiness": None, "actions": [], "knowledge": None, "case_memory": None,
            "sources": [],
            "category": (routing.QueryCategory.UNSUPPORTED.value if clarification
                         else routing.QueryCategory.CONVERSATION.value),
            "query_type": QueryType.CLARIFICATION.value, "case_state": None,
            "suggested_questions": list(reading.options), "available_actions": [],
            "document_highlights": [], "clarification_required": clarification,
            "followed_up": None, "context": None, "base_intent": None, "route_to": None,
            "response_source": routing.ResponseSource.CONVERSATION.value,
            "processing_ms": 0.0, "errors": [], "tools_invoked": [],
            "understanding": {"frame": None, "decided_by": "CONVERSATION_STATE",
                              "referents": {}, "short_query": None,
                              "llm": {"consulted": False, "status": "NOT_NEEDED"},
                              "parse_ms": 0.0, "case_stage": None},
        }
        if reading.outcome == conv.REFUSED:
            response["answer"] = guardrails.refusal(guardrails.Category.CROSS_CUSTOMER_DATA)
            response["intent"] = "GUARDRAIL_BLOCKED"
            response["guardrail"] = {"stage": "context", "action": "BLOCKED",
                                     "category": guardrails.Category.CROSS_CUSTOMER_DATA.value}
            response["suggested_questions"] = []
        if reading.outcome in (conv.STILL_AMBIGUOUS, conv.NEGATION, conv.INVALID_OPTION,
                               conv.PARTIAL_RESOLUTION):
            response["errors"] = [{"code": "UNSUPPORTED_REQUEST",
                                   "message": "The request was not understood."}]
    else:
        if reading.message != message:
            kwargs = dict(kwargs, message=reading.message)
        response = await _answer_unguarded(**dict(kwargs, context=merged))
        if reading.message != message:
            if not response.get("followed_up"):
                # the same block a bare follow-up publishes (followup.Resolution.public)
                response["followed_up"] = {"original_message": message[:200],
                                           "interpreted_as": reading.message[:200],
                                           "reason": reading.note or reading.outcome}
    kind = conv.turn_type(reading, response, state)
    trace["turn_type"] = kind
    # A CORRECTION, A RESOLVED CLARIFICATION OR A REPEAT is acknowledged in a
    # few words before the answer -- the answer itself is untouched.
    if str(response.get("answer") or "").strip() and not response.get("clarification_required") \
            and str(response.get("category") or "") not in ("CONVERSATION", "UNSUPPORTED") \
            and (reading.outcome in (conv.CORRECTION, conv.OPTION_RESOLVED, conv.YES_NO_RESPONSE)
                 or (reading.outcome == conv.REPLAY
                     and reading.note == "the previous question, asked again")):
        language = ((response.get("understanding") or {}).get("frame") or {}).get("language")
        ack_kind = {conv.CORRECTION: "CORRECTION", conv.REPLAY: "REPLAY"}.get(
            reading.outcome, "CLARIFICATION_RESPONSE")
        response["answer"] = _phrasing.acknowledge(
            str(response["answer"]), turn_type=ack_kind, language=language,
            seed=_phrasing.TURN_SEED.get())
    conv.update_from_response(state, reading.message or message, response, reading)
    trace["pending_after"] = (state.pending_clarification.public()
                              if state.pending_clarification else None)
    trace["state"] = state.public()
    understanding = response.get("understanding")
    if not isinstance(understanding, dict):
        understanding = {}
        response["understanding"] = understanding
    understanding["conversation"] = trace
    timings = response.setdefault("_timings", {})
    timings["security_ms"] = security_ms
    timings["conversation_ms"] = conversation_ms
    timings["total_ms"] = round((time.perf_counter() - security_started) * 1000, 2)
    return response


@functools.wraps(_answer_unguarded)
async def answer_question(**kwargs: Any) -> dict[str, Any]:
    """
    One FOS question, answered -- and THE LAST CHECK before any surface
    publishes it. Every return above goes through here, so no answer path
    (a deterministic answer, a handbook passage, a refusal) can publish
    code, a path, a credential or a tool payload: the sentence carrying it
    is dropped (guardrails.published), and a model-written answer never
    reaches this point unvalidated.
    """
    from app.security import guardrails
    from app.store import request_cache
    from app.agents.applicant.copilot.facts import field_state as _field_state

    evidence_token = _field_state.EVIDENCE.set([])
    try:
        with request_cache.scoped():
            response = await _conversational(**kwargs)
        _describe_answer(response, _field_state.EVIDENCE.get() or [])
    finally:
        _field_state.EVIDENCE.reset(evidence_token)
    answer = response.get("answer")
    if isinstance(answer, str) and answer:
        cleaned, verdict = guardrails.published(answer)
        if not verdict.allowed:
            response["answer"] = cleaned
            response["guardrail"] = {"stage": "output", "action": "REDACTED",
                                     "category": verdict.category.value}
    return response


#: What a document-facing intent asks ABOUT the thing it names.
_ASPECT = {"APPLICANT_PROFILE": "VALUE", "DOCUMENTS_UPLOADED": "UPLOADED",
           "DOCUMENT_VERIFICATION": "VERIFICATION", "DOCUMENTS_REQUIRED": "REQUIREMENT",
           "DOCUMENTS_PENDING": "PENDING", "DOCUMENTS_MISSING": "PENDING",
           "KYC_RESULT": "KYC", "DOCUMENT_DETAILS": "VALUE"}


def _describe_answer(response: dict[str, Any], evidence: list[dict[str, str]]) -> None:
    """
    WHAT WAS ASKED AND WHAT THE ANSWER RESTS ON -- kept apart from the case.
    `understanding.requested`: the capability, the exact field or document and
    the aspect asked (PAN number = PAN_NUMBER / VALUE, never "the PAN document").
    `understanding.answer_evidence`: the fields this answer resolved, their
    state and source. Case-level findings stay where they are; they are not
    evidence for an answer that did not use them.
    """
    understanding = response.get("understanding")
    if not isinstance(understanding, dict):
        return
    intent = str(response.get("intent") or "")
    frame = understanding.get("frame") if isinstance(understanding.get("frame"), dict) else {}
    fields = [e["field"] for e in evidence if e.get("field")]
    thing = "+".join(dict.fromkeys(fields)) if fields and intent in ("APPLICANT_PROFILE", "KYC_RESULT") \
        else (frame.get("document_type") or None)
    if intent in _ASPECT:
        understanding["requested"] = {"capability": intent, "field": thing,
                                      "aspect": _ASPECT[intent], "party": frame.get("party")}
    if evidence:
        understanding["answer_evidence"] = list(evidence)


async def _knowledge_reply(
    message: str,
    *,
    product: str | None = None,
    allow_model: bool = True,
    max_sentences: int | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """
    A knowledge answer. Configuration wins outright where it has one.

    THE ORDER IS THE FIX. A configuration question is answered from the same
    `accepts` list the upload endpoint enforces, the model is not called, and
    the retrieved passage is NOT appended.

    Appending it is what shipped the live failure. The exact answer was
    produced correctly and then followed by a handbook paragraph saying
    utility bills and rent agreements are common in the industry -- true, and
    read by an officer as a second list of things they could bring. The
    passage explains a slot; the question asked for document names; putting
    both in one answer let the wrong half be the memorable one.

    Retrieval still answers everything else: how verification works, what a
    status means, why a document failed. Those have no configured value to
    read and prose is the right form for them.
    """
    facts_for_product = facts.fact_set(facts.product_in(message) or product)

    try:
        exact = facts.authoritative_answer(message, product)
    except Exception:
        logger.exception("authoritative fact lookup failed")
        exact = None

    if exact is not None:
        # No model, no retrieval text. The answer is a configured value and
        # there is nothing a model could add to it that is not a risk.
        counters.record(called=False)
        detail = {
            "stage": knowledge_answer.STAGE,
            "retrieved": 0,
            "top_score": 1.0,
            "threshold": None,
            "confident": True,
            "authoritative": True,
            "citations": [f"configuration:{exact.kind}"],
        }
        # THE HANDBOOK PAGE THAT DESCRIBES THIS CONFIGURATION is cited beside
        # it -- only when retrieval confidently finds one. Its text is not
        # appended: the configured value is the answer.
        try:
            described = knowledge_answer.retrieve(message)
            if described is not None and described.confident and described.citations():
                detail["citations"].append(described.citations()[0])
                detail["versions"] = described.versions()
        except Exception:  # noqa: BLE001 - the configured answer stands alone
            pass
        return _cited(exact.text, detail), routing.ResponseSource.KNOWLEDGE.value, detail

    text, source, detail = await knowledge_answer.answer(
        message, allow_model=allow_model, max_sentences=max_sentences,
    )

    # A MODEL-WRITTEN ANSWER IS CHECKED BEFORE IT IS RETURNED.
    #
    # Retrieval being confident says the passages were relevant. It says
    # nothing about whether the sentence built from them kept their meaning,
    # and the live failure was exactly a reversed negation inside a relevant
    # passage.
    passage = detail.pop("_passage", None) if isinstance(detail, dict) else None
    if source == "llm":
        verdict = grounding.validate(text, facts_for_product)
        if verdict:
            # THE UNIFIED VALIDATOR as well: no number, date or decision word
            # the retrieved passage does not carry, and the common shape and
            # leakage checks (app/security/output_validation.py).
            from app.security import output_validation

            unified = output_validation.validate(
                text, surface="knowledge_phrase", truth=passage or "")
            if not unified.accepted:
                verdict = grounding.Verdict(False, [unified.value])
        if verdict:
            # WORDING ONLY: no number, name or claim the passage does not carry.
            from app.agents.applicant import fidelity

            faith = fidelity.check(text, source=passage or "", question=message)
            if not faith.faithful:
                verdict = grounding.Verdict(False, [f"fidelity: {faith.describe()}"])
        if not verdict:
            logger.warning(
                "FOS knowledge answer rejected by grounding (%s); using the "
                "retrieved text.", grounding.describe(verdict),
            )
            detail = dict(detail)
            detail["grounding_rejected"] = True
            return (_cited(knowledge_answer.retrieved_text_for(message), detail),
                    routing.ResponseSource.KNOWLEDGE.value, detail)

    return _cited(text, detail), _knowledge_source(source, detail), detail


def _cited(text: str, detail: dict[str, Any] | None) -> str:
    """
    A KNOWLEDGE ANSWER NAMES WHERE IT COMES FROM: the handbook document and
    section it was grounded in (and the version, when recorded), or the
    configured policy -- in words, never a path. A refused or unconfident
    retrieval cites nothing; a customer fact never reaches this function.
    """
    if not text or not isinstance(detail, dict) or detail.get("refused") \
            or not (detail.get("confident") or detail.get("authoritative")):
        return text
    citations = [str(c) for c in (detail.get("citations") or []) if c]
    if not citations:
        return text
    if citations[0].startswith("configuration:"):
        page = next((c.partition("#")[0] for c in citations[1:] if "#" in c or c.endswith(".md")), "")
        if page:
            title = page.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("_", " ").strip().capitalize()
            return f"{text}\n\nSource: the configured document policy (FOS handbook, {title})."
        return f"{text}\n\nSource: the configured document policy."
    document = citations[0].partition("#")[0]
    sections: list[str] = []
    for c in citations:
        doc, _, section = c.partition("#")
        if doc == document and section and section not in sections:
            sections.append(section)
    title = document.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("_", " ").strip().capitalize()
    version = next((str(v.get("version") or "") for v in detail.get("versions") or []
                    if isinstance(v, dict) and v.get("document") == document), "")
    said = f"Source: FOS handbook, {title}"
    if sections:
        said += " (" + ", ".join(sections[:2]) + ")"
    if version:
        said += f", version {version.split(':')[-1][:7]}"
    return f"{text}\n\n{said}."


def _knowledge_source(source: str, detail: dict[str, Any]) -> str:
    if source == "llm":
        return routing.ResponseSource.LLM.value
    if detail.get("confident"):
        return routing.ResponseSource.KNOWLEDGE.value
    return routing.ResponseSource.STRUCTURED.value


def _public_knowledge(detail: dict[str, Any]) -> dict[str, Any]:
    """
    What a caller is told about the retrieval behind a knowledge answer.

    Citations and a score, so an officer can see which part of the FOS
    handbook an answer came from and a reviewer can tell a refusal caused by
    a thin corpus from one caused by an off-topic question.

    NOT the retrieved text, the chunk ids or the prompt. Those are internals,
    and the answer already carries what they said.
    """
    return {
        "stage": detail.get("stage"),
        "grounded": bool(detail.get("confident")),
        "sources": list(detail.get("citations") or []),
        "top_score": detail.get("top_score", 0.0),
        # WHICH VERSION of the handbook answered: declared, or the content
        # hash of the file -- never an invented release number.
        "versions": list(detail.get("versions") or []),
    }


def _shape(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Lift tool results into the published response fields."""
    out: dict[str, Any] = {}

    view = results.get("applicant.360")
    if view:
        out.update({
            "applicant": view.get("applicant"),
            "application": view.get("application"),
            "stage": view.get("stage"),
            "documents": view.get("documents") or [],
            "checklist": view.get("checklist") or [],
            "policy": view.get("policy"),
            "pending_items": view.get("pending_items") or [],
            "next_action": view.get("next_action"),
            "readiness": view.get("readiness"),
        })
        return out

    if "applicant.get" in results:
        out["applicant"] = results["applicant.get"].get("applicant")
    if "application.get" in results:
        out["application"] = results["application.get"].get("application")
        out["stage"] = (out["application"] or {}).get("status")
    if "documents.get" in results:
        out["documents"] = results["documents.get"].get("documents") or []
    if "documents.checklist" in results:
        out["checklist"] = results["documents.checklist"].get("checklist") or []
        out["policy"] = results["documents.checklist"].get("policy")
    if "documents.verification" in results:
        payload = results["documents.verification"]
        if payload.get("found"):
            out["documents"] = [payload]
    if "workflow.pending_items" in results:
        out["pending_items"] = results["workflow.pending_items"].get("pending_items") or []
    if "workflow.next_action" in results:
        out["next_action"] = results["workflow.next_action"].get("next_action")
    if "workflow.readiness" in results:
        out["readiness"] = results["workflow.readiness"].get("readiness")

    return out


async def confirm_action(
    *,
    action: dict[str, Any],
    claims: dict[str, Any],
    request_id: str | None = None,
) -> dict[str, Any]:
    """
    Carry out a write the FOS has confirmed.

    The scope is checked again here rather than trusted from the proposal:
    the two calls are separate requests and may carry different tokens.
    """
    from app.mcp import applicant as tools

    started = time.perf_counter()
    request_id = request_id or f"aa_{uuid.uuid4().hex}"
    caller = Caller.from_claims(claims)

    try:
        intent = Intent(str(action.get("type") or ""))
    except ValueError as exc:
        raise AgentError("INVALID_ACTION", "Unknown action type.", 400) from exc

    if intent not in WRITE_INTENTS:
        raise AgentError("INVALID_ACTION", "That action is not a write.", 400)

    try:
        permissions.check_capability(caller, intent)
    except PermissionDenied as exc:
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=str(action.get("arguments", {}).get("applicant_id")),
                     case_id=str(action.get("arguments", {}).get("case_id")),
                     intent=intent.value, tools=[], write=True, confirmed=True,
                     status="DENIED", detail=exc.code)
        raise AgentError(exc.code, exc.message, http_status=403) from exc

    capability = str(action.get("tool") or "")
    handler = tools.WRITE_TOOLS.get(capability)
    if handler is None:
        raise AgentError("INVALID_ACTION", f"Unknown capability: {capability}", 400)
    # THE TOOL IS THE INTENT'S, OR NOTHING. The action is caller-supplied;
    # a confirmation typed UPDATE_APPLICANT must not run some other write.
    if _WRITE_TOOL_FOR.get(intent) != capability:
        raise AgentError("INVALID_ACTION",
                         "That tool does not carry out this action.", 400)

    arguments = {k: v for k, v in (action.get("arguments") or {}).items()
                 if v is not None}

    # PER-TOOL SCOPE AND OWNERSHIP, checked again here: the confirmation is
    # a separate request and may carry a different token.
    try:
        permissions.check_tool(caller, capability)
        if intent is not Intent.CREATE_APPLICANT:
            permissions.check_ownership(
                str(arguments.get("applicant_id") or ""),
                arguments.get("case_id") or None,
                caller=caller, write=True)
    except PermissionDenied as exc:
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=str(arguments.get("applicant_id") or ""),
                     case_id=str(arguments.get("case_id") or ""),
                     intent=intent.value, tools=[], write=True,
                     confirmed=True, status="DENIED", detail=exc.code)
        raise AgentError(exc.code, exc.message, http_status=403) from exc

    envelope = await handler(**arguments)

    # WHAT THE CALLER CREATED, THE CALLER OWNS.
    if envelope.ok and intent in (Intent.CREATE_APPLICANT,
                                  Intent.CREATE_APPLICATION):
        from app.security import access

        result = envelope.result or {}
        access.record_ownership(
            caller.subject,
            applicant_id=((result.get("applicant") or {}).get("applicant_id")
                          or arguments.get("applicant_id")),
            case_id=(result.get("application") or {}).get("case_id"))

    audit.record(
        request_id=request_id, subject=caller.subject,
        applicant_id=str(arguments.get("applicant_id") or ""),
        case_id=str(arguments.get("case_id") or ""),
        intent=intent.value, tools=[capability], write=True, confirmed=True,
        status="OK" if envelope.ok else "FAILED",
        detail=None if envelope.ok else (envelope.error.code if envelope.error else None),
    )

    if not envelope.ok:
        error = envelope.error
        raise AgentError(
            error.code if error else "ACTION_FAILED",
            error.message if error else "The action could not be completed.",
            http_status=404 if error and error.code == "NOT_FOUND" else 400,
        )

    return {
        "request_id": request_id,
        "action_id": action.get("action_id"),
        "type": intent.value,
        "applied": True,
        "result": envelope.result,
        "processing_ms": round((time.perf_counter() - started) * 1000, 2),
    }


__all__ = ["AgentError", "answer_question", "confirm_action"]
