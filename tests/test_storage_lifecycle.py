"""Regression tests for v1.4.0 storage lifecycle and public path hygiene."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from app.main import _public_document
from app.tasks.events import FileTaskEventRepository
from app.tasks.manager import PersistentTaskManager
from app.tasks.models import TaskRecord, utc_now
from app.tasks.repository import FileTaskRepository
from app.tasks.retention import TaskStorageReclaimer


def make_record(task_id: str, file_id: str, *, status: str = "succeeded") -> TaskRecord:
    return TaskRecord(task_id=task_id, file_id=file_id, filename="report.pdf", status=status,
                      finished_at=utc_now())


async def seed_storage(root: Path, task_id: str, file_id: str) -> None:
    (root / "artifacts" / task_id / "files" / "exports").mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / task_id / "files" / "exports" / "document.md").write_text("hello", encoding="utf-8")
    (root / "files" / file_id).mkdir(parents=True, exist_ok=True)
    (root / "files" / file_id / "source.pdf").write_bytes(b"%PDF-1.4")


def age_directory(path: Path, seconds: float = 7200) -> None:
    """Age a directory and its children past an orphan threshold."""
    stale = time.time() - seconds
    os.utime(path, (stale, stale))
    for child in path.iterdir():
        os.utime(child, (stale, stale))


@pytest.mark.asyncio
async def test_delete_task_removes_artifacts_and_upload(tmp_path: Path) -> None:
    repository = FileTaskRepository(tmp_path / "tasks")
    record = make_record("task_" + "a" * 32, "file_" + "b" * 32)
    await repository.create(record)
    await seed_storage(tmp_path, record.task_id, record.file_id)

    manager = PersistentTaskManager(lambda item: None, repository, event_repository=FileTaskEventRepository(tmp_path / "events"))

    assert await manager.delete(record.task_id) is True
    assert not (tmp_path / "artifacts" / record.task_id).exists()
    assert not (tmp_path / "files" / record.file_id).exists()


@pytest.mark.asyncio
async def test_delete_keeps_upload_still_referenced_by_retry(tmp_path: Path) -> None:
    """A retry reuses the original file_id; deleting the old task must not break it."""

    repository = FileTaskRepository(tmp_path / "tasks")
    original = make_record("task_" + "a" * 32, "file_" + "b" * 32)
    retried = make_record("task_" + "c" * 32, original.file_id)
    await repository.create(original)
    await repository.create(retried)
    await seed_storage(tmp_path, original.task_id, original.file_id)
    await seed_storage(tmp_path, retried.task_id, retried.file_id)

    manager = PersistentTaskManager(lambda item: None, repository, event_repository=FileTaskEventRepository(tmp_path / "events"))

    assert await manager.delete(original.task_id) is True
    assert not (tmp_path / "artifacts" / original.task_id).exists()
    # The retried task still needs its source file.
    assert (tmp_path / "files" / original.file_id).exists()


@pytest.mark.asyncio
async def test_delete_rejects_active_task_without_removing_storage(tmp_path: Path) -> None:
    repository = FileTaskRepository(tmp_path / "tasks")
    record = make_record("task_" + "a" * 32, "file_" + "b" * 32, status="running")
    await repository.create(record)
    await seed_storage(tmp_path, record.task_id, record.file_id)

    manager = PersistentTaskManager(lambda item: None, repository, event_repository=FileTaskEventRepository(tmp_path / "events"))

    with pytest.raises(ValueError):
        await manager.delete(record.task_id)
    assert (tmp_path / "artifacts" / record.task_id).exists()
    assert (tmp_path / "files" / record.file_id).exists()


@pytest.mark.asyncio
async def test_sweep_orphans_respects_age_threshold(tmp_path: Path) -> None:
    repository = FileTaskRepository(tmp_path / "tasks")
    await seed_storage(tmp_path, "task_" + "a" * 32, "file_" + "b" * 32)
    reclaimer = TaskStorageReclaimer(tmp_path, repository, orphan_min_age_seconds=3600)

    # Freshly created orphans are protected: an upload precedes its task record.
    report = await reclaimer.sweep_orphans()
    assert report.removed_artifact_dirs == 0 and report.removed_file_dirs == 0
    assert (tmp_path / "artifacts" / ("task_" + "a" * 32)).exists()

    # Age the orphan directories past the threshold and sweep again.
    age_directory(tmp_path / "artifacts" / ("task_" + "a" * 32))
    age_directory(tmp_path / "files" / ("file_" + "b" * 32))
    report = await reclaimer.sweep_orphans()
    assert report.removed_artifact_dirs == 1 and report.removed_file_dirs == 1
    assert not (tmp_path / "artifacts" / ("task_" + "a" * 32)).exists()
    assert not (tmp_path / "files" / ("file_" + "b" * 32)).exists()


@pytest.mark.asyncio
async def test_sweep_orphans_keeps_directories_referenced_by_tasks(tmp_path: Path) -> None:
    repository = FileTaskRepository(tmp_path / "tasks")
    record = make_record("task_" + "a" * 32, "file_" + "b" * 32)
    await repository.create(record)
    await seed_storage(tmp_path, record.task_id, record.file_id)
    age_directory(tmp_path / "artifacts" / record.task_id)
    age_directory(tmp_path / "files" / record.file_id)

    report = await TaskStorageReclaimer(tmp_path, repository, orphan_min_age_seconds=0).sweep_orphans()

    assert report.removed_artifact_dirs == 0 and report.removed_file_dirs == 0
    assert (tmp_path / "artifacts" / record.task_id).exists()
    assert (tmp_path / "files" / record.file_id).exists()


def test_public_document_strips_artifact_dir_and_paths() -> None:
    document = {
        "document_type": "pptx",
        "metadata": {"artifact_dir": "/srv/parseflow/data/artifacts/task_x/files", "slide_count": 1},
        "pages": [{"preview": {"filename": "ocr-page-0001.png", "content_type": "image/png"}}],
        "blocks": [{"text": "hi", "path": "/srv/parseflow/data/files/file_y/source.pptx"}],
        "output_dir": "/srv/parseflow/data",
    }

    public = _public_document(document)
    serialized = json.dumps(public, ensure_ascii=False)

    assert "artifact_dir" not in serialized
    assert "/srv/parseflow" not in serialized
    assert public["metadata"] == {"slide_count": 1}
    assert public["pages"][0]["preview"] == {"filename": "ocr-page-0001.png", "content_type": "image/png"}


def test_event_details_never_store_artifact_dir(tmp_path: Path) -> None:
    repository = FileTaskEventRepository(tmp_path / "events")
    sanitized = repository._sanitize({"artifact_dir": "/srv/data", "nested": {"output_dir": "/srv"}})
    assert sanitized == {"nested": {}}
