from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.main import app
from app.tasks.manager import PersistentTaskManager, TaskQueueFullError
from app.tasks.models import TaskRecord, TaskResultEnvelope
from app.tasks.repository import FileTaskRepository, TaskRevisionConflictError


async def request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


async def wait_for_terminal(task_id: str) -> dict:
    for _ in range(100):
        response = await request("GET", f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200, response.text
        task = response.json()
        if task["status"] in {"succeeded", "partial", "failed", "cancelled", "interrupted"}:
            return task
        await asyncio.sleep(0.02)
    raise AssertionError("任务未在测试时间内结束")


@pytest.mark.asyncio
async def test_upload_only_task_api_returns_path_free_normalized_result() -> None:
    response = await request("POST", "/api/v1/tasks/parse", files={"file": ("task.json", b'{"name":"task"}', "application/json")}, data={"data_id": "customer-001"})

    assert response.status_code == 202, response.text
    submitted = response.json()
    assert set(submitted) == {"task_id", "file_id", "status", "queue_position", "created_at", "links"}
    assert "path" not in response.text and "callback" not in response.text
    task = await wait_for_terminal(submitted["task_id"])
    assert task["status"] == "succeeded"
    assert task["data_id"] == "customer-001"
    assert task["result"]["document"]["document_type"] == "json"
    assert task["result"]["file_id"] == submitted["file_id"]
    assert "path" not in json.dumps(task)


@pytest.mark.asyncio
async def test_persistent_manager_recovers_queued_and_marks_active_interrupted(tmp_path: Path) -> None:
    repository = FileTaskRepository(tmp_path / "tasks")
    queued = TaskRecord(task_id="task_" + "1" * 32, file_id="file_" + "a" * 32)
    running = TaskRecord(task_id="task_" + "2" * 32, file_id="file_" + "b" * 32, status="running")
    await repository.create(queued)
    await repository.create(running)

    async def runner(record: TaskRecord) -> TaskResultEnvelope:
        return TaskResultEnvelope(status="succeeded", file_id=record.file_id)

    manager = PersistentTaskManager(runner, repository, max_concurrent_executions=1)
    await manager.start()
    try:
        for _ in range(50):
            finished = await manager.get(queued.task_id)
            if finished and finished.status == "succeeded":
                break
            await asyncio.sleep(0.01)
        assert (await manager.get(queued.task_id)).status == "succeeded"
        assert (await manager.get(running.task_id)).status == "interrupted"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_repository_enforces_revision_and_quarantines_invalid_json(tmp_path: Path) -> None:
    repository = FileTaskRepository(tmp_path / "tasks")
    record = TaskRecord(task_id="task_" + "3" * 32, file_id="file_" + "c" * 32)
    await repository.create(record)
    updated = await repository.update(record.task_id, 0, lambda item: setattr(item, "goal", "extract"))
    assert updated.revision == 1
    with pytest.raises(TaskRevisionConflictError):
        await repository.update(record.task_id, 0, lambda _: None)
    invalid = tmp_path / "tasks" / ("task_" + "4" * 32 + ".json")
    invalid.write_text("not json", encoding="utf-8")
    await repository.recover()
    assert not invalid.exists()
    assert (tmp_path / "tasks" / "quarantine" / invalid.name).exists()


@pytest.mark.asyncio
async def test_persistent_manager_limits_queue_and_cancels_queued_task(tmp_path: Path) -> None:
    release, running = asyncio.Event(), asyncio.Event()

    async def runner(record: TaskRecord) -> TaskResultEnvelope:
        running.set()
        await release.wait()
        return TaskResultEnvelope(status="succeeded", file_id=record.file_id)

    manager = PersistentTaskManager(runner, FileTaskRepository(tmp_path / "tasks"), max_concurrent_executions=1, max_queue_size=1)
    await manager.start()
    try:
        first = await manager.submit_parse("file_" + "d" * 32, None, None)
        await asyncio.wait_for(running.wait(), timeout=1)
        second = await manager.submit_parse("file_" + "e" * 32, None, None)
        with pytest.raises(TaskQueueFullError):
            await manager.submit_parse("file_" + "f" * 32, None, None)
        assert (await manager.cancel(second.task_id)).status == "cancelled"
        release.set()
        for _ in range(50):
            if (await manager.get(first.task_id)).status == "succeeded":
                break
            await asyncio.sleep(0.01)
        assert (await manager.get(first.task_id)).status == "succeeded"
    finally:
        await manager.stop()


def test_openapi_has_no_path_callback_or_output_directory_public_inputs() -> None:
    schema = app.openapi()
    serialized = json.dumps(schema)
    assert "/api/v1/tasks/parse" in serialized
    assert '"callback"' not in serialized
    assert '"output_dir"' not in serialized
    assert '"path"' not in serialized.replace('"in": "path"', '"in": "route_parameter"')
