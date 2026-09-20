from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from app.documents.models import ProviderError
from app.storage.ids import new_id
from app.tasks.events import FileTaskEventRepository
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
                 result_ttl_seconds: int = 86400, cleanup_interval_seconds: int = 300,
                 event_repository: FileTaskEventRepository | None = None) -> None:
        self.runner = runner
        self.repository = repository
        self.events = event_repository or FileTaskEventRepository(repository.root.parent / "events")
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
            elif record.status == "interrupted":
                timeline = await self.events.list(record.task_id)
                if not timeline.items or timeline.items[-1].type != "interrupted":
                    await self.events.append(record.task_id, "interrupted", message="服务重启中断了任务")
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

    async def submit_parse(self, file_id: str, goal: str | None, data_id: str | None,
                           parse_mode: str = "auto",
                           filename: str | None = None, content_type: str | None = None) -> TaskSubmitResponse:
        await self.start()
        if not self._accepting:
            raise RuntimeError("任务服务正在关闭")
        if self._queue.qsize() >= self.max_queue_size:
            raise TaskQueueFullError(self._queue.qsize(), self.max_queue_size)
        record = TaskRecord(task_id=new_id("task"), file_id=file_id, filename=filename, content_type=content_type,
                            goal=goal, parse_mode=parse_mode, data_id=data_id)
        await self.repository.create(record)
        await self.events.append(record.task_id, "created", message="任务已创建")
        await self.events.append(record.task_id, "queued", message="任务已进入队列")
        self._queue.put_nowait(record.task_id)
        return TaskSubmitResponse(
            task_id=record.task_id, file_id=file_id, status="queued", queue_position=self._queue.qsize(),
            created_at=record.created_at,
            links={"self": f"/api/v1/tasks/{record.task_id}", "file": f"/api/v1/files/{file_id}/content"},
        )

    async def get(self, task_id: str) -> TaskRecord | None:
        return await self.repository.get(task_id)

    async def list(self, status: str | None = None, query: str | None = None, limit: int = 50, cursor: str | None = None,
                   sort: str = "created_desc", file_type: str | None = None, provider: str | None = None,
                   error_code: str | None = None, created_after: datetime | None = None,
                   created_before: datetime | None = None) -> tuple[list[TaskRecord], str | None]:
        records = await self.repository.list()
        if status:
            records = [record for record in records if record.status == status]
        if file_type:
            suffix = file_type.casefold().lstrip(".")
            records = [record for record in records if self._file_type(record) == suffix]
        if provider:
            needle = provider.casefold()
            records = [record for record in records if any(needle in item.casefold() for item in self._providers(record))]
        if error_code:
            records = [record for record in records if self._error_code(record) == error_code]
        if created_after:
            lower = created_after.replace(tzinfo=timezone.utc) if created_after.tzinfo is None else created_after
            records = [record for record in records if record.created_at >= lower]
        if created_before:
            upper = created_before.replace(tzinfo=timezone.utc) if created_before.tzinfo is None else created_before
            records = [record for record in records if record.created_at <= upper]
        if query:
            needle = query.casefold()
            records = [record for record in records if needle in record.task_id.casefold() or needle in record.file_id.casefold() or needle in (record.data_id or "").casefold()
                       or needle in self._filename(record).casefold()]
        records.sort(key=lambda item: (item.created_at, item.task_id), reverse=sort != "created_asc")
        if cursor:
            position = next((index for index, record in enumerate(records) if record.task_id == cursor), None)
            # A cursor must refer to an item in this exact filtered sequence; never fall back to page one.
            records = records[position + 1:] if position is not None else []
        items = records[:limit]
        return items, items[-1].task_id if len(records) > len(items) else None

    @staticmethod
    def _document(record: TaskRecord) -> dict:
        return record.result.document if record.result and isinstance(record.result.document, dict) else {}

    @classmethod
    def _filename(cls, record: TaskRecord) -> str:
        if record.filename:
            return record.filename
        source = cls._document(record).get("source_file")
        return str(source.get("filename") or "") if isinstance(source, dict) else ""

    @classmethod
    def _file_type(cls, record: TaskRecord) -> str:
        filename = cls._filename(record)
        return filename.rsplit(".", 1)[-1].casefold() if "." in filename else str(cls._document(record).get("document_type") or "").casefold()

    @staticmethod
    def _providers(record: TaskRecord) -> list[str]:
        provenance = record.result.provenance if record.result else {}
        chain = provenance.get("provider_chain") if isinstance(provenance, dict) else []
        return [str(item) for item in chain] if isinstance(chain, list) else []

    @staticmethod
    def _error_code(record: TaskRecord) -> str | None:
        error = record.error or (record.result.error if record.result else None)
        return str(error.get("code")) if isinstance(error, dict) and error.get("code") else None

    async def retry(self, task_id: str) -> TaskSubmitResponse | None:
        record = await self.get(task_id)
        if record is None or record.status not in TERMINAL_STATUSES:
            return None
        submitted = await self.submit_parse(record.file_id, record.goal, record.data_id, record.parse_mode,
                                            record.filename, record.content_type)
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
        await self.events.delete(task_id)
        return True

    async def cancel(self, task_id: str) -> TaskRecord | None:
        record = await self.get(task_id)
        if record is None:
            return None
        if record.status == "queued":
            updated = await self.repository.update(task_id, record.revision, self._cancel_now)
            await self.events.append(task_id, "cancelled", message="任务已取消")
            return updated
        if record.status in {"planning", "running"}:
            updated = await self.repository.update(task_id, record.revision, self._request_cancel)
            await self.events.append(task_id, "cancel.requested", message="已请求取消任务")
            return updated
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
        updated = await self.repository.update(record.task_id, record.revision, mutate)
        if status == "planning":
            await self.events.append(record.task_id, "planning.started", message="开始规划解析任务")
        elif status == "running":
            await self.events.append(record.task_id, "step.started", message="开始执行解析", step="parse")
        return updated

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
            completed = await self.repository.update(current.task_id, current.revision, complete)
            await self.events.append(completed.task_id, "step.finished", message="解析执行结束", step="parse",
                                     details={"status": completed.status})
            for warning in completed.warnings:
                await self.events.append(completed.task_id, "warning", message=warning)
            await self.events.append(completed.task_id, completed.status, message="任务已结束")
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
        await self.events.append(task_id, "failed", message=message, details={"error_code": code})

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cleanup_interval_seconds)
            cutoff = utc_now() - timedelta(seconds=self.result_ttl_seconds)
            for record in await self.repository.list():
                if record.status in TERMINAL_STATUSES and record.finished_at and record.finished_at < cutoff:
                    await self.repository.delete(record.task_id)
                    await self.events.delete(record.task_id)


# v0.8.0 intentionally removes the in-memory/callback implementation.
InMemoryTaskManager = PersistentTaskManager
