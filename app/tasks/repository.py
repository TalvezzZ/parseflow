from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path

from app.storage.atomic import atomic_write_json
from app.storage.ids import require_id
from app.tasks.models import ACTIVE_STATUSES, TaskRecord, utc_now


class TaskRevisionConflictError(RuntimeError):
    pass


class FileTaskRepository:
    """Single-process task repository with atomic replacement and per-task locks."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.quarantine_root = self.root / "quarantine"
        self.root.mkdir(parents=True, exist_ok=True)
        self.quarantine_root.mkdir(exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    def _path(self, task_id: str) -> Path:
        require_id("task", task_id)
        return self.root / f"{task_id}.json"

    def _lock(self, task_id: str) -> asyncio.Lock:
        return self._locks.setdefault(task_id, asyncio.Lock())

    async def create(self, record: TaskRecord) -> TaskRecord:
        path = self._path(record.task_id)
        async with self._lock(record.task_id):
            if path.exists():
                raise TaskRevisionConflictError("任务已存在")
            atomic_write_json(path, record)
        return record

    async def get(self, task_id: str) -> TaskRecord | None:
        try:
            path = self._path(task_id)
        except ValueError:
            return None
        if not path.is_file() or path.is_symlink():
            return None
        try:
            return TaskRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    async def update(self, task_id: str, expected_revision: int, mutate: Callable[[TaskRecord], None]) -> TaskRecord:
        path = self._path(task_id)
        async with self._lock(task_id):
            if not path.is_file():
                raise FileNotFoundError(task_id)
            record = TaskRecord.model_validate_json(path.read_text(encoding="utf-8"))
            if record.revision != expected_revision:
                raise TaskRevisionConflictError("任务版本已变更")
            mutate(record)
            record.revision += 1
            record.updated_at = utc_now()
            atomic_write_json(path, record)
            return record

    async def list(self) -> list[TaskRecord]:
        records: list[TaskRecord] = []
        for path in self.root.glob("task_*.json"):
            try:
                records.append(TaskRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return sorted(records, key=lambda item: (item.created_at, item.task_id))

    async def recover(self) -> list[TaskRecord]:
        recovered: list[TaskRecord] = []
        for path in self.root.glob("task_*.json"):
            try:
                record = TaskRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:
                destination = self.quarantine_root / path.name
                shutil.move(path, destination)
                continue
            if record.status in ACTIVE_STATUSES - {"queued"}:
                async def interrupted(current: TaskRecord) -> None:  # pragma: no cover - type-only helper
                    current.status = "interrupted"
                def mutate(current: TaskRecord) -> None:
                    current.status = "interrupted"
                    current.finished_at = utc_now()
                    current.error = {"code": "task_interrupted", "message": "服务重启中断了任务", "retryable": True}
                record = await self.update(record.task_id, record.revision, mutate)
            recovered.append(record)
        return sorted(recovered, key=lambda item: (item.created_at, item.task_id))

    async def delete(self, task_id: str) -> None:
        path = self._path(task_id)
        async with self._lock(task_id):
            path.unlink(missing_ok=True)
