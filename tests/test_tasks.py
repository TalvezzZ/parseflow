from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.main import _parse_strategy, _public_document, app
from app.tasks.events import FileTaskEventRepository
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
    capabilities = await request("GET", "/api/v1/capabilities")
    assert capabilities.status_code == 200
    assert ".json" in capabilities.json()["allowed_suffixes"]
    assert {"file_type", "provider", "error_code"}.issubset(capabilities.json()["task_filters"])
    response = await request("POST", "/api/v1/tasks/parse", files={"file": ("task.json", b'{"name":"task"}', "application/json")}, data={"data_id": "customer-001"})

    assert response.status_code == 202, response.text
    submitted = response.json()
    assert set(submitted) == {"task_id", "file_id", "status", "queue_position", "created_at", "links"}
    assert "path" not in response.text and "callback" not in response.text
    task = await wait_for_terminal(submitted["task_id"])
    assert task["status"] == "succeeded"
    assert task["data_id"] == "customer-001"
    assert task["filename"] == "task.json" and task["content_type"] == "application/json"
    assert task["result"]["document"]["document_type"] == "json"
    assert task["result"]["file_id"] == submitted["file_id"]
    assert task["result"]["quality"]["input_classification"] == "json"
    assert task["result"]["provenance"]["provider_chain"]
    export = next(item for item in task["result"]["artifacts"] if item["filename"] == "document.json")
    downloaded = await request("GET", export["download_url"])
    assert downloaded.status_code == 200
    assert json.loads(downloaded.text)["document_type"] == "json"
    assert "path" not in json.dumps(task) and '"path"' not in downloaded.text
    by_type = await request("GET", "/api/v1/tasks?file_type=json&query=task.json")
    assert any(item["task_id"] == submitted["task_id"] for item in by_type.json()["items"])
    provider = task["result"]["provenance"]["provider_chain"][0]
    by_provider = await request("GET", f"/api/v1/tasks?provider={provider}")
    assert any(item["task_id"] == submitted["task_id"] for item in by_provider.json()["items"])
    future = await request("GET", "/api/v1/tasks?created_after=2999-01-01T00:00")
    assert future.status_code == 200 and future.json()["items"] == []
    invalid = await request("GET", "/api/v1/tasks?status=anything&sort=descending")
    assert invalid.status_code == 422
    expired_cursor = await request("GET", "/api/v1/tasks?cursor=task_" + "f" * 32)
    assert expired_cursor.status_code == 200 and expired_cursor.json()["items"] == []


def test_parse_strategy_accepts_only_bounded_goal_hints() -> None:
    assert _parse_strategy("请 OCR 优先处理") == "ocr_first"
    assert _parse_strategy("table-first") == "table_first"
    assert _parse_strategy("任意 provider=/tmp/private") == "auto"


def test_public_document_recursively_removes_server_paths() -> None:
    document = _public_document({"source_file": {"path": "/private/input", "file_id": "file_1"},
                                 "images": [{"path": "/private/image", "relative_path": "images/a.png"}],
                                 "metadata": {"nested": {"output_dir": "/private/out", "title": "safe"}}})
    serialized = json.dumps(document)
    assert "/private/" not in serialized
    assert document["images"][0]["relative_path"] == "images/a.png"


@pytest.mark.asyncio
async def test_manager_filters_persisted_result_facets_and_cursor(tmp_path: Path) -> None:
    repository = FileTaskRepository(tmp_path / "tasks")
    result = TaskResultEnvelope(status="failed", file_id="file_" + "d" * 32,
                                document={"source_file": {"filename": "report.pdf"}, "document_type": "pdf"},
                                provenance={"provider_chain": ["pdf.normal.test"]}, error={"code": "parse_failed"})
    record = TaskRecord(task_id="task_" + "5" * 32, file_id=result.file_id, status="failed", result=result,
                        error={"code": "parse_failed", "message": "failed"})
    await repository.create(record)
    manager = PersistentTaskManager(lambda item: asyncio.sleep(0, result=TaskResultEnvelope(status="succeeded", file_id=item.file_id)), repository)
    items, cursor = await manager.list(status="failed", file_type="pdf", provider="normal", error_code="parse_failed", limit=1)
    assert [item.task_id for item in items] == [record.task_id] and cursor is None


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


@pytest.mark.asyncio
async def test_terminal_task_can_retry_and_delete_via_public_api() -> None:
    created = await request("POST", "/api/v1/tasks/parse", files={"file": ("retry.json", b"{}", "application/json")})
    task_id = created.json()["task_id"]
    task = await wait_for_terminal(task_id)
    assert task["status"] == "succeeded"
    retried = await request("POST", f"/api/v1/tasks/{task_id}/retry")
    assert retried.status_code == 202
    retry_id = retried.json()["task_id"]
    retry_task = await request("GET", f"/api/v1/tasks/{retry_id}")
    assert retry_task.json()["retry_of"] == task_id
    deleted = await request("DELETE", f"/api/v1/tasks/{task_id}")
    assert deleted.status_code == 204
    assert (await request("GET", f"/api/v1/tasks/{task_id}")).status_code == 404


@pytest.mark.asyncio
async def test_event_repository_recursively_removes_private_fields(tmp_path: Path) -> None:
    repository = FileTaskEventRepository(tmp_path / "events")
    task_id = "task_" + "a" * 32
    await repository.append(task_id, "warning", details={"provider": "safe", "nested": {"path": "/secret", "value": 1}})
    event = (await repository.list(task_id)).items[0]
    assert event.details == {"provider": "safe", "nested": {"value": 1}}


@pytest.mark.asyncio
async def test_task_events_are_persistent_ordered_and_path_free() -> None:
    created = await request("POST", "/api/v1/tasks/parse", files={"file": ("events.json", b"{}", "application/json")})
    task_id = created.json()["task_id"]
    await wait_for_terminal(task_id)
    response = await request("GET", f"/api/v1/tasks/{task_id}/events?limit=2")
    assert response.status_code == 200
    first = response.json()
    assert [item["type"] for item in first["items"]] == ["created", "queued"]
    assert first["next_after"] == 2
    rest = (await request("GET", f"/api/v1/tasks/{task_id}/events?after=2")).json()["items"]
    assert [item["sequence"] for item in rest] == list(range(3, len(rest) + 3))
    assert rest[-1]["type"] == "succeeded"
    serialized = json.dumps(first | {"rest": rest})
    assert '"path"' not in serialized and '"output_dir"' not in serialized


def test_openapi_has_no_path_callback_or_output_directory_public_inputs() -> None:
    schema = app.openapi()
    serialized = json.dumps(schema)
    assert "/api/v1/tasks/parse" in serialized
    assert '"callback"' not in serialized
    assert '"output_dir"' not in serialized
    assert '"path"' not in serialized.replace('"in": "path"', '"in": "route_parameter"')
