"""
App entrypoint. Run with:
    uvicorn app.main:app --reload
"""

from fastapi import FastAPI

from app.api.routes import auth, case
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(title=settings.app_name)

# Auth routes  (login / refresh / logout / me)
app.include_router(auth.router)

# Case-data routes  (fetch case + app data — separate from auth)
app.include_router(case.router)


@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok"}
