from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from app.tasks.exports import ExportLimitExceeded, generate_document_exports


def _make_store(path: Path, cells: list[tuple[int, int, int, object]]) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE sheets (sheet_id INTEGER PRIMARY KEY, metadata TEXT NOT NULL);"
            "CREATE TABLE cells (sheet_id INTEGER, row INTEGER, column INTEGER, payload TEXT, "
            "PRIMARY KEY(sheet_id,row,column)) WITHOUT ROWID;"
        )
        for sheet_id in sorted({sheet_id for sheet_id, _, _, _ in cells} | {0, 1}):
            connection.execute(
                "INSERT INTO sheets(sheet_id,metadata) VALUES (?,?)",
                (sheet_id, json.dumps({"name": f"Sheet {sheet_id}"})),
            )
        connection.executemany(
            "INSERT INTO cells(sheet_id,row,column,payload) VALUES (?,?,?,?)",
            [(sheet_id, row, column, json.dumps(value if isinstance(value, dict) else {"value": value}))
             for sheet_id, row, column, value in cells],
        )


def _document() -> dict:
    return {
        "document_type": "xlsx",
        "metadata": {"excel_storage": {"filename": "workbook.sqlite"}},
        "representations": {"plain_text": "A preview", "markdown": "| A preview |"},
        "tables": [{"name": "Only preview", "rows": [["A preview"]]}],
    }


def test_disk_workbook_csv_preserves_real_coordinates_and_full_data(tmp_path: Path) -> None:
    _make_store(tmp_path / "workbook.sqlite", [
        (0, 1, 1, "first"), (0, 1, 3, {"value": None, "formula": "=1+2"}),
        (0, 3, 2, "last row, middle column"), (1, 2, 1, "after leading gap"),
    ])
    paths = generate_document_exports(_document(), tmp_path, max_bytes=8192)
    assert {path.name for path in paths} == {
        "document.txt", "document.md", "document.json", "sheet-000.csv", "sheet-001.csv"
    }
    with (tmp_path / "exports/sheet-000.csv").open(encoding="utf-8-sig", newline="") as handle:
        assert list(csv.reader(handle)) == [
            ["first", "", "'=1+2"], ["", "", ""], ["", "last row, middle column", ""]
        ]
    with (tmp_path / "exports/sheet-001.csv").open(encoding="utf-8-sig", newline="") as handle:
        assert list(csv.reader(handle)) == [[""], ["after leading gap"]]
    summary = json.loads((tmp_path / "exports/document.json").read_text())
    assert "tables" not in summary
    assert summary["excel_export"]["csv"] == "complete per-sheet data"
    assert "preview" in (tmp_path / "exports/document.txt").read_text().lower()
    assert "preview" in (tmp_path / "exports/document.md").read_text().lower()


def test_disk_workbook_csv_sanitizes_formula_injection(tmp_path: Path) -> None:
    _make_store(tmp_path / "workbook.sqlite", [
        (0, 1, 1, "=1+2"), (0, 1, 2, "  +evil"), (0, 1, 3, "-evil"),
        (0, 2, 1, "@evil"), (0, 2, 2, "ordinary"),
    ])
    generate_document_exports(_document(), tmp_path, max_bytes=8192)
    with (tmp_path / "exports/sheet-000.csv").open(encoding="utf-8-sig", newline="") as handle:
        assert list(csv.reader(handle)) == [
            ["'=1+2", "'  +evil", "'-evil"], ["'@evil", "ordinary", ""]
        ]


def test_disk_workbook_csv_budget_fails_fast_on_huge_sparse_gap(tmp_path: Path) -> None:
    _make_store(tmp_path / "workbook.sqlite", [(0, 1, 1, "first"), (0, 3_000_000, 1, "last")])
    with pytest.raises(ExportLimitExceeded):
        generate_document_exports(_document(), tmp_path, max_bytes=1024)
    assert not (tmp_path / "exports").exists()
    assert not (tmp_path / ".exports.tmp").exists()
    assert (tmp_path / "workbook.sqlite").is_file()


def test_sqlite_csv_stops_on_first_large_field_without_buffering_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tasks import exports

    _make_store(tmp_path / "workbook.sqlite", [(0, 1, column, "shared") for column in range(1, 1001)])
    shared_value = "x" * 1_000_000
    calls = 0

    def shared_string(payload: str) -> str:
        nonlocal calls
        calls += 1
        assert calls == 1, "budget must stop export before collecting the rest of this row"
        return shared_value

    monkeypatch.setattr(exports, "_sqlite_cell_value", shared_string)
    with pytest.raises(ExportLimitExceeded):
        exports._sqlite_csv_export(tmp_path / "workbook.sqlite", 0, tmp_path / "output.csv", 2048)
    assert calls == 1
    assert (tmp_path / "output.csv").stat().st_size <= 2048


def test_sqlite_csv_streams_quoted_fields_across_chunk_boundary(tmp_path: Path) -> None:
    value = 'x' * 8191 + '\"quoted,\r\n' + 'tail'
    _make_store(tmp_path / "workbook.sqlite", [(0, 1, 2, value)])
    generate_document_exports(_document(), tmp_path, max_bytes=20_000)
    with (tmp_path / "exports/sheet-000.csv").open(encoding="utf-8-sig", newline="") as handle:
        assert list(csv.reader(handle)) == [["", value]]


def test_generic_json_streaming_respects_budget(tmp_path: Path) -> None:
    with pytest.raises(ExportLimitExceeded):
        generate_document_exports({"large": "x" * 100_000}, tmp_path, max_bytes=1024)
    assert not (tmp_path / "exports").exists()
