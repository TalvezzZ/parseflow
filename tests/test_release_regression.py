"""Release-candidate regression coverage for public ParseFlow behaviors.

These tests use only temporary files and ASGI calls.  External tools (LibreOffice,
FFmpeg, and live PaddleOCR inference) stay in their dedicated integration tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.main import app
from app.planning.rule_planner import RuleBasedPlanner
from app.skills.registry import create_default_registry


async def request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


async def wait_for_task(task_id: str) -> dict:
    for _ in range(250):
        response = await request("GET", f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in {"succeeded", "partial", "failed", "cancelled", "interrupted"}:
            return payload
        await asyncio.sleep(0.02)
    raise AssertionError("任务未在测试时限内结束")


@pytest.mark.parametrize(
    ("suffix", "skill_name"),
    [
        (".pdf", "pdf.parse"),
        (".docx", "word.parse"),
        (".doc", "office.parse_pipeline"),
        (".xlsx", "excel.parse"),
        (".xlsm", "excel.parse"),
        (".xls", "office.parse_pipeline"),
        (".pptx", "ppt.parse"),
        (".ppt", "office.parse_pipeline"),
        (".rtf", "rtf.parse"),
        (".txt", "text.parse"),
        (".md", "text.parse"),
        (".markdown", "text.parse"),
        (".csv", "text.parse"),
        (".tsv", "text.parse"),
        (".html", "text.parse"),
        (".htm", "text.parse"),
        (".xml", "text.parse"),
        (".json", "text.parse"),
        (".yaml", "text.parse"),
        (".yml", "text.parse"),
        (".ini", "text.parse"),
        (".cfg", "text.parse"),
        (".conf", "text.parse"),
        (".log", "text.parse"),
        (".sql", "text.parse"),
        (".js", "text.parse"),
        (".ts", "text.parse"),
        (".css", "text.parse"),
        (".png", "image.parse"),
        (".jpg", "image.parse"),
        (".jpeg", "image.parse"),
        (".webp", "image.parse"),
        (".bmp", "image.parse"),
        (".tif", "image.parse"),
        (".tiff", "image.parse"),
        (".mp3", "audio.prepare"),
        (".wav", "audio.prepare"),
        (".m4a", "audio.prepare"),
        (".aac", "audio.prepare"),
        (".flac", "audio.prepare"),
        (".ogg", "audio.prepare"),
        (".mp4", "video.prepare"),
        (".mov", "video.prepare"),
        (".mkv", "video.prepare"),
        (".avi", "video.prepare"),
        (".webm", "video.prepare"),
    ],
)
def test_rule_planner_covers_every_publicly_allowed_format(suffix: str, skill_name: str) -> None:
    planner = RuleBasedPlanner(create_default_registry())

    plan = planner.create(f"/tmp/release-candidate{suffix}")

    assert plan.steps[0].skill_name == skill_name


def test_rule_planner_rejects_unknown_extensions() -> None:
    planner = RuleBasedPlanner(create_default_registry())

    with pytest.raises(ValueError, match="无法自动规划"):
        planner.create("/tmp/untrusted.exe")


@pytest.mark.asyncio
async def test_upload_rejects_unsupported_extension_and_preserves_request_id() -> None:
    response = await request("POST", "/api/v1/files", files={"file": ("malware.exe", b"not executable", "application/octet-stream")})

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "upload_rejected"
    assert response.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_upload_accepts_supported_image_extension() -> None:
    response = await request("POST", "/api/v1/files", files={"file": ("sample.png", b"not-inspected-on-upload", "image/png")})

    assert response.status_code == 201, response.text
    assert response.json()["filename"] == "sample.png"


@pytest.mark.asyncio
async def test_uploaded_file_can_be_queried_and_downloaded() -> None:
    source = b"title,value\nrelease,1\n"
    upload = await request("POST", "/api/v1/files", files={"file": ("release.csv", source, "text/csv")})
    assert upload.status_code == 201, upload.text
    stored = upload.json()

    metadata = await request("GET", f"/api/v1/files/{stored['file_id']}")
    download = await request("GET", f"/api/v1/files/{stored['file_id']}/content")

    assert metadata.status_code == 200
    assert metadata.json()["size_bytes"] == len(source)
    assert download.status_code == 200
    assert download.content == source


@pytest.mark.asyncio
async def test_automatic_parse_csv_produces_plan_and_unified_document() -> None:
    response = await request(
        "POST",
        "/api/v1/tasks/parse",
        data={"goal": "提取发布数据", "data_id": "release-candidate"},
        files={"file": ("release.csv", b"name,value\nParseFlow,1\n", "text/csv")},
    )
    assert response.status_code == 202, response.text

    task = await wait_for_task(response.json()["task_id"])
    document = task["result"]["document"]
    assert task["status"] == "succeeded"
    assert task["data_id"] == "release-candidate"
    assert task["plan"]["steps"][0]["skill_name"] == "text.parse"
    assert document["document_type"] == "csv"
    assert document["tables"][0]["rows"] == [["name", "value"], ["ParseFlow", "1"]]
    assert "ParseFlow" in document["representations"]["markdown"]


@pytest.mark.asyncio
async def test_automatic_parse_ignores_removed_callback_field() -> None:
    response = await request(
        "POST",
        "/api/v1/tasks/parse",
        data={"callback": "https://must-not-be-called.example", "goal": "extract"},
        files={"file": ("release.csv", b"a,b\n1,2\n", "text/csv")},
    )

    assert response.status_code == 202
    assert "callback" not in response.text


@pytest.mark.asyncio
async def test_text_parser_strips_active_html_and_rejects_unsafe_xml() -> None:
    html_response = await request("POST", "/api/v1/tasks/parse", files={"file": ("active.html", b"<html><body><script>secret()</script><style>body{}</style><h1>Safe title</h1></body></html>", "text/html")})
    xml_response = await request("POST", "/api/v1/tasks/parse", files={"file": ("unsafe.xml", b"<!DOCTYPE root [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]><root>&xxe;</root>", "application/xml")})
    html_task = await wait_for_task(html_response.json()["task_id"])
    xml_task = await wait_for_task(xml_response.json()["task_id"])

    assert html_task["result"]["document"]["representations"]["plain_text"] == "Safe title"
    assert xml_task["status"] == "failed"
    assert xml_task["error"]["code"] == "xml_unsafe_or_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "payload"),
    [
        ("/api/v1/parse/rtf", {"file_id": "missing", "path": "/tmp/no-such-file.rtf"}),
        ("/api/v1/parse/text", {"file_id": "missing", "path": "/tmp/no-such-file.txt"}),
        ("/api/v1/parse/image", {"file_id": "missing", "path": "/tmp/no-such-file.png"}),
        ("/api/v1/parse/pdf", {"file_id": "missing", "path": "/tmp/no-such-file.pdf"}),
        ("/api/v1/parse/docx", {"file_id": "missing", "path": "/tmp/no-such-file.docx"}),
        ("/api/v1/convert/office", {"file_id": "missing", "path": "/tmp/no-such-file.doc", "target_format": "docx"}),
        ("/api/v1/parse/excel", {"file_id": "missing", "path": "/tmp/no-such-file.xlsx"}),
        ("/api/v1/parse/ppt", {"file_id": "missing", "path": "/tmp/no-such-file.pptx"}),
        ("/api/v1/pipeline/parse-office", {"file_id": "missing", "path": "/tmp/no-such-file.doc"}),
        ("/api/v1/prepare/audio", {"file_id": "missing", "path": "/tmp/no-such-file.mp3"}),
        ("/api/v1/prepare/video", {"file_id": "missing", "path": "/tmp/no-such-file.mp4"}),
    ],
)
async def test_direct_api_endpoints_return_not_found_for_missing_source(endpoint: str, payload: dict) -> None:
    response = await request("POST", endpoint, json=payload)

    assert response.status_code == 404
    assert response.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_missing_resources_and_unknown_tasks_return_not_found() -> None:
    responses = await asyncio.gather(
        request("GET", "/api/v1/files/file_does_not_exist"),
        request("GET", "/api/v1/tasks/task_does_not_exist"),
    )

    assert [response.status_code for response in responses] == [404, 404]


@pytest.mark.asyncio
async def test_trusted_origin_receives_cors_preflight_headers() -> None:
    response = await request(
        "OPTIONS",
        "/api/v1/tasks/parse",
        headers={"Origin": "http://127.0.0.1:5173", "Access-Control-Request-Method": "POST"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert "POST" in response.headers["access-control-allow-methods"]
