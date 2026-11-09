from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from app import main as main_module
from app.main import _parse_strategy, _public_document, app
from app.tasks.events import FileTaskEventRepository
from app.tasks.manager import PersistentTaskManager, TaskQueueFullError, TaskShutdownTimeoutError
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


@pytest.mark.parametrize(
    "options",
    [
        {"max_concurrent_executions": 0},
        {"max_queue_size": 0},
        {"default_timeout_seconds": 0},
        {"shutdown_grace_seconds": -1},
        {"shutdown_cancel_grace_seconds": -1},
    ],
)
def test_manager_rejects_invalid_runtime_bounds(tmp_path: Path, options: dict[str, int]) -> None:
    async def runner(record: TaskRecord) -> TaskResultEnvelope:
        return TaskResultEnvelope(status="succeeded", file_id=record.file_id)

    with pytest.raises(ValueError):
        PersistentTaskManager(runner, FileTaskRepository(tmp_path / "tasks"), **options)


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
async def test_submission_rolls_back_if_queue_event_persistence_fails(tmp_path: Path) -> None:
    class FailingEvents(FileTaskEventRepository):
        async def append(self, task_id: str, event_type: str, **kwargs):
            if event_type == "queued":
                raise OSError("simulated event storage failure")
            return await super().append(task_id, event_type, **kwargs)

    repository = FileTaskRepository(tmp_path / "tasks")
    events = FailingEvents(tmp_path / "events")

    async def runner(record: TaskRecord) -> TaskResultEnvelope:
        return TaskResultEnvelope(status="succeeded", file_id=record.file_id)

    manager = PersistentTaskManager(runner, repository, event_repository=events)
    await manager.start()
    try:
        with pytest.raises(OSError, match="simulated event storage failure"):
            await manager.submit_parse("file_" + "5" * 32, None, None)
        assert await repository.list() == []
        assert manager._queue.qsize() == 0
        assert list(events.root.glob("task_*.json")) == []
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_concurrent_submissions_cannot_exceed_waiting_queue_capacity(tmp_path: Path) -> None:
    running, release = asyncio.Event(), asyncio.Event()

    async def runner(record: TaskRecord) -> TaskResultEnvelope:
        running.set()
        await release.wait()
        return TaskResultEnvelope(status="succeeded", file_id=record.file_id)

    manager = PersistentTaskManager(runner, FileTaskRepository(tmp_path / "tasks"),
                                    max_concurrent_executions=1, max_queue_size=1)
    await manager.start()
    try:
        await manager.submit_parse("file_" + "a" * 32, None, None)
        await asyncio.wait_for(running.wait(), timeout=1)
        results = await asyncio.gather(
            *(manager.submit_parse(f"file_{index:032x}", None, None) for index in range(20)),
            return_exceptions=True,
        )
        accepted = [item for item in results if not isinstance(item, Exception)]
        rejected = [item for item in results if isinstance(item, TaskQueueFullError)]
        assert len(accepted) == 1
        assert len(rejected) == 19
        assert manager._queue.qsize() == 1
    finally:
        release.set()
        await manager.stop()


@pytest.mark.asyncio
async def test_concurrent_first_submissions_start_one_worker_pool(tmp_path: Path) -> None:
    two_running, release = asyncio.Event(), asyncio.Event()
    active = 0

    async def runner(record: TaskRecord) -> TaskResultEnvelope:
        nonlocal active
        active += 1
        if active == 2:
            two_running.set()
        await release.wait()
        return TaskResultEnvelope(status="succeeded", file_id=record.file_id)

    manager = PersistentTaskManager(runner, FileTaskRepository(tmp_path / "tasks"),
                                    max_concurrent_executions=2, max_queue_size=10)
    try:
        results = await asyncio.gather(
            *(manager.submit_parse(f"file_{index:032x}", None, None) for index in range(6)),
            return_exceptions=True,
        )
        assert all(not isinstance(item, Exception) for item in results)
        await asyncio.wait_for(two_running.wait(), timeout=1)
        assert len(manager._workers) == 2
        assert active == 2
    finally:
        release.set()
        await manager.stop()


@pytest.mark.asyncio
async def test_stop_waits_for_in_progress_recovery(tmp_path: Path) -> None:
    recovery_entered, release_recovery = asyncio.Event(), asyncio.Event()

    class BarrierRepository(FileTaskRepository):
        async def recover(self):
            recovery_entered.set()
            await release_recovery.wait()
            return await super().recover()

    manager = PersistentTaskManager(
        lambda record: asyncio.sleep(0, result=TaskResultEnvelope(status="succeeded", file_id=record.file_id)),
        BarrierRepository(tmp_path / "tasks"),
    )
    starting = asyncio.create_task(manager.start())
    stopping = None
    try:
        await asyncio.wait_for(recovery_entered.wait(), timeout=1)
        stopping = asyncio.create_task(manager.stop())
        await asyncio.sleep(0)
        assert not stopping.done()
        release_recovery.set()
        await asyncio.gather(starting, stopping)
    finally:
        release_recovery.set()
        if not starting.done():
            await asyncio.gather(starting, return_exceptions=True)
        if stopping is not None and not stopping.done():
            await asyncio.gather(stopping, return_exceptions=True)
    assert manager._workers == []
    assert manager._cleanup_worker is None
    assert manager._started is False
    assert manager.shutdown_complete is True


@pytest.mark.asyncio
async def test_graceful_stop_finishes_active_task_and_recovers_queued_task(tmp_path: Path) -> None:
    active, release = asyncio.Event(), asyncio.Event()
    repository = FileTaskRepository(tmp_path / "tasks")

    async def runner(record: TaskRecord) -> TaskResultEnvelope:
        if record.file_id.endswith("1"):
            active.set()
            await release.wait()
        return TaskResultEnvelope(status="succeeded", file_id=record.file_id)

    manager = PersistentTaskManager(runner, repository, max_concurrent_executions=1,
                                    max_queue_size=1, shutdown_grace_seconds=1)
    await manager.start()
    first = await manager.submit_parse("file_" + "1" * 32, None, None)
    await asyncio.wait_for(active.wait(), timeout=1)
    second = await manager.submit_parse("file_" + "2" * 32, None, None)

    stopping = asyncio.create_task(manager.stop())
    await asyncio.sleep(0.01)
    assert not stopping.done()
    release.set()
    await stopping
    assert (await manager.get(first.task_id)).status == "succeeded"
    assert (await manager.get(second.task_id)).status == "queued"

    await manager.start()
    try:
        for _ in range(100):
            if (await manager.get(second.task_id)).status == "succeeded":
                break
            await asyncio.sleep(0.01)
        assert (await manager.get(second.task_id)).status == "succeeded"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_shutdown_grace_expiry_marks_active_task_interrupted(tmp_path: Path) -> None:
    active = asyncio.Event()

    async def runner(record: TaskRecord) -> TaskResultEnvelope:
        active.set()
        await asyncio.Event().wait()
        return TaskResultEnvelope(status="succeeded", file_id=record.file_id)

    manager = PersistentTaskManager(runner, FileTaskRepository(tmp_path / "tasks"),
                                    max_concurrent_executions=1, shutdown_grace_seconds=0.01)
    await manager.start()
    submitted = await manager.submit_parse("file_" + "3" * 32, None, None)
    await asyncio.wait_for(active.wait(), timeout=1)
    await manager.stop()

    interrupted = await manager.get(submitted.task_id)
    assert interrupted.status == "interrupted"
    assert interrupted.error["code"] == "task_interrupted"
    assert interrupted.error["retryable"] is True


@pytest.mark.asyncio
async def test_stop_bounds_uncooperative_runner_cancellation(tmp_path: Path) -> None:
    active, cancellation_seen, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def runner(record: TaskRecord) -> TaskResultEnvelope:
        active.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release.wait()
            return TaskResultEnvelope(status="succeeded", file_id=record.file_id)

    manager = PersistentTaskManager(
        runner,
        FileTaskRepository(tmp_path / "tasks"),
        max_concurrent_executions=1,
        shutdown_grace_seconds=0.01,
        shutdown_cancel_grace_seconds=0.02,
    )
    await manager.start()
    submitted = await manager.submit_parse("file_" + "4" * 32, None, None)
    await asyncio.wait_for(active.wait(), timeout=1)

    try:
        with pytest.raises(TaskShutdownTimeoutError):
            await asyncio.wait_for(manager.stop(), timeout=0.5)
        assert cancellation_seen.is_set()
        assert manager.shutdown_complete is False
        assert (await manager.get(submitted.task_id)).status == "interrupted"
        with pytest.raises(TaskShutdownTimeoutError):
            await manager.start()
        release.set()
        await asyncio.wait_for(asyncio.gather(*manager._workers, return_exceptions=True), timeout=1)
        await manager.start()
        assert manager._started and manager._workers
    finally:
        release.set()
        await asyncio.wait_for(manager.stop(), timeout=1)
    assert manager.shutdown_complete is True


@pytest.mark.asyncio
async def test_lifespan_retains_data_lock_after_shutdown_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    active, release = asyncio.Event(), asyncio.Event()

    class TrackingLock:
        acquired = False
        releases = 0

        def acquire(self) -> None:
            self.acquired = True

        def release(self) -> None:
            self.acquired = False
            self.releases += 1

    class FakeSessionManager:
        @asynccontextmanager
        async def run(self):
            yield

    async def runner(record: TaskRecord) -> TaskResultEnvelope:
        active.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            return TaskResultEnvelope(status="succeeded", file_id=record.file_id)

    manager = PersistentTaskManager(
        runner,
        FileTaskRepository(tmp_path / "tasks"),
        max_concurrent_executions=1,
        shutdown_grace_seconds=0.01,
        shutdown_cancel_grace_seconds=0.02,
    )
    directory_lock = TrackingLock()
    monkeypatch.setattr(main_module, "task_manager", manager)
    monkeypatch.setattr(main_module, "data_directory_lock", directory_lock)
    monkeypatch.setattr(main_module, "mcp", type("FakeMCP", (), {"session_manager": FakeSessionManager()})())

    try:
        with pytest.raises(TaskShutdownTimeoutError):
            async with main_module.lifespan(app):
                await manager.submit_parse("file_" + "6" * 32, None, None)
                await asyncio.wait_for(active.wait(), timeout=1)
        assert directory_lock.acquired is True
        assert directory_lock.releases == 0

        release.set()
        await asyncio.wait_for(asyncio.gather(*manager._workers, return_exceptions=True), timeout=1)
        assert manager.shutdown_complete is True
        # The lifespan has failed; keep the process lock fail-closed until exit.
        assert directory_lock.acquired is True
        assert directory_lock.releases == 0
    finally:
        release.set()
        if manager._workers or manager._started:
            await asyncio.wait_for(manager.stop(), timeout=1)


@pytest.mark.asyncio
async def test_retry_and_delete_task_api() -> None:
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
