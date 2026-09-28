"""
The credit memo -- a structured summary of the ASSESSMENT, never a new judgement.

STRUCTURED FIRST, ALWAYS. Every section is built deterministically from the
assessment: profile, banking, income, repayment, adverse, exceptions, and a
summary sentence set. That memo is complete on its own.

QWEN MAY ONLY REWORD THE SUMMARY. At most ONE model call per run, through the
common harness (RunContext.call_model: budget, deadline, timeout, tracking;
no prompt or completion recorded). The model sees the assessment's facts --
masked, marked untrusted -- and returns prose. It chooses no tool, evaluates
no policy, computes nothing and decides nothing. Its text is published only
if the unified validator accepts it (surface `credit_memo`): no leaked
identifier, no decision language, no number or date that is not in the
facts, and no assessment status other than the real one. Otherwise -- or if
the model is disabled, unreachable, slow or fails -- the deterministic
summary stands, and `validation` says why.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agents.credit import config
from app.agents.credit.schemas import (
    AssessmentStatus,
    CreditAssessment,
    CreditMemo,
    FindingCategory,
    FindingStatus,
)

logger = logging.getLogger(__name__)

PURPOSE = "credit_memo"
_ADVERSE = (FindingStatus.REVIEW, FindingStatus.NEGATIVE)

_SYSTEM = (
    "You summarise a credit underwriting ASSESSMENT for a credit officer, in at most "
    "three plain sentences. Use only the facts provided. Do not state or suggest any "
    "approval, rejection, sanction or loan decision: the Decision Agent decides. Do not "
    "introduce any number, date, name or identifier that is not in the facts. Refer to "
    "the bureau score as the bureau score. Plain text only."
)


# ---------------------------------------------------------------------------
# deterministic sections
# ---------------------------------------------------------------------------

def _rows(assessment: CreditAssessment, *categories: FindingCategory) -> list[dict[str, Any]]:
    return [{"rule": f.policy_rule_id, "status": f.status.value, "severity": f.severity.value,
             "description": f.description, "observed": f.observed_value,
             "party_role": f.subject.role if f.subject else None,
             "confidence": f.confidence}
            for f in assessment.findings if f.category in categories]


def _summary(assessment: CreditAssessment) -> str:
    adverse = [f for f in assessment.findings if f.status in _ADVERSE
               and f.severity.value in ("MEDIUM", "HIGH")]
    gaps = [f for f in assessment.findings if f.category is FindingCategory.DATA_GAP]
    positive = [f for f in assessment.findings if f.status is FindingStatus.POSITIVE]
    label = assessment.status.value.replace("_", " ").lower()
    parts = [f"Underwriting assessment: {label}; handed to the Decision Agent."]
    if adverse:
        lead = "; ".join(f.description.lower() for f in adverse[:3])
        more = f" and {len(adverse) - 3} more" if len(adverse) > 3 else ""
        parts.append(f"{len(adverse)} finding(s) need review: {lead}{more}.")
    elif positive:
        parts.append(f"{len(positive)} positive finding(s) and no finding needing review.")
    if gaps:
        parts.append(f"{len(gaps)} data gap(s) recorded.")
    if assessment.is_demo:
        parts.append("Based on demo, non-production policy and bureau data.")
    return " ".join(parts)


def structured(assessment: CreditAssessment) -> CreditMemo:
    fin = assessment.financial_observations
    return CreditMemo(
        profile={"parties": assessment.credit_profile.get("parties", []),
                 "findings": _rows(assessment, FindingCategory.CREDIT_PROFILE)},
        banking={"recorded": fin.get("banking_recorded"),
                 "findings": _rows(assessment, FindingCategory.BANKING)},
        income={"declared_monthly_income": fin.get("declared_monthly_income"),
                "recorded_salary_slip_amount": fin.get("recorded_salary_slip_amount"),
                "income_consistency_recorded": fin.get("income_consistency_recorded"),
                "findings": _rows(assessment, FindingCategory.INCOME_CONSISTENCY)},
        repayment={"findings": _rows(assessment, FindingCategory.REPAYMENT)},
        adverse={"findings": _rows(assessment, FindingCategory.ADVERSE,
                                   FindingCategory.CROSS_SOURCE)},
        exceptions={"exceptions": list(assessment.exceptions),
                    "data_gaps": list(assessment.data_gaps),
                    "status_reasons": list(assessment.status_reasons)},
        summary=_summary(assessment),
        response_source="STRUCTURED",
        validation="NOT_REQUIRED",
    )


# ---------------------------------------------------------------------------
# the one optional model call
# ---------------------------------------------------------------------------

def facts(assessment: CreditAssessment) -> dict[str, Any]:
    """The ONLY data the model sees: the assessment's own facts, no record ids."""
    return {
        "assessment_status": assessment.status.value,
        "findings": [{"category": f.category.value, "status": f.status.value,
                      "severity": f.severity.value, "description": f.description,
                      "observed": f.observed_value,
                      "party_role": f.subject.role if f.subject else None}
                     for f in assessment.findings if f.category is not FindingCategory.DATA_GAP],
        "data_gaps": len(assessment.data_gaps),
        "demo_data": assessment.is_demo,
        "structured_summary": _summary(assessment),
    }


def validate(text: str, assessment: CreditAssessment):
    """The unified validator's Result (accepted, cleaned text or reason, check name)."""
    from app.security import output_validation

    payload = facts(assessment)

    def other_status(cleaned: str) -> str | None:
        lowered = cleaned.lower()
        for status in AssessmentStatus:
            if status is assessment.status:
                continue
            if status.value.lower() in lowered or \
                    status.value.replace("_", " ").lower() in lowered:
                return f"summary asserted a different assessment status: {status.value}"
        return None

    other_status.__name__ = "assessment_status"
    return output_validation.validate(text, surface="credit_memo", truth=payload,
                                      extra=(other_status,))


async def _generate(payload: dict[str, Any]) -> str:
    import httpx

    from app.llm.config import ollama_host, ollama_model
    from app.security import guardrails

    body = {
        "model": ollama_model(),
        "messages": [
            {"role": "system", "content": _SYSTEM + " " + guardrails.UNTRUSTED_NOTICE},
            {"role": "user", "content": "FACTS:\n" + json.dumps(
                guardrails.untrusted(payload), indent=1, default=str)},
        ],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 110},
    }
    async with httpx.AsyncClient(timeout=config.memo_timeout_seconds()) as client:
        response = await client.post(f"{ollama_host().rstrip('/')}/api/chat", json=body)
        response.raise_for_status()
        content = (response.json().get("message") or {}).get("content")
    if not isinstance(content, str):
        raise ValueError("model response carried no text")
    return content


def _skip(run: Any, reason: str) -> None:
    from app.agents.runtime.trajectory import ModelCallRecord

    run.trajectory.record_model(ModelCallRecord(PURPOSE, None, "SKIPPED", 0.0, error=reason))


async def compose(assessment: CreditAssessment, run: Any, *, generator=None) -> CreditMemo:
    """The memo: structured always; the summary reworded by Qwen only if it validates."""
    memo = structured(assessment)
    if not config.llm_enabled():
        memo.validation = "LLM_DISABLED"
        _skip(run, "LLM_DISABLED")
        return memo

    if generator is None:
        from app.llm import availability

        if not availability.provider_reachable():
            memo.validation = "LLM_UNAVAILABLE"
            _skip(run, "LLM_UNAVAILABLE")
            return memo
        generator = _generate

    from app.agents.runtime.errors import ModelCallRefused
    from app.llm.config import ollama_model

    try:
        text = await run.call_model(PURPOSE, lambda: generator(facts(assessment)),
                                    model=ollama_model(),
                                    timeout=config.memo_timeout_seconds())
    except ModelCallRefused as exc:
        memo.validation = f"LLM_NOT_CALLED:{exc.code}"
        return memo
    except Exception as exc:  # timeout, transport, protocol -- the structured memo stands
        from app.agents.runtime.errors import normalize

        memo.validation = f"LLM_FAILED:{normalize(exc).code}"
        return memo

    result = validate(text, assessment)
    if result.accepted:
        memo.summary = result.value
        memo.response_source = "LLM"
        memo.validation = "PASSED"
    else:
        # The failing CHECK's name only -- never the model's words, which may be
        # the very text (a decision word, a leaked identifier) it was refused for.
        memo.validation = "DISCARDED:" + result.check
    return memo


__all__ = ["PURPOSE", "compose", "facts", "structured", "validate"]
