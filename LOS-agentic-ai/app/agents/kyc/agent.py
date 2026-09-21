"""
KYC Agent.

Answers one question: do these documents describe the same person,
consistently? It runs entirely on values the Document Agent and Financial
Agent have already extracted -- it never touches an image, and it never
re-runs OCR.

It does NOT establish that any document is genuine. That is the Verification
Agent's question, and neither agent detects a competent forgery.

Decisioning
-----------
Each check returns PASS, REVIEW, FAIL or SKIPPED. The overall verdict is the
worst of them, with one qualification: a check marked non-blocking in policy
can raise the result to REVIEW but cannot by itself FAIL an applicant. Address
and income are non-blocking by default because documents legitimately disagree
on both -- people move, and bank credits are not salary. The finding is never
suppressed; it is reported with its own FAIL status and reason codes, and only
its effect on the overall verdict is capped.
"""

from __future__ import annotations

import logging
import time

from app.agents.kyc import checks, config, fields as fields_lib
from app.agents.kyc.schemas import (
    CheckResult,
    CheckStatus,
    KycCheck,
    KycRequest,
    KycResult,
    ReasonCode,
)

logger = logging.getLogger(__name__)

# check -> (policy section name, function)
_CHECKS: tuple[tuple[KycCheck, str, object], ...] = (
    (KycCheck.NAME, "name", checks.check_name),
    (KycCheck.DOB, "dob", checks.check_dob),
    (KycCheck.ADDRESS, "address", checks.check_address),
    (KycCheck.PAN, "pan", checks.check_pan),
    (KycCheck.FATHER_NAME, "father_name", checks.check_father_name),
    (KycCheck.INCOME, "income", checks.check_income),
)


#: KycCheck -> the kyc_policies.yaml section that governs it. Exported so a
#: caller can ask whether a failed check is blocking without re-deriving the
#: mapping and letting the two copies drift.
CHECK_SECTIONS: dict[str, str] = {
    kind.value: section for kind, section, _run in _CHECKS
}


def check_is_blocking(check: str) -> bool:
    """Whether this check, when it fails, may drive the overall verdict."""
    section = CHECK_SECTIONS.get(str(check))
    return config.check_blocking(section) if section else False


_SEVERITY = {
    CheckStatus.PASS: 0,
    CheckStatus.SKIPPED: 0,
    CheckStatus.REVIEW: 1,
    CheckStatus.FAIL: 2,
}


def _cap(status: CheckStatus, blocking: bool) -> CheckStatus:
    """A non-blocking check contributes at most REVIEW to the overall verdict."""
    if blocking or status is not CheckStatus.FAIL:
        return status
    return CheckStatus.REVIEW


def run_kyc(request: KycRequest, request_id: str = "") -> KycResult:
    """Run every enabled check and combine them into one explainable verdict."""

    started = time.perf_counter()

    results: list[CheckResult] = []
    effective: list[CheckStatus] = []

    for kind, section, run in _CHECKS:
        try:
            result = run(request.documents)
        except Exception as exc:
            # A check that blows up must not take the whole assessment with it.
            # It is reported as REVIEW so the applicant reaches a human rather
            # than being passed or rejected on a bug.
            logger.exception(
                "KYC check %s failed request_id=%s", kind.value, request_id
            )
            result = CheckResult(
                check=kind,
                status=CheckStatus.REVIEW,
                detail=f"Check did not complete: {type(exc).__name__}: {exc}",
            )

        results.append(result)
        effective.append(_cap(result.status, config.check_blocking(section)))

    overall = max(effective, key=lambda s: _SEVERITY[s], default=CheckStatus.REVIEW)

    reason_codes: list[ReasonCode] = []
    for result in results:
        if result.status in (CheckStatus.REVIEW, CheckStatus.FAIL):
            reason_codes.extend(result.reason_codes)

    # A verdict of PASS has to mean something was actually cross-checked. With
    # nothing to compare against, "no disagreement found" is not evidence of
    # consistency, so it goes to review instead.
    #
    # This also covers the case where every check was SKIPPED: the caller asked
    # for a verdict, and SKIPPED is not one they can act on.
    minimum = int(config.threshold("decision", "min_sources_for_pass", 2.0))
    compared = sum(1 for result in results if result.status is not CheckStatus.SKIPPED)

    if overall in (CheckStatus.PASS, CheckStatus.SKIPPED) and (
        len(request.documents) < minimum or compared == 0
    ):
        overall = CheckStatus.REVIEW
        reason_codes.append(ReasonCode.INSUFFICIENT_SOURCES)

    # Stable, de-duplicated, order-independent.
    ordered = sorted({code for code in reason_codes}, key=lambda c: c.value)

    # The field-level view, DERIVED from the checks above. It re-compares
    # nothing and can change no verdict; if it could, the result would have
    # two authors and an auditor would have to read both.
    field_results = [
        fields_lib.build(result, request.documents)
        for result in results
        if result.check in fields_lib.CHECK_TO_FIELD
    ]
    overall_score, overall_confidence = fields_lib.roll_up(field_results)

    return KycResult(
        request_id=request_id,
        applicant_id=request.applicant_id,
        status=overall,
        reason_codes=ordered,
        checks=results,
        fields=field_results,
        overall_score=overall_score,
        overall_confidence=overall_confidence,
        sources_received=len(request.documents),
        policy_version=config.policy_version(),
        processing_ms=round((time.perf_counter() - started) * 1000, 2),
    )


def configuration() -> dict:
    """What this agent checks, and the thresholds it is running with."""
    return {
        "enabled": config.enabled(),
        "version": config.version(),
        "policy_version": config.policy_version(),
        "policy_file": str(config.policy_path()),
        "checks": {
            section: {
                "enabled": config.check_enabled(section),
                "blocking": config.check_blocking(section),
                "thresholds": {
                    k: v for k, v in config.section(section).items()
                    if k not in ("enabled", "blocking")
                },
            }
            for _kind, section, _run in _CHECKS
        },
        "statuses": {
            "PASS": "every comparable field agrees across documents",
            "REVIEW": "a difference a human should look at",
            "FAIL": "a blocking field plainly disagrees",
            "SKIPPED": "nothing to cross-check, or switched off in policy",
        },
        "field_weights": config.section("fields").get("weights") or {},
        "confidence_model": {
            "method_base": config.section("confidence").get("method_base") or {},
            "penalties": config.section("confidence").get("penalties") or {},
            "low_quality_below": config.threshold(
                "confidence", "low_quality_below", 0.60),
            "ambiguity_margin": config.threshold(
                "confidence", "ambiguity_margin", 0.05),
            "note": (
                "match_score is how closely values agree. confidence is how "
                "far that answer can be relied on. They are computed "
                "separately and neither is derived from the other."
            ),
        },
        "authenticity_checked": False,
        "authenticity_note": (
            "KYC establishes consistency BETWEEN documents. It does not "
            "establish that any of them is genuine."
        ),
    }


__all__ = [
    "run_kyc", "configuration", "check_is_blocking", "CHECK_SECTIONS",
]
