from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from app.documents.models import ParseContext


class TranscriptSegment(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str
    language: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class TranscriptResult(BaseModel):
    provider: str
    language: str | None = None
    duration_ms: int = Field(ge=0)
    segments: list[TranscriptSegment] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AsrProvider(Protocol):
    """Disabled-by-default contract for a future locally verified ASR provider."""

    name: str

    async def transcribe(self, context: ParseContext, audio_path: str, *, max_duration_seconds: int) -> TranscriptResult: ...
