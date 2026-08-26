from __future__ import annotations

import zipfile
from pathlib import Path

from app.security.budgets import ResourceBudgetExceeded


def validate_zip_archive(path: str | Path, *, max_entries: int, max_uncompressed_bytes: int, max_ratio: float = 200.0) -> None:
    """Reject unsafe OOXML/ZIP central directories before a library expands them."""
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise ResourceBudgetExceeded("archive_invalid", "压缩文档不是有效 ZIP/OOXML 文件") from exc
    if len(entries) > max_entries:
        raise ResourceBudgetExceeded("archive_entry_limit_exceeded", "压缩文档条目数量超过限制")
    total = 0
    for entry in entries:
        name = entry.filename.replace("\\", "/")
        if name.startswith("/") or any(part == ".." for part in name.split("/")):
            raise ResourceBudgetExceeded("archive_path_unsafe", "压缩文档包含不安全路径")
        total += entry.file_size
        if total > max_uncompressed_bytes:
            raise ResourceBudgetExceeded("archive_uncompressed_limit_exceeded", "压缩文档解压后大小超过限制")
        if entry.compress_size and entry.file_size / entry.compress_size > max_ratio:
            raise ResourceBudgetExceeded("resource_budget_exceeded", "压缩文档压缩比异常")
