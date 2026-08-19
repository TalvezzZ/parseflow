"""HTTP-level release gates for task queue and controlled local storage."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import UploadFile

import app.main as main_module
from app.documents.models import DocumentBlock, DocumentPage, DocumentResult, SkillResult
from app.files import LocalFileStore
from app.tasks.manager import InMemoryTaskManager
from app.tasks.models import TaskRecord


async def request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


@pytest.fixture
def isolated_file_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalFileStore:
    store = LocalFileStore(str(tmp_path / "files"), max_size_mb=1, allowed_suffixes=".csv,.png")
    monkeypatch.setattr(main_module, "file_store", store)
    return store


@pytest.mark.asyncio
async def test_upload_at_limit_succeeds_and_oversize_is_cleaned(isolated_file_store: LocalFileStore, tmp_path: Path) -> None:
    exact = b"x" * (1024 * 1024)
    uploaded = await isolated_file_store.save(UploadFile(filename="exact.csv", file=__import__("io").BytesIO(exact)))
    assert Path(uploaded.path).read_bytes() == exact

    oversize = b"x" * (1024 * 1024 + 1)
    response = await request("POST", "/api/v1/files", files={"file": ("oversize.csv", oversize, "text/csv")})

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "upload_rejected"
    directories = [path for path in isolated_file_store.root.glob("file_*") if path.is_dir()]
    assert len(directories) == 1


@pytest.mark.asyncio
async def test_upload_sanitizes_filename_and_keeps_nested_artifact_contained(isolated_file_store: LocalFileStore) -> None:
    upload = await request("POST", "/api/v1/files", files={"file": ("../../report.csv", b"a,b\n1,2\n", "text/csv")})
    assert upload.status_code == 201
    stored = upload.json()
    assert stored["filename"] == "report.csv"
    assert Path(stored["path"]).parent == isolated_file_store.root / stored["file_id"]

    artifact_dir = isolated_file_store.artifact_dir(stored["file_id"])
    assert artifact_dir is not None
    nested = artifact_dir / "reports" / "result.txt"
    nested.parent.mkdir()
    nested.write_text("safe", encoding="utf-8")

    allowed = await request("GET", f"/api/v1/files/{stored['file_id']}/artifacts/reports/result.txt")
    escaped = await request("GET", f"/api/v1/files/{stored['file_id']}/artifacts/%2e%2e/metadata.json")
    assert allowed.status_code == 200
    assert allowed.content == b"safe"
    assert escaped.status_code == 404


def fake_image_result(context, text: str) -> SkillResult:
    block = DocumentBlock(kind="text", text=text, page_number=1, bbox=[1, 2, 30, 12], metadata={"confidence": 0.98})
    document = DocumentResult(
        document_id=context.file.file_id,
        source_file=context.file,
        document_type="image",
        pages=[DocumentPage(page_number=1, text=text, blocks=[block])],
        blocks=[block],
        representations={"plain_text": text, "markdown": text},
        provenance={"ocr": {"engine": "PaddleOCR"}},
    )
    return SkillResult(status="success", skill_name="image.parse", data={"document": document.model_dump()})


@pytest.mark.asyncio
async def test_image_parse_api_serializes_normalized_ocr_document(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "sample.png"
    source.write_bytes(b"png fixture")

    async def fake_execute(skill_name: str, context) -> SkillResult:
        assert skill_name == "image.parse"
        return fake_image_result(context, "ParseFlow image")

    monkeypatch.setattr(main_module.executor, "execute", fake_execute)
    response = await request("POST", "/api/v1/parse/image", json={"file_id": "image-api", "path": str(source), "mime_type": "image/png"})

    assert response.status_code == 200, response.text
    document = response.json()["data"]["document"]
    assert document["document_type"] == "image"
    assert document["pages"][0]["page_number"] == 1
    assert document["representations"] == {"plain_text": "ParseFlow image", "markdown": "ParseFlow image"}
    assert document["blocks"][0]["bbox"] == [1.0, 2.0, 30.0, 12.0]
    assert document["blocks"][0]["metadata"]["confidence"] == 0.98
    assert document["provenance"]["ocr"]["engine"] == "PaddleOCR"


@pytest.mark.asyncio
async def test_automatic_image_upload_selects_image_skill(monkeypatch: pytest.MonkeyPatch, isolated_file_store: LocalFileStore) -> None:
    async def fake_execute(skill_name: str, context) -> SkillResult:
        assert skill_name == "image.parse"
        return fake_image_result(context, "image text")

    monkeypatch.setattr(main_module.executor, "execute", fake_execute)
    submitted = await request("POST", "/api/v1/parse", files={"file": ("sample.png", b"fixture", "image/png")})
    assert submitted.status_code == 202, submitted.text

    for _ in range(100):
        task = await request("GET", f"/api/v1/tasks/{submitted.json()['task_id']}")
        assert task.status_code == 200
        payload = task.json()
        if payload["status"] in {"succeeded", "failed", "partial"}:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("图片自动解析任务未完成")

    assert payload["status"] == "succeeded"
    assert payload["plan"]["steps"][0]["skill_name"] == "image.parse"
    assert payload["result"]["result"]["data"]["document"]["representations"]["plain_text"] == "image text"


@pytest.mark.asyncio
async def test_task_endpoints_map_queue_disabled_and_missing_task(monkeypatch: pytest.MonkeyPatch) -> None:
    original = main_module.settings.task_queue_enabled
    main_module.settings.task_queue_enabled = False
    try:
        disabled = await request("POST", "/api/v1/parse", files={"file": ("disabled.csv", b"a,b\n1,2\n", "text/csv")})
        assert disabled.status_code == 503
        assert disabled.json()["detail"]["code"] == "task_queue_disabled"
    finally:
        main_module.settings.task_queue_enabled = original

    missing = await request("POST", "/api/v1/tasks/task_missing/cancel")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_task_cancel_http_maps_queued_and_running_states(monkeypatch: pytest.MonkeyPatch) -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    async def held_runner(_: TaskRecord) -> dict:
        started.set()
        await release.wait()
        return {"status": "success"}

    manager = InMemoryTaskManager(held_runner, max_concurrent_executions=1, max_queue_size=2)
    monkeypatch.setattr(main_module, "task_manager", manager)
    try:
        first = await manager.submit("skill.execute", {}, None, None)
        await asyncio.wait_for(started.wait(), timeout=1)
        second = await manager.submit("skill.execute", {}, None, None)

        cancelled = await request("POST", f"/api/v1/tasks/{second.task_id}/cancel")
        running = await request("POST", f"/api/v1/tasks/{first.task_id}/cancel")

        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert running.status_code == 409
        assert running.json()["detail"]["code"] == "task_not_cancellable"
    finally:
        release.set()
        await manager.stop()


@pytest.mark.asyncio
async def test_task_submission_maps_queue_full_to_http_429(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    async def held_runner(_: TaskRecord) -> dict:
        started.set()
        await release.wait()
        return {"status": "success"}

    manager = InMemoryTaskManager(held_runner, max_concurrent_executions=1, max_queue_size=1)
    monkeypatch.setattr(main_module, "task_manager", manager)
    source = tmp_path / "task.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    payload = {"skill_name": "text.parse", "file_id": "queue-test", "path": str(source)}
    try:
        first = await request("POST", "/api/v1/tasks/skill", json=payload)
        assert first.status_code == 202
        await asyncio.wait_for(started.wait(), timeout=1)
        second = await request("POST", "/api/v1/tasks/skill", json=payload)
        third = await request("POST", "/api/v1/tasks/skill", json=payload)

        assert second.status_code == 202
        assert third.status_code == 429
        assert third.json()["detail"]["code"] == "task_queue_full"
        assert third.json()["detail"]["max_queue_size"] == 1
    finally:
        release.set()
        await manager.stop()
