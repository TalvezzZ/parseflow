from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest


@pytest.mark.asyncio
async def test_large_text_task_record_is_preview_full_file_survives_restart_and_delete(tmp_path: Path, monkeypatch) -> None:
    from app import main
    from app.tasks.repository import FileTaskRepository
    monkeypatch.setattr(main.settings, "result_preview_max_bytes", 16384)
    text = "完整正文数据\n" * 20000 + "THE_END_OF_COMPLETE_DOCUMENT"
    task_id = None
    root = None
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        try:
            response = await client.post("/api/v1/tasks/parse", files={"file": ("long.txt", text.encode(), "text/plain")})
            assert response.status_code == 202, response.text
            task_id = response.json()["task_id"]
            for _ in range(400):
                response = await client.get(f"/api/v1/tasks/{task_id}")
                task = response.json()
                if task["status"] in {"succeeded", "partial", "failed"}:
                    break
                await asyncio.sleep(0.02)
            assert task["status"] == "succeeded", task
            ref = task["result"]["result_ref"]
            assert ref["preview_truncated"] is True
            assert len(response.content) < 150 * 1024
            assert "THE_END_OF_COMPLETE_DOCUMENT" not in response.text
            downloaded = await client.get(ref["document_url"])
            assert downloaded.status_code == 200
            full = downloaded.json()
            assert full["representations"]["plain_text"].endswith("THE_END_OF_COMPLETE_DOCUMENT")
            assert "完整正文数据" in full["representations"]["plain_text"]
            root = main.artifact_repository.workspace(task_id).parent
            restarted = FileTaskRepository(main.task_repository.root)
            loaded = await restarted.get(task_id)
            assert loaded.result.result_ref["storage_key"] == "result/manifest.json"
            assert len(loaded.model_dump_json().encode()) < 150 * 1024
            manifest = (await client.get(ref["manifest_url"])).json()
            assert manifest["units"]
            assert all("sha256" in item for item in manifest["resources"])
            assert str(root) not in json.dumps(manifest)
        finally:
            if task_id:
                response = await client.delete(f"/api/v1/tasks/{task_id}")
                assert response.status_code == 204
                assert not root or not root.exists()
