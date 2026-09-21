"""
Human-readable summary for the unified response.

The summary is the ONLY place a language model is allowed anywhere near this
service, and it runs strictly AFTER every decision is final. Classification,
verification, extraction, KYC and the PASS/REVIEW/FAIL verdict are all
deterministic and already computed before a single token is generated.

The model therefore cannot:
    invent a field          -- it is handed values, never documents
    alter an extracted value-- its output is prose, never merged into fields
    override verification   -- the verdict is already in the envelope
    override KYC            -- likewise
    decide the outcome      -- it is told the outcome

A generated summary is additionally validated before use: it is rejected
wholesale if it is empty, over-long, returns structured data, states a number
that does not appear in the evidence, or asserts a verdict other than the
computed one. A rejected summary is discarded entirely -- never partially
merged -- and the deterministic sentence is used instead.

With the model switched off, unavailable, slow or wrong, the service still
returns a correct summary. That is the point.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

MIN_SUMMARY_CHARS = 20
MAX_SUMMARY_CHARS = 400

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

_VERDICTS = ("PASS", "REVIEW", "FAIL", "REJECTED", "SUCCESS", "PARTIAL", "FAILED")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def llm_enabled() -> bool:
    """
    Whether to attempt a generated summary.

    Default OFF. The deterministic sentence is accurate and free, while a
    model call adds seconds to a path whose decisions are already final --
    latency nobody is getting decision quality for. Switch it on where a
    reviewer-facing narrative is worth the wait.
    """
    return (
        os.getenv("LOS_LLM_SUMMARY_ENABLED", "false") or "false"
    ).strip().lower() == "true"


def llm_timeout_seconds() -> float:
    """
    How long to wait for one summary sentence.

    A FAST-FAIL BUDGET, not a generation allowance. This bounds the case the
    connect probe cannot catch: a provider whose socket accepts and whose
    model then hangs -- an Ollama running without the configured model
    pulled, or a reasoning model that thinks for ten seconds. The probe
    reports it reachable, correctly, and only this timeout ends the wait.

    Measured on a real four-document request against a slow local model, the
    old four-second budget cost 4.3 seconds of pure waiting on every request
    that fell outside the unavailable-cache window -- for a sentence the
    deterministic path writes in under a millisecond.

    1.5 seconds is the budget an OPTIONAL final-stage nicety gets on a
    synchronous API. A model that cannot produce one sentence in that time is
    not going to be part of this response. Overrunning costs nothing but the
    sentence: the deterministic summary is already written, and every
    decision -- classification, verification, extraction, KYC, the
    cross-document checks, the decision and the next action -- was final
    before the model was consulted and never consults it.

    Raise LOS_LLM_SUMMARY_TIMEOUT_SECONDS where a generated narrative is
    worth the wait; a batch or reviewer-facing path is a fair place to.
    """
    try:
        return float(os.getenv("LOS_LLM_SUMMARY_TIMEOUT_SECONDS", "1.5"))
    except ValueError:
        return 1.5


# ---------------------------------------------------------------------------
# Deterministic summary
# ---------------------------------------------------------------------------

def _document_sentence(envelope: dict[str, Any]) -> str:
    document = envelope.get("document") or {}
    doc_type = document.get("type") or "UNKNOWN"
    status = envelope.get("status") or "UNKNOWN"
    verification = (envelope.get("verification") or {}).get("status")
    extraction = envelope.get("extraction")

    if doc_type == "UNKNOWN":
        opening = "Document could not be identified"
    else:
        opening = f"{doc_type.replace('_', ' ').title()} identified"

    parts = [opening]

    if verification:
        parts.append(f"verification {verification}")

    if extraction is None:
        parts.append("no fields returned")
    else:
        count = len(extraction.get("fields") or {})
        parts.append(f"{count} field(s) extracted")

    return f"{'; '.join(parts)}. Overall {status}."


def _kyc_sentence(kyc: dict[str, Any] | None) -> str:
    if not kyc:
        return ""

    status = kyc.get("status")
    reasons = kyc.get("reason_codes") or []

    if not status:
        return ""

    if status == "PASS":
        return " Cross-document KYC checks passed."

    # Name the documents that disagree, and on which field. "KYC REVIEW:
    # name mismatch" tells a reviewer to re-read the whole bundle; "name
    # differs across pan.jpg, dl.jpg" tells them where to look.
    disagreements = [
        check for check in (kyc.get("checks") or [])
        if str(check.get("status") or "").upper() in {"FAIL", "REVIEW"}
    ]

    if disagreements:
        clauses = []
        for check in disagreements[:3]:
            field = str(check.get("check") or "").replace("_", " ").lower()
            sources = list(check.get("source_ids") or [])
            if sources:
                clauses.append(f"{field} differs across {', '.join(sources)}")
            else:
                clauses.append(f"{field} could not be reconciled")

        return f" KYC {status}: " + "; ".join(clauses) + "."

    if reasons:
        readable = ", ".join(str(r).replace("_", " ").lower() for r in reasons[:4])
        return f" KYC {status}: {readable}."

    return f" KYC {status}."


def _party_kyc_sentence(envelope: dict[str, Any]) -> str:
    """
    Which PERSON needs attention, on a case carrying two of them.

    WHY THE CASE-LEVEL SENTENCE IS NOT ENOUGH. KYC is scoped per party,
    so a case-level roll-up says "KYC REVIEW: name differs across
    rpan.jpg, dl1.jpg; name differs across lpan.jpg, dl2.jpg" -- four
    filenames and no indication that two of them are a different
    person. A reviewer opening the case has to work out who is who from
    the filenames before they can act.

    Falls back to the case-level sentence when there is only one party,
    so a single-applicant summary is exactly what it always was.
    """
    per_party = envelope.get("party_kyc") or {}
    if len(per_party) < 2:
        return ""

    roles = {
        str(envelope.get("applicant_id") or ""): "Primary applicant",
        str(envelope.get("co_applicant_id") or ""): "Co-applicant",
    }

    clauses = []
    for party_id, payload in per_party.items():
        name = roles.get(str(party_id)) or str(party_id)
        status = str((payload or {}).get("status") or "")

        if not status or status == "SKIPPED":
            clauses.append(f"{name} KYC was not run")
        elif status == "PASS":
            clauses.append(f"{name} KYC passed")
        else:
            detail = _reason_phrase(payload.get("reason_codes") or [])
            verdict = ("requires review" if status == "REVIEW"
                       else status.lower())
            clauses.append(f"{name} KYC {verdict}{detail}")

    return " " + ". ".join(clauses) + "."


#: How a reason code reads in a sentence. Anything not listed is
#: rendered from the code itself rather than dropped -- an unnamed
#: reason is still a reason a reviewer needs.
_REASON_WORDS = {"DOB": "DOB"}


def _reason_phrase(codes: list[Any]) -> str:
    """
    ": DOB, father name and name mismatch", or nothing.

    Mismatch codes are collapsed into one list with a single trailing
    "mismatch", because "DOB mismatch, father name mismatch and name
    mismatch" says the word three times to no purpose. Codes that are
    not mismatches keep their own wording.
    """
    mismatches: list[str] = []
    others: list[str] = []

    for code in codes[:4]:
        text = str(code).strip().upper()
        if text.endswith("_MISMATCH"):
            stem = text[: -len("_MISMATCH")]
            mismatches.append(_REASON_WORDS.get(stem,
                                                stem.replace("_", " ").lower()))
        else:
            others.append(text.replace("_", " ").lower())

    parts: list[str] = []
    if mismatches:
        parts.append(_and_list(mismatches) + " mismatch")
    parts.extend(others)

    return f": {_and_list(parts)}" if parts else ""


def _and_list(items: list[str]) -> str:
    """a, b and c."""
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def deterministic_summary(envelope: dict[str, Any]) -> str:
    """
    A correct one-line summary built from the envelope alone.

    Always available, always consistent with the structured result, and the
    fallback whenever the model is off, unavailable or rejected.
    """
    documents = envelope.get("documents")

    if isinstance(documents, list):
        total = len(documents)
        by_status: dict[str, int] = {}
        for item in documents:
            key = str(item.get("status", "UNKNOWN"))
            by_status[key] = by_status.get(key, 0) + 1

        breakdown = ", ".join(
            f"{count} {status.lower()}" for status, count in sorted(by_status.items())
        )
        head = (
            f"{total} document(s) processed ({breakdown})."
            if breakdown else f"{total} document(s) processed."
        )
        # PARTY-AWARE WHERE THERE ARE TWO PARTIES. Naming the person
        # rather than the filenames is what makes the sentence
        # actionable on a joint application.
        kyc_sentence = (_party_kyc_sentence(envelope)
                        or _kyc_sentence(envelope.get("kyc")))

        return (head + kyc_sentence
                + f" Overall {envelope.get('status', 'UNKNOWN')}.").strip()

    return (_document_sentence(envelope) + _kyc_sentence(envelope.get("kyc"))).strip()


# ---------------------------------------------------------------------------
# Generated summary
# ---------------------------------------------------------------------------

def build_llm_payload(envelope: dict[str, Any]) -> dict[str, Any]:
    """
    The only facts the model is shown.

    Deliberately narrow: statuses, counts and reason codes. Extracted values
    are NOT included -- the model has no reason to see a customer's PAN or
    date of birth to write one sentence about whether the checks passed, and
    what it is never shown it cannot leak.
    """
    document = envelope.get("document") or {}
    extraction = envelope.get("extraction")
    kyc = envelope.get("kyc") or {}

    payload: dict[str, Any] = {
        "status": envelope.get("status"),
        "document_type": document.get("type"),
        "category": document.get("category"),
        "verification_status": (envelope.get("verification") or {}).get("status"),
        "field_count": 0 if extraction is None else len(extraction.get("fields") or {}),
        "error_codes": [e.get("code") for e in (envelope.get("errors") or [])],
    }

    if kyc:
        payload["kyc_status"] = kyc.get("status")
        payload["kyc_reason_codes"] = kyc.get("reason_codes") or []

    documents = envelope.get("documents")
    if isinstance(documents, list):
        payload["document_count"] = len(documents)
        payload["document_statuses"] = [d.get("status") for d in documents]

        # THE COUNTS, WORKED OUT HERE RATHER THAN BY THE MODEL.
        #
        # Given only a list of statuses, the model summarising a mixed bundle
        # writes "3 succeeded, 1 rejected" -- correct, derivable, and rejected
        # by validate_llm_summary, because 3 and 1 appear nowhere in what it
        # was shown. The whole sentence was then discarded and the summary
        # fell back to deterministic. Measured on a four-document envelope,
        # that was 5 rejections in 8.
        #
        # These are the same statuses one line above, counted. No new
        # information reaches the model, and nothing here is a value from a
        # document -- it stays statuses and counts.
        counts: dict[str, int] = {}
        for document in documents:
            key = str(document.get("status") or "UNKNOWN")
            counts[key] = counts.get(key, 0) + 1
        payload["document_status_counts"] = counts

    return payload


def _allowed_numbers(payload: dict[str, Any]) -> set[str]:
    """Every numeric token the model is permitted to reproduce."""
    allowed: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            allowed.add(str(node))
            if isinstance(node, float) and node.is_integer():
                allowed.add(str(int(node)))
        elif isinstance(node, str):
            for token in _NUMBER.findall(node):
                allowed.add(token)

    walk(payload)
    return allowed


def validate_llm_summary(
    text: str,
    envelope: dict[str, Any],
) -> tuple[bool, str]:
    """
    Check a generated summary before it is allowed anywhere near a response.

    Returns (accepted, cleaned_text_or_reason). A summary that fails for any
    reason is discarded whole; nothing is salvaged from it.
    """
    if not isinstance(text, str):
        return False, "summary was not a string"

    cleaned = _THINK_BLOCK.sub("", text).strip().strip('"').strip()

    if len(cleaned) < MIN_SUMMARY_CHARS:
        return False, "summary too short"
    if len(cleaned) > MAX_SUMMARY_CHARS:
        return False, "summary too long"
    if cleaned.lstrip().startswith(("{", "[")):
        return False, "summary returned structured data"

    payload = build_llm_payload(envelope)
    allowed = _allowed_numbers(payload)

    for token in _NUMBER.findall(cleaned):
        variants = {token}
        if "." in token:
            variants.add(token.rstrip("0").rstrip("."))
        if not (variants & allowed):
            return False, f"summary contained unsupported number: {token}"

    # The model must not assert an outcome other than the computed one.
    #
    # EVERY computed one, which includes the per-document statuses. Those are
    # in the payload the model is handed, as `document_statuses`, so a
    # sentence saying "one document was REJECTED" is quoting the data rather
    # than inventing a verdict -- and was being discarded for it. On a
    # four-document bundle that false rejection fired 5 times in 8.
    #
    # This does not loosen the check. A verdict the deterministic pipeline
    # did not produce is still refused; the set now simply matches what the
    # pipeline actually computed and showed the model.
    upper = cleaned.upper()
    computed = {
        str(payload.get("status") or "").upper(),
        str(payload.get("verification_status") or "").upper(),
        str(payload.get("kyc_status") or "").upper(),
    }
    computed.update(
        str(status or "").upper()
        for status in (payload.get("document_statuses") or [])
    )
    for verdict in _VERDICTS:
        if re.search(rf"\b{verdict}\b", upper) and verdict not in computed:
            return False, f"summary asserted an uncomputed verdict: {verdict}"

    # ON A JOINT APPLICATION THE SENTENCE MUST SAY WHOSE.
    #
    # Everything above checks that the model did not INVENT anything. It
    # cannot check that the model said enough, and on a two-party case
    # "enough" is a contract requirement rather than a nicety: a reviewer
    # has to know WHICH of two people needs attention before they can do
    # anything. The model wrote "Loan officer review shows all documents
    # except Kyc status as successful, with multiple Kyc reason codes
    # noted" -- true, harmless, and useless for that.
    #
    # The deterministic sentence always says whose, so the bar here is
    # simply that a generated one must too. A model that learns to name
    # both parties and their outcomes is still allowed to win.
    missing = _unnamed_parties(cleaned, envelope)
    if missing:
        return False, f"summary did not identify: {', '.join(missing)}"

    return True, cleaned


#: How each party is named in a sentence a reviewer reads.
_PARTY_WORDS = {
    "applicant_id": ("primary applicant", "applicant"),
    "co_applicant_id": ("co-applicant", "coapplicant", "co applicant"),
}


def _unnamed_parties(text: str, envelope: dict[str, Any]) -> list[str]:
    """
    Which parties a generated summary failed to account for.

    Empty on a single-applicant case: there is only one person, the
    existing deterministic sentence never named them either, and
    requiring it would reject every summary that works today.

    A party is accounted for when the sentence names them AND states an
    outcome for them -- naming somebody and saying nothing about them is
    the same omission in a longer sentence.
    """
    per_party = envelope.get("party_kyc") or {}
    if len(per_party) < 2:
        return []

    lowered = text.lower()
    missing: list[str] = []

    for field, words in _PARTY_WORDS.items():
        if not envelope.get(field):
            continue
        named = any(word in lowered for word in words)
        if not named:
            missing.append(words[0])

    # An outcome word has to appear for each party, not once overall.
    if not missing and len(_OUTCOME_WORD.findall(lowered)) < len(per_party):
        missing.append("an outcome for each party")

    return missing


#: Words a sentence uses to state a party's KYC outcome.
_OUTCOME_WORD = re.compile(
    r"\b(pass(?:ed|es)?|review|requires review|fail(?:ed|s)?|"
    r"mismatch(?:es)?|skipped|not run|clear(?:ed)?)\b"
)


# THE PROMPT IS A LATENCY CONTROL AS WELL AS AN INSTRUCTION.
#
# Generation time on CPU is dominated by how many tokens come out, and this
# model runs at roughly 17 tokens/second here. The previous wording allowed
# "one or two sentences" and produced a median of 21 output tokens on a
# two-document application -- around 1.2s of generation alone, most of a 1.5s
# budget before any HTTP or framework overhead.
#
# Measured through the production client, 8 generations across each of five
# envelope shapes (single PASS, single PARTIAL, two-document mismatch,
# four-document mixed, rejected):
#
#     prompt                     accepted   median    p95    in budget
#     previous wording             40/40    1379ms  2081ms     25/40
#     "one short sentence" only    31/40     994ms  1233ms     40/40
#     this wording                 40/40    1112ms  1518ms     37/40
#
# THE MIDDLE ROW IS WHY THIS PROMPT IS NOT SHORTER. Simply demanding brevity
# was the fastest option and the wrong one: on an application whose document
# passed but whose KYC wanted more sources, the model wrote "Document
# verification for loan failed" -- an outcome that had not happened. It was
# caught (11 times in 12) and discarded, so the speed bought nothing and the
# summary fell back anyway. Naming the fields to lean on, and forbidding
# outcome words the data does not use, is what restored fidelity.
#
# The instruction not to derive its own counts is here for the same reason:
# asked to describe a mixed bundle the model wrote "3 succeeded, 1 rejected",
# and those derived numbers appear nowhere in what it was shown, so the whole
# sentence was discarded. It is handed the counts instead -- see
# `document_status_counts` in build_llm_payload.
_SYSTEM_PROMPT = (
    "Summarise an already-decided document check for a loan officer in ONE "
    "sentence of at most 25 words. You are not deciding anything.\n"
    "- Plain prose. No markup, no JSON, no lists.\n"
    "- Use the given status, kyc_status and document_status_counts. Never "
    "call anything failed, passed, rejected or approved unless the data "
    "uses that exact word.\n"
    "- State no number that is not in the data. Do not work out your own "
    "counts.\n"
    "- No causes, no advice, no preamble."
)


def _messages(payload: dict[str, Any]) -> list[Any]:
    """
    The system and user turns, built for the shared client.

    Serialised compactly rather than with indent=2. The indentation was worth
    about 40 prompt tokens on a multi-document payload, and prompt tokens are
    read at the same rate everything else is.
    """
    import json

    from agent_framework import Message

    return [
        Message(role="system", contents=[_SYSTEM_PROMPT]),
        Message(role="user", contents=[
            json.dumps(payload, separators=(",", ":"), default=str)
        ]),
    ]



def summary_max_tokens() -> int:
    """
    A hard ceiling on the sentence the model may write.

    THE REASON THE BUDGET IS ACHIEVABLE. Generation time is dominated by how
    many tokens come out, and an uncapped model writes until it decides to
    stop -- measured at 1,599 ms for a summary that only ever needed one
    sentence. This is the alternative to raising the timeout: bound the work
    rather than the patience.

    The default was chosen by measuring generation WHERE IT ACTUALLY RUNS --
    immediately after OCR, on a CPU those workers have just saturated, which
    is materially slower than an idle benchmark:

        cap   median    max     (budget 1500 ms)
         48   1273 ms   7069 ms   unstable
         64   1194 ms   1200 ms   chosen
         80   1442 ms   1536 ms   over budget at the tail

    A truncated sentence would be rejected by validate_llm_summary and fall
    back deterministically, so the cap can only cost the sentence, never
    correctness.
    """
    try:
        return max(16, int(os.getenv("LOS_LLM_SUMMARY_MAX_TOKENS", "64")))
    except ValueError:
        return 64


def keep_alive() -> str:
    """
    How long Ollama should hold the model in memory after a call.

    THE COLD-LOAD PROBLEM. Ollama unloads an idle model after five minutes by
    default. Loading qwen2.5:3b back costs 2281 ms here -- measured, on its
    own, before a single token is generated. That alone overruns the 1.5 s
    budget, and the overrun then marks the provider unavailable for the whole
    cooldown, so every request in the next minute falls back too. One quiet
    stretch therefore costs far more than one summary.

    Startup warms the model once; this keeps it warm. The cost is the model's
    resident memory (roughly 2 GB) for the configured window.

    Set LOS_LLM_KEEP_ALIVE to "0" to restore Ollama's unload-immediately
    behaviour, or to "-1" to hold the model indefinitely.
    """
    return (os.getenv("LOS_LLM_KEEP_ALIVE") or "30m").strip() or "30m"


def _generation_options() -> dict[str, Any]:
    """
    Options for the one summary call.

    Low temperature because this is a restatement of computed facts, not
    creative writing: the same application should describe itself the same
    way twice.

    A plain dict rather than ChatOptions: `keep_alive` is an Ollama top-level
    parameter that ChatOptions has no field for, and OllamaChatClient accepts
    a mapping and routes each key to the right place -- max_tokens and
    temperature into the nested model options, keep_alive alongside them.
    Verified against agent_framework_ollama 1.0.0b260813.
    """
    return {
        "max_tokens": summary_max_tokens(),
        "temperature": 0.2,
        "keep_alive": keep_alive(),
    }


async def _agenerate(payload: dict[str, Any]) -> str:
    """
    One model call. Raises on any failure; the caller falls back.

    Goes through app.llm.provider.create_ollama_client() so host and model
    configuration live in one place for the whole service.
    """
    import asyncio

    from app.llm import availability
    from app.llm.provider import create_ollama_client

    # Fail fast when the provider is not listening.
    #
    # Without this the generation timeout was the only bound, so an absent
    # Ollama cost the FULL timeout on every request -- about 8 seconds added
    # to a 2.7 second call, for a sentence the deterministic path writes for
    # free. The probe bounds the failure case in milliseconds and leaves the
    # success case untouched, and its verdict is cached so a ten-document
    # application does not pay it ten times over.
    if not availability.provider_reachable():
        raise ConnectionError("model provider is not reachable")

    client = create_ollama_client()

    try:
        response = await asyncio.wait_for(
            client.get_response(
                _messages(payload), stream=False, options=_generation_options()
            ),
            timeout=llm_timeout_seconds(),
        )
    except Exception:
        # A failure the probe could not predict: the socket accepted and the
        # model then hung or errored. Recorded so the NEXT request skips it
        # rather than rediscovering it the expensive way.
        #
        # mark_slow, NOT mark_unavailable. The provider answered -- late, or
        # badly -- which is a different thing from the provider being gone,
        # and it earns a much shorter hold-off. Measured before this
        # distinction existed: one request overran the budget by 3 ms and the
        # 60-second unreachable cooldown then suppressed the model on the
        # next fifteen requests, all of which had time to spare.
        #
        # Exactly one attempt per request. There is no retry here and there
        # must not be one -- the caller already holds a correct answer, and
        # retrying spends a reviewer's latency to maybe improve a sentence.
        availability.mark_slow("generation call failed")
        raise

    text = getattr(response, "text", None)
    if not isinstance(text, str):
        raise ValueError("model response carried no text")
    return text


def _generate(payload: dict[str, Any]) -> str:
    """
    Synchronous form, for callers already off the event loop.

    The client is async-only, so this drives it on its own loop. That is safe
    here because the only sync caller is the document workflow, which runs on
    the document executor -- a worker thread with no loop of its own. Called
    where a loop IS running, asyncio.run raises and the caller falls back to
    the deterministic summary rather than deadlocking.
    """
    import asyncio

    return asyncio.run(_agenerate(payload))


def build_summary(
    envelope: dict[str, Any],
    use_llm: bool | None = None,
) -> tuple[str, str]:
    """
    Return (summary, source) where source is "deterministic" or "llm".

    Never raises and never blocks a decision: the deterministic sentence is
    computed first, and the generated one has to earn its place by passing
    validation.
    """
    fallback = deterministic_summary(envelope)

    enabled = llm_enabled() if use_llm is None else use_llm
    if not enabled:
        return fallback, "deterministic"

    try:
        raw = _generate(build_llm_payload(envelope))
    except Exception as exc:
        logger.warning(
            "LOS summary model unavailable (%s: %s); using deterministic summary.",
            type(exc).__name__, exc,
        )
        return fallback, "deterministic"

    accepted, value = validate_llm_summary(raw, envelope)
    if not accepted:
        logger.warning("LOS summary rejected (%s); using deterministic summary.", value)
        return fallback, "deterministic"

    return value, "llm"



async def warmup() -> float:
    """
    Load the summary model before the first request needs it.

    Ollama unloads an idle model, so the first generation after a quiet
    period pays the load and blows the 1.5 second budget -- which then marks
    the provider unavailable for the cooldown, and EVERY request in that
    window falls back too. Measured on a fresh server: one TimeoutError
    followed by twelve requests that skipped the model entirely.

    The same reason the OCR engines are warmed at startup, and the same
    shape: a throwaway call off the request path.

    Never raises and never marks the provider unavailable -- a warm-up that
    failed must not poison the cooldown for real traffic. Returns the
    milliseconds spent.
    """
    import time

    started = time.perf_counter()

    try:
        from agent_framework import Message

        from app.llm.provider import create_ollama_client

        client = create_ollama_client()
        await asyncio.wait_for(
            client.get_response(
                [Message(role="user", contents=["ok"])],
                stream=False,
                options=_generation_options(),
            ),
            timeout=max(30.0, llm_timeout_seconds() * 10),
        )
    except Exception as exc:
        logger.info("LLM warmup skipped (%s: %s)", type(exc).__name__, exc)

    return (time.perf_counter() - started) * 1000


async def build_summary_async(
    envelope: dict[str, Any],
    use_llm: bool | None = None,
) -> tuple[str, str]:
    """
    Async form of build_summary, for callers on the event loop.

    The deterministic sentence is computed first and is what gets returned
    unless a generated one passes validation, so the decision path never waits
    on a model to produce a correct answer -- only on a nicer one.
    """
    fallback = deterministic_summary(envelope)

    enabled = llm_enabled() if use_llm is None else use_llm
    if not enabled:
        return fallback, "deterministic"

    try:
        raw = await _agenerate(build_llm_payload(envelope))
    except Exception as exc:
        logger.warning(
            "LOS summary model unavailable (%s: %s); using deterministic summary.",
            type(exc).__name__, exc,
        )
        return fallback, "deterministic"

    accepted, value = validate_llm_summary(raw, envelope)
    if not accepted:
        logger.warning("LOS summary rejected (%s); using deterministic summary.", value)
        return fallback, "deterministic"

    return value, "llm"


__all__ = [
    "build_summary", "build_summary_async", "deterministic_summary",
    "validate_llm_summary", "build_llm_payload", "llm_enabled",
    "llm_timeout_seconds", "keep_alive", "summary_max_tokens", "warmup",
]
