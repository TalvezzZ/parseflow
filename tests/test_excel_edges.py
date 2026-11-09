from __future__ import annotations

import asyncio
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from app.documents.models import FileInput, ParseContext
from app.skills.excel.adapters.openpyxl import OpenpyxlProvider
from app.skills.excel.store import read_sheet_page

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def context(source: Path, output: Path) -> ParseContext:
    return ParseContext(file=FileInput(file_id="excel-edges", path=str(source)), metadata={"artifact_dir": str(output)})


def full_cells(output: Path) -> list[dict]:
    return [cell for row in read_sheet_page(output / "workbook.sqlite", 0)["rows"] for cell in row["cells"]]


def test_auxiliary_shared_strings_are_erased_before_download(tmp_path: Path) -> None:
    import xlsxwriter
    source = tmp_path / "hidden.xlsx"
    sentinel = "PRIVATE_HIDDEN_STRING_78459_SHOULD_NOT_SURVIVE"
    workbook = xlsxwriter.Workbook(source)
    workbook.add_worksheet("Visible").write(0, 0, "public")
    hidden = workbook.add_worksheet("Hidden")
    hidden.write(0, 0, sentinel)
    hidden.hide()
    workbook.close()
    output = tmp_path / "out"
    result = OpenpyxlProvider()._parse_sync(context(source, output))
    assert result.status == "success", result.error
    assert sentinel.encode() not in (output / "workbook.sqlite").read_bytes()
    assert [item["value"] for item in full_cells(output)] == ["public"]


def test_phonetic_runs_are_not_appended_to_cell_text(tmp_path: Path) -> None:
    from app.skills.excel.adapters.openpyxl import _string_text
    element = ET.fromstring(f'<si xmlns="{NS[1:-1]}"><t>中文</t><r><t>正文</t></r><rPh sb="0" eb="2"><t>zhongwen</t></rPh></si>')
    assert _string_text(element) == "中文正文"


@pytest.mark.asyncio
async def test_export_worker_stops_and_removes_partial_exports(tmp_path: Path, monkeypatch) -> None:
    from app.skills.excel import process as worker_process
    real_create = asyncio.create_subprocess_exec
    child = None
    output = tmp_path / "out"
    output.mkdir()
    (output / ".exports.tmp").mkdir()
    (output / ".exports.tmp" / "partial.csv").write_text("partial")
    marker = tmp_path / ".cancel"

    async def launch(*args, **kwargs):
        nonlocal child
        child = await real_create(args[0], "-c", "import time; time.sleep(60)", **kwargs)
        return child

    monkeypatch.setattr(worker_process.asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(worker_process.export_in_worker({}, output, max_bytes=1000, cancel_path=marker))
    for _ in range(100):
        if child is not None:
            break
        await asyncio.sleep(0.01)
    marker.touch()
    assert await asyncio.wait_for(task, timeout=3) == "cancelled"
    assert child.returncode is not None
    assert not (output / ".exports.tmp").exists()
    assert not list(tmp_path.glob(".excel-export-*"))
