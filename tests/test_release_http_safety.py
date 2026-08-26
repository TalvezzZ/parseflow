"""HTTP release gates for v0.8.0 opaque storage and task APIs."""
from __future__ import annotations
import asyncio
import json
import httpx
import pytest
from app.main import app


async def request(method: str, path: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://testserver") as client:
        return await client.request(method,path,**kwargs)


async def task_from_upload(filename: str,payload: bytes,content_type: str) -> dict:
    created=await request("POST","/api/v1/tasks/parse",files={"file":(filename,payload,content_type)})
    assert created.status_code == 202, created.text
    for _ in range(100):
        task=(await request("GET",f"/api/v1/tasks/{created.json()['task_id']}")).json()
        if task["status"] in {"succeeded","partial","failed","cancelled","interrupted"}: return task
        await asyncio.sleep(.02)
    raise AssertionError("task did not finish")


@pytest.mark.asyncio
async def test_file_metadata_is_public_and_path_free() -> None:
    payload=b"a,b\n1,2\n"; upload=await request("POST","/api/v1/files",files={"file":("../../report.csv",payload,"text/csv")})
    assert upload.status_code == 201
    metadata=upload.json(); assert metadata["filename"] == "report.csv"
    assert "path" not in metadata and "relative_path" not in metadata and "storage_name" not in metadata
    download=await request("GET",metadata["download_url"]); assert download.content == payload


@pytest.mark.asyncio
async def test_image_upload_returns_normalized_document_without_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.documents.models import DocumentBlock, DocumentResult, SkillResult
    import app.main as main_module
    async def fake_execute(skill_name, context):
        assert skill_name == "image.parse"
        document=DocumentResult(document_id=context.file.file_id,source_file=context.file,document_type="image",blocks=[DocumentBlock(kind="text",text="image text")],representations={"plain_text":"image text"})
        return SkillResult(status="success",skill_name=skill_name,data={"document":document.model_dump()})
    monkeypatch.setattr(main_module.executor,"execute",fake_execute)
    task=await task_from_upload("sample.png",b"fixture","image/png")
    assert task["status"] == "succeeded"
    assert task["result"]["document"]["representations"]["plain_text"] == "image text"
    assert "path" not in json.dumps(task)


@pytest.mark.asyncio
async def test_path_callback_and_relative_artifact_routes_are_removed() -> None:
    routes=["/api/v1/parse/image","/api/v1/parse/text","/api/v1/tasks/skill","/api/v1/tasks/office-pipeline","/api/v1/files/file_" + "0"*32 + "/artifacts/anything"]
    for route in routes:
        response=await request("POST" if "artifacts" not in route else "GET",route,json={"path":"/tmp/private","output_dir":"/tmp/out","callback":"https://example.test"})
        assert response.status_code in {404,405}


def test_public_openapi_has_no_callback_output_dir_or_path_fields() -> None:
    serialized=json.dumps(app.openapi()).replace('"in": "path"','"in": "route_parameter"')
    assert '"callback"' not in serialized and '"output_dir"' not in serialized and '"path"' not in serialized
