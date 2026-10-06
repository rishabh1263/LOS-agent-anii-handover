"""
Request/response models for the case-data endpoint.
Kept fully separate from auth schemas -- Case ID / APP ID are NOT
linked to any logged-in username; any authenticated user can query
any valid combination.
"""

from typing import Any

from pydantic import BaseModel, Field


class CaseFetchRequest(BaseModel):
    """Body for POST /api/v1/case/fetch"""

    case_id: str = Field(..., min_length=1, examples=["CASE-abc123"])
    app_id: str = Field(..., min_length=1, examples=["APP-xyz789"])


class CaseDataResponse(BaseModel):
    """
    All data returned for a given Case ID + APP ID pair.
    Both direct structured fields and a normalized `data` dict are provided.
    """

    case_id: str
    app_id: str
    found: bool = True
    applicant: dict[str, Any] | None = None
    application: dict[str, Any] | None = None
    checklist: list[dict[str, Any]] | None = None
    documents: list[dict[str, Any]] | None = None
    required_documents: list[str] | None = None
    stage: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    message: str = "Case data fetched successfully"
