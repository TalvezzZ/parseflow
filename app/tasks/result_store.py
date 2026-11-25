"""Versioned file-backed results. Task records carry only bounded previews/references."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import re
import shutil
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from uuid import uuid4


class ResultStorageError(ValueError):
    pass


class ResultPageTooLarge(ResultStorageError):
    pass


class ResultWriteCancelled(ResultStorageError):
    pass


_JSON = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False)


# 结果目录协议的稳定身份；读者应同时校验 schema 与 schema_version。
RESULT_MANIFEST_SCHEMA = "parseflow.result"
RESULT_MANIFEST_VERSION = 1


def safe_result_path(workspace: Path, storage_key: str) -> Path:
    """Resolve relative keys without following even intermediate symlinks."""
    relative = PurePosixPath(storage_key)
    if not storage_key or relative.is_absolute() or ".." in relative.parts or "\\" in storage_key:
        raise ResultStorageError("无效的结果文件引用")
    root = workspace.resolve()
    candidate = root
    for part in relative.parts:
        if part in {"", "."}:
            raise ResultStorageError("无效的结果文件引用")
        candidate = candidate / part
        if candidate.is_symlink():
            raise ResultStorageError("结果文件不能是符号链接")
    candidate.resolve().relative_to(root)
    return candidate


def _json_chunks(value: Any, depth: int = 0) -> Iterator[bytes]:
    """Stream strings too: JSONEncoder.iterencode creates a full large string token."""
    if depth > 128:
        raise ResultStorageError("结构化结果嵌套超过预算")
    if isinstance(value, str):
        yield b'"'
        for index in range(0, len(value), 8192):
            yield _JSON.encode(value[index:index + 8192])[1:-1].encode("utf-8")
        yield b'"'
    elif isinstance(value, dict):
        yield b"{"
        for index, (key, item) in enumerate(value.items()):
            if index:
                yield b","
            yield from _json_chunks(str(key), depth + 1)
            yield b":"
            yield from _json_chunks(item, depth + 1)
        yield b"}"
    elif isinstance(value, (list, tuple)):
        yield b"["
        for index, item in enumerate(value):
            if index:
                yield b","
            yield from _json_chunks(item, depth + 1)
        yield b"]"
    else:
        yield _JSON.encode(value).encode("utf-8")


class _Publisher:
    def __init__(self, root: Path, max_bytes: int, cancel: threading.Event):
        self.root, self.max_bytes, self.cancel = root, max_bytes, cancel
        self.used = 0
        self.files = 0

    def check(self, amount: int = 0):
        if self.cancel.is_set():
            raise ResultWriteCancelled("结果写入已取消")
        self.used += amount
        if self.used > self.max_bytes:
            raise ResultStorageError("完整解析结果超过文件存储预算")

    def write(self, relative: str, chunks: Iterator[bytes]) -> dict[str, Any]:
        self.files += 1
        if self.files > 100_000:
            raise ResultStorageError("结果文件数量超过预算")
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        with path.open("wb") as handle:
            for chunk in chunks:
                self.check(len(chunk))
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        return {"storage_key": "result/" + relative, "size_bytes": size, "sha256": digest.hexdigest()}

    def json(self, relative: str, value: Any) -> dict[str, Any]:
        return self.write(relative, _json_chunks(value))


def bounded_preview(document: dict[str, Any], max_bytes: int = 256 * 1024) -> tuple[dict, bool]:
    """A single global budget also covers provenance/extensions and long metadata."""
    remaining = max(4096, max_bytes // 3)  # JSON escaping/markers/field overhead reserve.
    shortened = False
    nodes = 0
    limits = {"pages": 5, "blocks": 100, "tables": 30, "images": 30, "cells": 100,
              "rows": 30, "sheets": 30}

    def visit(item: Any, key: str = "", depth: int = 0):
        nonlocal remaining, shortened, nodes
        nodes += 1
        if depth > 16 or nodes > 10000 or remaining < 64:
            shortened = True
            return None
        if isinstance(item, str):
            allowed = min(8000, max(0, remaining // 6))
            value = item[:allowed]
            remaining -= len(value.encode("utf-8")) + 8
            if len(value) != len(item):
                shortened = True
            return value
        if isinstance(item, dict):
            result = {}
            for field, value in item.items():
                if remaining < 64 or len(result) >= 100:
                    shortened = True
                    break
                remaining -= len(str(field).encode()) + 8
                result[field] = visit(value, str(field), depth + 1)
            return result
        if isinstance(item, list):
            result = []
            limit = limits.get(key, 50)
            for value in item[:limit]:
                if remaining < 64:
                    break
                remaining -= 8
                converted = visit(value, key, depth + 1)
                if converted is None and isinstance(value, (dict, list, str)):
                    shortened = True
                    break
                result.append(converted)
            if len(result) != len(item):
                shortened = True
            return result
        remaining -= 24
        return item

    # Stable identity/summary before large lists, then content. Preserve registry data refs.
    ordered = {key: document[key] for key in ("document_id", "document_type", "source_file", "parser", "quality", "warnings") if key in document}
    for key in ("metadata", "representations", "pages", "tables", "blocks", "images", "provenance", "extensions"):
        if key in document:
            ordered[key] = document[key]
    if set(document) - set(ordered):
        shortened = True
    preview = visit(ordered)
    # Markers must exist even when the source metadata consumed the budget.
    metadata = preview.get("metadata") if isinstance(preview.get("metadata"), dict) else {}
    metadata["result_storage"] = {"schema_version": 1, "filename": "result/manifest.json", "preview_truncated": shortened}
    preview["metadata"] = metadata
    excel = (document.get("metadata") or {}).get("excel_storage")
    sheet_directory = []
    if isinstance(excel, dict) and excel.get("filename") in {"workbook.sqlite", "workbook/manifest.json"}:
        metadata["excel_storage"] = {"filename": excel["filename"], "schema_version": excel.get("schema_version", 1)}
        existing = {item.get("sheet_id"): item for item in (preview.get("tables") or []) if isinstance(item, dict)}
        for table in (document.get("tables") or [])[:30]:
            if table.get("data_ref") not in {"workbook.sqlite", "workbook/manifest.json"}:
                continue
            info = {key: table[key] for key in ("sheet_id", "data_ref", "row_count", "semantic_cell_count") if key in table}
            info["sheet_name"] = str(table.get("sheet_name") or table.get("name") or "工作表")[:80]
            info["preview_truncated"] = bool(table.get("preview_truncated")) or table.get("sheet_id") not in existing
            sheet_directory.append(info)
        tables = preview.get("tables") if isinstance(preview.get("tables"), list) else []
        if any(info["sheet_id"] not in existing for info in sheet_directory):
            shortened = True
            preview["tables"] = sheet_directory
            metadata["result_storage"]["preview_truncated"] = True
        else:
            for info in sheet_directory:
                table = existing[info["sheet_id"]]
                if table.get("data_ref") != info["data_ref"] or "rows" not in table or "cells" not in table:
                    info["preview_truncated"] = True
                    shortened = True
                table.update(info)
            metadata["result_storage"]["preview_truncated"] = shortened
    media = (document.get("metadata") or {}).get("media")
    media_identity = {}
    if document.get("document_type") in {"audio", "video"} and isinstance(media, dict):
        for key in ("media_type", "duration_ms", "format", "subtitle_available", "transcript_available", "asr_status",
                    "subtitle_count", "subtitle_cue_count", "content_scope", "timeline_source", "timeline_file",
                    "timeline_time_base", "timeline_schema_version", "selected_audio_stream_index", "selected_subtitle_stream_index",
                    "audio_mode", "subtitle_mode", "source_start_time", "duration_basis"):
            value = media.get(key)
            if value is None or isinstance(value, (bool, int, float, str)):
                media_identity[key] = value[:256] if isinstance(value, str) else value
        media_identity["stages"] = [{key: (str(value)[:256] if isinstance(value, str) else value)
            for key, value in stage.items() if key in {"name", "status", "reason", "cue_count"}}
            for stage in media.get("stages", [])[:4] if isinstance(stage, dict)]
        current_media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
        current_media.update(media_identity)
        metadata["media"] = current_media
    for key in ("pages", "blocks", "tables", "images"):
        if not isinstance(preview.get(key), list):
            preview[key] = []
    if not isinstance(preview.get("representations"), dict):
        preview["representations"] = {}
    # Explicit final byte guard, with minimal identity fallback for extreme key escaping.
    if len(_JSON.encode(preview).encode()) > max_bytes:
        shortened = True
        preview = {key: preview.get(key) for key in ("document_id", "document_type", "source_file", "parser", "quality")}
        preview.update(pages=[], blocks=[], tables=sheet_directory, images=[], representations={},
                       metadata={"result_storage": {"schema_version": 1, "filename": "result/manifest.json", "preview_truncated": True}})
        if media_identity:
            preview["metadata"]["media"] = media_identity
        if isinstance(excel, dict) and excel.get("filename") in {"workbook.sqlite", "workbook/manifest.json"}:
            preview["metadata"]["excel_storage"] = {"filename": excel["filename"], "schema_version": excel.get("schema_version", 1)}
    return preview, shortened


def publish_result(workspace: Path, document: dict[str, Any] | None, *, payload: dict[str, Any] | None = None,
                   max_bytes: int = 4096 * 1024 * 1024, preview_bytes: int = 256 * 1024,
                   cancel: threading.Event | None = None) -> tuple[dict | None, dict]:
    """Publish immutable full JSON, representations, and bounded unit chunks atomically."""
    cancel = cancel or threading.Event()
    workspace.mkdir(parents=True, exist_ok=True)
    temporary = workspace / (".result-" + uuid4().hex)
    temporary.mkdir()
    publisher = _Publisher(temporary, max_bytes, cancel)
    # schema 身份与版本分开：schema 标识协议名，schema_version 标识该协议的结构版本。
    manifest: dict[str, Any] = {"schema": RESULT_MANIFEST_SCHEMA, "schema_version": RESULT_MANIFEST_VERSION,
                                "document_type": (document or {}).get("document_type", "media"),
                                "units": [], "representations": {}, "resources": []}
    preview = None
    try:
        canonical = document if document is not None else {"document_type": "media", "payload": payload or {}}
        resource = publisher.json("document.json", canonical)
        manifest["document"] = resource
        manifest["resources"].append(resource)
        if payload:
            resource = publisher.json("payload.json", payload)
            manifest["payload"] = resource
            manifest["resources"].append(resource)
        for index, (kind, text) in enumerate(((document or {}).get("representations") or {}).items()):
            if not isinstance(text, str):
                continue
            suffix = {"plain_text": "txt", "markdown": "md", "html": "html"}.get(kind, "txt")
            resource = publisher.write(f"representations/{index:03d}.{suffix}", (text[start:start+8192].encode() for start in range(0, len(text), 8192)))
            manifest["representations"][kind] = resource
            manifest["resources"].append(resource)
        def unit(kind: str, label: str, records, metadata: dict | None = None):
            unit_id = f"unit-{len(manifest['units']):06d}"
            if len(manifest["units"]) >= 10000:
                raise ResultStorageError("结果内容单元数量超过预算")
            info = {"unit_id": unit_id, "kind": kind, "label": label[:256], "record_count": 0,
                    "chunks": [], "metadata": metadata or {}}
            batch = bytearray()
            batch_records = 0
            first = 0
            def flush():
                nonlocal batch, batch_records, first
                if not batch_records:
                    return
                relative = f"units/{unit_id}/{len(info['chunks']):06d}.jsonl.gz"
                self_data = bytes(batch)
                # Compression input bounded to one normal batch or one explicit record.
                resource = publisher.write(relative, iter((gzip.compress(self_data, compresslevel=1, mtime=0),)))
                resource.update(first_record=first, record_count=batch_records, uncompressed_bytes=len(self_data))
                info["chunks"].append(resource)
                manifest["resources"].append(resource)
                first += batch_records
                batch, batch_records = bytearray(), 0
            for record in records:
                publisher.check()
                encoded = bytearray()
                for piece in _json_chunks(record):
                    if len(encoded) + len(piece) > 8 * 1024 * 1024:
                        # Complete record is still available in document.json; expose explicit reference.
                        encoded = bytearray(_JSON.encode({"oversized_record": True, "document_ref": "result/document.json", "record_index": info["record_count"]}).encode())
                        break
                    encoded.extend(piece)
                if batch and (len(batch) + len(encoded) > 512 * 1024 or batch_records >= 256):
                    flush()
                batch.extend(encoded)
                batch.append(10)
                batch_records += 1
                info["record_count"] += 1
            flush()
            manifest["units"].append(info)

        if document:
            for index, page in enumerate(document.get("pages") or []):
                unit("page", f"第 {page.get('page_number', index + 1)} 页", iter((page,)))
            if document.get("blocks"):
                blocks = document["blocks"]
                for start in range(0, len(blocks), 256):
                    unit("blocks", f"内容块 {start+1}–{min(start+256,len(blocks))}", iter(blocks[start:start+256]))
            for index, table in enumerate(document.get("tables") or []):
                # Excel's full cell data already has a dedicated manifest and is not copied.
                external = table.get("data_ref")
                unit("sheet" if external else "table", str(table.get("sheet_name") or table.get("name") or f"表格 {index+1}"),
                     iter(()) if external else iter(table.get("cells") or table.get("rows") or (table,)),
                     {"data_ref": external, "sheet_id": table.get("sheet_id")} if external else {})
            if document.get("images"):
                unit("images", "图片", iter(document["images"]))
            preview, shortened = bounded_preview(document, preview_bytes)
            shortened = shortened or bool((document.get("quality") or {}).get("preview_truncated")) or any(
                bool(table.get("preview_truncated")) for table in (document.get("tables") or []))
            preview["metadata"]["result_storage"]["preview_truncated"] = shortened
            manifest["preview_truncated"] = shortened
            excel = (document.get("metadata") or {}).get("excel_storage")
            if excel:
                manifest["external_storage"] = excel
        else:
            unit("media", "媒体处理结果", iter((payload or {},)))
            manifest["preview_truncated"] = False
        for binary in workspace.rglob("*"):
            relative = binary.relative_to(workspace)
            if any(part.startswith(".") for part in relative.parts) or relative.parts[0] in {"result", "exports"}:
                continue
            if not binary.is_file():
                continue
            path = safe_result_path(workspace, relative.as_posix())
            size = path.stat().st_size
            publisher.check(size)
            publisher.files += 1
            if publisher.files > 100_000:
                raise ResultStorageError("结果文件数量超过预算")
            digest = hashlib.sha256()
            with path.open("rb") as source:
                while piece := source.read(65536):
                    publisher.check()
                    digest.update(piece)
            # Excel chunks are already registered and hashed in the external manifest.
            # Account bytes here without duplicating its potentially large block directory.
            if relative.parts[0] != "workbook" or "chunks" not in relative.parts:
                manifest["resources"].append({"storage_key": relative.as_posix(), "size_bytes": size,
                                               "sha256": digest.hexdigest(), "kind": "generated_file"})
        publisher.check()
        publisher.json("manifest.json", manifest)
        destination = safe_result_path(workspace, "result")
        if destination.exists():
            raise ResultStorageError("结果目录已发布，禁止覆盖")
        publisher.check()
        os.replace(temporary, destination)
        # result_ref 是任务记录中的稳定引用；它与 manifest 使用同一协议版本，便于客户端校验。
        return preview, {"schema": RESULT_MANIFEST_SCHEMA, "schema_version": RESULT_MANIFEST_VERSION,
                         "storage_key": "result/manifest.json",
                         "preview_truncated": manifest["preview_truncated"],
                         "document_size_bytes": manifest["document"]["size_bytes"]}
    finally:
        if temporary.resolve().parent == workspace.resolve() and temporary.name.startswith(".result-"):
            shutil.rmtree(temporary, ignore_errors=True)


def remove_file_result(workspace: Path) -> None:
    root = workspace.resolve()
    target = root / "result"
    if target.is_symlink():
        target.unlink()
    elif target.resolve().parent == root and target.is_dir():
        shutil.rmtree(target)


async def publish_result_async(workspace: Path, document: dict | None, *, payload: dict | None = None,
                               max_bytes: int = 4096 * 1024 * 1024, preview_bytes: int = 256 * 1024,
                               cancel_path: Path | None = None) -> tuple[dict | None, dict]:
    cancelled = threading.Event()
    writer = asyncio.create_task(asyncio.to_thread(publish_result, workspace, document, payload=payload,
        max_bytes=max_bytes, preview_bytes=preview_bytes, cancel=cancelled))
    try:
        while not writer.done():
            done, _ = await asyncio.wait((writer,), timeout=0.1)
            if cancel_path is not None and cancel_path.exists():
                cancelled.set()
            if done:
                break
        value = await writer
        if cancelled.is_set() or (cancel_path is not None and cancel_path.exists()):
            remove_file_result(workspace)
            raise ResultWriteCancelled("结果写入已取消")
        return value
    except asyncio.CancelledError:
        cancelled.set()
        # Join the cooperative writer before task cancellation can trigger deletion.
        try:
            await asyncio.shield(writer)
        except (ResultWriteCancelled, ResultStorageError):
            pass
        remove_file_result(workspace)
        raise


def load_result_manifest(workspace: Path) -> dict:
    path = safe_result_path(workspace, "result/manifest.json")
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ResultStorageError("结果目录超过大小预算")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != RESULT_MANIFEST_VERSION:
        raise ResultStorageError("不支持的结果存储版本")
    # 旧 manifest 没有 schema 字段；只校验出现的身份，避免拒绝既有任务。
    declared = value.get("schema")
    if declared is not None and declared != RESULT_MANIFEST_SCHEMA:
        raise ResultStorageError("结果目录 schema 身份不匹配")
    if not isinstance(value.get("units"), list) or not isinstance(value.get("resources"), list):
        raise ResultStorageError("结果目录结构损坏")
    for unit in value["units"]:
        if not isinstance(unit, dict) or not isinstance(unit.get("unit_id"), str) or not isinstance(unit.get("record_count"), int) or not isinstance(unit.get("chunks"), list):
            raise ResultStorageError("结果单元目录损坏")
        for chunk in unit["chunks"]:
            if not isinstance(chunk, dict) or not all(key in chunk for key in ("storage_key", "size_bytes", "sha256", "first_record", "record_count")):
                raise ResultStorageError("结果数据块目录损坏")
    if not all(isinstance(item, dict) and isinstance(item.get("storage_key"), str) for item in value["resources"]):
        raise ResultStorageError("结果资源目录损坏")
    return value


def read_unit_records(workspace: Path, unit_id: str, *, offset: int = 0, limit: int = 100,
                      max_bytes: int = 1024 * 1024) -> dict:
    try:
        return _read_unit_records(workspace, unit_id, offset=offset, limit=limit, max_bytes=max_bytes)
    except (ResultStorageError, FileNotFoundError, KeyError):
        raise
    except (OSError, EOFError, ValueError, TypeError) as exc:
        raise ResultStorageError("结果数据块损坏，无法读取") from exc


def _read_unit_records(workspace: Path, unit_id: str, *, offset: int = 0, limit: int = 100,
                      max_bytes: int = 1024 * 1024) -> dict:
    if not re.fullmatch(r"unit-\d{6}", unit_id) or not 0 <= offset <= 2**63-1 or not 1 <= limit <= 200:
        raise ResultStorageError("无效的结果分页参数")
    manifest = load_result_manifest(workspace)
    unit = next((item for item in manifest["units"] if item["unit_id"] == unit_id), None)
    if unit is None:
        raise KeyError(unit_id)
    rows, used, position = [], 0, offset
    for chunk in unit["chunks"]:
        first, count = chunk["first_record"], chunk["record_count"]
        if first + count <= offset:
            continue
        path = safe_result_path(workspace, chunk["storage_key"])
        if path.stat().st_size != chunk["size_bytes"]:
            raise ResultStorageError("结果数据块大小校验失败")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while piece := source.read(65536):
                digest.update(piece)
        if digest.hexdigest() != chunk["sha256"]:
            raise ResultStorageError("结果数据块校验失败")
        with gzip.open(path, "rb") as handle:
            for index in range(count):
                line = handle.readline(8 * 1024 * 1024 + 2)
                if len(line) > 8 * 1024 * 1024 + 1:
                    raise ResultStorageError("结果数据块记录超过预算")
                if not line:
                    raise ResultStorageError("结果数据块不完整")
                if first + index < offset:
                    continue
                if len(rows) >= limit or used + len(line) > max_bytes:
                    if not rows:
                        raise ResultPageTooLarge("此记录超过预览上限，请下载完整解析结果")
                    return {"unit_id": unit_id, "records": rows, "next_offset": position}
                if not line:
                    raise ResultStorageError("结果数据块不完整")
                rows.append(json.loads(line))
                used += len(line)
                position = first + index + 1
    return {"unit_id": unit_id, "records": rows, "next_offset": position if position < unit["record_count"] else None}


def resolve_result_resource(workspace: Path, storage_key: str) -> Path:
    manifest = load_result_manifest(workspace)
    allowed = {item["storage_key"]: item for item in manifest["resources"]}
    allowed["result/manifest.json"] = None
    external = manifest.get("external_storage") or {}
    if external.get("filename") == "workbook/manifest.json":
        from app.skills.excel.store import load_workbook_manifest
        workbook = load_workbook_manifest(safe_result_path(workspace, "workbook/manifest.json"))
        allowed.setdefault("workbook/manifest.json", None)
        for sheet in workbook.get("sheets", []):
            for chunk in sheet.get("chunks", []):
                allowed["workbook/" + chunk["file"]] = chunk
    if storage_key not in allowed:
        raise FileNotFoundError(storage_key)
    path = safe_result_path(workspace, storage_key)
    if not path.is_file():
        raise FileNotFoundError(storage_key)
    expected = allowed[storage_key]
    if expected is not None:
        if path.stat().st_size != expected["size_bytes"]:
            raise ResultStorageError("完整结果文件大小校验失败")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(65536):
                digest.update(chunk)
        if digest.hexdigest() != expected["sha256"]:
            raise ResultStorageError("完整结果文件校验失败")
    return path
