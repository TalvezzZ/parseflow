from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.skills.media.asr import TranscriptResult, TranscriptSegment


def test_asr_contract_has_timeline_language_and_unknown_confidence() -> None:
    result = TranscriptResult(provider="local", language="zh", duration_ms=1200,
                              segments=[TranscriptSegment(start_ms=0, end_ms=1200, text="你好")])
    assert result.segments[0].confidence is None
    assert result.model_dump()["segments"][0]["language"] is None


def test_asr_contract_rejects_invalid_confidence() -> None:
    with pytest.raises(ValidationError):
        TranscriptSegment(start_ms=0, end_ms=1, text="x", confidence=1.1)
