"""Per-sheet chunk files, with read compatibility for legacy SQLite workbooks."""
from __future__ import annotations

import gzip
import hashlib
import shutil
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


class LegacyExcelStoreWriter:
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

    def __enter__(self) -> "LegacyExcelStoreWriter":
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


def _read_sqlite_page(path: Path, sheet_id: int, *, after_row: int = 0, limit: int = 100,
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


# v2: the lookup connection is private and never contains the final cells.


class ExcelStoreWriter:
    def __init__(self, path: Path, *, max_bytes: int = 4096 * 1024 * 1024,
                 chunk_records: int = 4096, chunk_bytes: int = 1024 * 1024):
        self.path = Path(path)
        if self.path.name != 'manifest.json' or self.path.parent.name != 'workbook':
            raise ValueError('Excel file results must target workbook/manifest.json')
        if min(max_bytes, chunk_records, chunk_bytes) <= 0:
            raise ValueError('Excel chunk budgets must be positive')
        self.root = self.path.parent
        self.root.parent.mkdir(parents=True, exist_ok=True)
        self.temporary = self.root.with_name(f".{self.root.name}.{uuid4().hex}.tmp")
        self.temporary.mkdir()
        self.lookup = self.temporary / '.lookup.sqlite'
        self.connection = sqlite3.connect(self.lookup)
        self.connection.executescript('PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE; PRAGMA cache_size=-2048;')
        self.connection.execute(f'PRAGMA max_page_count={max(4, max_bytes // 4096)}')
        self.max_bytes, self.chunk_records, self.chunk_bytes = max_bytes, chunk_records, chunk_bytes
        self.sheets: dict[int, dict] = {}
        self._file = self._gzip = None
        self._chunk = None
        self._sheet = None
        self._last = (0, 0)
        self._finished = self._closed = False
        self.stored_bytes = 0
        self._chunks = 0

    def __enter__(self):
        return self

    def add_sheet(self, sheet_id: int, metadata: dict) -> None:
        self._close_chunk()
        self.sheets[sheet_id] = {'metadata': dict(metadata), 'chunks': [], 'row_count': 0}
        self._sheet, self._last = sheet_id, (0, 0)
        (self.temporary / 'sheets' / str(sheet_id) / 'chunks').mkdir(parents=True)

    def update_sheet(self, sheet_id: int, metadata: dict) -> None:
        self._close_chunk()
        self.sheets[sheet_id]['metadata'] = dict(metadata)

    def add_cell(self, sheet_id: int, cell: dict) -> None:
        position = (cell['row'], cell['column'])
        if sheet_id != self._sheet or position <= self._last:
            raise ValueError('Excel cells must be emitted once in ascending row/column order')
        payload = (json.dumps(cell, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
        if len(payload) > 16 * 1024 * 1024:
            raise ExcelStorageLimitError('Excel 单元格记录超过16 MB限制')
        threshold = self._chunk and (self._chunk['records'] >= self.chunk_records or self._chunk['uncompressed_bytes'] + len(payload) > self.chunk_bytes)
        if threshold and (position[0] != self._last[0] or self._chunk['uncompressed_bytes'] + len(payload) > 2 * self.chunk_bytes):
            self._close_chunk()
        if self._chunk is None:
            self._chunks += 1
            if self._chunks > 100000:
                raise ExcelStorageLimitError('Excel 数据块数量超过限制')
            chunks = self.sheets[sheet_id]['chunks']
            relative = f'sheets/{sheet_id}/chunks/{len(chunks):06d}.jsonl.gz'
            self._file = (self.temporary / relative).open('wb')
            self._gzip = gzip.GzipFile(filename='', mode='wb', fileobj=self._file, compresslevel=1, mtime=0)
            self._chunk = {'file': relative, 'first_row': position[0], 'last_row': position[0],
                           'first_column': position[1], 'last_column': position[1],
                           'min_column': position[1], 'max_column': position[1],
                           'records': 0, 'uncompressed_bytes': 0}
        self._gzip.write(payload)
        self._chunk['records'] += 1
        self._chunk['uncompressed_bytes'] += len(payload)
        self._chunk['last_row'] = position[0]
        self._chunk['last_column'] = position[1]
        self._chunk['min_column'] = min(self._chunk['min_column'], position[1])
        self._chunk['max_column'] = max(self._chunk['max_column'], position[1])
        if position[0] != self._last[0]:
            self.sheets[sheet_id]['row_count'] += 1
        self._last = position
        if self._chunk['records'] % 512 == 0:
            self.check_budget()

    def _close_chunk(self):
        if self._chunk is None:
            return
        self._gzip.close()
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        path = self.temporary / self._chunk['file']
        self._chunk['size_bytes'] = path.stat().st_size
        with path.open('rb') as handle:
            self._chunk['sha256'] = hashlib.file_digest(handle, 'sha256').hexdigest()
        self.stored_bytes += self._chunk['size_bytes']
        self.sheets[self._sheet]['chunks'].append(self._chunk)
        self._file = self._gzip = self._chunk = None
        self.check_budget()

    def flush(self):
        self.connection.commit()
        self.check_budget()

    def check_budget(self):
        active = self._file.tell() if self._file else 0
        lookup = self.connection.execute('PRAGMA page_count').fetchone()[0] * 4096 if not self._closed else 0
        if self.stored_bytes + active + lookup > self.max_bytes:
            raise ExcelStorageLimitError('Excel 数据存储超过磁盘预算')

    def finish(self):
        self._close_chunk()
        self.connection.close()
        self._closed = True
        self.lookup.unlink(missing_ok=True)
        manifest = {'schema_version': 2, 'format': 'excel-jsonl-gzip', 'sheets': [dict(sheet_id=key, **value) for key, value in self.sheets.items()]}
        encoded = json.dumps(manifest, ensure_ascii=False, separators=(',', ':')).encode()
        if len(encoded) > 32 * 1024 * 1024 or self.stored_bytes + len(encoded) > self.max_bytes:
            raise ExcelStorageLimitError('Excel 目录超过存储预算')
        with (self.temporary / 'manifest.json').open('wb') as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if self.root.exists():
            raise FileExistsError(self.root)
        os.replace(self.temporary, self.root)
        self._finished = True

    def __exit__(self, *args):
        if self._gzip:
            self._gzip.close()
        if self._file:
            self._file.close()
        if not self._closed:
            self.connection.close()
        if not self._finished:
            shutil.rmtree(self.temporary, ignore_errors=True)


def _safe_chunk(root: Path, relative: str) -> Path:
    from pathlib import PurePosixPath
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or any(part in ('..', '.') for part in parts):
        raise ValueError('无效的 Excel 数据块路径')
    candidate = root
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError('Excel 数据块不可为符号链接')
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError('Excel 数据块路径越界')
    return candidate


def load_workbook_manifest(path: Path) -> dict:
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('Excel 目录超过大小限制')
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 2 or manifest.get('format') != 'excel-jsonl-gzip':
        raise ValueError('不支持的 Excel 文件存储格式')
    return manifest


def iter_sheet_cells(path: Path, sheet_id: int, *, after_row: int = 0):
    manifest = load_workbook_manifest(path)
    sheet = next((item for item in manifest['sheets'] if item['sheet_id'] == sheet_id), None)
    if sheet is None:
        raise KeyError(sheet_id)
    last = (0, 0)
    for chunk in sheet['chunks']:
        if chunk['last_row'] <= after_row:
            continue
        target = _safe_chunk(path.parent, chunk['file'])
        if target.stat().st_size != chunk['size_bytes']:
            raise ValueError('Excel 数据块大小校验失败')
        with target.open('rb') as handle:
            if hashlib.file_digest(handle, 'sha256').hexdigest() != chunk['sha256']:
                raise ValueError('Excel 数据块校验失败')
        total = records = 0
        with gzip.open(target, 'rb') as handle:
            while True:
                line = handle.readline(16 * 1024 * 1024 + 1)
                if not line:
                    break
                total += len(line)
                records += 1
                if len(line) > 16 * 1024 * 1024 or total > chunk['uncompressed_bytes'] or records > chunk['records']:
                    raise ValueError('Excel 数据块解压预算超限')
                cell = json.loads(line)
                position = (cell['row'], cell['column'])
                if position <= last or not 1 <= position[0] <= 1048576 or not 1 <= position[1] <= 16384:
                    raise ValueError('Excel 数据块坐标顺序无效')
                last = position
                if position[0] > after_row:
                    yield cell
        if total != chunk['uncompressed_bytes'] or records != chunk['records']:
            raise ValueError('Excel 数据块记录数校验失败')


def read_sheet_page(path: Path, sheet_id: int, *, after_row: int = 0, limit: int = 100,
                    max_bytes: int = 1024 * 1024) -> dict[str, Any]:
    path = Path(path)
    if path.suffix != '.json':
        return _read_sqlite_page(path, sheet_id, after_row=after_row, limit=limit, max_bytes=max_bytes)
    if not 0 <= sheet_id <= 2**31-1 or not 0 <= after_row <= 1048576 or not 1 <= limit <= 200:
        raise ValueError('无效的 Excel 分页参数')
    manifest = load_workbook_manifest(path)
    sheet = next((item for item in manifest['sheets'] if item['sheet_id'] == sheet_id), None)
    if sheet is None:
        raise KeyError(sheet_id)
    rows, current, used, row_bytes, has_more = [], None, 0, 0, False
    for cell in iter_sheet_cells(path, sheet_id, after_row=after_row):
        if current is None or cell['row'] != current['row']:
            if current is not None:
                rows.append(current)
                used += row_bytes
            if len(rows) >= limit:
                has_more = True
                current = None
                break
            current, row_bytes = {'row': cell['row'], 'cells': []}, 0
        row_bytes += len(json.dumps(cell, ensure_ascii=False, separators=(',', ':')).encode()) + 2
        if row_bytes > max_bytes or used + row_bytes > max_bytes:
            if not rows:
                raise ExcelPageTooLarge('此行数据超过预览大小上限，请下载完整数据查看')
            current = None
            has_more = True
            break
        current['cells'].append(cell)
    if current is not None:
        rows.append(current)
    metadata = sheet['metadata']
    return {'sheet_id': sheet_id, 'sheet_name': metadata.get('name') or metadata.get('sheet_name') or str(sheet_id),
            'rows': rows, 'next_after_row': rows[-1]['row'] if has_more and rows else None,
            'semantic_cell_count': metadata.get('semantic_cell_count', 0), 'row_count': metadata.get('row_count', 0)}
