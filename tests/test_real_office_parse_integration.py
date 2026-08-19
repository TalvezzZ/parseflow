"""真实 Excel/PPT 解析 API 集成测试。"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import httpx
import pytest

from app.main import app


async def post(path: str, payload: dict) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(path, json=payload)


def make_xlsx(path: Path) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sales"
    sheet.append(["Product", "Amount", "Total"])
    sheet.append(["A", 2, "=B2*10"])
    sheet.append(["B", 3, "=B3*10"])
    sheet.merge_cells("A5:C5")
    sheet["A5"] = "Merged note"
    workbook.create_sheet("Hidden").sheet_state = "hidden"
    workbook.save(path)


def contaminate_declared_dimension(path: Path) -> None:
    """模拟仅由样式污染造成的超大 Excel 声明范围。"""
    temporary = path.with_suffix(".rewritten.xlsx")
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            payload = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                payload = payload.replace(b'<dimension ref="A1:C5"/>', b'<dimension ref="A1:XFD1048576"/>')
            target.writestr(item, payload)
    temporary.replace(path)


def make_pptx(path: Path) -> None:
    from pptx import Presentation

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "Quarterly Results"
    textbox = slide.shapes.add_textbox(1000000, 1500000, 5000000, 800000)
    textbox.text = "Revenue increased"
    table_shape = slide.shapes.add_table(2, 2, 1000000, 2500000, 5000000, 1500000)
    table_shape.table.cell(0, 0).text = "Metric"
    table_shape.table.cell(0, 1).text = "Value"
    table_shape.table.cell(1, 0).text = "Revenue"
    table_shape.table.cell(1, 1).text = "100"
    presentation.save(path)


@pytest.mark.asyncio
async def test_real_xlsx_parse_api(tmp_path: Path) -> None:
    source = tmp_path / "report.xlsx"
    make_xlsx(source)

    response = await post("/api/v1/parse/excel", {"file_id": "xlsx-real", "path": str(source)})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "success"
    document = payload["data"]["document"]
    assert document["document_type"] == "xlsx"
    assert document["metadata"]["sheet_count"] == 1
    assert "=B2*10" in document["representations"]["plain_text"]
    assert document["tables"][0]["sheet_name"] == "Sales"
    assert "A5:C5" in document["tables"][0]["merged_ranges"]


@pytest.mark.asyncio
async def test_xlsx_used_range_pollution_is_sparse_and_warned(tmp_path: Path) -> None:
    source = tmp_path / "polluted.xlsx"
    make_xlsx(source)
    contaminate_declared_dimension(source)

    response = await post("/api/v1/parse/excel", {"file_id": "xlsx-polluted", "path": str(source)})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "success"
    document = payload["data"]["document"]
    sheet = document["extensions"]["workbook"]["sheets"][0]
    assert sheet["declared_dimension"] == "A1:XFD1048576"
    assert sheet["semantic_dimension"] == "A1:C5"
    assert len(sheet["cells"]) == 10
    assert any(warning.startswith("excel_used_range_polluted:") for warning in document["warnings"])


@pytest.mark.asyncio
async def test_real_pptx_parse_api(tmp_path: Path) -> None:
    source = tmp_path / "presentation.pptx"
    make_pptx(source)

    response = await post("/api/v1/parse/ppt", {"file_id": "pptx-real", "path": str(source)})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "success"
    document = payload["data"]["document"]
    assert document["document_type"] == "pptx"
    assert len(document["pages"]) == 1
    assert "Quarterly Results" in document["representations"]["plain_text"]
    assert document["tables"][0]["rows"][1] == ["Revenue", "100"]


@pytest.mark.asyncio
async def test_real_legacy_xls_to_xlsx_parse_api(tmp_path: Path) -> None:
    soffice = shutil.which("soffice")
    if soffice is None:
        pytest.skip("当前环境未提供 LibreOffice")
    source_xlsx = tmp_path / "seed.xlsx"
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    make_xlsx(source_xlsx)
    created = subprocess.run([soffice, "--headless", "--convert-to", "xls", "--outdir", str(legacy_dir), str(source_xlsx)], capture_output=True, text=True, timeout=60, check=False)
    source_xls = legacy_dir / "seed.xls"
    assert created.returncode == 0, created.stderr or created.stdout
    assert source_xls.is_file(), created.stderr or created.stdout

    response = await post("/api/v1/parse/excel", {"file_id": "xls-real", "path": str(source_xls)})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["conversion"]["target_format"] == "xlsx"
    assert payload["data"]["document"]["document_type"] == "xlsx"
