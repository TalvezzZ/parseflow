from __future__ import annotations

import csv
import io
import json
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Iterator


class ExportLimitExceeded(ValueError):
    pass


def generate_document_exports(document: dict[str, Any], workspace: Path, *, max_bytes: int) -> list[Path]:
    """Generate a bounded export set and publish it only after every file succeeds."""
    staging = workspace / ".exports.tmp"
    export_root = workspace / "exports"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    outputs: list[Path] = []
    used = 0

    def add_chunks(name: str, chunks: Iterable[str]) -> None:
        nonlocal used
        path = staging / name
        with path.open("wb") as handle:
            for chunk in chunks:
                # JSONEncoder may yield one huge string token. Bound each encoded piece.
                for offset in range(0, len(chunk), 8192):
                    encoded = chunk[offset:offset + 8192].encode("utf-8")
                    if used + len(encoded) > max_bytes:
                        raise ExportLimitExceeded(f"combined exports exceed {max_bytes} bytes")
                    handle.write(encoded)
                    used += len(encoded)
        outputs.append(path)

    def add_text(name: str, content: str) -> None:
        add_chunks(name, (content,))

    def add_json(name: str, content: Any) -> None:
        add_chunks(name, json.JSONEncoder(ensure_ascii=False, indent=2).iterencode(content))

    try:
        representations = document.get("representations") if isinstance(document.get("representations"), dict) else {}
        plain_text = str(representations.get("plain_text") or "")
        markdown = str(representations.get("markdown") or plain_text)
        excel_storage = document.get("metadata", {}).get("excel_storage") if isinstance(document.get("metadata"), dict) else None
        disk_workbook = (workspace / "workbook.sqlite") if isinstance(excel_storage, dict) and excel_storage.get("filename") == "workbook.sqlite" else None
        if disk_workbook is not None and disk_workbook.is_file():
            add_text("document.txt", "Workbook preview (not the full workbook)\n" + plain_text if plain_text else "")
            add_text("document.md", "# Workbook preview (not the full workbook)\n\n" + markdown if markdown else "")
            # The SQLite database, not the in-memory table previews, is the primary artifact.
            summary = {key: value for key, value in document.items() if key not in {"tables", "representations"}}
            summary["excel_export"] = {"source": "workbook.sqlite", "csv": "complete per-sheet data",
                                       "document.txt": "preview", "document.md": "preview"}
            add_json("document.json", summary)
            for sheet_id in _sqlite_sheet_ids(disk_workbook):
                csv_path = staging / f"sheet-{sheet_id:03d}.csv"
                used += _sqlite_csv_export(disk_workbook, sheet_id, csv_path, max_bytes - used)
                outputs.append(csv_path)
        else:
            add_text("document.txt", plain_text)
            add_text("document.md", markdown)
            add_json("document.json", document)
            tables = [table for table in (document.get("tables") or []) if isinstance(table, dict)]
            if len(tables) > 100:
                raise ExportLimitExceeded("table export count exceeds 100")
            total_cells = sum(sum(len(row) for row in _rows(table)) for table in tables)
            if total_cells > max(10_000, max_bytes // 8):
                raise ExportLimitExceeded("table export cell budget exceeded")
            if tables:
                xlsx = _xlsx_export(tables, staging / "tables.xlsx", max_bytes - used)
                outputs.append(xlsx)
                used += xlsx.stat().st_size
            for index, table in enumerate(tables, start=1):
                rows = _rows(table)
                add_json(f"table-{index:03d}.json", table)
                add_text(f"table-{index:03d}.md", _markdown_table(rows))
                csv_path = staging / f"table-{index:03d}.csv"
                used += _csv_export(rows, csv_path, max_bytes - used)
                outputs.append(csv_path)
        shutil.rmtree(export_root, ignore_errors=True)
        staging.replace(export_root)
        return [export_root / path.name for path in outputs]
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _sqlite_sheet_ids(path: Path) -> Iterator[int]:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        for (sheet_id,) in connection.execute("SELECT sheet_id FROM sheets ORDER BY sheet_id"):
            yield sheet_id


def _sqlite_cell_value(payload: str) -> Any:
    cell = json.loads(payload)
    if not isinstance(cell, dict):
        return cell
    value = cell.get("value")
    return value if value is not None else cell.get("formula")


def _sqlite_csv_export(path: Path, sheet_id: int, output: Path, remaining_bytes: int) -> int:
    """Stream the complete sheet and sparse coordinate gaps without buffering rows."""
    if remaining_bytes < 3:
        raise ExportLimitExceeded("combined exports exceed configured bytes")
    written = 3
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection, output.open("wb") as handle:
        handle.write(b"\xef\xbb\xbf")
        first_row, first_column, last_column = connection.execute(
            "SELECT MIN(row), MIN(column), MAX(column) FROM cells WHERE sheet_id = ?", (sheet_id,)
        ).fetchone()
        if first_row is None:
            return written
        # Cell coordinates use Excel's one-based row and column numbering.
        if first_row < 1 or first_column < 1:
            raise ValueError("spreadsheet coordinates must be one-based")
        width = last_column
        if width > 16384:
            raise ExportLimitExceeded("sheet exceeds Excel's 16384-column width")
        empty_row = ("\"\"\r\n" if width == 1 else "," * (width - 1) + "\r\n").encode("utf-8")
        if written + len(empty_row) > remaining_bytes:
            raise ExportLimitExceeded("combined exports exceed configured bytes")
        next_row = 1

        def emit_gap(count: int) -> None:
            nonlocal written
            if count < 0:
                raise ValueError("spreadsheet rows must be ordered and nonnegative")
            if written + count * len(empty_row) > remaining_bytes:
                raise ExportLimitExceeded("combined exports exceed configured bytes")
            # Large sparse row gaps require bounded writes, not a per-row loop.
            batch = empty_row * max(1, 65536 // len(empty_row))
            while count:
                rows = min(count, len(batch) // len(empty_row))
                handle.write(batch[:rows * len(empty_row)])
                written += rows * len(empty_row)
                count -= rows

        def emit_bytes(encoded: bytes) -> None:
            nonlocal written
            if written + len(encoded) > remaining_bytes:
                raise ExportLimitExceeded("combined exports exceed configured bytes")
            handle.write(encoded)
            written += len(encoded)

        def emit_field(value: Any) -> None:
            cell = _safe_tabular_value(value)
            text = "" if cell is None else str(cell)
            quoted = (not text and width == 1) or any(character in text for character in ',"\r\n')
            if quoted:
                emit_bytes(b'"')
            for offset in range(0, len(text), 8192):
                chunk = text[offset:offset + 8192]
                if quoted:
                    chunk = chunk.replace('"', '""')
                emit_bytes(chunk.encode("utf-8"))
            if quoted:
                emit_bytes(b'"')

        current_row: int | None = None
        previous_column = 0
        for row_index, column_index, payload in connection.execute(
            "SELECT row, column, payload FROM cells WHERE sheet_id = ? ORDER BY row, column", (sheet_id,)
        ):
            if row_index != current_row:
                if current_row is not None:
                    emit_bytes(b"," * (width - previous_column) + b"\r\n")
                    next_row = current_row + 1
                emit_gap(row_index - next_row)
                current_row = row_index
                previous_column = 0
            if column_index < 1 or column_index > width or column_index <= previous_column:
                raise ValueError("invalid spreadsheet column")
            separators = column_index - previous_column if previous_column else column_index - 1
            emit_bytes(b"," * separators)
            emit_field(_sqlite_cell_value(payload))
            previous_column = column_index
        if current_row is not None:
            emit_bytes(b"," * (width - previous_column) + b"\r\n")
    return written


def _rows(table: dict[str, Any]) -> list[list[Any]]:
    rows = table.get("rows")
    if not isinstance(rows, list):
        return []
    return [row if isinstance(row, list) else [row] for row in rows]


def _markdown_table(rows: list[list[Any]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = [[str(cell if cell is not None else "").replace("|", "\\|").replace("\n", " ") for cell in row] + [""] * (width - len(row)) for row in rows]
    return "\n".join(["| " + " | ".join(normalized[0]) + " |", "| " + " | ".join(["---"] * width) + " |",
                      *("| " + " | ".join(row) + " |" for row in normalized[1:])]) + "\n"


def _safe_tabular_value(value: Any) -> str | int | float | bool | None:
    scalar = value if isinstance(value, (str, int, float, bool)) or value is None else str(value)
    if isinstance(scalar, str) and scalar.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + scalar
    return scalar


def _safe_sheet_name(value: Any, index: int, used: set[str]) -> str:
    base = re.sub(r"[\[\]:*?/\\]", "_", str(value or f"Table {index}")).strip("'")[:31] or f"Table {index}"
    name, suffix = base, 2
    while name.casefold() in used:
        marker = f"-{suffix}"
        name, suffix = base[:31 - len(marker)] + marker, suffix + 1
    used.add(name.casefold())
    return name


def _xlsx_export(tables: list[dict[str, Any]], path: Path, remaining_bytes: int) -> Path:
    import xlsxwriter
    workbook = xlsxwriter.Workbook(path, {"constant_memory": True, "strings_to_formulas": False, "strings_to_urls": False})
    try:
        used: set[str] = set()
        for index, table in enumerate(tables, start=1):
            sheet = workbook.add_worksheet(_safe_sheet_name(table.get("sheet_name") or table.get("name"), index, used))
            for row_index, row in enumerate(_rows(table)):
                for column_index, value in enumerate(row):
                    sheet.write(row_index, column_index, _safe_tabular_value(value))
    finally:
        workbook.close()
    if path.stat().st_size > remaining_bytes:
        raise ExportLimitExceeded("combined exports exceed configured bytes")
    return path


def _csv_export(rows: list[list[Any]], path: Path, remaining_bytes: int) -> int:
    if remaining_bytes < 3:
        raise ExportLimitExceeded("combined exports exceed configured bytes")
    written = 0
    with path.open("wb") as handle:
        handle.write(b"\xef\xbb\xbf")
        written = 3
        for row in rows:
            buffer = io.StringIO(newline="")
            csv.writer(buffer).writerow([_safe_tabular_value(cell) for cell in row])
            encoded = buffer.getvalue().encode("utf-8")
            if written + len(encoded) > remaining_bytes:
                raise ExportLimitExceeded("combined exports exceed configured bytes")
            handle.write(encoded)
            written += len(encoded)
    return written
