"""KYC Agent: cross-document consistency for one applicant."""

from app.agents.kyc.agent import configuration, run_kyc
from app.agents.kyc.schemas import (
    AddressInput,
    CheckResult,
    CheckStatus,
    Evidence,
    IncomeInput,
    KycCheck,
    KycDocumentType,
    KycRequest,
    KycResult,
    ReasonCode,
    SourceDocument,
)

__all__ = [
    "run_kyc", "configuration",
    "KycRequest", "KycResult", "SourceDocument", "AddressInput", "IncomeInput",
    "CheckResult", "CheckStatus", "KycCheck", "KycDocumentType", "ReasonCode",
    "Evidence",
]
