from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from app.main import app
from app.security.archive import validate_zip_archive
from app.security.budgets import ParseBudget, ResourceBudgetExceeded, validate_output_size
from app.storage.janitor import StorageJanitor
from app.tasks.models import TaskRecord, utc_now
from app.tasks.repository import FileTaskRepository


def test_archive_guard_rejects_traversal_and_expansion(tmp_path: Path) -> None:
    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../escape.txt", "x")
    with pytest.raises(ResourceBudgetExceeded, match="不安全路径") as error:
        validate_zip_archive(traversal, max_entries=10, max_uncompressed_bytes=1024)
    assert error.value.code == "archive_path_unsafe"

    oversized = tmp_path / "oversized.zip"
    with zipfile.ZipFile(oversized, "w") as archive:
        archive.writestr("large.txt", "x" * 1025)
    with pytest.raises(ResourceBudgetExceeded) as error:
        validate_zip_archive(oversized, max_entries=10, max_uncompressed_bytes=1024)
    assert error.value.code == "archive_uncompressed_limit_exceeded"


def test_output_budget_has_stable_error_code() -> None:
    budget = ParseBudget(max_input_bytes=10, max_output_bytes=3, timeout_seconds=1)
    with pytest.raises(ResourceBudgetExceeded) as error:
        validate_output_size("toolong", budget)
    assert error.value.code == "output_limit_exceeded"


@pytest.mark.asyncio
async def test_ready_checks_local_task_runtime() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        # A task starts the persistent workers in ASGI test mode.
        created = await client.post("/api/v1/tasks/parse", files={"file": ("ready.json", b"{}", "application/json")})
        assert created.status_code == 202
        ready = await client.get("/ready")
    assert ready.status_code == 200, ready.text
    assert ready.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_storage_janitor_keeps_active_tasks_and_removes_expired_temp(tmp_path: Path) -> None:
    repository = FileTaskRepository(tmp_path / "tasks")
    active = TaskRecord(task_id="task_" + "a" * 32, file_id="file_" + "b" * 32, status="running")
    await repository.create(active)
    stale = tmp_path / "stale.tmp"
    stale.write_bytes(b"stale")
    import os
    os.utime(stale, (0, 0))
    report = await StorageJanitor(tmp_path, repository, task_ttl_seconds=0, tmp_max_age_seconds=1).clean()
    assert await repository.get(active.task_id) is not None
    assert not stale.exists()
    assert report.removed_temp_files == 1


def test_public_contracts_do_not_expose_internal_security_paths() -> None:
    schema = json.dumps(app.openapi()).replace('"in": "path"', '"in": "route_parameter"')
    assert '"output_dir"' not in schema
    assert '"callback"' not in schema
