from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from app import main as main_module
from app.main import app
from app.observability import HttpMetrics, JsonFormatter, TaskExecutionMetrics
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


def test_prometheus_metrics_use_route_templates_and_no_identifier_labels() -> None:
    metrics = HttpMetrics()
    metrics.record(200, 25, method="GET", route="/api/v1/tasks/{task_id}")
    for index in range(100):
        metrics.record(200, 1, method=f"CUSTOM-{index}", route="/health")
    body = metrics.prometheus(task_counts={"running": 1}, queue_depth=0, storage_bytes=12)
    assert 'route="/api/v1/tasks/{task_id}"' in body
    assert len(metrics.route_counts) == 2
    assert metrics.route_counts[("OTHER", "/health", "200")] == 100
    assert 'method="OTHER"' in body and "CUSTOM-" not in body
    assert "task_" + "a" * 32 not in body and "file_" + "b" * 32 not in body
    assert "parseflow_tasks_current" in body and "parseflow_storage_bytes 12" in body


@pytest.mark.asyncio
async def test_prometheus_snapshot_uses_short_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time

    tasks = tmp_path / "tasks"
    tasks.mkdir()
    record = TaskRecord(task_id="task_" + "c" * 32, file_id="file_" + "d" * 32, status="queued")
    (tasks / f"{record.task_id}.json").write_text(record.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(main_module, "data_root", tmp_path)
    monkeypatch.setattr(main_module, "_metrics_cache_at", 0.0)
    monkeypatch.setattr(main_module, "_metrics_cache_value", None)

    original_scan = main_module._scan_metrics_snapshot
    scan_count = 0
    scan_count_lock = threading.Lock()

    def counted_scan() -> tuple[dict[str, int], int]:
        nonlocal scan_count
        with scan_count_lock:
            scan_count += 1
        time.sleep(0.03)
        return original_scan()

    monkeypatch.setattr(main_module, "_scan_metrics_snapshot", counted_scan)
    concurrent = await asyncio.gather(*(main_module._metrics_snapshot() for _ in range(20)))
    first_counts, first_bytes = concurrent[0]
    assert scan_count == 1
    assert first_counts["queued"] == 1
    assert all(result == concurrent[0] for result in concurrent)

    (tmp_path / "extra.bin").write_bytes(b"more")
    cached_counts, cached_bytes = await main_module._metrics_snapshot()
    assert cached_counts == first_counts and cached_bytes == first_bytes
    assert scan_count == 1

    monkeypatch.setattr(main_module, "_metrics_cache_at", 0.0)
    refreshed_counts, refreshed_bytes = await main_module._metrics_snapshot()
    assert refreshed_counts == first_counts and refreshed_bytes > first_bytes
    assert scan_count == 2


def test_task_metrics_export_fixed_labels_and_histograms() -> None:
    metrics = TaskExecutionMetrics()
    metrics.observe_queue_wait(2.0)
    metrics.observe_duration(4.0, "succeeded")
    metrics.observe_duration(9.0, "task-id-must-not-be-a-label")
    metrics.record_queue_rejection("full")
    metrics.record_recovery("queued")

    body = metrics.prometheus()
    assert 'parseflow_task_queue_wait_seconds_bucket{le="5.0"} 1' in body
    assert 'parseflow_task_duration_seconds_count{status="succeeded"} 1' in body
    assert 'parseflow_task_duration_seconds_count{status="other"} 1' in body
    assert body.count("# TYPE parseflow_task_duration_seconds histogram") == 1
    assert 'parseflow_task_queue_rejections_total{reason="full"} 1' in body
    assert 'parseflow_task_recoveries_total{outcome="queued"} 1' in body
    assert "task-id-must-not-be-a-label" not in body


def test_json_formatter_emits_explicit_fields_without_arbitrary_secrets() -> None:
    import logging
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "request completed", (), None)
    record.request_id = "req_safe"
    record.status = 200
    record.api_key = "must-not-serialize"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["request_id"] == "req_safe" and payload["service_version"]
    assert "api_key" not in payload


def test_public_contracts_do_not_expose_internal_security_paths() -> None:
    schema = json.dumps(app.openapi()).replace('"in": "path"', '"in": "route_parameter"')
    assert '"output_dir"' not in schema
    assert '"callback"' not in schema
