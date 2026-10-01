"""
THE NATURAL RESPONSE LAYER: a response PLAN for every answer, and -- only
behind CHATBOT_NATURAL_COMPOSITION -- an optional model rewording of it.

    STRUCTURED TRUTH (the deterministic answer, already authorized/masked)
      + CONTEXT (turn type) + USER LANGUAGE + RESPONSE GOAL (the plan)
        -> NATURAL RESPONSE PLANNER            (always; deterministic)
        -> OPTIONAL QWEN                       (flag; eligible plans only)
        -> FIDELITY                            (every fact must survive)
        -> the existing PRIVACY guard          (guardrails.published)

THE MODEL CONTRACT. The model receives ONLY the answer this service already
decided to give -- authorized, masked, relevant fields only -- plus the
question, the user's language and the plan. No record, no tool, no id it was
not already going to be shown. It may change STYLE. It cannot change TRUTH:
every number, identifier, email, date, code, status, negation, party and
name of the deterministic answer must appear in the reworded one, and nothing
factual may appear that the deterministic answer did not carry. Anything
else is DISCARDED and the deterministic answer is published unchanged.

NEVER COMPOSED: refusals, clarifications, security, IDs and single fields,
KYC truth, verification truth, document state, a recorded reason (reported,
never paraphrased -- see agent.py). Composed only where wording helps: a
knowledge explanation, a summary, an expansion, the next step.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

FLAG = "CHATBOT_NATURAL_COMPOSITION"

#: Response plans.
ANSWER_ONLY, EXPLANATION, NEXT_STEP, SUMMARY, EXPANSION, KNOWLEDGE = (
    "ANSWER_ONLY", "ANSWER_EXPLANATION", "ANSWER_NEXT_STEP", "CONCISE_SUMMARY",
    "PROGRESSIVE_EXPANSION", "KNOWLEDGE_EXPLANATION")
REFUSAL, CLARIFICATION, CONVERSATION = "REFUSAL", "CLARIFICATION", "CONVERSATION"

#: Plans the model may reword (when the flag is on).
_COMPOSABLE = frozenset({KNOWLEDGE, SUMMARY, EXPANSION, NEXT_STEP})
#: Intents whose truth is never reworded, whatever the plan.
_NEVER = frozenset({"GUARDRAIL_BLOCKED", "KYC_RESULT", "DOCUMENT_VERIFICATION", "DOCUMENTS_UPLOADED",
                    "CASE_HISTORY", "APPLICATION_STATUS", "UNKNOWN", "GREETING", "HELP",
                    "HUMAN_HANDOFF_REQUESTED", "OUT_OF_SCOPE"})


def enabled() -> bool:
    return str(os.getenv(FLAG, "false")).strip().lower() in ("1", "true", "yes", "on")


def plan(response: dict[str, Any]) -> str:
    """What SHAPE of answer this turn needs -- decided from the turn, not the words."""
    intent = str(response.get("intent") or "")
    if intent == "GUARDRAIL_BLOCKED" or response.get("guardrail"):
        return REFUSAL
    if response.get("clarification_required"):
        return CLARIFICATION
    if str(response.get("category") or "") == "CONVERSATION":
        return CONVERSATION
    conversation = ((response.get("understanding") or {}).get("conversation") or {})
    note = str(conversation.get("note") or "")
    if intent in ("FOS_KNOWLEDGE", "STAGE_PROCESS"):
        return KNOWLEDGE
    if "expanded" in note:
        return EXPANSION
    if intent in ("NEXT_ACTION", "PENDING_ITEMS"):
        return NEXT_STEP
    if intent in ("FULL_SUMMARY", "APPLICANT_DETAILS"):
        return SUMMARY
    if intent in ("CASE_HISTORY", "READINESS"):
        return EXPLANATION
    return ANSWER_ONLY


# ---- fidelity ------------------------------------------------------------------
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_MONEY_OR_NUMBER = re.compile(r"(?:₹\s?)?\d[\d,]*(?:\.\d+)?%?")
_CODE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b|\b[A-Z]{2,}[0-9][A-Z0-9-]*\b|\b[\w-]*\d[\w-]*[A-Za-z][\w-]*\b")
_MONTHS = ("January|February|March|April|May|June|July|August|September|October|November|December|"
           "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec")
_DATE = re.compile(rf"\b\d{{1,2}}\s+(?:{_MONTHS})\s+\d{{4}}\b")
_STATUS = re.compile(r"\b(verified|rejected|under\s+review|in\s+review|missing|pending|approved|declined|"
                     r"passed|failed|mandatory|optional|required|blocked|satisfied|uploaded|outstanding|"
                     r"accepted|not\s+accepted|recorded|not\s+recorded|provided|not\s+provided)\b", re.I)
_NEGATION = re.compile(r"\b(not|no|never|none|nothing|cannot|can't|isn't|aren't|hasn't|haven't|doesn't|"
                       r"don't|won't|nahi|nahin)\b|n't\b", re.I)
_PARTY = re.compile(r"\bco-?\s?applicant\b|\bprimary\s+applicant\b", re.I)
_ALLOWED_CAPS = frozenset("""
Sure Yes No The It Its It's This That These Those Your You You've You're Here There A An In On For And But
So Also Only If When As To Of At By With From Right Okay OK Of course Currently Both Each Every
Haan Ji Aapka Aapki Aapke Aap Hai Theek
""".split())


def _numbers(text: str) -> set[str]:
    return {re.sub(r"[₹,\s]", "", m) for m in _MONEY_OR_NUMBER.findall(text) if re.search(r"\d", m)}


def _facts(text: str) -> dict[str, set[str]]:
    return {
        "emails": {e.lower() for e in _EMAIL.findall(text)},
        "numbers": _numbers(_EMAIL.sub(" ", text)),
        "codes": set(_CODE.findall(_EMAIL.sub(" ", text))),
        "dates": set(_DATE.findall(text)),
        "statuses": {re.sub(r"\s+", " ", s.lower()) for s in _STATUS.findall(text)},
        "party": {p.lower().replace(" ", "-") for p in _PARTY.findall(text)},
    }


def fidelity(source: str, candidate: str) -> list[str]:
    """Every fact of `source` kept, nothing factual added. [] means faithful."""
    problems: list[str] = []
    want, got = _facts(source), _facts(candidate)
    for kind in ("emails", "numbers", "codes", "dates"):
        if want[kind] - got[kind]:
            problems.append(f"dropped {kind}")
        if got[kind] - want[kind]:
            problems.append(f"added {kind}")
    if want["statuses"] != got["statuses"]:
        problems.append("status changed")
    if want["party"] != got["party"]:
        problems.append("party changed")
    if bool(_NEGATION.search(source)) != bool(_NEGATION.search(candidate)):
        problems.append("negation changed")
    # every NAMED term of the truth survives ("Driving Licence", "Voter ID")
    candidate_lower = candidate.lower()
    for sentence in re.split(r"(?<=[.!?])\s+", source):
        for i, word in enumerate(re.findall(r"[A-Za-z][\w'-]*", sentence)):
            if (i or word.isupper()) and word[:1].isupper() and word not in _ALLOWED_CAPS \
                    and word.lower() not in candidate_lower:
                problems.append(f"dropped name {word!r}")
                break
    # a restriction or a generalisation the truth did not make
    restrictive = re.compile(r"\b(only|just|except|always|every|all|any|must|guaranteed?)\b", re.I)
    if {m.lower() for m in restrictive.findall(candidate)} - {m.lower() for m in restrictive.findall(source)}:
        problems.append("scope changed")
    source_words = {w.lower() for w in re.findall(r"[A-Za-z][\w'-]*", source)}
    for sentence in re.split(r"(?<=[.!?])\s+", candidate):
        for i, word in enumerate(re.findall(r"[A-Za-z][\w'-]*", sentence)):
            # a sentence may open with a common word; any other capitalised
            # word -- first or not -- must come from the truth
            if word[:1].isupper() and word not in _ALLOWED_CAPS and word.lower() not in source_words:
                problems.append(f"added name {word!r}")
                break
    if len(candidate) > max(int(len(source) * 1.6), len(source) + 120):
        problems.append("longer than the truth")
    if "Source:" in candidate or re.search(r"\b\w+/\w+\.(py|ya?ml|json|md)\b", candidate):
        problems.append("internal detail")
    # EVERY CLAUSE OF THE TRUTH SURVIVES: each source sentence keeps most of
    # its content words -- "None of them is mandatory here" cannot be dropped
    # from an answer and the rest kept (seen in the A/B run).
    candidate_stems = {w.lower()[:5] for w in re.findall(r"[A-Za-z][\w'-]*", candidate)}
    for sentence in re.split(r"(?<=[.!?;])\s+", source):
        content = [w.lower() for w in re.findall(r"[A-Za-z][\w'-]*", sentence)
                   if len(w) >= 4 and w.lower() not in _STOP]
        if len(content) >= 3:
            kept = sum(1 for w in content if w[:5] in candidate_stems)
            if kept / len(content) < 0.6:
                problems.append("dropped a clause")
                break
    return problems


_STOP = frozenset("""
that this these those with from have been will would could should there their them they then than
what when where which while into onto over under about also only just very such some more most
each every other your yours here were does done being
""".split())


# ---- composition ----------------------------------------------------------------
_SYSTEM = (
    "You reword an assistant's answer for a loan field officer so it reads naturally, like a helpful "
    "colleague. The ANSWER you are given is the complete truth. Keep EVERY number, amount, date, "
    "identifier, email, name, status, party and negation exactly as written. Add no fact, no advice, "
    "no field, no greeting, no emoji. Do not repeat the question. Reply in the requested language. "
    "Return only the reworded answer."
)
_INSTRUCTION = {
    KNOWLEDGE: "Explain it plainly in one or two sentences.",
    SUMMARY: "Give it as a short, natural summary.",
    EXPANSION: "Present it as the next level of detail on the same topic.",
    NEXT_STEP: "Say what to do next, directly.",
}
_LANGUAGE = {"en": "English", "hi": "Hindi (Devanagari)", "hi-Latn": "Hinglish (Hindi in Latin script)",
             "mr": "Marathi"}


def _timeout(config) -> float:
    """CHATBOT_COMPOSITION_TIMEOUT_S, else the agent's model budget."""
    try:
        return float(os.getenv("CHATBOT_COMPOSITION_TIMEOUT_S") or config.llm_timeout_seconds())
    except ValueError:
        return config.llm_timeout_seconds()


async def _reword(body: str, question: str, plan_name: str, language: str) -> str | None:
    """One bounded model call. None on any failure (the deterministic answer stands)."""
    try:
        import asyncio

        from agent_framework import Message

        from app.agents.applicant import config
        from app.llm import availability
        from app.llm.provider import create_ollama_client
        from app.security import guardrails

        if not availability.provider_reachable():
            return None
        client = create_ollama_client()
        response = await asyncio.wait_for(
            client.get_response(
                [Message(role="system", contents=[_SYSTEM + " " + guardrails.UNTRUSTED_NOTICE]),
                 Message(role="user", contents=[
                     f"Language: {_LANGUAGE.get(language, 'English')}\n"
                     f"Style: {_INSTRUCTION.get(plan_name, '')}\n"
                     f"Question: {guardrails.untrusted(question)}\n"
                     f"ANSWER: {body}"])],
                stream=False,
                options={"max_tokens": 160, "temperature": 0.2},
            ),
            timeout=_timeout(config),
        )
        text = getattr(response, "text", None)
        if not isinstance(text, str):
            return None
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip().strip('"')
        return text or None
    except Exception:  # noqa: BLE001 - composition is optional; never costs the answer
        logger.info("natural composition unavailable; deterministic answer kept")
        return None


async def finish(response: dict[str, Any], question: str) -> dict[str, Any]:
    """Attach the plan; when enabled and eligible, try a faithful natural rewording."""
    plan_name = plan(response)
    record: dict[str, Any] = {"plan": plan_name, "enabled": enabled(), "attempted": False,
                              "accepted": False, "reason": None, "ms": 0.0}
    understanding = response.get("understanding")
    if isinstance(understanding, dict):
        understanding["response_plan"] = plan_name
        understanding["natural_composition"] = record
    answer = response.get("answer")
    intent = str(response.get("intent") or "")
    if not enabled():
        record["reason"] = "FLAG_OFF"
        return response
    # ONE MODEL CALL PER REQUEST. An answer the model already wrote, or a turn
    # where the model was already asked (to phrase, or to read the question),
    # is not handed to it again: measured, the second call doubled the latency
    # (2.2 s + a 2.5 s timeout) to reword the model's own sentence.
    u = understanding if isinstance(understanding, dict) else {}
    if str(response.get("response_source") or "") == "LLM"             or (u.get("model_routing") or {}).get("phrased_by_model")             or (u.get("llm") or {}).get("consulted"):
        record["reason"] = "MODEL_ALREADY_USED"
        return response
    if plan_name not in _COMPOSABLE or intent in _NEVER or not isinstance(answer, str) or not answer.strip()             or "don't have enough information" in answer:          # an honest no-answer is said as is
        record["reason"] = "NOT_ELIGIBLE"
        return response
    body, sep, citation = answer.partition("\n\nSource:")
    frame = ((understanding or {}).get("frame") or {}) if isinstance(understanding, dict) else {}
    language = str(frame.get("language") or "en")
    started = time.perf_counter()
    record["attempted"] = True
    # SIZES ONLY: what the model was given and wrote (a rejected candidate's
    # TEXT is never published -- it may carry exactly the fact that was rejected)
    record["input_chars"] = len(_SYSTEM) + len(body.strip()) + len(question or "")
    candidate = await _reword(body.strip(), question, plan_name, language)
    record["ms"] = round((time.perf_counter() - started) * 1000, 1)
    record["output_chars"] = len(candidate or "")
    if candidate is None:
        record["reason"] = "MODEL_UNAVAILABLE"
        return response
    problems = fidelity(body, candidate)
    # A REWORDING THAT CANNOT BE VERIFIED IS NOT PUBLISHED. The truth is
    # checked word by word; text in another script (or a language other than
    # the one the truth is written in) cannot be, so it is discarded -- the
    # A/B run showed a Hinglish question reworded into unrelated Devanagari.
    if language != "en" or re.search(r"[ऀ-ॿ]", candidate):
        problems.append("unverifiable language")
    if problems:
        record["reason"] = "FIDELITY_REJECTED: " + "; ".join(problems[:3])
        return response
    record["accepted"] = True
    record["reason"] = "COMPOSED"
    response["answer"] = candidate + (sep + citation if sep else "")
    return response


__all__ = ["FLAG", "enabled", "fidelity", "finish", "plan"]
