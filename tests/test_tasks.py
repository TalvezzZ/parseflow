from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.main import app
from app.tasks.manager import InMemoryTaskManager, TaskQueueFullError
from app.tasks.models import TaskRecord


async def post(path: str, payload: dict) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(path, json=payload)


async def get(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


async def wait_for_terminal(task_id: str) -> dict:
    for _ in range(100):
        response = await get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200, response.text
        task = response.json()
        if task["status"] in {"succeeded", "failed", "cancelled"}:
            return task
        await asyncio.sleep(0.02)
    raise AssertionError("任务未在测试时间内结束")


def make_xlsx(path: Path) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.append(["name", "value"])
    workbook.active.append(["task", 1])
    workbook.save(path)


@pytest.mark.asyncio
async def test_skill_task_api_executes_and_preserves_data_id(tmp_path: Path) -> None:
    source = tmp_path / "task.xlsx"
    make_xlsx(source)

    response = await post("/api/v1/tasks/skill", {
        "skill_name": "excel.parse",
        "file_id": "task-xlsx",
        "path": str(source),
        "data_id": "customer-001",
    })

    assert response.status_code == 202, response.text
    submitted = response.json()
    assert submitted["status"] == "queued"
    task = await wait_for_terminal(submitted["task_id"])
    assert task["status"] == "succeeded"
    assert task["data_id"] == "customer-001"
    assert task["result"]["skill_name"] == "excel.parse"
    assert task["callback_status"] == "not_requested"


@pytest.mark.asyncio
async def test_manager_limits_queue_and_cancels_queued_task() -> None:
    release = asyncio.Event()
    running = asyncio.Event()

    async def runner(_: TaskRecord) -> dict:
        running.set()
        await release.wait()
        return {"status": "success"}

    manager = InMemoryTaskManager(runner, max_concurrent_executions=1, max_queue_size=1)
    try:
        first = await manager.submit("skill.execute", {}, None, None)
        await asyncio.wait_for(running.wait(), timeout=1)
        second = await manager.submit("skill.execute", {}, None, None)
        with pytest.raises(TaskQueueFullError) as error:
            await manager.submit("skill.execute", {}, None, None)
        assert error.value.queued_tasks == 1
        cancelled = manager.cancel(second.task_id)
        assert cancelled is not None
        assert cancelled.status == "cancelled"
        release.set()
        for _ in range(50):
            if manager.get(first.task_id).status == "succeeded":
                break
            await asyncio.sleep(0.01)
        assert manager.get(first.task_id).status == "succeeded"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_callback_non_200_does_not_change_execution_status(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[dict] = []

    class MockClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url: str, json: dict) -> httpx.Response:
            received.append({"url": url, "payload": json})
            return httpx.Response(502)

    monkeypatch.setattr("app.tasks.manager.httpx.AsyncClient", MockClient)

    async def runner(_: TaskRecord) -> dict:
        return {"status": "success", "value": "done"}

    manager = InMemoryTaskManager(runner)
    try:
        submitted = await manager.submit("skill.execute", {"timeout_seconds": 10}, "business-1", "https://example.test/callback")
        for _ in range(50):
            record = manager.get(submitted.task_id)
            if record and record.status == "succeeded" and record.callback_status == "failed":
                break
            await asyncio.sleep(0.01)
        record = manager.get(submitted.task_id)
        assert record is not None
        assert record.status == "succeeded"
        assert record.callback_status == "failed"
        assert record.callback_status_code == 502
        assert received[0]["payload"]["data_id"] == "business-1"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_callback_http_200_is_recorded_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class MockClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url: str, json: dict) -> httpx.Response:
            return httpx.Response(200)

    monkeypatch.setattr("app.tasks.manager.httpx.AsyncClient", MockClient)

    async def runner(_: TaskRecord) -> dict:
        return {"status": "success"}

    manager = InMemoryTaskManager(runner)
    try:
        submitted = await manager.submit("skill.execute", {}, None, "https://example.test/callback")
        for _ in range(50):
            record = manager.get(submitted.task_id)
            if record and record.callback_status == "succeeded":
                break
            await asyncio.sleep(0.01)
        assert manager.get(submitted.task_id).status == "succeeded"
        assert manager.get(submitted.task_id).callback_status == "succeeded"
    finally:
        await manager.stop()
