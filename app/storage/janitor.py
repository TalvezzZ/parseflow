from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from app.tasks.models import TERMINAL_STATUSES, TaskRecord
from app.tasks.repository import FileTaskRepository


@dataclass(frozen=True)
class JanitorReport:
    removed_tasks: int = 0
    removed_temp_files: int = 0
    freed_bytes: int = 0
    failures: int = 0


class StorageJanitor:
    """Conservative local cleanup: never removes active task records or their files."""

    def __init__(self, data_root: str | Path, repository: FileTaskRepository, *, task_ttl_seconds: int, tmp_max_age_seconds: int) -> None:
        self.root = Path(data_root).resolve()
        self.repository = repository
        self.task_ttl_seconds = task_ttl_seconds
        self.tmp_max_age_seconds = tmp_max_age_seconds

    async def clean(self) -> JanitorReport:
        now = time.time()
        removed_tasks = removed_temp_files = freed_bytes = failures = 0
        for task in await self.repository.list():
            if task.status not in TERMINAL_STATUSES or not task.finished_at:
                continue
            if now - task.finished_at.timestamp() < self.task_ttl_seconds:
                continue
            try:
                await self.repository.delete(task.task_id)
                artifact_dir = self.root / "artifacts" / task.task_id
                freed_bytes += self._tree_size(artifact_dir)
                shutil.rmtree(artifact_dir, ignore_errors=True)
                removed_tasks += 1
            except OSError:
                failures += 1
        for path in self.root.rglob("*.tmp"):
            try:
                if path.is_file() and not path.is_symlink() and now - path.stat().st_mtime >= self.tmp_max_age_seconds:
                    freed_bytes += path.stat().st_size
                    path.unlink()
                    removed_temp_files += 1
            except OSError:
                failures += 1
        return JanitorReport(removed_tasks, removed_temp_files, freed_bytes, failures)

    @staticmethod
    def _tree_size(path: Path) -> int:
        if not path.exists():
            return 0
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file() and not item.is_symlink())
