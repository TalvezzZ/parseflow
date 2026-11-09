"""Disk-backed Excel cells. Only bounded previews belong in task records."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4
from typing import Any


class ExcelStorageLimitError(ValueError):
    pass


class ExcelPageTooLarge(ValueError):
    pass


def open_excel_store(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("Excel 数据文件不存在")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA cache_size=-2048")
    return connection


class ExcelStoreWriter:
    """Publish a complete SQLite store atomically; discard interrupted writes."""

    def __init__(self, path: Path, *, max_bytes: int = 4096 * 1024 * 1024):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        self.max_bytes = max_bytes
        self.connection = sqlite3.connect(self.temporary)
        self.connection.execute("PRAGMA page_size=4096")
        self.connection.execute("PRAGMA cache_size=-2048")
        self.connection.execute("PRAGMA temp_store=FILE")
        self.connection.execute("PRAGMA secure_delete=ON")
        # This database is private staging data until finish() fsyncs and renames it.
        # A failed batch discards the whole store, so per-batch recovery journals are unnecessary.
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA synchronous=OFF")
        self.connection.execute(f"PRAGMA max_page_count={max(1, max_bytes // 4096)}")
        self.connection.executescript("""
            CREATE TABLE sheets (sheet_id INTEGER PRIMARY KEY, metadata TEXT NOT NULL);
            CREATE TABLE cells (
                sheet_id INTEGER NOT NULL, row INTEGER NOT NULL, column INTEGER NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(sheet_id, row, column)
            ) WITHOUT ROWID;
            PRAGMA user_version=1;
        """)
        self._batch: list[tuple[int, int, int, str]] = []
        self._batch_bytes = 0
        self._finished = False
        self._closed = False

    def __enter__(self) -> "ExcelStoreWriter":
        return self

    def add_sheet(self, sheet_id: int, metadata: dict[str, Any]) -> None:
        self.connection.execute("INSERT INTO sheets VALUES (?, ?)", (sheet_id, json.dumps(metadata, ensure_ascii=False)))

    def update_sheet(self, sheet_id: int, metadata: dict[str, Any]) -> None:
        self.flush()
        self.connection.execute("UPDATE sheets SET metadata=? WHERE sheet_id=?", (json.dumps(metadata, ensure_ascii=False), sheet_id))

    def add_cell(self, sheet_id: int, cell: dict[str, Any]) -> None:
        payload = json.dumps(cell, ensure_ascii=False, separators=(",", ":"))
        self._batch.append((sheet_id, cell["row"], cell["column"], payload))
        self._batch_bytes += len(payload.encode("utf-8"))
        if len(self._batch) >= 512 or self._batch_bytes >= 512 * 1024:
            self.flush()

    def flush(self) -> None:
        try:
            if self._batch:
                self.connection.executemany("INSERT OR REPLACE INTO cells VALUES (?, ?, ?, ?)", self._batch)
                self._batch.clear()
                self._batch_bytes = 0
            self.connection.commit()
            self.check_budget()
        except sqlite3.DatabaseError as exc:
            if "full" in str(exc).lower():
                raise ExcelStorageLimitError("Excel 数据存储超过磁盘预算") from exc
            raise

    def check_budget(self) -> None:
        pages = self.connection.execute("PRAGMA page_count").fetchone()[0]
        if pages * 4096 > self.max_bytes:
            raise ExcelStorageLimitError("Excel 数据存储超过磁盘预算")

    def finish(self) -> None:
        self.flush()
        self.connection.close()
        self._closed = True
        with self.temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(self.temporary, self.path)
        self._finished = True

    def __exit__(self, *args: object) -> None:
        if not self._closed:
            self.connection.close()
        if not self._finished:
            self.temporary.unlink(missing_ok=True)
        self.temporary.with_name(self.temporary.name + "-journal").unlink(missing_ok=True)


def read_sheet_page(path: Path, sheet_id: int, *, after_row: int = 0, limit: int = 100,
                    max_bytes: int = 1024 * 1024) -> dict[str, Any]:
    """Read keyset pages of complete nonempty rows with a hard payload budget.

    A single oversized row is rejected explicitly rather than silently losing cells.
    Full values remain available in the downloadable data store.
    """
    if not 0 <= sheet_id <= 2**31 - 1 or not 0 <= after_row <= 1_048_576 or not 1 <= limit <= 200:
        raise ValueError("无效的 Excel 分页参数")
    with closing(open_excel_store(path)) as connection:
        found = connection.execute("SELECT metadata FROM sheets WHERE sheet_id=?", (sheet_id,)).fetchone()
        if found is None:
            raise KeyError(sheet_id)
        metadata = json.loads(found[0])
        candidates = connection.execute(
            "SELECT DISTINCT row FROM cells WHERE sheet_id=? AND row>? ORDER BY row LIMIT ?",
            (sheet_id, after_row, limit + 1),
        ).fetchall()
        rows: list[dict[str, Any]] = []
        used = 0
        has_more = len(candidates) > limit
        for (row_number,) in candidates[:limit]:
            cells: list[dict[str, Any]] = []
            row_bytes = 0
            oversized = False
            for (payload,) in connection.execute(
                "SELECT payload FROM cells WHERE sheet_id=? AND row=? ORDER BY column", (sheet_id, row_number),
            ):
                row_bytes += len(payload.encode("utf-8")) + 2
                if row_bytes > max_bytes:
                    oversized = True
                    break
                cells.append(json.loads(payload))
            if oversized or used + row_bytes > max_bytes:
                if not rows:
                    raise ExcelPageTooLarge("此行数据超过预览大小上限，请下载完整数据查看")
                has_more = True
                break
            rows.append({"row": row_number, "cells": cells})
            used += row_bytes
        return {
            "sheet_id": sheet_id,
            "sheet_name": metadata.get("name") or metadata.get("sheet_name") or str(sheet_id),
            "rows": rows,
            "next_after_row": rows[-1]["row"] if has_more and rows else None,
            "semantic_cell_count": metadata.get("semantic_cell_count", 0),
            "row_count": metadata.get("row_count", 0),
        }
