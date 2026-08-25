from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from openpyxl import Workbook

from app.main import app


async def request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


async def wait_for_task(task_id: str) -> dict:
    for _ in range(500):
        response = await request("GET", f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200, response.text
        task = response.json()
        if task["status"] in {"succeeded", "partial", "failed", "cancelled"}:
            return task
        await asyncio.sleep(0.02)
    raise AssertionError("自动规划任务未在测试时间内完成")


@pytest.mark.asyncio
async def test_single_upload_creates_plan_and_executes_xlsx(tmp_path: Path) -> None:
    source = tmp_path / "auto.xlsx"
    workbook = Workbook()
    workbook.active.append(["name", "value"])
    workbook.active.append(["alpha", 1])
    workbook.save(source)

    response = await request("POST", "/api/v1/parse", files={"file": (source.name, source.read_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})

    assert response.status_code == 202, response.text
    submitted = response.json()
    task = await wait_for_task(submitted["task_id"])
    assert task["status"] == "succeeded"
    assert task["plan"]["planner"]["name"] == "rule-based"
    assert task["plan"]["steps"][0]["skill_name"] == "excel.parse"
    assert task["plan"]["steps"][0]["status"] == "succeeded"
    assert task["result"]["file_id"].startswith("file_")
    assert task["result"]["result"]["data"]["document"]["document_type"] == "xlsx"


@pytest.mark.asyncio
async def test_single_upload_auto_routes_complex_rtf(tmp_path: Path) -> None:
    source = tmp_path / "auto.rtf"
    source.write_text(r"{\rtf1\ansi\trowd\cellx2000\cellx4000 Name\cell Value\cell\row}", encoding="utf-8")

    response = await request("POST", "/api/v1/parse", data={"goal": "提取文档内容", "data_id": "auto-rtf"}, files={"file": (source.name, source.read_bytes(), "application/rtf")})

    assert response.status_code == 202, response.text
    task = await wait_for_task(response.json()["task_id"])
    assert task["status"] == "succeeded"
    assert task["data_id"] == "auto-rtf"
    assert task["plan"]["steps"][0]["skill_name"] == "rtf.parse"
    result = task["result"]["result"]
    assert result["data"]["rtf_routing"]["selected_path"] == "convert_to_docx"
