"""
THE TOOL REGISTRY OF THE PLANNER (2026-11-02): a tool is a typed Python function; its JSON schema is GENERATED from the
signature (pydantic), never written by hand, and its arguments are VALIDATED against that schema before it runs.

    @tool("list_cases", "The officer's own cases, filtered / sorted.")
    async def list_cases(ctx: ToolContext, filter: Literal["all", "kyc_issue", ...] = "all", limit: int = 5): ...

    schemas()            {name: JSON schema of its arguments}   (ctx is never part of it)
    validate(name, args) the arguments as the function receives them, or ToolArgumentError
    run(name, ctx, args) validated, permission-checked, time-limited execution -> ToolResult

Read-only tools only: a tool declares `writes=False`; the registry refuses to register a writing tool (decisions,
approvals and changes stay in their own authorised workflows).
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pydantic import ConfigDict, ValidationError, create_model


class ToolArgumentError(ValueError):
    """Arguments that do not match the tool's schema (never passed to the tool)."""


@dataclass
class ToolContext:
    """What a tool may use: the verified caller and the request's case context (never model-supplied)."""
    claims: dict[str, Any]
    request_id: str
    case_id: str | None = None
    applicant_id: str | None = None
    lang: str = "en"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    name: str
    ok: bool
    reply: dict[str, Any] | None = None       # the reply the tool's own (deterministic) handler built
    error: str | None = None
    ms: float = 0.0


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable[..., Awaitable[dict[str, Any] | None]]
    model: Any                                  # the pydantic model of the arguments
    needs_case: bool = False
    timeout_seconds: float = 8.0


_TOOLS: dict[str, Tool] = {}


def tool(name: str, description: str, *, needs_case: bool = False, writes: bool = False,
         timeout_seconds: float = 8.0):
    """Register a read-only tool; its argument schema comes from its signature (all parameters after `ctx`)."""
    if writes:
        raise ValueError(f"tool {name!r}: the planner registers read-only tools only")

    def wrap(fn):
        params = list(inspect.signature(fn).parameters.values())[1:]          # without ctx
        hints = inspect.get_annotations(fn, eval_str=True)
        fields = {p.name: (hints.get(p.name, Any), ... if p.default is inspect.Parameter.empty else p.default)
                  for p in params}
        model = create_model(f"{name}_args", __config__=ConfigDict(extra="forbid"), **fields)
        _TOOLS[name] = Tool(name, description, fn, model, needs_case, timeout_seconds)
        return fn

    return wrap


def tools() -> dict[str, Tool]:
    return dict(_TOOLS)


def schemas() -> dict[str, dict[str, Any]]:
    """{name: JSON schema of the arguments} -- generated, so it never drifts from the code."""
    out = {}
    for name, t in _TOOLS.items():
        s = t.model.model_json_schema()
        s.pop("title", None)
        for prop in (s.get("properties") or {}).values():
            prop.pop("title", None)
        out[name] = s
    return out


def validate(name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    t = _TOOLS.get(name)
    if t is None:
        raise ToolArgumentError(f"unknown tool {name!r}")
    try:
        return t.model(**(args or {})).model_dump()
    except ValidationError as exc:
        raise ToolArgumentError(f"{name}: {exc.errors()[0].get('msg', 'invalid arguments')}") from None


def _enums(name: str) -> dict[str, set[str]]:
    """{field: its allowed values} of a tool (from the generated schema; Optional fields included)."""
    out = {}
    for field_name, spec in (schemas()[name].get("properties") or {}).items():
        values = spec.get("enum") or [e for a in spec.get("anyOf") or [] for e in a.get("enum") or []]
        if values:
            out[field_name] = {str(v) for v in values}
    return out


def repair(name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """The model's arguments, with a value written into the WRONG field moved to the field it belongs to
    ({"filter": "oldest"} -> {"sort": "oldest"}). Nothing else is changed: a value that is valid in no field
    ({"filter": "approved"}) or a missing required field still raises ToolArgumentError -- the plan is then dropped,
    never run with a guessed default (that would answer "show approved cases" with every case)."""
    args = dict(args or {})
    try:
        return validate(name, args)
    except ToolArgumentError:
        pass
    enums = _enums(name)
    fixed = dict(args)
    for key, value in args.items():
        allowed = enums.get(key)
        if allowed is None or str(value) in allowed:
            continue
        home = [f for f, vals in enums.items() if f != key and str(value) in vals and f not in args]
        if len(home) != 1:
            raise ToolArgumentError(f"{name}: {key}={value!r} is not an allowed value")
        fixed.pop(key)
        fixed[home[0]] = value
    return validate(name, fixed)


async def run(name: str, ctx: ToolContext, args: dict[str, Any] | None) -> ToolResult:
    """Validate, check the case permission the tool needs, run with its time limit. Never raises."""
    started = time.perf_counter()
    try:
        checked = validate(name, args)
        t = _TOOLS[name]
        if t.needs_case and not ctx.case_id:
            return ToolResult(name, False, error="NO_CASE_IN_SCOPE", ms=0.0)
        reply = await asyncio.wait_for(t.fn(ctx, **checked), t.timeout_seconds)
        ok = isinstance(reply, dict) and bool(reply.get("answer") or reply.get("markdown"))
        return ToolResult(name, ok, reply if ok else None, None if ok else "EMPTY",
                          round((time.perf_counter() - started) * 1000, 1))
    except ToolArgumentError as exc:
        return ToolResult(name, False, error=f"INVALID_ARGUMENTS: {exc}")
    except asyncio.TimeoutError:
        return ToolResult(name, False, error="TIMEOUT", ms=round((time.perf_counter() - started) * 1000, 1))
    except Exception as exc:  # noqa: BLE001 - a failed tool is reported, never invented
        return ToolResult(name, False, error=type(exc).__name__, ms=round((time.perf_counter() - started) * 1000, 1))


__all__ = ["Tool", "ToolArgumentError", "ToolContext", "ToolResult", "repair", "run", "schemas", "tool", "tools",
           "validate"]
