"""
MODEL ROUTING -- when the language model is consulted, and when it never is.

Three paths, decided per turn from the intent, the turn type and the shape
of the question -- never from the model's own opinion of the question:

    FAST    a deterministic capability answers and a deterministic phrasing
            layer words it; no model call (simple field retrieval, status,
            stage, document lists, identifiers, clarifications, small talk)
    MODEL   the model may phrase the ALREADY-DETERMINED answer or help read a
            genuinely difficult sentence: knowledge answers, broad summaries,
            multi-part answers, corrections and nuanced conversational replies
            -- at most ONE call per turn, validated before it is published
    NEVER   nothing the model says can matter: authorization, ownership,
            record reads, calculations, KYC / verification / eligibility truth,
            lending decisions, refusals

The policy is DATA (applicant_agent.yaml: chatbot.model_routing) with the
defaults below; it changes wording and latency, never a fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Route(str, Enum):
    FAST = "FAST"
    MODEL = "MODEL"
    NEVER = "NEVER"


#: Intents whose answers are QUOTED from the records and never phrased by a
#: model (a recorded reason, verdict or value must reach the reader verbatim).
_NEVER = frozenset({
    "GUARDRAIL_BLOCKED", "OUT_OF_SCOPE", "UNKNOWN", "APPLICANT_PROFILE", "CASE_HISTORY",
    "CASE_FINDINGS", "KYC_RESULT", "ELIGIBILITY", "INCOME_EVIDENCE", "DOCUMENT_DETAILS",
    "DOCUMENT_VERIFICATION", "APPLICANT_DETAILS", "APPLICANT_MISSING_INFO",
})
#: Intents whose answers are explanatory prose the model may word.
_MODEL = frozenset({
    "FOS_KNOWLEDGE", "STAGE_PROCESS", "FULL_SUMMARY", "MIXED", "NEXT_ACTION", "READINESS",
    "POLICY_EXPLANATION", "CASE_PORTFOLIO",
})


@dataclass(frozen=True)
class Decision:
    route: Route
    reason: str


def _policy() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("model_routing")


def decide(intent: str | None, *, turn_type: str | None = None, category: str | None = None,
           compound: bool = False, clarification: bool = False, language: str | None = None,
           model_reachable: bool = True) -> Decision:
    """The path for this turn. Deterministic; reads the policy, not the model."""
    name = str(intent or "UNKNOWN").upper()
    policy = _policy()
    if not policy.get("enabled", True) or not model_reachable:
        return Decision(Route.FAST, "model routing disabled" if not policy.get("enabled", True)
                        else "model unreachable")
    if clarification or str(category or "").upper() in ("UNSUPPORTED", "CONVERSATION"):
        return Decision(Route.NEVER, "a clarification, refusal or small talk is never phrased")
    never = set(_NEVER) | {str(x).upper() for x in policy.get("never", []) or []}
    model = (set(_MODEL) | {str(x).upper() for x in policy.get("model", []) or []}) - never
    if name in never:
        return Decision(Route.NEVER, f"{name} is quoted from the records")
    if compound and policy.get("compound_uses_model", True) and name not in never:
        return Decision(Route.MODEL, "a multi-part answer is joined naturally")
    if turn_type == "CORRECTION" and policy.get("corrections_use_model", False):
        return Decision(Route.MODEL, "a correction is acknowledged conversationally")
    if name in model:
        return Decision(Route.MODEL, f"{name} is explanatory prose")
    return Decision(Route.FAST, f"{name} is a simple recorded answer")


__all__ = ["Decision", "Route", "decide"]
