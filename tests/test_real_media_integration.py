"""真实 FFmpeg 媒体预处理 API 集成测试，不调用 ASR 模型。"""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import httpx
import pytest

from app.main import app


def write_silent_wav(path: Path, seconds: int = 1, sample_rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * seconds * sample_rate)


async def post(path: str, payload: dict) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(path, json=payload)


@pytest.mark.asyncio
async def test_real_audio_prepare_api(tmp_path: Path) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("当前环境未提供 FFmpeg/FFprobe，无法验证真实媒体预处理")
    source = tmp_path / "input.wav"
    output_dir = tmp_path / "artifacts"
    write_silent_wav(source)

    response = await post(
        "/api/v1/prepare/audio",
        {"file_id": "audio-real", "path": str(source), "output_dir": str(output_dir)},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "success"
    media = payload["data"]["media"]
    assert media["media_type"] == "audio"
    assert media["transcript_available"] is False
    assert media["asr_status"] == "not_requested"
    audio = next(item for item in media["artifacts"] if item["kind"] == "normalized_audio")
    normalized = Path(audio["path"])
    assert normalized.is_file()
    with wave.open(str(normalized), "rb") as output:
        assert output.getnchannels() == 1
        assert output.getframerate() == 16000


@pytest.mark.asyncio
async def test_real_video_prepare_extracts_audio_and_subtitle(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("当前环境未提供 FFmpeg/FFprobe，无法验证真实媒体预处理")
    audio = tmp_path / "source.wav"
    subtitle = tmp_path / "source.srt"
    source = tmp_path / "source.mp4"
    output_dir = tmp_path / "artifacts"
    write_silent_wav(audio)
    subtitle.write_text("1\n00:00:00,000 --> 00:00:00,800\nEmbedded subtitle\n", encoding="utf-8")
    created = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:d=1",
            "-i",
            str(audio),
            "-i",
            str(subtitle),
            "-shortest",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-c:s",
            "mov_text",
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert created.returncode == 0, created.stderr

    response = await post(
        "/api/v1/prepare/video",
        {"file_id": "video-real", "path": str(source), "output_dir": str(output_dir)},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "success"
    media = payload["data"]["media"]
    assert media["media_type"] == "video"
    assert media["audio_streams"]
    assert media["subtitle_streams"]
    assert media["transcript_available"] is False
    artifacts = {item["kind"]: Path(item["path"]) for item in media["artifacts"]}
    assert artifacts["normalized_audio"].is_file()
    assert artifacts["subtitle"].is_file()
    assert "Embedded subtitle" in artifacts["subtitle"].read_text(encoding="utf-8")
