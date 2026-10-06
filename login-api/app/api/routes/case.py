"""
Case-data routes.

POST /api/v1/case/fetch
  - Requires a valid Bearer JWT (same token issued by /login).
  - Accepts { case_id, app_id } in the request body.
  - Case ID and APP ID are NOT tied to the logged-in user -- any
    authenticated user can access any valid combination.
  - Returns all data associated with that Case ID + APP ID.

NOTE: The actual data-fetch logic below is a stub that calls your
existing FOS / LOS services (or DB) via the same access-token the
frontend already holds.  Replace the stub block with real service calls
once the upstream API contract is confirmed.
"""

import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.security import get_current_user
from app.schemas.case import CaseDataResponse, CaseFetchRequest

router = APIRouter(prefix="/api/v1/case", tags=["Case"])

# ---------------------------------------------------------------------------
# Optional upstream base URLs read from .env so nothing is hard-coded.
# If not set the stubs return empty data -- replace with real calls later.
# ---------------------------------------------------------------------------
_FOS_BASE = os.getenv("FOS_BASE_URL", "").rstrip("/")
_LOS_BASE = os.getenv("LOS_BASE_URL", "").rstrip("/")


def _extract_bearer_token(request: Request) -> str:
    """Extract the raw Bearer token string from the Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return ""


async def _fetch_upstream(
    case_id: str,
    app_id: str,
    token: str,
) -> dict:
    """
    Aggregate case + application data from upstream FOS / LOS APIs.
    Replace / extend this function as your service contracts evolve.
    """
    result: dict = {}

    # --- FOS applicant / case data (best-effort) -------------------------
    if _FOS_BASE:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{_FOS_BASE}/api/v1/fos/applicants/{app_id}",
                    params={"case_id": case_id},
                    headers={"Authorization": f"Bearer {token}"},
                )
                if r.is_success:
                    result["applicant"] = r.json()
        except Exception:
            pass  # upstream not reachable -- continue

    # --- LOS process / document data (best-effort) -----------------------
    if _LOS_BASE:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{_LOS_BASE}/api/v1/los/cases/{case_id}",
                    params={"app_id": app_id},
                    headers={"Authorization": f"Bearer {token}"},
                )
                if r.is_success:
                    result["case"] = r.json()
        except Exception:
            pass

    # --- Fallback stub when no upstreams are configured ------------------
    if not result:
        result = {
            "case_id": case_id,
            "app_id": app_id,
            "note": (
                "No upstream FOS_BASE_URL / LOS_BASE_URL configured. "
                "Set them in .env to fetch real data."
            ),
        }

    return result


@router.post("/fetch", response_model=CaseDataResponse)
async def fetch_case_data(
    payload: CaseFetchRequest,
    request: Request,
    current_user: str = Depends(get_current_user),
) -> CaseDataResponse:
    """
    Fetch all data for a given Case ID + APP ID.

    Authentication: Bearer JWT from /login.
    Authorization:  Any authenticated user -- IDs are NOT user-scoped.
    The same Bearer token is forwarded to upstream FOS / LOS services
    so they can authenticate the caller.
    """
    case_id = payload.case_id.strip()
    app_id = payload.app_id.strip()

    if not case_id or not app_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="case_id and app_id must not be blank.",
        )

    # Forward the caller's own Bearer token to upstream services so they
    # can authenticate. This avoids storing or re-issuing service tokens.
    bearer_token = _extract_bearer_token(request)
    data = await _fetch_upstream(case_id, app_id, token=bearer_token)

    applicant = data.get("applicant") if isinstance(data, dict) else None
    application = data.get("application") if isinstance(data, dict) else None
    checklist = data.get("checklist") if isinstance(data, dict) else None
    documents = data.get("documents") if isinstance(data, dict) else None
    required_documents = data.get("required_documents") if isinstance(data, dict) else None
    stage = data.get("stage") if isinstance(data, dict) else None

    return CaseDataResponse(
        case_id=case_id,
        app_id=app_id,
        found=True,
        applicant=applicant,
        application=application,
        checklist=checklist,
        documents=documents,
        required_documents=required_documents,
        stage=stage,
        data=data,
        message="Case data fetched successfully",
    )
