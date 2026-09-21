"""
KYC Agent API.

Consumes what the Document Agent and Financial Agent have ALREADY extracted
and cross-checks it. There is no upload here and no OCR: the caller posts
normalised values, which keeps the endpoint fast and its answers reproducible
for the same inputs.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Response, status

from app.agents.kyc.agent import configuration, run_kyc
from app.agents.kyc.config import enabled
from app.agents.kyc.schemas import CheckStatus, KycRequest, KycResult

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/kyc", tags=["KYC Agent"])


@router.get("/", summary="What this agent checks and how it is configured")
async def kyc_configuration() -> dict:
    return configuration()


@router.post(
    "",
    response_model=KycResult,
    summary="Cross-check one applicant's documents",
    description=(
        "Compares name, date of birth, address, PAN and income across the "
        "documents supplied. Consumes normalised Document Agent / Financial "
        "Agent output; it does not run OCR. Establishes consistency between "
        "documents, NOT that any of them is genuine."
    ),
)
async def assess(request: KycRequest, response: Response) -> KycResult:
    request_id = f"kyc_{uuid.uuid4().hex}"

    if not enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "request_id": request_id,
                "error": "KYC_AGENT_DISABLED",
                "message": "The KYC agent is switched off.",
            },
        )

    try:
        result = run_kyc(request, request_id=request_id)

    except Exception as exc:
        logger.exception("KYC assessment failed request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "request_id": request_id,
                "error": "KYC_ASSESSMENT_FAILED",
                "message": "KYC assessment failed unexpectedly.",
            },
        ) from exc

    # A FAIL is a client-visible rejection of the documents, not a server
    # error, and it carries a full result body. Mirrors the Verification
    # Agent's contract so callers handle both the same way.
    if result.status is CheckStatus.FAIL:
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY

    return result
