"""
Summary generation for the Fraud & Risk Agent.

Two paths:

  1. deterministic_summary(...)  — always available, no dependencies.
  2. LLMSummaryGenerator         — optional Ollama call, strictly validated.

The LLM can only ever replace the `summary` STRING. It never sees or returns
the score, category or outcome as mutable values. If the LLM is unavailable,
slow, errors, or returns output that fails validation, the deterministic
summary is used and the assessment completes normally.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.agents.fraud_risk.config import llm_timeout_seconds
from app.agents.fraud_risk.engine import RiskAssessment
from app.agents.fraud_risk.prompts import (
    build_summary_user_prompt,
    get_system_prompt,
)
from app.agents.fraud_risk.schemas import Severity
from app.llm.config import ollama_host, ollama_model

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 320
MIN_SUMMARY_CHARS = 15


# ---------------------------------------------------------------------------
# DETERMINISTIC SUMMARY
# ---------------------------------------------------------------------------

_RULE_PHRASES = {
    "INCOME_MISMATCH": "declared income exceeds verified income",
    "DEDUPE_MATCH": "a duplicate customer match",
    "VERIFICATION_NEGATIVE": "a negative verification result",
    "PD_STATUS_NEGATIVE": "a negative personal discussion",
    "REFERENCE_NEGATIVE": "an adverse reference check",
    "MOB_TOPUP_INELIGIBLE": "insufficient months on book for top-up",
    "FOIR_BREACH": "an elevated fixed-obligation-to-income ratio",
    "LTV_BREACH": "an elevated loan-to-value ratio",
    "MANDATORY_DOCS_MISSING": "missing mandatory documents",
    "LEGAL_TITLE_RISK": "unresolved legal title items",
}


def deterministic_summary(assessment: RiskAssessment) -> str:
    """
    Short fallback summary built from the assessment alone. Never raises.

    Two sentences: the lead finding, then the verdict.
    """
    category = assessment.risk_category.value
    score = assessment.risk_score
    outcome = assessment.final_outcome.value

    if not assessment.flags:
        gaps = (
            f" {len(assessment.data_gaps)} check(s) not evaluated."
            if assessment.data_gaps
            else ""
        )
        return f"No risk rules triggered. {category} risk (score {score}); outcome {outcome}.{gaps}"

    lead = assessment.flags[0]
    phrase = _RULE_PHRASES.get(lead.rule, lead.rule.replace("_", " ").lower())

    if lead.rule == "INCOME_MISMATCH":
        ev = lead.evidence
        headline = (
            f"Declared income {ev.get('declared_income')} against verified "
            f"{ev.get('verified_income')} ({ev.get('variance_pct')}% over)"
        )
    else:
        headline = phrase.capitalize()

    others = len(assessment.flags) - 1
    extra = f", plus {others} further finding(s)" if others > 0 else ""

    return f"{headline}{extra}. {category} risk (score {score}); outcome {outcome}."


# ---------------------------------------------------------------------------
# LLM PAYLOAD
# ---------------------------------------------------------------------------


def build_llm_payload(assessment: RiskAssessment) -> dict[str, Any]:
    """
    The ONLY data the LLM receives. Already-calculated values, read-only.
    """
    return {
        "risk_category": assessment.risk_category.value,
        "risk_score": assessment.risk_score,
        "final_outcome": assessment.final_outcome.value,
        "flags": [
            {
                "rule": f.rule,
                "severity": f.severity.value,
                "message": f.message,
                "evidence": f.evidence,
            }
            for f in assessment.flags
        ],
        "unevaluated_checks": [g.rule for g in assessment.data_gaps],
    }


# ---------------------------------------------------------------------------
# OUTPUT VALIDATION
# ---------------------------------------------------------------------------



def _collect_allowed_numbers(payload: dict[str, Any]) -> set[str]:
    """Every numeric token the LLM may reproduce (the unified primitive)."""
    from app.security.output_validation import allowed_numbers

    return allowed_numbers(payload, counts=False, two_decimals=True)


def validate_llm_summary(text: str, assessment: RiskAssessment) -> tuple[bool, str]:
    """
    Validate an LLM summary. Returns (is_valid, cleaned_or_reason).

    Rejects output that is empty, over-long, contains structured markup, or
    states a number not present in the evidence. A rejected summary is
    discarded entirely — it is never partially merged.
    """
    from app.security import output_validation

    payload: dict[str, Any] = {}

    def truth() -> dict[str, Any]:
        payload.update(build_llm_payload(assessment))
        return payload

    def computed_category(cleaned: str) -> str | None:
        # The LLM must not assert a category or outcome other than the computed one.
        upper = cleaned.upper()
        for other in ("LOW", "MEDIUM", "HIGH"):
            if other != assessment.risk_category.value and f"{other} RISK" in upper:
                return f"summary asserted a different risk category: {other}"
        for other in ("PASS", "REVIEW", "FAIL"):
            if other != assessment.final_outcome.value and f"OUTCOME: {other}" in upper:
                return f"summary asserted a different outcome: {other}"
        return None

    # THE UNIFIED VALIDATOR (app/security/output_validation.py): guardrail,
    # shape, decision language, numbers and dates, then the computed
    # risk category and outcome.
    return output_validation.validate(
        text, surface="fraud_summary", truth=truth,
        extra=(computed_category,)).pair()


# ---------------------------------------------------------------------------
# LLM CLIENT
# ---------------------------------------------------------------------------


class LLMSummaryGenerator:
    """
    Calls Ollama's /api/chat for a summary.

    Uses httpx directly rather than agent_framework's OllamaChatClient: this is
    a single stateless completion with no tools, so the Agent/MCP machinery
    would add a heavy import for no benefit. Host and model still come from
    app.llm.config, so configuration stays in one place.
    """

    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        temperature: float | None = None,
        prompt_version: str = "v1",
    ) -> None:
        self.prompt_version = prompt_version
        self.host = (host or ollama_host()).rstrip("/")
        self.model = model or ollama_model()
        self.timeout = timeout if timeout is not None else llm_timeout_seconds()
        self.temperature = 0.1 if temperature is None else float(temperature)

    def generate(self, assessment: RiskAssessment) -> str:
        """
        Returns raw model text. Raises on any transport or protocol failure;
        the caller is responsible for falling back.
        """
        body = self._build_body(assessment)

        response = httpx.post(
            f"{self.host}/api/chat",
            json=body,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return self._extract(response.json())

    def _build_body(self, assessment: RiskAssessment) -> dict[str, Any]:
        from app.security import guardrails

        # Provider and document text is DATA, never an instruction.
        payload = guardrails.untrusted(build_llm_payload(assessment))
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": get_system_prompt(self.prompt_version)
                 + " " + guardrails.UNTRUSTED_NOTICE},
                {
                    "role": "user",
                    "content": build_summary_user_prompt(
                        json.dumps(payload, indent=2, default=str)
                    ),
                },
            ],
            "stream": False,
            "options": {"temperature": self.temperature, "num_predict": 120},
        }

    async def agenerate(self, assessment: RiskAssessment) -> str:
        """
        Async variant. Used by the API route so a slow or hanging Ollama call
        cannot block the FastAPI event loop and stall every other request.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.host}/api/chat",
                json=self._build_body(assessment),
            )
            response.raise_for_status()
            return self._extract(response.json())

    @staticmethod
    def _extract(data: dict[str, Any]) -> str:
        message = data.get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("Ollama response did not contain message.content")
        return content


__all__ = [
    "deterministic_summary",
    "build_llm_payload",
    "validate_llm_summary",
    "LLMSummaryGenerator",
    "MAX_SUMMARY_CHARS",
    "MIN_SUMMARY_CHARS",
]