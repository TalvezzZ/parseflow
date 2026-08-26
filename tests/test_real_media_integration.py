"""FFmpeg media preparation through public v0.8.0 upload tasks."""
from __future__ import annotations
import asyncio
import shutil
import wave
from pathlib import Path
import httpx
import pytest
from app.main import app


def write_silent_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(8000); output.writeframes(b"\0\0" * 8000)


async def parse_upload(path: Path) -> tuple[dict, httpx.AsyncClient]:
    client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://testserver")
    created=await client.post("/api/v1/tasks/parse",files={"file":(path.name,path.read_bytes(),"audio/wav")}); assert created.status_code == 202
    for _ in range(150):
        task=(await client.get(f"/api/v1/tasks/{created.json()['task_id']}")).json()
        if task["status"] in {"succeeded","partial","failed","cancelled","interrupted"}: return task,client
        await asyncio.sleep(.02)
    await client.aclose(); raise AssertionError("task did not finish")


@pytest.mark.asyncio
async def test_real_audio_prepare_upload_task_artifacts(tmp_path: Path) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"): pytest.skip("当前环境未提供 FFmpeg/FFprobe")
    source=tmp_path/"input.wav"; write_silent_wav(source); task,client=await parse_upload(source)
    try:
        assert task["status"] in {"succeeded","partial"}
        listed=await client.get(f"/api/v1/tasks/{task['task_id']}/artifacts"); assert listed.status_code == 200
        for artifact in listed.json()["artifacts"]:
            assert (await client.get(f"/api/v1/tasks/{task['task_id']}/artifacts/{artifact['artifact_id']}")).status_code == 200
    finally: await client.aclose()


@pytest.mark.asyncio
async def test_legacy_media_path_route_is_removed() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://testserver") as client:
        response=await client.post("/api/v1/prepare/audio",json={"path":"/tmp/private.wav","output_dir":"/tmp/out"})
    assert response.status_code == 404
