from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import threading
from pathlib import Path

import pytest

from app.tasks import result_store as store


def synthetic(kind: str) -> dict:
    records = [{"kind": "text", "text": f"{kind}-内容-{i}-" + "文" * 2000, "metadata": {"index": i}} for i in range(320)]
    return {"document_id": "doc-1", "document_type": kind,
            "source_file": {"file_id": "file-1", "filename": f"input.{kind}"},
            "parser": {"name": "synthetic", "version": "1"}, "quality": {"truncated": 0},
            "pages": [{"page_number": 1, "blocks": records[:2]}] if kind == "pdf" else [],
            "blocks": records, "tables": [{"name": "表", "rows": [[1, None, "三"], [2, 3, 4]]}],
            "images": [{"relative_path": "images/source.png", "filename": "source.png"}] if kind == "image" else [],
            "representations": {"plain_text": "正文" * 20000, "markdown": "# 标题\n" + "正文" * 20000},
            "extensions": {"original": {"format": kind}}, "provenance": {"provider": "synthetic"}}


@pytest.mark.parametrize("kind", ["pdf", "docx", "pptx", "image", "txt"])
def test_full_document_preserved_while_task_preview_is_bounded(tmp_path: Path, kind: str) -> None:
    document = synthetic(kind)
    before = json.dumps(document, ensure_ascii=False)
    preview, reference = store.publish_result(tmp_path, document, preview_bytes=16384)
    assert json.loads((tmp_path / "result/document.json").read_text()) == document
    assert json.dumps(document, ensure_ascii=False) == before
    assert len(json.dumps(preview, ensure_ascii=False).encode()) <= 16384
    assert reference["preview_truncated"] is True
    assert reference["storage_key"] == "result/manifest.json"
    assert reference["document_size_bytes"] == (tmp_path / "result/document.json").stat().st_size
    assert str(tmp_path) not in json.dumps(reference)
    manifest = store.load_result_manifest(tmp_path)
    assert manifest["document_type"] == kind
    for resource in manifest["resources"]:
        path = store.resolve_result_resource(tmp_path, resource["storage_key"])
        assert path.stat().st_size == resource["size_bytes"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == resource["sha256"]
    blocks = [unit for unit in manifest["units"] if unit["kind"] == "blocks"]
    assert sum(unit["record_count"] for unit in blocks) == 320
    assert store.read_unit_records(tmp_path, blocks[0]["unit_id"], offset=254, limit=2)["records"] == document["blocks"][254:256]
    representation = manifest["representations"]["plain_text"]
    assert store.resolve_result_resource(tmp_path, representation["storage_key"]).read_text() == document["representations"]["plain_text"]


def test_media_payload_is_preserved_without_a_document(tmp_path: Path) -> None:
    payload = {"conversion": {"target": "audio.wav"}, "segments": [{"start": i, "text": "片段"} for i in range(300)]}
    preview, ref = store.publish_result(tmp_path, None, payload=payload)
    assert preview is None
    canonical = json.loads((tmp_path / "result/document.json").read_text())
    assert canonical == {"document_type": "media", "payload": payload}
    assert json.loads((tmp_path / "result/payload.json").read_text()) == payload
    manifest = store.load_result_manifest(tmp_path)
    assert manifest["units"][0]["kind"] == "media"
    assert store.read_unit_records(tmp_path, "unit-000000")["records"] == [payload]


def test_unit_paging_preserves_every_record_and_byte_limited_cursor(tmp_path: Path) -> None:
    rows = [{"index": i, "text": "x" * 100} for i in range(600)]
    store.publish_result(tmp_path, {"document_type": "csv", "tables": [{"rows": rows}]})
    found, offset = [], 0
    while True:
        page = store.read_unit_records(tmp_path, "unit-000000", offset=offset, limit=37, max_bytes=1000)
        assert 0 < len(page["records"]) < 37
        found.extend(page["records"])
        offset = page["next_offset"]
        if offset is None:
            break
    assert found == rows
    assert store.read_unit_records(tmp_path, "unit-000000", offset=1000)["records"] == []
    with pytest.raises(KeyError):
        store.read_unit_records(tmp_path, "unit-999999")
    with pytest.raises(store.ResultStorageError):
        store.read_unit_records(tmp_path, "../escape")
    with pytest.raises(store.ResultPageTooLarge):
        store.read_unit_records(tmp_path, "unit-000000", max_bytes=10)


def test_oversize_unit_record_has_explicit_pointer_and_complete_canonical(tmp_path: Path) -> None:
    record = {"text": "x" * (8 * 1024 * 1024 + 1)}
    document = {"document_type": "pdf", "pages": [record]}
    store.publish_result(tmp_path, document)
    marker = store.read_unit_records(tmp_path, "unit-000000")["records"][0]
    assert marker == {"oversized_record": True, "document_ref": "result/document.json", "record_index": 0}
    assert json.loads((tmp_path / "result/document.json").read_text()) == document


def test_checksum_detects_same_size_chunk_tampering(tmp_path: Path) -> None:
    store.publish_result(tmp_path, {"blocks": [{"text": "hello"}]})
    manifest = store.load_result_manifest(tmp_path)
    resource = manifest["units"][0]["chunks"][0]
    path = store.safe_result_path(tmp_path, resource["storage_key"])
    value = bytearray(path.read_bytes())
    value[-1] ^= 1
    path.write_bytes(value)
    with pytest.raises(store.ResultStorageError, match="校验"):
        store.read_unit_records(tmp_path, "unit-000000")


@pytest.mark.parametrize("key", ["../secret", "/etc/passwd", "result/../../secret", "result\\secret", ""])
def test_invalid_storage_paths_are_rejected(tmp_path: Path, key: str) -> None:
    with pytest.raises(store.ResultStorageError):
        store.safe_result_path(tmp_path, key)


def test_symlinks_and_unlisted_files_are_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    store.publish_result(workspace, {"blocks": [{"text": "safe"}]})
    (workspace / "secret.txt").write_text("private")
    with pytest.raises(FileNotFoundError):
        store.resolve_result_resource(workspace, "secret.txt")
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(store.ResultStorageError, match="符号链接"):
        store.safe_result_path(workspace, "linked/data.json")
    document = workspace / "result/document.json"
    document.unlink()
    document.symlink_to(workspace / "secret.txt")
    with pytest.raises(store.ResultStorageError, match="符号链接"):
        store.resolve_result_resource(workspace, "result/document.json")


def test_storage_cap_and_precancel_leave_no_partial_result(tmp_path: Path) -> None:
    with pytest.raises(store.ResultStorageError, match="预算"):
        store.publish_result(tmp_path, {"blocks": [{"text": "x" * 10000}]}, max_bytes=100)
    assert list(tmp_path.iterdir()) == []
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(store.ResultWriteCancelled):
        store.publish_result(tmp_path, {"text": "safe"}, cancel=cancel)
    assert list(tmp_path.iterdir()) == []


def test_published_results_cannot_be_overwritten(tmp_path: Path) -> None:
    store.publish_result(tmp_path, {"document_id": "first"})
    original = (tmp_path / "result/document.json").read_bytes()
    with pytest.raises(store.ResultStorageError, match="覆盖"):
        store.publish_result(tmp_path, {"document_id": "second"})
    assert (tmp_path / "result/document.json").read_bytes() == original
    assert not list(tmp_path.glob(".result-*"))


@pytest.mark.asyncio
async def test_async_cancellation_joins_writer_and_removes_result(tmp_path: Path, monkeypatch) -> None:
    entered, stopped = threading.Event(), threading.Event()

    def cooperative(*args, cancel, **kwargs):
        entered.set()
        assert cancel.wait(timeout=5)
        stopped.set()
        raise store.ResultWriteCancelled("cancelled")

    monkeypatch.setattr(store, "publish_result", cooperative)
    task = asyncio.create_task(store.publish_result_async(tmp_path, {"text": "safe"}))
    await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert not (tmp_path / "result").exists()


def test_json_streaming_chunks_preserve_large_escaped_strings() -> None:
    text = ('中文\\\"\n' * 50000) + "tail"
    document = {"text": text, "nested": [True, None, 3.25]}
    chunks = list(store._json_chunks(document))
    assert max(map(len, chunks)) <= 8192 * 6
    assert json.loads(b"".join(chunks)) == document


@pytest.mark.parametrize("manifest", [{"schema_version": 1}, {"schema_version": 1, "units": "bad", "resources": []}, {"schema_version": 2}])
def test_invalid_manifest_shape_is_rejected(tmp_path: Path, manifest: dict) -> None:
    root = tmp_path / "result"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(store.ResultStorageError):
        store.load_result_manifest(tmp_path)


def _replace_unit_chunk(workspace: Path, data: bytes, *, record_count: int | None = None) -> None:
    manifest_path = workspace / "result/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    resource = manifest["units"][0]["chunks"][0]
    (workspace / resource["storage_key"]).write_bytes(data)
    resource.update(size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    if record_count is not None:
        resource["record_count"] = record_count
        manifest["units"][0]["record_count"] = record_count
    manifest_path.write_text(json.dumps(manifest))


@pytest.mark.parametrize("data", [b"not-gzip", gzip.compress(b"not-json\n"), gzip.compress(b'{"text":"incomplete"')])
def test_malformed_unit_chunks_raise_controlled_storage_error(tmp_path: Path, data: bytes) -> None:
    store.publish_result(tmp_path, {"blocks": [{"text": "original"}]})
    _replace_unit_chunk(tmp_path, data)
    with pytest.raises(store.ResultStorageError):
        store.read_unit_records(tmp_path, "unit-000000")


@pytest.mark.parametrize("offset", [0, 1, 2])
def test_declared_chunk_count_cannot_exceed_available_records(tmp_path: Path, offset: int) -> None:
    store.publish_result(tmp_path, {"blocks": [{"text": "original"}]})
    _replace_unit_chunk(tmp_path, gzip.compress(b'{"text":"original"}\n'), record_count=3)
    with pytest.raises(store.ResultStorageError):
        store.read_unit_records(tmp_path, "unit-000000", offset=offset)


def test_bounded_excel_task_preview_preserves_storage_identity_after_large_metadata() -> None:
    from app.tasks.models import TaskResultEnvelope
    storage = {"schema_version": 2, "filename": "workbook/manifest.json"}
    document = {"document_id": "stable-id", "document_type": "xlsx", "source_file": {"file_id": "file-1"},
                "metadata": {"sheets": [{"sheet_id": i, "name": "长工作表名" * 1000} for i in range(100)],
                             "excel_storage": storage},
                "tables": [{"sheet_id": i, "data_ref": "workbook/manifest.json", "cells": [{"value": "x" * 10000}]} for i in range(100)]}
    preview, shortened = store.bounded_preview(document, max_bytes=16384)
    task = TaskResultEnvelope(status="succeeded", file_id="file-1", document=preview,
                              result_ref={"storage_key": "result/manifest.json"})
    assert shortened is True
    assert len(task.model_dump_json().encode()) <= 20 * 1024
    assert preview["document_id"] == "stable-id"
    assert preview["document_type"] == "xlsx"
    assert preview["metadata"]["excel_storage"] == storage

@pytest.mark.asyncio
async def test_async_marker_cancellation_removes_even_late_published_result(tmp_path: Path, monkeypatch) -> None:
    cancel_path = tmp_path / "cancel"
    original = store.publish_result

    def late_cancel(*args, **kwargs):
        result = original(*args, **kwargs)
        cancel_path.touch()
        return result

    monkeypatch.setattr(store, "publish_result", late_cancel)
    with pytest.raises(store.ResultWriteCancelled):
        await store.publish_result_async(tmp_path, {"text": "safe"}, cancel_path=cancel_path)
    assert not (tmp_path / "result").exists()


def test_manifest_declares_schema_identity(tmp_path):
    """结果目录必须带 schema 身份与版本，便于读者判断协议。"""
    from app.tasks.result_store import RESULT_MANIFEST_SCHEMA, RESULT_MANIFEST_VERSION, publish_result

    publish_result(tmp_path, {"document_type": "pdf", "pages": []}, max_bytes=1024 * 1024)

    manifest = json.loads((tmp_path / "result" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == RESULT_MANIFEST_SCHEMA
    assert manifest["schema_version"] == RESULT_MANIFEST_VERSION


def test_legacy_manifest_without_schema_still_reads(tmp_path):
    """旧 manifest 没有 schema 字段；不得因此拒绝已有任务。"""
    from app.tasks.result_store import load_result_manifest

    (tmp_path / "result").mkdir(parents=True)
    payload = {"schema_version": 1, "document_type": "pdf", "units": [], "representations": {}, "resources": []}
    (tmp_path / "result" / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    assert load_result_manifest(tmp_path)["schema_version"] == 1


def test_unknown_schema_identity_rejected(tmp_path):
    from app.tasks.result_store import ResultStorageError, load_result_manifest

    (tmp_path / "result").mkdir(parents=True)
    payload = {"schema": "other.protocol", "schema_version": 1, "document_type": "pdf",
               "units": [], "representations": {}, "resources": []}
    (tmp_path / "result" / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ResultStorageError):
        load_result_manifest(tmp_path)


def test_schema_snapshot_matches_published_manifest(tmp_path):
    """快照与真实产物必须一致，避免文档漂移。"""
    import json as _json
    from pathlib import Path as _Path

    from app.tasks.result_store import publish_result

    publish_result(tmp_path, {"document_type": "pdf", "pages": []}, max_bytes=1024 * 1024)
    manifest = _json.loads((tmp_path / "result" / "manifest.json").read_text(encoding="utf-8"))
    snapshot = _json.loads((_Path(__file__).resolve().parents[1] / "schemas/result-manifest-v1.json").read_text())

    for field in snapshot["required"]:
        assert field in manifest
    assert manifest["schema"] == snapshot["properties"]["schema"]["const"]
    assert manifest["schema_version"] == snapshot["properties"]["schema_version"]["const"]


def test_result_ref_declares_same_protocol_as_manifest(tmp_path):
    """result_ref 与 manifest 必须声明同一协议，客户端才能统一校验。"""
    from app.tasks.result_store import publish_result

    _, result_ref = publish_result(tmp_path, {"document_type": "pdf", "pages": []}, max_bytes=1024 * 1024)
    manifest = json.loads((tmp_path / "result" / "manifest.json").read_text(encoding="utf-8"))

    assert result_ref["schema"] == manifest["schema"]
    assert result_ref["schema_version"] == manifest["schema_version"]
