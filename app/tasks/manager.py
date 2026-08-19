from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import httpx

from app.documents.models import ProviderError
from app.tasks.models import TaskMetrics, TaskRecord, TaskSubmitResponse, utc_now


TaskRunner = Callable[[TaskRecord], Awaitable[dict[str, Any]]]


class TaskQueueFullError(Exception):
    def __init__(self, queued_tasks: int, max_queue_size: int) -> None:
        self.queued_tasks = queued_tasks
        self.max_queue_size = max_queue_size
        super().__init__("当前待处理任务较多，请稍后重试。")


class InMemoryTaskManager:
    """单进程 FIFO 队列；状态不跨服务重启持久化。"""

    def __init__(
        self,
        runner: TaskRunner,
        max_concurrent_executions: int = 2,
        max_queue_size: int = 100,
        default_timeout_seconds: int = 1800,
        result_ttl_seconds: int = 86400,
        cleanup_interval_seconds: int = 300,
        callback_timeout_seconds: int = 15,
    ) -> None:
        self.runner = runner
        self.max_concurrent_executions = max_concurrent_executions
        self.max_queue_size = max_queue_size
        self.default_timeout_seconds = default_timeout_seconds
        self.result_ttl_seconds = result_ttl_seconds
        self.cleanup_interval_seconds = cleanup_interval_seconds
        self.callback_timeout_seconds = callback_timeout_seconds
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_queue_size)
        self._tasks: dict[str, TaskRecord] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._cleanup_worker: asyncio.Task[None] | None = None
        self._started = False

    async def start(self) -> None:
        current_loop = asyncio.get_running_loop()
        if self._started and self._workers and all(worker.get_loop() is current_loop and not worker.done() for worker in self._workers):
            return
        # ASGI test clients can create separate event loops. Production has one loop,
        # but reset stale in-memory workers rather than accepting tasks with no consumer.
        if self._started:
            self._workers = []
            self._cleanup_worker = None
            self._queue = asyncio.Queue(maxsize=self.max_queue_size)
            self._started = False
        self._started = True
        self._workers = [asyncio.create_task(self._worker(index), name=f"parse-agent-task-worker-{index}") for index in range(self.max_concurrent_executions)]
        self._cleanup_worker = asyncio.create_task(self._cleanup_loop(), name="parse-agent-task-cleanup")

    async def stop(self) -> None:
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

    async def submit(self, task_type: str, request: dict[str, Any], data_id: str | None, callback: str | None) -> TaskSubmitResponse:
        await self.start()
        self._cleanup_expired()
        if self._queue.full():
            raise TaskQueueFullError(self._queue.qsize(), self.max_queue_size)
        task_id = f"task_{uuid.uuid4().hex}"
        record = TaskRecord(task_id=task_id, task_type=task_type, data_id=data_id, callback=callback,
                            callback_status="pending" if callback else "not_requested", request=request)
        self._tasks[task_id] = record
        self._queue.put_nowait(task_id)
        return TaskSubmitResponse(task_id=task_id, status="queued", queue_position=self._queue.qsize(), created_at=record.created_at)

    def get(self, task_id: str) -> TaskRecord | None:
        self._cleanup_expired()
        return self._tasks.get(task_id)

    def cancel(self, task_id: str) -> TaskRecord | None:
        record = self.get(task_id)
        if record is None or record.status != "queued":
            return record
        record.status = "cancelled"
        record.finished_at = utc_now()
        record.duration_ms = int((record.finished_at - record.created_at).total_seconds() * 1000)
        return record

    def metrics(self) -> TaskMetrics:
        self._cleanup_expired()
        records = list(self._tasks.values())
        return TaskMetrics(
            queued_tasks=sum(record.status == "queued" for record in records),
            running_tasks=sum(record.status == "running" for record in records),
            completed_tasks=sum(record.status in {"succeeded", "partial", "failed", "cancelled"} for record in records),
            max_concurrent_executions=self.max_concurrent_executions,
            max_queue_size=self.max_queue_size,
        )

    async def _worker(self, _: int) -> None:
        while True:
            task_id = await self._queue.get()
            try:
                record = self._tasks.get(task_id)
                if record is None or record.status == "cancelled":
                    continue
                await self._execute(record)
            finally:
                self._queue.task_done()

    async def _execute(self, record: TaskRecord) -> None:
        record.status = "running"
        record.started_at = utc_now()
        timeout = int(record.request.get("timeout_seconds") or self.default_timeout_seconds)
        started = time.perf_counter()
        try:
            record.result = await asyncio.wait_for(self.runner(record), timeout=timeout)
            status = record.result.get("status")
            record.status = "partial" if status == "partial" else "succeeded" if status == "success" else "failed"
            if record.status == "failed":
                record.error = record.result.get("error") or {"code": "task_failed", "message": "任务执行失败", "retryable": False}
        except TimeoutError:
            record.status = "failed"
            record.error = ProviderError(code="task_timeout", message=f"任务超过 {timeout} 秒", retryable=False).model_dump()
        except Exception as exc:  # Ensure one provider failure never kills a worker.
            record.status = "failed"
            record.error = ProviderError(code="task_execution_failed", message=str(exc), retryable=False).model_dump()
        finally:
            record.finished_at = utc_now()
            record.duration_ms = int((time.perf_counter() - started) * 1000)
            if record.callback:
                await self._notify_callback(record)

    async def _notify_callback(self, record: TaskRecord) -> None:
        payload = record.model_dump(exclude={"request", "callback_status", "callback_status_code", "callback_error"}, mode="json")
        try:
            async with httpx.AsyncClient(timeout=self.callback_timeout_seconds) as client:
                response = await client.post(record.callback, json=payload)
            record.callback_status_code = response.status_code
            if response.status_code == 200:
                record.callback_status = "succeeded"
            else:
                record.callback_status = "failed"
                record.callback_error = f"回调服务返回 HTTP {response.status_code}，仅 HTTP 200 视为成功"
        except Exception as exc:
            record.callback_status = "failed"
            record.callback_error = str(exc)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cleanup_interval_seconds)
            self._cleanup_expired()

    def _cleanup_expired(self) -> None:
        cutoff = utc_now() - timedelta(seconds=self.result_ttl_seconds)
        expired = [task_id for task_id, record in self._tasks.items()
                   if record.finished_at and record.finished_at < cutoff]
        for task_id in expired:
            self._tasks.pop(task_id, None)
