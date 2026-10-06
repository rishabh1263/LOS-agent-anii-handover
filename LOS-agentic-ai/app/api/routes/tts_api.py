"""
POST /api/v1/tts -- speak a chat response in the response language's voice.

The frontend sends back `presentation.audio.text` and `.language` from a chat
response. Authenticated like every business route; the text is capped and never
logged. 503 with a CONFIGURATION_GAP code when no TTS service is configured.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.security.auth import require_jwt
from app.tts import service

router = APIRouter(tags=["Speech"])


class TtsRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000, description="`presentation.audio.text` from a chat reply.")
    language: str = Field(..., max_length=12, examples=["mr"], description="`presentation.audio.language`.")


@router.post("/tts", summary="Speak a chat response (backend TTS, one voice per language)",
             responses={200: {"content": {"audio/wav": {}}, "description": "Audio bytes."}})
async def speak(request: TtsRequest, claims: dict[str, Any] = Depends(require_jwt)) -> Response:
    import asyncio

    try:
        audio, media_type = await asyncio.to_thread(service.synthesize, request.text, request.language)
    except service.TtsUnavailable as exc:
        raise HTTPException(503, detail={"code": exc.code, "message": exc.message}) from None
    return Response(content=audio, media_type=media_type)


__all__ = ["router"]
