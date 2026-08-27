from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.storage.atomic import atomic_write_json
from app.storage.ids import require_id
from app.tasks.models import utc_now


class TaskEvent(BaseModel):
    sequence: int = Field(ge=1)
    type: str
    at: str
    task_id: str
    step: str | None = None
    message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class TaskEventList(BaseModel):
    items: list[TaskEvent]
    next_after: int | None = None


class FileTaskEventRepository:
    """Atomic, path-free per-task event timelines."""

    FORBIDDEN_DETAILS = {"path", "output_dir", "source_path", "target_path", "artifact_path", "callback", "content"}

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

    async def append(self, task_id: str, event_type: str, *, message: str | None = None,
                     step: str | None = None, details: dict[str, Any] | None = None) -> TaskEvent:
        safe_details = self._sanitize(details or {})
        path = self._path(task_id)
        async with self._lock(task_id):
            events = self._read(path)
            event = TaskEvent(sequence=len(events) + 1, type=event_type, at=utc_now().isoformat(), task_id=task_id,
                              message=message, step=step, details=safe_details)
            events.append(event)
            atomic_write_json(path, [item.model_dump(mode="json") for item in events])
            return event

    async def list(self, task_id: str, *, after: int = 0, limit: int = 100) -> TaskEventList:
        path = self._path(task_id)
        events = [item for item in self._read(path) if item.sequence > after]
        items = events[:limit]
        return TaskEventList(items=items, next_after=items[-1].sequence if len(events) > len(items) else None)

    async def delete(self, task_id: str) -> None:
        self._path(task_id).unlink(missing_ok=True)

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self._sanitize(item) for key, item in value.items() if key not in self.FORBIDDEN_DETAILS}
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        return value

    def _read(self, path: Path) -> list[TaskEvent]:
        if not path.exists():
            return []
        try:
            import json
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [TaskEvent.model_validate(item) for item in payload]
        except Exception:
            destination = self.quarantine_root / path.name
            if not destination.exists():
                shutil.move(path, destination)
            return []
