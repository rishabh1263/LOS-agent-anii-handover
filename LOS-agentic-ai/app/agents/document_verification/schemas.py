from typing import Any
from pydantic import BaseModel, Field


class DocumentVerificationRequest(BaseModel):
    document_type: str = Field(..., description="Configured document type.")
    request: str | None = Field(default=None)


class DocumentVerificationResponse(BaseModel):
    request_id: str
    document_type: str
    agent: str
    decision: str
    summary: str
    raw_verification: dict[str, Any] | None = None
