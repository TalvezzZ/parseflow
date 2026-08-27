from __future__ import annotations

import csv
import io
import json
import re
import shutil
from pathlib import Path
from typing import Any


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

    def add_text(name: str, content: str) -> None:
        nonlocal used
        encoded = content.encode("utf-8")
        if used + len(encoded) > max_bytes:
            raise ExportLimitExceeded(f"combined exports exceed {max_bytes} bytes")
        path = staging / name
        path.write_bytes(encoded)
        outputs.append(path)
        used += len(encoded)

    try:
        representations = document.get("representations") if isinstance(document.get("representations"), dict) else {}
        plain_text = str(representations.get("plain_text") or "")
        markdown = str(representations.get("markdown") or plain_text)
        add_text("document.txt", plain_text)
        add_text("document.md", markdown)
        add_text("document.json", json.dumps(document, ensure_ascii=False, indent=2))
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
            add_text(f"table-{index:03d}.json", json.dumps(table, ensure_ascii=False, indent=2))
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
