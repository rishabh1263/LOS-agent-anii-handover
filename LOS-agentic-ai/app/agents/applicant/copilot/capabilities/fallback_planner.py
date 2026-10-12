"""
BOUNDED FALLBACK PLANNER (2026-10-11, audit P2-K): when the deterministic route cannot place a typed question on an
open case (the engine's intent is UNKNOWN), the local model may PROPOSE up to `max_calls` read-only lookups; the
deterministic engine answers each one exactly as if its canonical question had been typed.

The model never writes the answer and never reaches a tool directly. Each proposed call is validated before anything
runs, and ONE invalid call rejects the whole plan:

    tool        one of `planner.tools` (catalogue case intents that only READ; no write, upload, draft or hold)
    case_id     required, non-empty, the turn's own case, and authorised for the caller (workspace.authorize)
    document    required (non-empty, a known document) only for a tool that needs one; otherwise ignored
    party       APPLICANT / CO_APPLICANT or absent
    calls       1..max_calls, no duplicates

The model call and every lookup share ONE budget (`budget_seconds`). No plan, an invalid plan, the model slow / down
/ short of memory, or every lookup failing: None -- the deterministic reply stands, and the outcome is recorded on
the turn (`understanding.fallback_planner`). A lookup that fails while others succeed is NAMED as not answered,
never filled in. Config: intent_catalogue.yaml `planner`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_CASE_ID = re.compile(r"^CASE-[0-9A-F]{6,}$")
_PARTIES = ("APPLICANT", "CO_APPLICANT")


def cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.semantics import meaning

    return meaning.catalogue().get("planner") or {}


def enabled() -> bool:
    """The local planner (config `planner.enabled`) or the stronger-model proof of concept (COPILOT_STRONG_PLANNER)."""
    from app.llm import bedrock_planner

    return bool(cfg().get("enabled", False)) or bedrock_planner.flag_on()


def _tools() -> dict[str, dict[str, Any]]:
    """The read-only tools: configured names that are catalogue CASE intents with a canonical question."""
    from app.agents.applicant.copilot.semantics import meaning

    intents = meaning.catalogue().get("intents") or {}
    return {name: intents[name] for name in cfg().get("tools") or []
            if (intents.get(name) or {}).get("kind") == "case" and (intents.get(name) or {}).get("canonical")}


def needed(message: str) -> bool:
    """
    Whether deterministic routing could NOT place this message: no rule names it (UNKNOWN) and the fuller reading
    (examples, frame) is not confident either -- its answer would be a guess. A placed question never reaches the model.
    """
    from app.agents.applicant.copilot.semantics import intents

    if intents.classify(message).intent is not intents.Intent.UNKNOWN:
        return False
    return intents.understand(message, has_case=True).confidence != "high"


class PlanRejected(ValueError):
    """The proposed plan failed validation: nothing of it runs."""


def validate(plan: Any, *, case_id: str, claims: dict[str, Any]) -> list[dict[str, Any]]:
    """The validated calls of a proposed plan, each {tool, case_id, document, party, canonical}; else PlanRejected."""
    from app.agents.applicant.copilot.capabilities import workspace
    from app.agents.applicant.copilot.semantics import meaning
    from app.security import access

    calls = (plan or {}).get("calls") if isinstance(plan, dict) else None
    if not isinstance(calls, list) or not calls:
        raise PlanRejected("no_calls")
    if len(calls) > int(cfg().get("max_calls", 3)):
        raise PlanRejected("too_many_calls")
    tools, valid, seen = _tools(), [], set()
    for call in calls:
        if not isinstance(call, dict):
            raise PlanRejected("call_not_an_object")
        tool = str(call.get("tool") or "").strip()
        if tool not in tools:
            raise PlanRejected(f"tool_not_allowed:{tool or '-'}")
        target = str(call.get("case_id") or "").strip().upper()
        if not target:
            raise PlanRejected("empty_case_id")
        if not _CASE_ID.match(target) or target != str(case_id or "").upper():
            raise PlanRejected("case_outside_turn")
        try:
            workspace.authorize(claims, target)
        except access.AccessDenied:
            raise PlanRejected("not_authorised") from None
        party = call.get("party")
        party = str(party).strip().upper() if party not in (None, "", "null") else None
        if party is not None and party not in _PARTIES:
            raise PlanRejected("bad_party")
        spec = tools[tool]
        document = None
        if spec.get("needs") == "document":
            named = str(call.get("document") or "").strip()
            if not named:
                raise PlanRejected("empty_document")
            document = meaning.document_in(named)
            if not document:
                raise PlanRejected("unknown_document")
        key = (tool, target, document, party)
        if key in seen:
            continue
        seen.add(key)
        valid.append({"tool": tool, "case_id": target, "document": document, "party": party,
                      "canonical": meaning._canonical(spec, party, document)})
    return valid


def _content(message: str, case_id: str) -> str:
    from app.agents.applicant.copilot.semantics import meaning

    # A SHORT PROMPT (2026-10-12): on a CPU the model's time goes on READING the prompt -- the full catalogue
    # descriptions made every plan miss the 10 s budget. Each tool is its name and a few words (config
    # planner.hints, else the description's first words).
    hints = cfg().get("hints") or {}
    words = int(cfg().get("hint_words", 7))
    lines = "\n".join(f"- {name}: {hints.get(name) or ' '.join(str(spec.get('description', '')).split()[:words])}"
                      + (" (needs document)" if spec.get("needs") else "") for name, spec in _tools().items())
    return (f"OPEN CASE: {case_id}\nMESSAGE: {meaning._masked(message)}\nTOOLS:\n{lines}\n"
            f"At most {int(cfg().get('max_calls', 3))} calls.")


async def _ollama(prefix: str, content: str, timeout: float) -> dict:
    import httpx

    from app.agents.los.summary import keep_alive
    from app.llm.config import ollama_host, ollama_model, with_num_ctx

    model = str(cfg().get("model") or ollama_model())
    schema = {"type": "object", "required": ["calls"], "properties": {"calls": {"type": "array", "items": {
        "type": "object", "required": ["tool", "case_id"],
        # CONSTRAINED DECODING: the model can only write an allow-listed tool name (validation still re-checks)
        "properties": {"tool": {"type": "string", "enum": sorted(_tools())}, "case_id": {"type": "string"},
                       "document": {"type": "string"}, "party": {"type": "string"}}}}}}
    body = {"model": model, "stream": False, "keep_alive": keep_alive(), "format": schema,
            "messages": [{"role": "system", "content": prefix}, {"role": "user", "content": content}],
            "options": with_num_ctx({"temperature": 0.0, "num_predict": int(cfg().get("num_predict", 160))})}
    if model.startswith("qwen3"):
        body["think"] = False
    async with httpx.AsyncClient(timeout=timeout + 0.5) as client:
        response = await client.post(f"{ollama_host().rstrip('/')}/api/chat", json=body)
        response.raise_for_status()
        return response.json()


def _model_ready() -> bool:
    from app.llm import availability
    from app.llm.memory import free_gb

    free = free_gb()
    if free is not None and free < float(cfg().get("min_free_ram_gb", 2.0)):
        return False
    return bool(availability.provider_reachable())


Lookup = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


async def plan_and_answer(message: str, *, case_id: str | None, claims: dict[str, Any], lookup: Lookup,
                          generator=None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """
    (the combined result, the record) -- or (None, the record) when the deterministic reply must stand.
    `lookup(call)` answers ONE validated call through the deterministic engine and returns its result dict.
    """
    record: dict[str, Any] = {"status": "skipped"}
    if not enabled() or not case_id or not str(message or "").strip():
        return None, record
    if generator is None:
        from app.llm import bedrock_planner

        if bedrock_planner.flag_on():
            # THE STRONGER-MODEL PROOF OF CONCEPT (app/llm/bedrock_planner.py): every gate open, or nothing is sent
            blocked = bedrock_planner.blocked_reason()
            if blocked is not None:
                record["status"] = blocked
                return None, record
            record["provider"] = "bedrock"
            generator = bedrock_planner.generator()
        else:
            if not _model_ready():
                record["status"] = "model_unavailable"
                return None, record
            generator = _ollama
    budget = float(cfg().get("budget_seconds", 10.0))
    started = time.perf_counter()

    async def run() -> tuple[dict[str, Any] | None, dict[str, Any]]:
        body = await generator(str(cfg().get("instructions") or "").strip(), _content(message, case_id), budget)
        if isinstance((body or {}).get("usage"), dict):
            record["usage"] = body["usage"]          # tokens (a provider that reports them); never the text
        if (body or {}).get("clarify"):
            # the model wrote text instead of a plan (a question back / a refusal): never shown -- the deterministic
            # reply (its own clarification) stands
            record["status"] = "clarify"
            return None, record
        raw = ((body or {}).get("message") or {}).get("content") or ""
        try:
            plan = json.loads(raw) if raw.strip().startswith("{") else None
        except ValueError:
            plan = None
        try:
            calls = validate(plan, case_id=case_id, claims=claims)
        except PlanRejected as exc:
            record.update(status="rejected", reason=str(exc))
            return None, record
        record["calls"] = [c["tool"] for c in calls]
        answered, failed, merged = [], [], None
        for call in calls:
            try:
                result = await lookup(call)
            except Exception:  # noqa: BLE001 - one lookup down: named as not answered, never filled in
                logger.warning("planner: lookup %s failed", call["tool"], exc_info=True)
                failed.append(call)
                continue
            text = str((result or {}).get("answer") or "").strip()
            if not text or str((result or {}).get("intent") or "") in ("UNKNOWN", "GUARDRAIL_BLOCKED"):
                failed.append(call)
                continue
            answered.append(text)
            if merged is None:
                merged = dict(result)
            else:
                for key in ("tools_invoked", "tool_trace", "sources"):
                    merged[key] = list(merged.get(key) or []) + list(result.get(key) or [])
        if merged is None:
            record["status"] = "no_answer"
            return None, record
        if failed:
            from app.agents.applicant.copilot.semantics import meaning

            names = ", ".join(str((meaning.catalogue().get("intents") or {}).get(c["tool"], {}).get("canonical")
                                  or c["tool"]).rstrip("?").lower() for c in failed)
            answered.append(str(cfg()["not_answered"]).format(what=names))      # the sentence: config only
        merged["answer"] = " ".join(answered)
        record.update(status="answered" if not failed else "partial",
                      failed=[c["tool"] for c in failed])
        return merged, record

    try:
        result, record = await asyncio.wait_for(run(), budget)
    except asyncio.TimeoutError:
        record = {"status": "timeout", **({"provider": record["provider"]} if record.get("provider") else {})}
        result = None
    except Exception as exc:  # noqa: BLE001 - the model down / invalid output: the deterministic reply stands
        logger.warning("planner: failed (%s)", type(exc).__name__, exc_info=True)
        record = {"status": "model_failed", "error": type(exc).__name__,
                  **({"provider": record["provider"]} if record.get("provider") else {})}
        result = None
    record["ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result, record


__all__ = ["PlanRejected", "cfg", "enabled", "needed", "plan_and_answer", "validate"]
