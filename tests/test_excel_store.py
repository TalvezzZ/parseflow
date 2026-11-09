from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.skills.excel.store import ExcelPageTooLarge, ExcelStorageLimitError, ExcelStoreWriter, open_excel_store, read_sheet_page


def cell(row: int, column: int = 1, value: object = "value") -> dict:
    return {"row": row, "column": column, "coordinate": f"A{row}", "value": value, "formula": None}


def test_store_atomic_publication_and_keyset_pages(tmp_path: Path) -> None:
    path = tmp_path / "workbook.sqlite"
    with ExcelStoreWriter(path) as writer:
        writer.add_sheet(0, {"name": "数据", "semantic_cell_count": 4, "row_count": 3})
        for item in [cell(2), cell(2, 3), cell(100), cell(9999)]:
            writer.add_cell(0, item)
        assert not path.exists()
        writer.finish()
    page = read_sheet_page(path, 0, limit=2)
    assert [item["row"] for item in page["rows"]] == [2, 100]
    assert [item["column"] for item in page["rows"][0]["cells"]] == [1, 3]
    assert page["next_after_row"] == 100
    last = read_sheet_page(path, 0, after_row=100, limit=2)
    assert [item["row"] for item in last["rows"]] == [9999]
    assert last["next_after_row"] is None
    assert last["sheet_name"] == "数据"
    with pytest.raises(KeyError):
        read_sheet_page(path, 1)


def test_aborted_store_is_not_published(tmp_path: Path) -> None:
    path = tmp_path / "workbook.sqlite"
    with pytest.raises(RuntimeError):
        with ExcelStoreWriter(path) as writer:
            writer.add_sheet(0, {"name": "Data"})
            writer.add_cell(0, cell(1))
            writer.flush()
            raise RuntimeError("interrupted")
    assert list(tmp_path.iterdir()) == []


def test_pages_bound_bytes_without_silently_dropping_rows(tmp_path: Path) -> None:
    path = tmp_path / "workbook.sqlite"
    with ExcelStoreWriter(path) as writer:
        writer.add_sheet(0, {"name": "Data"})
        writer.add_cell(0, cell(1, value="small"))
        writer.add_cell(0, cell(2, value="x" * 1000))
        writer.finish()
    page = read_sheet_page(path, 0, limit=100, max_bytes=300)
    assert [row["row"] for row in page["rows"]] == [1]
    assert page["next_after_row"] == 1
    with pytest.raises(ExcelPageTooLarge):
        read_sheet_page(path, 0, after_row=1, max_bytes=300)


def test_sqlite_budget_is_enforced(tmp_path: Path) -> None:
    path = tmp_path / "workbook.sqlite"
    with pytest.raises(ExcelStorageLimitError):
        with ExcelStoreWriter(path, max_bytes=32768) as writer:
            writer.add_sheet(0, {"name": "Data"})
            for row in range(100):
                writer.add_cell(0, cell(row + 1, value="x" * 1000))
            writer.finish()
    assert not path.exists()


@pytest.mark.asyncio
async def test_excel_page_api_is_path_free_and_task_scoped(tmp_path: Path, monkeypatch) -> None:
    import httpx
    from app import main
    from app.tasks.artifacts import ArtifactRepository
    from app.tasks.models import TaskRecord, TaskResultEnvelope

    task_id = "task_" + "9" * 32
    task = TaskRecord(task_id=task_id, file_id="file_" + "8" * 32, status="succeeded",
                      result=TaskResultEnvelope(status="succeeded", file_id="file_" + "8" * 32,
                          document={"metadata": {"excel_storage": {"filename": "workbook.sqlite", "schema_version": 1}}}))
    artifacts = ArtifactRepository(tmp_path / "artifacts")
    path = artifacts.workspace(task_id) / "workbook.sqlite"
    with ExcelStoreWriter(path) as writer:
        writer.add_sheet(0, {"name": "数据"})
        writer.add_cell(0, cell(5, 3, "hello"))
        writer.finish()

    async def get_task(identity: str):
        return task if identity == task_id else None

    monkeypatch.setattr(main.task_manager, "get", get_task)
    monkeypatch.setattr(main, "artifact_repository", artifacts)
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"/api/v1/tasks/{task_id}/sheets/0/rows")
        assert response.status_code == 200, response.text
        assert response.json()["rows"][0]["cells"][0]["column"] == 3
        assert str(tmp_path) not in response.text
        assert (await client.get(f"/api/v1/tasks/{task_id}/sheets/99/rows")).status_code == 404
        assert (await client.get(f"/api/v1/tasks/{task_id}/sheets/0/rows?limit=201")).status_code == 422
        assert (await client.get(f"/api/v1/tasks/{task_id}/sheets/{2**100}/rows")).status_code == 422
        assert (await client.get(f"/api/v1/tasks/{task_id}/sheets/0/rows?after_row={2**100}")).status_code == 422
        assert (await client.get("/api/v1/tasks/task_missing/sheets/0/rows")).status_code == 404
        task.status = "running"
        assert (await client.get(f"/api/v1/tasks/{task_id}/sheets/0/rows")).status_code == 404
