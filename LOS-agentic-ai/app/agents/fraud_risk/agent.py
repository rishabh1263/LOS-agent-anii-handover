"""
Fraud & Risk Agent (Agent 2).

    FraudRiskRequest
          |
          v
    RiskRuleEngine        <- deterministic: flags, score, category, outcome
          |
          v
    Summary generation    <- LLM if available, deterministic otherwise
          |
          v
    FraudRiskResponse

The summary is produced AFTER the deterministic values are final. The score,
category and outcome are copied from the engine's assessment into the response
without passing through the LLM path, so an LLM failure or a malicious LLM
response cannot alter them.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from app.agents.fraud_risk.config import (
    enabled,
    get_policy,
    llm_summary_enabled,
    version,
)
from app.agents.fraud_risk import audit, metrics
from app.agents.fraud_risk.engine import RiskAssessment, RiskRuleEngine
from app.agents.fraud_risk.schemas import FraudRiskRequest, FraudRiskResponse
from app.agents.fraud_risk.summary import (
    LLMSummaryGenerator,
    deterministic_summary,
    validate_llm_summary,
)

logger = logging.getLogger(__name__)


class FraudRiskAgent:
    """Agent 2. Instantiate once and reuse; holds no per-request state."""

    name = "fraud_risk_agent"

    def __init__(
        self,
        policy: dict[str, Any] | None = None,
        summary_generator: LLMSummaryGenerator | None = None,
        use_llm: bool | None = None,
        agent_config: Any | None = None,
    ) -> None:
        self.policy = policy if policy is not None else get_policy()
        self.engine = RiskRuleEngine(self.policy)
        self._summary_generator = summary_generator
        self._use_llm = use_llm
        # Orchestration AgentConfig. When supplied, its model, temperature and
        # prompt_version drive the LLM call instead of environment defaults.
        self.agent_config = agent_config

    def _make_generator(self) -> LLMSummaryGenerator:
        """Build a generator honouring the orchestration configuration."""
        cfg = self.agent_config
        if cfg is None:
            return LLMSummaryGenerator()
        return LLMSummaryGenerator(
            model=getattr(cfg, "model", None),
            timeout=getattr(cfg, "timeout", None),
            temperature=getattr(cfg, "temperature", None),
            prompt_version=getattr(cfg, "prompt_version", "v1") or "v1",
        )

    # -- summary ----------------------------------------------------------

    def _build_summary(self, assessment: RiskAssessment) -> tuple[str, str]:
        """Returns (summary, source). Never raises."""
        fallback = deterministic_summary(assessment)

        use_llm = self._use_llm if self._use_llm is not None else llm_summary_enabled()
        if not use_llm:
            return fallback, "deterministic"

        generator = self._summary_generator or self._make_generator()

        try:
            raw = generator.generate(assessment)
        except Exception as exc:
            logger.warning(
                "Fraud & Risk LLM summary unavailable (%s: %s); using deterministic summary.",
                type(exc).__name__,
                exc,
            )
            return fallback, "deterministic_fallback"

        is_valid, result = validate_llm_summary(raw, assessment)
        if not is_valid:
            logger.warning(
                "Fraud & Risk LLM summary rejected (%s); using deterministic summary.",
                result,
            )
            return fallback, "deterministic_fallback"

        return result, "llm"

    # -- main -------------------------------------------------------------

    async def _abuild_summary(self, assessment: RiskAssessment) -> tuple[str, str]:
        """Async summary path. Never raises."""
        fallback = deterministic_summary(assessment)

        use_llm = self._use_llm if self._use_llm is not None else llm_summary_enabled()
        if not use_llm:
            return fallback, "deterministic"

        generator = self._summary_generator or self._make_generator()

        try:
            if hasattr(generator, "agenerate"):
                raw = await generator.agenerate(assessment)
            else:
                raw = generator.generate(assessment)
        except Exception as exc:
            logger.warning(
                "Fraud & Risk LLM summary unavailable (%s: %s); using deterministic summary.",
                type(exc).__name__,
                exc,
            )
            return fallback, "deterministic_fallback"

        is_valid, result = validate_llm_summary(raw, assessment)
        if not is_valid:
            logger.warning(
                "Fraud & Risk LLM summary rejected (%s); using deterministic summary.",
                result,
            )
            return fallback, "deterministic_fallback"

        return result, "llm"

    async def aassess(
        self, request: FraudRiskRequest, request_id: str | None = None
    ) -> FraudRiskResponse:
        """
        Async assessment. Used by the API route so a slow LLM call cannot
        block the event loop. The deterministic engine is identical to
        assess(); only the summary path differs.
        """
        if not enabled():
            raise RuntimeError("Fraud & Risk Agent is disabled.")

        started = time.perf_counter()
        assessment = self.engine.evaluate(request)
        summary, source = await self._abuild_summary(assessment)
        response = self._to_response(request, assessment, summary, source)
        self._audit(response, assessment, (time.perf_counter() - started) * 1000, request_id)
        return response

    def _audit(
        self,
        response: FraudRiskResponse,
        assessment: RiskAssessment,
        duration_ms: float,
        request_id: str | None = None,
    ) -> None:
        rid = request_id or str(uuid.uuid4())

        metrics.record_assessment(
            outcome=response.final_outcome.value,
            category=response.risk_category.value,
            rules=[f.rule for f in assessment.flags],
            summary_source=response.summary_source,
            duration_ms=duration_ms,
        )

        logger.info(
            "risk_assessment request_id=%s application_id=%s outcome=%s "
            "category=%s score=%d flags=%d gaps=%d source=%s duration_ms=%.1f",
            rid,
            response.application_id,
            response.final_outcome.value,
            response.risk_category.value,
            response.risk_score,
            len(assessment.flags),
            len(assessment.data_gaps),
            response.summary_source,
            duration_ms,
        )

        audit.record(
            request_id=rid,
            application_id=response.application_id,
            policy_version=response.policy_version,
            policy_signed_off=response.policy_signed_off,
            risk_score=response.risk_score,
            risk_category=response.risk_category.value,
            final_outcome=response.final_outcome.value,
            flags=[f.model_dump(mode="json") for f in assessment.flags],
            data_gaps=[g.model_dump(mode="json") for g in assessment.data_gaps],
            summary_source=response.summary_source,
            duration_ms=duration_ms,
        )

    def _to_response(
        self,
        request: FraudRiskRequest,
        assessment: RiskAssessment,
        summary: str,
        source: str,
    ) -> FraudRiskResponse:
        return FraudRiskResponse(
            agent=self.name,
            version=version(),
            policy_version=str(self.policy.get("policy_version", "")),
            policy_signed_off=bool(self.policy.get("signed_off", False)),
            application_id=request.application_id,
            risk_category=assessment.risk_category,
            risk_score=assessment.risk_score,
            summary=summary,
            final_outcome=assessment.final_outcome,
            flags=assessment.flags,
            evidence=assessment.evidence,
            data_gaps=assessment.data_gaps,
            summary_source=source,
            rules_not_implemented=list(self.policy.get("not_implemented", [])),
        )

    def assess(self, request: FraudRiskRequest) -> FraudRiskResponse:
        """Synchronous assessment. Kept for tests, scripts and batch use."""
        if not enabled():
            raise RuntimeError("Fraud & Risk Agent is disabled.")

        started = time.perf_counter()

        # 1. Deterministic engine. Complete and final before any LLM call.
        assessment = self.engine.evaluate(request)

        # 2. Summary. Cannot influence step 1.
        summary, source = self._build_summary(assessment)

        # 3. Response assembled from the assessment, not from the LLM.
        response = self._to_response(request, assessment, summary, source)
        self._audit(response, assessment, (time.perf_counter() - started) * 1000)
        return response


def build_fraud_risk_agent(**kwargs: Any) -> FraudRiskAgent:
    """Factory mirroring build_document_agent() in the document agent."""
    return FraudRiskAgent(**kwargs)


__all__ = ["FraudRiskAgent", "build_fraud_risk_agent"]