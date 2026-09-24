"""
A short summary of where a case stands, for a person to read.

WHAT THE MODEL IS FOR, AND WHAT IT IS NOT. Every value in the summary
is decided before a model is involved: the stage, whether the
applicant record is complete, each document's status, readiness, the
next action. The model is handed those conclusions and asked to put
them in two sentences. It does not compute a status, decide a
document, judge readiness, choose a next action, or assess risk -- it
phrases what the records already say.

WHY BOTHER, IF IT ONLY REPHRASES. The deterministic sentence below is
correct and reads like a form. An officer scanning twenty cases wants
"PAN is verified; the bank statement is still being read", not a list
of enum values. That is worth a model call and nothing more.

IT IS NEVER THE ONLY ANSWER. `summarise` returns the deterministic
text when the model is off, unreachable, slow, or produces something
too long or empty, and says which happened through `response_source`.
A summary is never fabricated and never contradicts the state it was
given, because the state is all it is given.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Two sentences, about three hundred characters. Enforced rather than
#: requested: a model asked for brevity complies most of the time.
MAX_CHARACTERS = 300
MAX_SENTENCES = 2

STRUCTURED = "structured"
STRUCTURED_AND_LLM = "structured+llm"

_SYSTEM = (
    "You write one or two short sentences telling a loan officer where "
    "an application stands. You are given the facts the system has "
    "already established. Rephrase them plainly; never add a fact, a "
    "status, a document, a date or a judgement that is not in them, and "
    "never contradict them. Do not decide whether anything is ready, "
    "approved or risky -- if the facts do not say it, it is not yours to "
    "say. No field names, no codes, no enum values, no bullet lists, no "
    "identifiers. Under 300 characters. A fact that is absent has not "
    "been established -- say nothing about it rather than assuming it "
    "is zero, complete, or ready."
)

#: Stored statuses, as a person says them.
_WORDS = {
    "VERIFIED": "verified",
    "PASS": "verified",
    "REVIEW": "under review",
    "REJECTED": "rejected",
    "UPLOADED": "uploaded",
    "PROCESSING": "being processed",
    "MISSING": "not uploaded",
}

_TYPE_WORDS = {
    "BANK_STATEMENT": "bank statement",
    "SALE_DEED": "sale deed",
    "DRIVING_LICENCE": "driving licence",
    "VOTER_ID": "voter ID",
    "ADDRESS_PROOF": "address proof",
    "INCOME_PROOF": "income proof",
    "PAN": "PAN",
    "AADHAAR": "Aadhaar",
    "PASSPORT": "passport",
}


def _readable_type(value: Any) -> str:
    key = str(value or "").upper()
    return _TYPE_WORDS.get(key, key.replace("_", " ").lower() or "document")


def _readable_status(value: Any) -> str:
    key = str(value or "").upper()
    return _WORDS.get(key, key.replace("_", " ").lower() or "unknown")


def state_of(envelope: dict[str, Any]) -> dict[str, Any]:
    """
    The compact, deterministic state a summary may be built from.

    AN ALLOWLIST, and a short one. Everything here is already in the
    response the caller receives; nothing is recomputed, and nothing
    the model could mistake for permission to decide is included.
    """
    applicant = envelope.get("applicant") or {}
    application = envelope.get("application") or {}
    readiness = envelope.get("readiness") or {}
    next_action = envelope.get("next_action") or {}

    documents = []
    for document in envelope.get("documents") or []:
        documents.append({
            "document": _readable_type(document.get("document_type")
                                       or document.get("type")),
            "status": _readable_status(document.get("verification_status")
                                       or document.get("status")),
        })

    queued = [
        {"document": _readable_type(job.get("document_type")),
         "state": "still being read in the background"}
        for job in envelope.get("processing_queue") or []
        if str(job.get("status") or "") in {"QUEUED", "PROCESSING"}
    ]

    # ABSENT IS NOT ZERO, AND NOT FALSE.
    #
    # A question about documents is answered from a pruned envelope
    # that carries no readiness and no pending items. Reported as
    # `items_outstanding: 0, ready_for_handoff: None`, that reads as
    # "nothing outstanding", and the model duly announced a case was
    # ready while the answer beside it listed three things blocking
    # it. What is not known is left out, and the prompt is told that
    # a missing key means unknown.
    state: dict[str, Any] = {}

    if documents:
        state["documents"] = documents

    stage = envelope.get("stage") or application.get("status")
    if stage:
        state["stage"] = stage
    if applicant:
        state["applicant_details_complete"] = not (
            applicant.get("missing_fields") or [])
    if queued:
        state["still_being_read"] = queued

    # ONLY WHAT IS POSITIVELY KNOWN. An empty list here is the agent
    # not having been asked, not the case being clear: the envelope
    # initialises every field, so `pending_items: []` arrives on a
    # question about documents and was reported as "nothing
    # outstanding" -- next to an answer listing three blockers.
    outstanding = len(envelope.get("pending_items") or [])
    if outstanding:
        state["items_outstanding"] = outstanding

    blocking = readiness.get("blocking_items") or []
    if blocking:
        state["blocking"] = [
            str(item.get("detail") or item.get("code") or "").strip()
            for item in blocking[:4]
        ]

    ready = readiness.get("ready")
    if ready is None and readiness.get("status"):
        ready = str(readiness["status"]).upper() == "READY"
    if ready is not None:
        state["ready_for_handoff"] = ready

    action = next_action.get("action") or next_action.get("code")
    if action:
        state["next_action"] = action

    return state


def deterministic(state: dict[str, Any]) -> str:
    """
    The summary when no model writes one. Correct, if a little flat.

    THIS IS THE FLOOR, not a placeholder: it is what the caller gets
    whenever the model is unavailable, and it has to stand on its own.
    """
    parts: list[str] = []

    stage = state.get("stage")
    if stage:
        parts.append(f"The application is at {_readable_status(stage)}")
    else:
        parts.append("The application is open")

    documents = state.get("documents") or []
    if documents:
        listed = ", ".join(f"{d['document']} {d['status']}"
                           for d in documents[:3])
        parts.append(f"with {listed}")

    first = " ".join(parts).strip() + "."

    tail = []
    if state.get("still_being_read"):
        tail.append("A document is still being read in the background.")
    blocking = state.get("blocking") or []
    if blocking:
        tail.append(f"{len(blocking)} item(s) are blocking handoff.")
    outstanding = int(state.get("items_outstanding") or 0)
    if outstanding and not blocking:
        tail.append(f"{outstanding} item(s) remain outstanding.")

    return " ".join([first] + tail[:1]).strip()


def _trimmed(text: str) -> str:
    """Two sentences at most, and never past the character limit."""
    import re

    said = " ".join((text or "").split())
    if not said:
        return ""

    sentences = re.split(r"(?<=[.!?])\s+", said)[:MAX_SENTENCES]
    said = " ".join(sentences).strip()

    if len(said) > MAX_CHARACTERS:
        said = said[:MAX_CHARACTERS].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return said


async def summarise(envelope: dict[str, Any], *,
                    timeout: float = 12.0) -> tuple[str, str]:
    """
    A short summary, and where it came from.

    Returns `(summary, response_source)`. The source is
    `structured+llm` only when a model actually wrote the words that
    came back -- a fallback says `structured`, because claiming
    otherwise would misreport how the sentence was produced.
    """
    state = state_of(envelope)
    fallback = deterministic(state)

    written = await _generate(state, timeout)
    if not written:
        return fallback, STRUCTURED

    return written, STRUCTURED_AND_LLM


async def _generate(state: dict[str, Any], timeout: float) -> str:
    import asyncio
    import json

    try:
        from agent_framework import Message

        from app.agents.applicant import config
        from app.llm import availability
        from app.llm.provider import create_ollama_client

        if not config.llm_enabled() or not availability.provider_reachable():
            return ""

        client = create_ollama_client()
        response = await asyncio.wait_for(
            client.get_response(
                [Message(role="system", contents=[_SYSTEM]),
                 Message(role="user", contents=[
                     json.dumps(state, separators=(",", ":"), default=str)])],
                stream=False,
            ),
            timeout=timeout,
        )
    except Exception as exc:
        logger.info("Case summary fell back to the computed text: %s",
                    type(exc).__name__)
        return ""

    return _trimmed(_text_of(response))


def _text_of(response: Any) -> str:
    for attribute in ("text", "content"):
        value = getattr(response, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for message in reversed(list(getattr(response, "messages", None) or [])):
        for content in getattr(message, "contents", None) or []:
            value = getattr(content, "text", None) or (
                content if isinstance(content, str) else None)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


__all__ = ["MAX_CHARACTERS", "MAX_SENTENCES", "STRUCTURED",
           "STRUCTURED_AND_LLM", "deterministic", "state_of", "summarise"]
