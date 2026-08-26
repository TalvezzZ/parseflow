from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import UploadFile
from pydantic import BaseModel

from app.storage.atomic import atomic_write_json
from app.storage.ids import new_id, require_id


class StoredFilePublic(BaseModel):
    file_id: str
    filename: str
    content_type: str | None = None
    size_bytes: int
    created_at: datetime
    download_url: str


class StoredFileRecord(BaseModel):
    """Internal metadata; never serialize this model from an HTTP/MCP handler."""

    file_id: str
    filename: str
    content_type: str | None = None
    size_bytes: int
    created_at: datetime
    storage_name: str
    relative_path: str
    sha256: str


class LocalFileStore:
    """Controlled local upload repository with separate public and internal views."""

    def __init__(self, root: str, max_size_mb: int, allowed_suffixes: str, min_free_mb: int = 0) -> None:
        self.root = Path(root).resolve()
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.min_free_bytes = min_free_mb * 1024 * 1024
        self.allowed_suffixes = {item.strip().lower() for item in allowed_suffixes.split(",") if item.strip()}
        self.root.mkdir(parents=True, exist_ok=True)

    async def save(self, upload: UploadFile) -> StoredFileRecord:
        filename = Path(upload.filename or "upload.bin").name
        suffix = Path(filename).suffix.lower()
        if self.allowed_suffixes and suffix not in self.allowed_suffixes:
            raise ValueError(f"不支持上传该文件类型: {suffix or '<无后缀>'}")
        if self.min_free_bytes and shutil.disk_usage(self.root).free < self.min_free_bytes:
            raise ValueError("storage_capacity_exceeded: 存储可用空间低于安全阈值")
        file_id = new_id("file")
        directory = self._directory(file_id)
        directory.mkdir(parents=True, exist_ok=False)
        storage_name = f"source{suffix}" if suffix else "source"
        target = self._safe_child(directory, storage_name)
        temporary = self._safe_child(directory, f".{storage_name}.upload")
        written = 0
        digest = hashlib.sha256()
        try:
            with temporary.open("xb") as output:
                while chunk := await upload.read(1024 * 1024):
                    written += len(chunk)
                    if written > self.max_size_bytes:
                        raise ValueError(f"文件超过 {self.max_size_bytes // (1024 * 1024)} MB 上传限制")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            record = StoredFileRecord(
                file_id=file_id,
                filename=filename,
                content_type=upload.content_type,
                size_bytes=written,
                created_at=datetime.now(timezone.utc),
                storage_name=storage_name,
                relative_path=f"{file_id}/{storage_name}",
                sha256=digest.hexdigest(),
            )
            atomic_write_json(self._metadata_path(file_id), record)
            return record
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        finally:
            await upload.close()

    def get(self, file_id: str) -> StoredFileRecord | None:
        try:
            metadata = self._metadata_path(file_id)
        except ValueError:
            return None
        if not metadata.is_file() or metadata.is_symlink():
            return None
        try:
            return StoredFileRecord.model_validate_json(metadata.read_text(encoding="utf-8"))
        except Exception:
            return None

    def public(self, file_id: str) -> StoredFilePublic | None:
        record = self.get(file_id)
        if record is None:
            return None
        return StoredFilePublic(
            file_id=record.file_id,
            filename=record.filename,
            content_type=record.content_type,
            size_bytes=record.size_bytes,
            created_at=record.created_at,
            download_url=f"/api/v1/files/{record.file_id}/content",
        )

    def source_path(self, file_id: str) -> Path | None:
        record = self.get(file_id)
        if record is None:
            return None
        try:
            path = self._safe_child(self.root, record.relative_path)
        except ValueError:
            return None
        return path if path.is_file() and not path.is_symlink() else None

    def delete(self, file_id: str) -> None:
        try:
            directory = self._directory(file_id)
        except ValueError:
            return
        shutil.rmtree(directory, ignore_errors=True)

    def _directory(self, file_id: str) -> Path:
        require_id("file", file_id)
        return self._safe_child(self.root, file_id)

    def _metadata_path(self, file_id: str) -> Path:
        return self._safe_child(self._directory(file_id), "metadata.json")

    @staticmethod
    def _safe_child(root: Path, relative: str) -> Path:
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("非法存储路径") from exc
        return candidate


# Retained as an import compatibility name; callers must use StoredFilePublic at public boundaries.
StoredFile = StoredFilePublic
