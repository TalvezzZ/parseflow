from __future__ import annotations

import asyncio
import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from openpyxl import Workbook
from openpyxl.comments import Comment

from app.documents.models import FileInput, ParseContext
from app.skills.excel.adapters.openpyxl import OpenpyxlProvider
from app.skills.excel.store import read_sheet_page


NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def context(source: Path, output: Path) -> ParseContext:
    return ParseContext(file=FileInput(file_id="excel-test", path=str(source)), metadata={"artifact_dir": str(output)})


def replace_zip(source: Path, changes: dict[str, bytes]) -> None:
    target = source.with_suffix(".rewrite.xlsx")
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as rewritten:
        names = set(original.namelist())
        for name in original.namelist():
            rewritten.writestr(name, changes.get(name, original.read(name)))
        for name, value in changes.items():
            if name not in names:
                rewritten.writestr(name, value)
    target.replace(source)


def full_cells(output: Path, sheet_id: int = 0) -> list[dict]:
    with closing(sqlite3.connect(output / "workbook.sqlite")) as connection:
        return [json.loads(row[0]) for row in connection.execute(
            "SELECT payload FROM cells WHERE sheet_id=? ORDER BY row,column", (sheet_id,))]


def test_streaming_values_dates_formulas_metadata_and_preview(tmp_path: Path) -> None:
    source = tmp_path / "input.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "明细"
    sheet.append(["名称", None, "金额"])
    sheet.append(["苹果", True, 12.5])
    sheet["A3"] = datetime(2026, 1, 2, 3, 4, 5)
    sheet["C3"] = "=SUM(C2:C2)"
    sheet["D4"].comment = Comment("只有批注", "作者")
    sheet["E5"].hyperlink = "https://example.com"
    sheet.merge_cells("A6:C6")
    sheet["A6"] = "合并标题"
    workbook.save(source)
    with zipfile.ZipFile(source) as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    for row_node in root.find(NS + "sheetData"):
        for node in list(row_node):
            if node.get("r") == "E5":
                row_node.remove(node)
    replace_zip(source, {"xl/worksheets/sheet1.xml": ET.tostring(root)})
    output = tmp_path / "out"
    result = OpenpyxlProvider(max_preview_cells=2)._parse_sync(context(source, output))
    assert result.status == "success", result.error
    document = result.document
    assert document is not None
    assert document.quality["truncated"] in (0, False)
    assert document.quality["preview_truncated"] in (1, True)
    cells = {item["coordinate"]: item for item in full_cells(output)}
    assert cells["C2"]["value"] == 12.5
    assert cells["B2"]["value"] is True
    assert cells["A3"]["value"] == "2026-01-02T03:04:05"
    assert cells["C3"]["formula"] == "=SUM(C2:C2)"
    assert cells["D4"]["comment"] == "只有批注"
    assert cells["E5"]["hyperlink"] == "https://example.com"
    assert "A6:C6" in document.tables[0]["merged_ranges"]
    assert len(document.tables[0]["cells"]) <= 2
    assert "cells" not in document.provenance["sheets"][0]
    assert "cells" not in document.extensions["workbook"]["sheets"][0]


def test_workbook_relationships_respect_reordered_sheets(tmp_path: Path) -> None:
    source = tmp_path / "reordered.xlsx"
    workbook = Workbook()
    workbook.active.title = "First"
    workbook.active["A1"] = "first-value"
    workbook.create_sheet("Second")["A1"] = "second-value"
    workbook.save(source)
    with zipfile.ZipFile(source) as archive:
        root = ET.fromstring(archive.read("xl/workbook.xml"))
    sheets = root.find(NS + "sheets")
    sheets[:] = list(reversed(list(sheets)))
    replace_zip(source, {"xl/workbook.xml": ET.tostring(root)})
    output = tmp_path / "out"
    result = OpenpyxlProvider()._parse_sync(context(source, output))
    assert result.status == "success", result.error
    table = result.document.tables[0]
    assert table["sheet_name"] == "Second"
    assert full_cells(output, table["sheet_id"])[0]["value"] == "second-value"


def test_sparse_and_style_pollution_does_not_expand_rectangles(tmp_path: Path) -> None:
    from openpyxl.styles import Font
    source = tmp_path / "sparse.xlsx"
    workbook = Workbook()
    workbook.active["A1"] = "start"
    workbook.active["XFD1048576"].font = Font(bold=True)
    workbook.active["Z90000"] = "far"
    workbook.save(source)
    output = tmp_path / "out"
    result = OpenpyxlProvider()._parse_sync(context(source, output))
    assert result.status == "success", result.error
    assert len(full_cells(output)) == 2
    assert read_sheet_page(output / "workbook.sqlite", 0, limit=1)["next_after_row"] == 1


def test_limits_and_hidden_sheets_are_applied_before_cell_loading(tmp_path: Path) -> None:
    source = tmp_path / "limits.xlsx"
    workbook = Workbook()
    workbook.active["A1"] = "visible"
    workbook.active["B2"] = "second"
    workbook.create_sheet("Hidden")["A1"] = "hidden"
    workbook["Hidden"].sheet_state = "hidden"
    workbook.create_sheet("VeryHidden")["A1"] = "very-hidden"
    workbook["VeryHidden"].sheet_state = "veryHidden"
    workbook.save(source)
    output = tmp_path / "out"
    result = OpenpyxlProvider(max_semantic_cells=1)._parse_sync(context(source, output))
    assert result.status == "partial", result.error
    assert len(full_cells(output)) == 1
    assert len(result.document.tables) == 1


def test_shared_strings_and_shared_formula_followers(tmp_path: Path) -> None:
    source = tmp_path / "shared.xlsx"
    workbook = Workbook()
    workbook.active["A1"] = "placeholder"
    workbook.save(source)
    sheet = f'''<worksheet xmlns="{NS[1:-1]}"><sheetData><row r="1">
      <c r="A1" t="s"><v>0</v></c><c r="B1"><f t="shared" ref="B1:B2" si="0">A1+1</f><v>2</v></c>
      </row><row r="2"><c r="B2"><f t="shared" si="0"/><v>3</v></c></row></sheetData></worksheet>'''
    strings = f'<sst xmlns="{NS[1:-1]}" count="1" uniqueCount="1"><si><t>shared-text</t></si></sst>'
    with zipfile.ZipFile(source) as archive:
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    ET.SubElement(relationships, "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship", {
        "Id": "rIdShared", "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings", "Target": "sharedStrings.xml"})
    replace_zip(source, {"xl/worksheets/sheet1.xml": sheet.encode(), "xl/sharedStrings.xml": strings.encode(),
                         "xl/_rels/workbook.xml.rels": ET.tostring(relationships)})
    output = tmp_path / "out"
    result = OpenpyxlProvider()._parse_sync(context(source, output))
    assert result.status == "success", result.error
    cells = {item["coordinate"]: item for item in full_cells(output)}
    assert cells["A1"]["value"] == "shared-text"
    assert cells["B1"]["formula"] == "=A1+1"
    assert cells["B2"]["formula"] == "=A2+1"
    assert cells["B2"]["cached_value"] == 3


@pytest.mark.asyncio
async def test_worker_publishes_only_complete_outputs(tmp_path: Path) -> None:
    source = tmp_path / "input.xlsx"
    workbook = Workbook()
    workbook.active["A1"] = "ok"
    workbook.save(source)
    output = tmp_path / "out"
    result = await OpenpyxlProvider().parse(context(source, output))
    assert result.status == "success", result.error
    assert (output / "workbook.sqlite").is_file()
    assert "worker_peak_rss_bytes" in result.metrics
    assert not list(tmp_path.glob(".excel-worker-*"))
    assert not (output / "request.json").exists()


@pytest.mark.asyncio
async def test_worker_is_killed_on_coroutine_cancellation(tmp_path: Path, monkeypatch) -> None:
    from app.skills.excel import process as worker_process
    real_create = asyncio.create_subprocess_exec
    child = None

    async def launch(*args, **kwargs):
        nonlocal child
        child = await real_create(args[0], "-c", "import time; time.sleep(60)", **kwargs)
        return child

    monkeypatch.setattr(worker_process.asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(worker_process.parse_in_worker(context(tmp_path / "unused.xlsx", tmp_path / "out"), {}))
    for _ in range(100):
        if child is not None:
            break
        await asyncio.sleep(0.01)
    assert child is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.returncode is not None
    assert not list(tmp_path.glob(".excel-worker-*"))


@pytest.mark.asyncio
async def test_worker_honors_cancel_marker(tmp_path: Path, monkeypatch) -> None:
    from app.skills.excel import process as worker_process
    real_create = asyncio.create_subprocess_exec
    child = None

    async def launch(*args, **kwargs):
        nonlocal child
        child = await real_create(args[0], "-c", "import time; time.sleep(60)", **kwargs)
        return child

    monkeypatch.setattr(worker_process.asyncio, "create_subprocess_exec", launch)
    ctx = context(tmp_path / "unused.xlsx", tmp_path / "out")
    marker = tmp_path / ".cancel"
    ctx.metadata["excel_cancel_path"] = str(marker)
    task = asyncio.create_task(worker_process.parse_in_worker(ctx, {}))
    for _ in range(100):
        if child is not None:
            break
        await asyncio.sleep(0.01)
    assert child is not None
    marker.touch()
    result = await asyncio.wait_for(task, timeout=3)
    assert result.status == "failed" and result.error.code == "task_cancelled"
    assert child.returncode is not None
    assert not (tmp_path / "out" / "workbook.sqlite").exists()
