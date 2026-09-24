from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from app.tasks.models import TaskRecord


@dataclass(frozen=True)
class ReclaimReport:
    """Outcome of a storage reclamation pass."""

    removed_artifact_dirs: int = 0
    removed_file_dirs: int = 0
    freed_bytes: int = 0
    failures: int = 0


class TaskStorageReclaimer:
    """Reclaim local storage owned by terminal tasks and orphaned directories.

    Two rules keep this safe on a single-process deployment:

    - Uploaded source files may be shared: retrying a task reuses the original
      ``file_id``. A source directory is therefore only removed when no surviving
      task record references that file.
    - Orphan directories are only removed once they are older than
      ``orphan_min_age_seconds``. An upload directory is created before its task
      record, so a freshly written upload can briefly look orphaned.
    """

    def __init__(self, data_root: str | Path, repository, *, file_store=None,
                 orphan_min_age_seconds: int = 3600) -> None:
        self.root = Path(data_root).resolve()
        self.repository = repository
        self.file_store = file_store
        self.orphan_min_age_seconds = orphan_min_age_seconds

    def artifact_dir(self, task_id: str) -> Path:
        return self.root / "artifacts" / task_id

    def file_dir(self, file_id: str) -> Path:
        return self.root / "files" / file_id

    async def reclaim_task(self, record: TaskRecord) -> ReclaimReport:
        """Remove artifacts owned by one task, plus its upload when unreferenced."""

        freed = self._tree_size(self.artifact_dir(record.task_id))
        removed_artifacts = 1 if self._remove_tree(self.artifact_dir(record.task_id)) else 0
        removed_files = 0
        if not await self._file_id_still_referenced(record.file_id):
            freed += self._tree_size(self.file_dir(record.file_id))
            if self.file_store is not None:
                # Delegate so the file store drops its own metadata consistently.
                self.file_store.delete(record.file_id)
                removed_files = 1
            elif self._remove_tree(self.file_dir(record.file_id)):
                removed_files = 1
        return ReclaimReport(removed_artifact_dirs=removed_artifacts, removed_file_dirs=removed_files, freed_bytes=freed)

    async def sweep_orphans(self) -> ReclaimReport:
        """Conservatively remove directories that no task record references."""

        records = await self.repository.list()
        known_tasks = {record.task_id for record in records}
        referenced_files = {record.file_id for record in records}
        cutoff = time.time() - self.orphan_min_age_seconds
        artifact_dirs = file_dirs = freed = failures = 0
        for directory, known, is_artifact in (
            (self.root / "artifacts", known_tasks, True),
            (self.root / "files", referenced_files, False),
        ):
            if not directory.is_dir():
                continue
            for child in sorted(directory.iterdir()):
                if not child.is_dir() or child.is_symlink() or child.name in known:
                    continue
                try:
                    if child.stat().st_mtime > cutoff:
                        continue
                except OSError:
                    failures += 1
                    continue
                size = self._tree_size(child)
                if self._remove_tree(child):
                    freed += size
                    if is_artifact:
                        artifact_dirs += 1
                    else:
                        file_dirs += 1
                else:
                    failures += 1
        return ReclaimReport(removed_artifact_dirs=artifact_dirs, removed_file_dirs=file_dirs,
                             freed_bytes=freed, failures=failures)

    async def _file_id_still_referenced(self, file_id: str) -> bool:
        if not file_id:
            return False
        return any(record.file_id == file_id for record in await self.repository.list())

    def _remove_tree(self, path: Path) -> bool:
        """Remove a directory tree; return True when something was actually removed."""
        try:
            if not path.is_dir() or path.is_symlink():
                return False
        except OSError:
            return False
        try:
            shutil.rmtree(path)
            return True
        except OSError:
            return False

    @staticmethod
    def _tree_size(path: Path) -> int:
        try:
            if not path.is_dir() or path.is_symlink():
                return 0
        except OSError:
            return 0
        total = 0
        for item in path.rglob("*"):
            try:
                if item.is_file() and not item.is_symlink():
                    total += item.stat().st_size
            except OSError:
                continue
        return total
