from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from openpyxl import Workbook

from app.tasks.manager import PersistentTaskManager
from app.tasks.models import TaskResultEnvelope
from app.tasks.repository import FileTaskRepository


@pytest.mark.asyncio
async def test_late_cancel_discards_excel_outputs_for_opaque_mcp_task(tmp_path: Path) -> None:
    started, released = asyncio.Event(), asyncio.Event()
    repository = FileTaskRepository(tmp_path / "tasks")

    async def runner(record):
        output = tmp_path / "artifacts" / record.task_id / "files"
        output.mkdir(parents=True, exist_ok=True)
        (output / "workbook.sqlite").write_bytes(b"already published")
        (output.parent / "manifest.json").write_text("[]")
        started.set()
        await released.wait()
        return TaskResultEnvelope(status="succeeded", file_id=record.file_id)

    manager = PersistentTaskManager(runner, repository, cleanup_interval_seconds=3600)
    try:
        submitted = await manager.submit_parse("file_" + "4" * 32, None, None)
        await asyncio.wait_for(started.wait(), timeout=3)
        await manager.cancel(submitted.task_id)
        root = tmp_path / "artifacts" / submitted.task_id
        assert (root / ".excel-cancel").is_file()
        released.set()
        for _ in range(100):
            record = await manager.get(submitted.task_id)
            if record.status == "cancelled":
                break
            await asyncio.sleep(0.01)
        assert record.status == "cancelled"
        assert not (root / "files" / "workbook.sqlite").exists()
        assert not (root / "manifest.json").exists()
    finally:
        released.set()
        await manager.stop()


@pytest.mark.asyncio
async def test_excel_task_upload_pagination_and_complete_csv() -> None:
    from app import main
    source = BytesIO()
    workbook = Workbook()
    workbook.active.title = "明细"
    workbook.active["A1"] = "名称"
    workbook.active["C1"] = "金额"
    workbook.active["A105"] = "完整数据末行"
    workbook.active["C105"] = 999
    workbook.save(source)
    transport = httpx.ASGITransport(app=main.app)
    task_id = None
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        try:
            response = await client.post("/api/v1/tasks/parse", files={"file": ("pagination.xlsx", source.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
            assert response.status_code == 202, response.text
            task_id = response.json()["task_id"]
            for _ in range(300):
                response = await client.get(f"/api/v1/tasks/{task_id}")
                task = response.json()
                if task["status"] in {"succeeded", "partial", "failed"}:
                    break
                await asyncio.sleep(0.02)
            assert task["status"] == "succeeded", task
            assert task["result"]["quality"]["truncated"] is False
            assert task["result"]["document"]["tables"][0]["preview_truncated"] is True
            page = (await client.get(f"/api/v1/tasks/{task_id}/sheets/0/rows?limit=1")).json()
            assert page["next_after_row"] == 1
            last = (await client.get(f"/api/v1/tasks/{task_id}/sheets/0/rows?after_row=1")).json()
            assert last["rows"][0]["row"] == 105
            assert last["rows"][0]["cells"][1]["column"] == 3
            artifacts = task["result"]["artifacts"]
            csv = next(item for item in artifacts if item["filename"] == "sheet-000.csv")
            data = await client.get(csv["download_url"])
            assert "完整数据末行,,999" in data.content.decode("utf-8-sig")
            assert any(item["filename"] == "workbook.sqlite" for item in artifacts)
            assert "request.json" not in str(artifacts)
        finally:
            if task_id:
                await client.delete(f"/api/v1/tasks/{task_id}")
