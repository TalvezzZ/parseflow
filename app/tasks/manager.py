from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta

from app.documents.models import ProviderError
from app.storage.ids import new_id
from app.tasks.models import TERMINAL_STATUSES, TaskMetrics, TaskRecord, TaskResultEnvelope, TaskSubmitResponse, utc_now
from app.tasks.repository import FileTaskRepository, TaskRevisionConflictError


TaskRunner = Callable[[TaskRecord], Awaitable[TaskResultEnvelope]]


class TaskQueueFullError(Exception):
    def __init__(self, queued_tasks: int, max_queue_size: int) -> None:
        self.queued_tasks = queued_tasks
        self.max_queue_size = max_queue_size
        super().__init__("当前待处理任务较多，请稍后重试。")


class PersistentTaskManager:
    """Single-process persistent queue; disk records are the source of truth."""

    def __init__(self, runner: TaskRunner, repository: FileTaskRepository, *, max_concurrent_executions: int = 2,
                 max_queue_size: int = 100, default_timeout_seconds: int = 1800,
                 result_ttl_seconds: int = 86400, cleanup_interval_seconds: int = 300) -> None:
        self.runner = runner
        self.repository = repository
        self.max_concurrent_executions = max_concurrent_executions
        self.max_queue_size = max_queue_size
        self.default_timeout_seconds = default_timeout_seconds
        self.result_ttl_seconds = result_ttl_seconds
        self.cleanup_interval_seconds = cleanup_interval_seconds
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._cleanup_worker: asyncio.Task[None] | None = None
        self._started = False
        self._accepting = False
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._started and self._loop is loop and any(not worker.done() for worker in self._workers):
            return
        # ASGI test clients may use a new loop. Disk is authoritative, so rebuild
        # consumers and recover queued records rather than keeping stale workers.
        if self._started:
            self._workers = []
            self._cleanup_worker = None
            self._queue = asyncio.Queue()
        self._loop = loop
        self._started = True
        self._accepting = True
        recovered = await self.repository.recover()
        for record in recovered:
            if record.status == "queued":
                self._queue.put_nowait(record.task_id)
        self._workers = [asyncio.create_task(self._worker(index), name=f"parseflow-task-worker-{index}")
                         for index in range(self.max_concurrent_executions)]
        self._cleanup_worker = asyncio.create_task(self._cleanup_loop(), name="parseflow-task-cleanup")

    async def stop(self) -> None:
        self._accepting = False
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        if self._cleanup_worker:
            self._cleanup_worker.cancel()
            await asyncio.gather(self._cleanup_worker, return_exceptions=True)
        self._workers = []
        self._cleanup_worker = None
        self._started = False

    async def submit_parse(self, file_id: str, goal: str | None, data_id: str | None) -> TaskSubmitResponse:
        await self.start()
        if not self._accepting:
            raise RuntimeError("任务服务正在关闭")
        if self._queue.qsize() >= self.max_queue_size:
            raise TaskQueueFullError(self._queue.qsize(), self.max_queue_size)
        record = TaskRecord(task_id=new_id("task"), file_id=file_id, goal=goal, data_id=data_id)
        await self.repository.create(record)
        self._queue.put_nowait(record.task_id)
        return TaskSubmitResponse(
            task_id=record.task_id, file_id=file_id, status="queued", queue_position=self._queue.qsize(),
            created_at=record.created_at,
            links={"self": f"/api/v1/tasks/{record.task_id}", "file": f"/api/v1/files/{file_id}/content"},
        )

    async def get(self, task_id: str) -> TaskRecord | None:
        return await self.repository.get(task_id)

    async def list(self, status: str | None = None, query: str | None = None, limit: int = 50, cursor: str | None = None,
                   sort: str = "created_desc") -> tuple[list[TaskRecord], str | None]:
        records = await self.repository.list()
        if status:
            records = [record for record in records if record.status == status]
        if query:
            needle = query.casefold()
            records = [record for record in records if needle in record.task_id.casefold() or needle in record.file_id.casefold() or needle in (record.data_id or "").casefold()]
        records.sort(key=lambda item: (item.created_at, item.task_id), reverse=sort != "created_asc")
        if cursor:
            position = next((index for index, record in enumerate(records) if record.task_id == cursor), None)
            records = records[position + 1:] if position is not None else records
        items = records[:limit]
        return items, items[-1].task_id if len(records) > len(items) else None

    async def retry(self, task_id: str) -> TaskSubmitResponse | None:
        record = await self.get(task_id)
        if record is None or record.status not in TERMINAL_STATUSES:
            return None
        submitted = await self.submit_parse(record.file_id, record.goal, record.data_id)
        created = await self.get(submitted.task_id)
        if created:
            await self.repository.update(created.task_id, created.revision, lambda item: setattr(item, "retry_of", task_id))
        return submitted

    async def delete(self, task_id: str) -> bool:
        record = await self.get(task_id)
        if record is None:
            return False
        if record.status not in TERMINAL_STATUSES:
            raise ValueError("active_task_cannot_delete")
        await self.repository.delete(task_id)
        return True

    async def cancel(self, task_id: str) -> TaskRecord | None:
        record = await self.get(task_id)
        if record is None:
            return None
        if record.status == "queued":
            return await self.repository.update(task_id, record.revision, self._cancel_now)
        if record.status in {"planning", "running"}:
            return await self.repository.update(task_id, record.revision, self._request_cancel)
        return record

    @staticmethod
    def _cancel_now(record: TaskRecord) -> None:
        record.status = "cancelled"
        record.cancel_requested = True
        record.finished_at = utc_now()
        record.duration_ms = int((record.finished_at - record.created_at).total_seconds() * 1000)

    @staticmethod
    def _request_cancel(record: TaskRecord) -> None:
        record.status = "cancelling"
        record.cancel_requested = True

    async def metrics(self) -> TaskMetrics:
        records = await self.repository.list()
        return TaskMetrics(
            queued_tasks=sum(record.status == "queued" for record in records),
            running_tasks=sum(record.status in {"planning", "running", "cancelling"} for record in records),
            completed_tasks=sum(record.status in TERMINAL_STATUSES for record in records),
            max_concurrent_executions=self.max_concurrent_executions,
            max_queue_size=self.max_queue_size,
        )

    async def _worker(self, _: int) -> None:
        while True:
            task_id = await self._queue.get()
            try:
                record = await self.get(task_id)
                if record is not None and record.status == "queued":
                    await self._execute(record)
            finally:
                self._queue.task_done()

    async def _transition(self, record: TaskRecord, status: str) -> TaskRecord:
        def mutate(current: TaskRecord) -> None:
            current.status = status  # type: ignore[assignment]
            if status == "planning":
                current.started_at = utc_now()
        return await self.repository.update(record.task_id, record.revision, mutate)

    async def _execute(self, record: TaskRecord) -> None:
        try:
            record = await self._transition(record, "planning")
            if record.cancel_requested:
                await self.repository.update(record.task_id, record.revision, self._cancel_now)
                return
            record = await self._transition(record, "running")
            started = time.perf_counter()
            envelope = await asyncio.wait_for(self.runner(record), timeout=self.default_timeout_seconds)
            current = await self.get(record.task_id)
            if current is None:
                return
            def complete(value: TaskRecord) -> None:
                if value.cancel_requested:
                    self._cancel_now(value)
                    return
                value.result = envelope
                value.warnings = envelope.warnings
                value.error = envelope.error
                value.status = envelope.status
                value.finished_at = utc_now()
                value.duration_ms = int((time.perf_counter() - started) * 1000)
            await self.repository.update(current.task_id, current.revision, complete)
        except TimeoutError:
            await self._fail(record.task_id, "task_timeout", f"任务超过 {self.default_timeout_seconds} 秒")
        except Exception as exc:
            await self._fail(record.task_id, "task_execution_failed", str(exc))

    async def _fail(self, task_id: str, code: str, message: str) -> None:
        current = await self.get(task_id)
        if current is None or current.status in TERMINAL_STATUSES:
            return
        def fail(record: TaskRecord) -> None:
            record.status = "failed"
            record.error = ProviderError(code=code, message=message, retryable=False).model_dump()
            record.finished_at = utc_now()
            record.duration_ms = int((record.finished_at - (record.started_at or record.created_at)).total_seconds() * 1000)
        await self.repository.update(current.task_id, current.revision, fail)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cleanup_interval_seconds)
            cutoff = utc_now() - timedelta(seconds=self.result_ttl_seconds)
            for record in await self.repository.list():
                if record.status in TERMINAL_STATUSES and record.finished_at and record.finished_at < cutoff:
                    await self.repository.delete(record.task_id)


# v0.8.0 intentionally removes the in-memory/callback implementation.
InMemoryTaskManager = PersistentTaskManager
