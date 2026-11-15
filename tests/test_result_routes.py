from __future__ import annotations

import gzip
import json
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.tasks.artifacts import ArtifactRepository
from app.tasks.result_routes import result_router
from app.tasks.result_store import publish_result


@pytest.fixture
def setup_routes(tmp_path: Path):
    tasks = {}
    artifacts = ArtifactRepository(tmp_path / "artifacts")

    async def get(task_id):
        return tasks.get(task_id)

    manager = SimpleNamespace(get=get)
    app = FastAPI()
    app.include_router(result_router(manager, artifacts))

    def add_task(task_id="task_" + "1" * 32, *, document=None, status="succeeded", disk=False, payload=None):
        workspace = artifacts.workspace(task_id)
        reference = None
        preview = document
        if disk:
            preview, reference = publish_result(workspace, document, payload=payload, preview_bytes=8192)
        tasks[task_id] = SimpleNamespace(status=status, result=SimpleNamespace(
            document=preview, result_ref=reference, conversion=payload))
        return task_id, workspace

    return app, tasks, add_task


@pytest.mark.asyncio
async def test_file_result_manifest_download_and_cursor_paging(setup_routes) -> None:
    app, _, add_task = setup_routes
    rows = [{"index": index, "text": "内容" * 100} for index in range(300)]
    document = {"document_type": "docx", "blocks": rows, "representations": {"plain_text": "完整正文" * 10000}}
    task_id, _ = add_task(document=document, disk=True)
    base = f"/api/v1/tasks/{task_id}/result"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(base)
        assert response.status_code == 200
        manifest = response.json()
        assert manifest["schema_version"] == 1
        assert manifest["preview_truncated"] is True
        canonical = await client.get(manifest["document_url"])
        assert canonical.json() == document
        assert "attachment" in canonical.headers["content-disposition"]
        unit = manifest["units"][0]
        first = (await client.get(base + f"/units/{unit['unit_id']}/records?limit=2")).json()
        assert first["records"] == rows[:2]
        assert first["next_offset"] == 2
        second = (await client.get(base + f"/units/{unit['unit_id']}/records?offset=2&limit=2")).json()
        assert second["records"] == rows[2:4]
        resource = next(item for item in manifest["resources"] if item["storage_key"].startswith("result/representations/"))
        assert (await client.get(resource["download_url"])).text == document["representations"]["plain_text"]
        assert (await client.get(base + "/files/result/manifest.json")).json()["schema_version"] == 1
        assert (await client.get(base + "/units/unit-999999/records")).status_code == 404
        assert (await client.get(base + "/units/not-a-unit/records")).status_code == 422
        assert (await client.get(base + "/units/unit-000000/records?limit=201")).status_code == 422
        assert (await client.get(base + "/units/unit-000000/records?offset=-1")).status_code == 422


@pytest.mark.asyncio
async def test_legacy_embedded_document_and_units_still_available(setup_routes) -> None:
    app, _, add_task = setup_routes
    document = {"document_type": "pdf", "pages": [{"page_number": 1}], "blocks": [{"text": "a"}, {"text": "b"}], "tables": []}
    task_id, _ = add_task(document=document)
    base = f"/api/v1/tasks/{task_id}/result"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        manifest = (await client.get(base)).json()
        assert manifest["legacy"] is True
        assert manifest["schema_version"] == 0
        assert [unit["kind"] for unit in manifest["units"]] == ["pages", "blocks"]
        assert all("records" not in unit for unit in manifest["units"])
        assert (await client.get(manifest["document_url"])).json() == document
        first = (await client.get(base + "/units/unit-000001/records?limit=1")).json()
        assert first == {"unit_id": "unit-000001", "records": [{"text": "a"}], "next_offset": 1}
        assert (await client.get(base + "/units/unit-000001/records?offset=1")).json()["next_offset"] is None
        assert (await client.get(base + "/files/result/document.json")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "planning", "running", "cancelling", "failed", "cancelled", "interrupted"])
async def test_results_unavailable_until_success_or_partial(setup_routes, status: str) -> None:
    app, _, add_task = setup_routes
    task_id, _ = add_task(document={"blocks": [{"text": "private"}]}, status=status, disk=True)
    base = f"/api/v1/tasks/{task_id}/result"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for suffix in ["", "/document", "/units/unit-000000/records", "/files/result/document.json"]:
            response = await client.get(base + suffix)
            assert response.status_code == 404
            assert response.json()["detail"]["code"] == "result_not_ready"
        assert (await client.get("/api/v1/tasks/missing/result")).status_code == 404


@pytest.mark.asyncio
async def test_download_allowlist_and_symlinks_are_enforced(setup_routes, tmp_path: Path) -> None:
    app, _, add_task = setup_routes
    task_id, workspace = add_task(document={"blocks": [{"text": "public"}]}, disk=True)
    (workspace / "private.json").write_text('{"secret":true}')
    base = f"/api/v1/tasks/{task_id}/result"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(base + "/files/private.json")).status_code == 404
        assert (await client.get(base + "/files/result/unlisted.json")).status_code == 404
        target = workspace / "result/document.json"
        target.unlink()
        target.symlink_to(workspace / "private.json")
        assert (await client.get(base + "/document")).status_code == 404
        assert (await client.get(base + "/files/result/document.json")).status_code == 404


@pytest.mark.asyncio
async def test_partial_results_media_and_oversize_records(setup_routes) -> None:
    app, _, add_task = setup_routes
    task_id, _ = add_task(document={"document_type": "pdf", "pages": [{"text": "x" * (1024 * 1024 + 100)}]}, disk=True, status="partial")
    media_id, _ = add_task("task_" + "2" * 32, document=None, disk=True, payload={"audio": {"duration": 10}})
    legacy_id, _ = add_task("task_" + "3" * 32, document={"blocks": [{"text": "x" * (1024 * 1024 + 100)}]})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(f"/api/v1/tasks/{task_id}/result")).status_code == 200
        oversized = await client.get(f"/api/v1/tasks/{task_id}/result/units/unit-000000/records")
        assert oversized.status_code == 413
        assert (await client.get(f"/api/v1/tasks/{legacy_id}/result/units/unit-000001/records")).status_code == 413
        canonical = (await client.get(f"/api/v1/tasks/{media_id}/result/document")).json()
        assert canonical["payload"] == {"audio": {"duration": 10}}


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_data", [b"invalid-gzip", gzip.compress(b"not-json\n")])
async def test_malformed_chunk_with_valid_digest_returns_422(setup_routes, chunk_data: bytes) -> None:
    import hashlib
    app, _, add_task = setup_routes
    task_id, workspace = add_task(document={"blocks": [{"text": "public"}]}, disk=True)
    manifest_path = workspace / "result/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    chunk = manifest["units"][0]["chunks"][0]
    (workspace / chunk["storage_key"]).write_bytes(chunk_data)
    chunk.update(size_bytes=len(chunk_data), sha256=hashlib.sha256(chunk_data).hexdigest())
    manifest_path.write_text(json.dumps(manifest))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/tasks/{task_id}/result/units/unit-000000/records")
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_result_page"

@pytest.mark.asyncio
async def test_tampered_chunk_returns_controlled_error(setup_routes) -> None:
    app, _, add_task = setup_routes
    task_id, workspace = add_task(document={"blocks": [{"text": "public"}]}, disk=True)
    manifest = json.loads((workspace / "result/manifest.json").read_text())
    chunk = workspace / manifest["units"][0]["chunks"][0]["storage_key"]
    data = bytearray(chunk.read_bytes())
    data[-1] ^= 1
    chunk.write_bytes(data)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/tasks/{task_id}/result/units/unit-000000/records")
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_result_page"
