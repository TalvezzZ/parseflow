from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.tasks import result_store as store
from app.tasks.manager import PersistentTaskManager
from app.tasks.models import TaskResultEnvelope
from app.tasks.repository import FileTaskRepository


def test_existing_binary_outputs_share_result_storage_budget(tmp_path: Path) -> None:
    (tmp_path / "normalized.wav").write_bytes(b"x" * 4096)
    with pytest.raises(store.ResultStorageError, match="预算"):
        store.publish_result(tmp_path, None, payload={"media": {}}, max_bytes=1024)
    assert not (tmp_path / "result").exists()


def test_authoritative_download_detects_same_size_corruption(tmp_path: Path) -> None:
    store.publish_result(tmp_path, {"document_type": "txt", "blocks": [{"text": "KEEP"}]})
    path = tmp_path / "result/document.json"
    path.write_bytes(path.read_bytes().replace(b"KEEP", b"LOST"))
    with pytest.raises(store.ResultStorageError, match="校验"):
        store.resolve_result_resource(tmp_path, "result/document.json")


def test_generated_media_and_images_are_manifest_registered(tmp_path: Path) -> None:
    (tmp_path / "audio.wav").write_bytes(b"audio")
    images = tmp_path / "images"
    images.mkdir()
    (images / "1.png").write_bytes(b"image")
    store.publish_result(tmp_path, None, payload={"media": {"transcript_available": False}})
    manifest = store.load_result_manifest(tmp_path)
    keys = {resource["storage_key"] for resource in manifest["resources"]}
    assert {"audio.wav", "images/1.png"} <= keys
    assert store.resolve_result_resource(tmp_path, "audio.wav").read_bytes() == b"audio"


def test_excel_preview_restores_late_table_reference(tmp_path: Path) -> None:
    document = {"document_type": "xlsx", "metadata": {"excel_storage": {
        "filename": "workbook/manifest.json", "schema_version": 2}}, "tables": [{
            "sheet_id": 0, "sheet_name": "S", "cells": [{"value": "x" * 100}] * 100,
            "rows": [], "data_ref": "workbook/manifest.json", "row_count": 100,
            "semantic_cell_count": 100}]}
    preview, shortened = store.bounded_preview(document, 4096)
    assert shortened
    assert preview["tables"][0]["data_ref"] == "workbook/manifest.json"
    assert preview["tables"][0]["row_count"] == 100


@pytest.mark.asyncio
async def test_late_cancel_removes_non_excel_media_outputs(tmp_path: Path) -> None:
    entered, proceed = asyncio.Event(), asyncio.Event()
    repository = FileTaskRepository(tmp_path / "tasks")

    async def runner(record):
        workspace = tmp_path / "artifacts" / record.task_id / "files"
        workspace.mkdir(parents=True)
        (workspace / "normalized.wav").write_bytes(b"normalized audio")
        entered.set()
        await proceed.wait()
        return TaskResultEnvelope(status="succeeded", file_id=record.file_id)

    manager = PersistentTaskManager(runner, repository, cleanup_interval_seconds=3600)
    try:
        submitted = await manager.submit_parse("file_" + "a" * 32, None, None, filename="clip.mp3")
        await asyncio.wait_for(entered.wait(), timeout=2)
        await manager.cancel(submitted.task_id)
        proceed.set()
        for _ in range(100):
            task = await manager.get(submitted.task_id)
            if task.status == "cancelled":
                break
            await asyncio.sleep(0.01)
        assert task.status == "cancelled"
        assert not (tmp_path / "artifacts" / submitted.task_id / "files").exists()
    finally:
        proceed.set()
        await manager.stop()
