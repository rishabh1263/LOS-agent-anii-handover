"""
STRONGER-MODEL PLANNER, PROOF OF CONCEPT (2026-10-12; config intent_catalogue.yaml `planner.strong`).

A second PROVIDER for the existing bounded planner (capabilities/fallback_planner.py) -- not a second planner. The
model only PROPOSES a typed plan through ONE tool ("plan_lookups", Bedrock Converse tool use); fallback_planner.validate
still checks every call (allow-listed read-only tool, the turn's own case, authorised for the caller, known document,
<= max_calls, no duplicates) and the deterministic engine answers each call. The model never sees a tool result, case
record or answer: only the officer's MASKED message, the open case id and the tool names.

THREE GATES, ALL REQUIRED, ALL DEFAULT OFF -- nothing leaves the machine unless every one is set:
    COPILOT_STRONG_PLANNER=true            the feature flag
    LOS_EXTERNAL_MODEL_APPROVED=true       the data-handling approval for sending masked officer text to the provider
    planner.strong.model_id + region       a configured model
and credentials resolvable by the standard AWS chain (never read, printed or stored here).

Verified against the official docs (2026-10-12): Converse request `toolConfig.tools[].toolSpec{name, description,
inputSchema.json}`, `toolChoice.auto` (the default, supported by every tool-use model; the specific-`tool` choice is
documented for a subset only and is NOT used), response `stopReason` (`tool_use` / `end_turn` / ...), the `toolUse`
content block's `input`, and `usage{inputTokens, outputTokens, totalTokens}`. SDK retries are switched off
(max_attempts=1): one call per turn, inside the planner's budget -- never an uncontrolled retry.

Rollback: unset COPILOT_STRONG_PLANNER (or LOS_EXTERNAL_MODEL_APPROVED) -- the local planner path is unchanged.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

TOOL_NAME = "plan_lookups"


def _truthy(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in ("1", "true", "yes", "on")


def settings() -> dict[str, Any]:
    from app.agents.applicant.copilot.capabilities import fallback_planner

    return dict(fallback_planner.cfg().get("strong") or {})


def flag_on() -> bool:
    """The feature flag (default OFF)."""
    return _truthy("COPILOT_STRONG_PLANNER")


def blocked_reason() -> str | None:
    """Why the external model must NOT be called now, or None when every gate is open."""
    if not flag_on():
        return "flag_off"
    if not _truthy("LOS_EXTERNAL_MODEL_APPROVED"):
        return "external_not_approved"
    s = settings()
    if str(s.get("provider") or "") != "bedrock":
        return "provider_not_configured"
    if not s.get("model_id") or not s.get("region"):
        return "model_not_configured"
    return None


def _client():
    """A bedrock-runtime client with bounded timeouts and NO SDK retries (one attempt per turn)."""
    import boto3
    from botocore.config import Config

    s = settings()
    return boto3.client("bedrock-runtime", region_name=str(s["region"]), config=Config(
        connect_timeout=float(s.get("connect_timeout_s", 2)), read_timeout=float(s.get("read_timeout_s", 8)),
        retries={"max_attempts": 1, "mode": "standard"}))


def tool_spec(tools: list[str]) -> dict[str, Any]:
    """The ONE tool the model may call: a plan of up to 3 typed lookups (tool names constrained to the allow-list)."""
    return {"toolSpec": {
        "name": TOOL_NAME,
        "description": "Plan read-only lookups on the officer's open case. Call with an empty list if no tool fits.",
        "inputSchema": {"json": {
            "type": "object", "required": ["calls"],
            "properties": {"calls": {"type": "array", "maxItems": 3, "items": {
                "type": "object", "required": ["tool", "case_id"],
                "properties": {"tool": {"type": "string", "enum": sorted(tools)},
                               "case_id": {"type": "string"},
                               "document": {"type": "string"},
                               "party": {"type": "string", "enum": ["APPLICANT", "CO_APPLICANT"]}}}}}}}}}


def request(prefix: str, content: str, tools: list[str]) -> dict[str, Any]:
    s = settings()
    return {"modelId": str(s["model_id"]),
            "system": [{"text": prefix}],
            "messages": [{"role": "user", "content": [{"text": content}]}],
            "inferenceConfig": {"maxTokens": int(s.get("max_tokens", 300)), "temperature": 0.0},
            "toolConfig": {"tools": [tool_spec(tools)], "toolChoice": {"auto": {}}}}


def parse(response: dict[str, Any]) -> dict[str, Any]:
    """
    The Converse response in the planner's shape: {"message": {"content": <plan JSON or "">}, "usage", "stop_reason",
    "clarify"}. Only the `plan_lookups` tool's input is read; text the model writes instead (a question back, a
    refusal) is never shown to the officer -- it marks the turn `clarify` and the deterministic reply stands.
    """
    import json

    content = (((response or {}).get("output") or {}).get("message") or {}).get("content") or []
    plan = next((block["toolUse"].get("input") for block in content
                 if isinstance(block, dict) and isinstance(block.get("toolUse"), dict)
                 and block["toolUse"].get("name") == TOOL_NAME), None)
    usage = (response or {}).get("usage") or {}
    return {"message": {"content": json.dumps(plan) if isinstance(plan, dict) else ""},
            "stop_reason": (response or {}).get("stopReason"),
            "clarify": plan is None,
            "usage": {"input_tokens": int(usage.get("inputTokens") or 0),
                      "output_tokens": int(usage.get("outputTokens") or 0)}}


def cost_usd(usage: dict[str, Any]) -> float | None:
    """Estimated cost from CONFIGURED prices (planner.strong.price_per_mtok_usd); None when no price is configured."""
    price = settings().get("price_per_mtok_usd") or {}
    if price.get("input") is None or price.get("output") is None:
        return None
    return round(usage.get("input_tokens", 0) / 1e6 * float(price["input"])
                 + usage.get("output_tokens", 0) / 1e6 * float(price["output"]), 6)


def generator(client_factory=None):
    """The planner generator for this provider: (prefix, content, timeout) -> planner-shaped body."""
    async def generate(prefix: str, content: str, timeout: float) -> dict[str, Any]:
        reason = blocked_reason()
        if reason is not None:                       # defence in depth: the gate is checked again at call time
            raise PermissionError(reason)
        from app.agents.applicant.copilot.capabilities import fallback_planner

        tools = sorted(fallback_planner._tools())
        client = (client_factory or _client)()
        response = await asyncio.wait_for(asyncio.to_thread(client.converse, **request(prefix, content, tools)),
                                          timeout)
        return parse(response)
    return generate


__all__ = ["TOOL_NAME", "blocked_reason", "cost_usd", "flag_on", "generator", "parse", "request", "settings",
           "tool_spec"]
