"""
App entrypoint. Run with:
    uvicorn app.main:app --reload
"""

from fastapi import FastAPI

from app.api.routes import auth
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(title=settings.app_name)

app.include_router(auth.router)


@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok"}
