"""
THE PLANNER: ONE model call per turn decides what the officer means (2026-11-02).

    plan(message, history, case_open) -> Plan(intent, tool, args, question, ms, error)

* intents: SOCIAL, GENERAL_KNOWLEDGE, LOS_HANDBOOK, CASE_SPECIFIC, ANALYTICS, CLARIFICATION (a follow-up is resolved by
  the model from the history into one of these, with its arguments filled from the earlier turn)
* tools and their argument schemas come from the registry (generated from the functions, never hand-written); the
  model's output is CONSTRAINED by a JSON schema (Ollama `format`), then the arguments are validated again
* the system prompt is byte-identical every call (Ollama reuses its cached prefix); history and the message follow

Config: conversation_general.yaml `planner`. Env COPILOT_PLANNER.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from app.agents.applicant.copilot.planner import registry
from app.agents.applicant.copilot.planner import tools as _tools  # noqa: F401  (registers the tools)

INTENTS = ("SOCIAL", "LOS_HANDBOOK", "LOS_TECHNICAL", "CASE_SPECIFIC", "ANALYTICS", "OUT_OF_DOMAIN",
           "CLARIFICATION")
#: THE COMPACT CODES THE MODEL WRITES (one plan = ~10-25 tokens instead of 20-54; measured 2026-11-03: generation, not
#: the cached prompt, is the cost of a planner call on a CPU host)
INTENT_CODES = {"S": "SOCIAL", "H": "LOS_HANDBOOK", "T": "LOS_TECHNICAL", "C": "CASE_SPECIFIC", "A": "ANALYTICS",
                "O": "OUT_OF_DOMAIN", "Q": "CLARIFICATION"}
TOOL_CODES = {"cq": "case_question", "lc": "list_cases", "pa": "portfolio_analytics", "hb": "handbook",
              "ns": "next_stage", "-": "none"}


def cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.capabilities import general

    return general.cfg().get("planner") or {}


def enabled() -> bool:
    flag = (os.getenv("COPILOT_PLANNER") or "").strip().lower()
    if flag:
        return flag in {"1", "true", "yes", "on"}
    return bool(cfg().get("enabled", False))


@dataclass
class Plan:
    intent: str = "CLARIFICATION"
    tool: str = "none"
    args: dict[str, Any] = field(default_factory=dict)
    question: str = ""
    ms: float = 0.0
    error: str | None = None
    raw: str = ""


def _tool_lines() -> str:
    """One line per tool: code, name, what it does, its arguments with their allowed values -- from the generated
    schemas."""
    lines = []
    code_of = {v: k for k, v in TOOL_CODES.items()}
    for name, t in registry.tools().items():
        props = registry.schemas()[name].get("properties") or {}
        args = []
        for arg, spec in props.items():
            options = spec.get("enum") or [e for a in spec.get("anyOf") or [] for e in a.get("enum") or []]
            kind = "|".join(map(str, options)) if options else spec.get("type") or "string"
            args.append(f"{arg}: {kind}")
        lines.append(f"- {code_of.get(name, name)} = {name}({', '.join(args)}): {t.description}")
    return "\n".join(lines)


@lru_cache(maxsize=1)
def system_prompt() -> str:
    return str(cfg().get("instructions") or "").replace("{tools}", _tool_lines())


#: the intent a chosen tool implies (the label follows the tool: a handbook lookup IS a handbook question)
TOOL_INTENT = {"handbook": "LOS_HANDBOOK", "next_stage": "LOS_HANDBOOK", "case_question": "CASE_SPECIFIC",
               "list_cases": "CASE_SPECIFIC", "portfolio_analytics": "ANALYTICS"}


def output_schema() -> dict[str, Any]:
    """CONSTRAINED DECODING of the intent and tool CODES (a nested per-tool grammar was measured worse: 43 % vs 82 % --
    the model fell to "none"); the arguments are validated and repaired by the registry instead."""
    return {"type": "object",
            "properties": {"i": {"type": "string", "enum": list(INTENT_CODES)},
                           "t": {"type": "string", "enum": [c for c, n in TOOL_CODES.items()
                                                            if n == "none" or n in registry.tools()]},
                           "a": {"type": "object"},
                           "q": {"type": "string"}},
            "required": ["i", "t"]}


async def plan(message: str, history: list[dict[str, str]] | None = None, *, case_open: bool = False,
               model: str | None = None) -> Plan:
    import asyncio

    import httpx

    from app.llm import turn_budget as _tb
    from app.llm.config import ollama_host, with_num_ctx

    spec = cfg()
    note = "[a case is open]" if case_open else "[no case is open]"
    messages = ([{"role": "system", "content": system_prompt()}] + list(history or [])[-int(spec.get("history_turns", 6)):]
                + [{"role": "user", "content": f"{note} {str(message)[:400]}"}])
    body = {"model": model or str(spec.get("model")), "stream": False, "messages": messages,
            "format": output_schema(), "keep_alive": spec.get("keep_alive", "30m"),
            "options": with_num_ctx({"temperature": 0.0, "num_predict": int(spec.get("num_predict", 80))})}
    timeout = float(spec.get("timeout_seconds", 6.0))
    started = time.perf_counter()
    out = Plan()
    try:
        _tb.check("planner")
        async with httpx.AsyncClient(timeout=timeout + 0.5) as client:
            response = await asyncio.wait_for(
                _tb.guarded(client.post(f"{ollama_host().rstrip('/')}/api/chat", json=body), "planner"), timeout)
            out.raw = str(((response.json() or {}).get("message") or {}).get("content") or "")
        data = json.loads(out.raw)
        out.intent = INTENT_CODES.get(str(data.get("i")), "CLARIFICATION")
        tool = TOOL_CODES.get(str(data.get("t")), "none")
        out.tool = tool if tool in registry.tools() else "none"
        out.question = str(data.get("q") or "")
        if out.tool != "none":
            try:
                out.args = registry.repair(out.tool, data.get("a") if isinstance(data.get("a"), dict) else {})
                if not (out.intent == "LOS_TECHNICAL" and out.tool == "handbook"):
                    out.intent = TOOL_INTENT.get(out.tool, out.intent)
            except registry.ToolArgumentError as exc:
                out.error, out.tool, out.args = f"INVALID_ARGUMENTS: {exc}", "none", {}
    except Exception as exc:  # noqa: BLE001 - no plan: the caller's own path answers
        out.error = type(exc).__name__
    out.ms = round((time.perf_counter() - started) * 1000, 1)
    return out


__all__ = ["INTENTS", "Plan", "cfg", "enabled", "output_schema", "plan", "system_prompt"]
