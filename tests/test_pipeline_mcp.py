from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.agent.pipeline import OfficeParsePipeline
from app.documents.models import FileInput, ParseContext
from app.main import pipeline


def make_xlsx(path: Path) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["Name", "Value"])
    sheet.append(["alpha", 10])
    workbook.save(path)


@pytest.mark.asyncio
async def test_pipeline_parses_direct_xlsx(tmp_path: Path) -> None:
    source = tmp_path / "data.xlsx"
    make_xlsx(source)
    result = await pipeline.execute(ParseContext(file=FileInput(file_id="pipeline-xlsx", path=str(source))))

    assert result.status == "success"
    assert [step.name for step in result.steps] == ["excel.parse"]
    assert result.result["document"]["document_type"] == "xlsx"


@pytest.mark.asyncio
async def test_pipeline_converts_legacy_xls_then_parses(tmp_path: Path) -> None:
    soffice = shutil.which("soffice")
    if soffice is None:
        pytest.skip("当前环境未提供 LibreOffice")
    seed = tmp_path / "seed.xlsx"
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    make_xlsx(seed)
    created = subprocess.run([soffice, "--headless", "--convert-to", "xls", "--outdir", str(legacy_dir), str(seed)], capture_output=True, text=True, timeout=60, check=False)
    source = legacy_dir / "seed.xls"
    assert created.returncode == 0, created.stderr or created.stdout
    result = await pipeline.execute(ParseContext(file=FileInput(file_id="pipeline-xls", path=str(source))))

    assert result.status == "success"
    assert [step.name for step in result.steps] == ["office.convert", "excel.parse"]
    assert result.steps[0].output_path
    assert result.result["conversion"]["target_format"] == "xlsx"


def test_remote_mcp_exposes_only_opaque_id_tools() -> None:
    from app.mcp_server import mcp

    tools = mcp._tool_manager.list_tools()
    names = {tool.name for tool in tools}
    assert names == {"list_skills", "preview_parse_plan", "submit_file_id", "get_task", "cancel_task", "list_artifacts"}
    for tool in tools:
        schema = str(tool.parameters)
        assert "path" not in schema
        assert "output_dir" not in schema
        assert "callback" not in schema
