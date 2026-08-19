from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import UploadFile
from pydantic import BaseModel


class StoredFile(BaseModel):
    file_id: str
    filename: str
    content_type: str | None = None
    size_bytes: int
    path: str
    created_at: datetime


class LocalFileStore:
    """无数据库的受控本地文件存储；每个文件都有专属 artifact 目录。"""

    def __init__(self, root: str, max_size_mb: int, allowed_suffixes: str) -> None:
        self.root = Path(root).resolve()
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.allowed_suffixes = {item.strip().lower() for item in allowed_suffixes.split(",") if item.strip()}

    async def save(self, upload: UploadFile) -> StoredFile:
        filename = Path(upload.filename or "upload.bin").name
        suffix = Path(filename).suffix.lower()
        if self.allowed_suffixes and suffix not in self.allowed_suffixes:
            raise ValueError(f"不支持上传该文件类型: {suffix or '<无后缀>'}")
        file_id = f"file_{uuid.uuid4().hex}"
        directory = self.root / file_id
        directory.mkdir(parents=True, exist_ok=False)
        target = directory / filename
        written = 0
        try:
            with target.open("wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    written += len(chunk)
                    if written > self.max_size_bytes:
                        raise ValueError(f"文件超过 {self.max_size_bytes // (1024 * 1024)} MB 上传限制")
                    output.write(chunk)
            record = StoredFile(file_id=file_id, filename=filename, content_type=upload.content_type,
                                size_bytes=written, path=str(target), created_at=datetime.now(timezone.utc))
            (directory / "artifacts").mkdir()
            self._metadata_path(file_id).write_text(record.model_dump_json(), encoding="utf-8")
            return record
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        finally:
            await upload.close()

    def get(self, file_id: str) -> StoredFile | None:
        path = self._metadata_path(file_id)
        if not path.is_file():
            return None
        return StoredFile.model_validate_json(path.read_text(encoding="utf-8"))

    def artifact_dir(self, file_id: str) -> Path | None:
        record = self.get(file_id)
        if record is None:
            return None
        return self.root / file_id / "artifacts"

    def artifact_path(self, file_id: str, relative_path: str) -> Path | None:
        artifact_dir = self.artifact_dir(file_id)
        if artifact_dir is None:
            return None
        candidate = (artifact_dir / relative_path).resolve()
        try:
            candidate.relative_to(artifact_dir.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def _metadata_path(self, file_id: str) -> Path:
        if not file_id.startswith("file_") or any(part in file_id for part in ("/", "\\", "..")):
            return self.root / "__invalid__"
        return self.root / file_id / "metadata.json"
