from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.main import app, file_store, settings


async def request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_upload_then_parse_csv_and_download_artifact() -> None:
    upload = await request("POST", "/api/v1/files", files={"file": ("report.csv", b"name,value\nalpha,1\n", "text/csv")})
    assert upload.status_code == 201, upload.text
    stored = upload.json()
    assert stored["filename"] == "report.csv"

    parsed = await request("POST", "/api/v1/parse/text", json={"file_id": stored["file_id"], "path": stored["path"]})
    assert parsed.status_code == 200, parsed.text
    document = parsed.json()["data"]["document"]
    assert document["document_type"] == "csv"
    assert document["tables"][0]["rows"][1] == ["alpha", "1"]

    artifact_dir = file_store.artifact_dir(stored["file_id"])
    assert artifact_dir is not None
    artifact = artifact_dir / "result.txt"
    artifact.write_text("artifact content", encoding="utf-8")
    downloaded = await request("GET", f"/api/v1/files/{stored['file_id']}/artifacts/result.txt")
    assert downloaded.status_code == 200
    assert downloaded.content == b"artifact content"
    blocked = await request("GET", f"/api/v1/files/{stored['file_id']}/artifacts/../../metadata.json")
    assert blocked.status_code == 404


@pytest.mark.asyncio
async def test_text_and_html_parse_api_and_request_metrics() -> None:
    markdown = Path("/tmp/parse-agent-hardening.md")
    html = Path("/tmp/parse-agent-hardening.html")
    markdown.write_text("# Heading\n\nHello world", encoding="utf-8")
    html.write_text("<html><body><h1>Title</h1><a href='https://example.test'>Link</a></body></html>", encoding="utf-8")
    try:
        markdown_response = await request("POST", "/api/v1/parse/text", json={"file_id": "md", "path": str(markdown)})
        assert markdown_response.status_code == 200
        assert "Hello world" in markdown_response.json()["data"]["document"]["representations"]["plain_text"]
        html_response = await request("POST", "/api/v1/parse/text", json={"file_id": "html", "path": str(html)})
        assert html_response.status_code == 200
        assert html_response.json()["data"]["document"]["metadata"]["links"][0]["href"] == "https://example.test"
        assert html_response.headers["X-Request-ID"]
        metrics = await request("GET", "/api/v1/metrics")
        assert metrics.status_code == 200
        assert metrics.json()["http"]["requests_total"] >= 2
    finally:
        markdown.unlink(missing_ok=True)
        html.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_xml_and_rtf_direct_parse_api(tmp_path: Path) -> None:
    xml = tmp_path / "order.xml"
    rtf = tmp_path / "note.rtf"
    xml.write_text('<order id="A001"><customer>Alice</customer><total currency="CNY">100</total></order>', encoding="utf-8")
    rtf.write_text(r"{\rtf1\ansi\b Important\b0  note from RTF}", encoding="utf-8")

    xml_response = await request("POST", "/api/v1/parse/text", json={"file_id": "xml", "path": str(xml)})
    assert xml_response.status_code == 200, xml_response.text
    xml_document = xml_response.json()["data"]["document"]
    assert xml_document["document_type"] == "xml"
    assert xml_document["representations"]["plain_text"] == xml.read_text(encoding="utf-8")
    assert "<code>&lt;order&gt;</code>" in xml_document["representations"]["html"]
    assert "Alice" in xml_document["representations"]["html"]
    assert xml_document["extensions"]["xml"]["root_tag"] == "order"
    assert any(node["path"] == "/order/total" and node["attributes"]["currency"] == "CNY" for node in xml_document["extensions"]["xml"]["nodes"])

    rtf_response = await request("POST", "/api/v1/parse/text", json={"file_id": "rtf", "path": str(rtf)})
    assert rtf_response.status_code == 200, rtf_response.text
    rtf_document = rtf_response.json()["data"]["document"]
    assert rtf_document["document_type"] == "rtf"
    assert "Important" in rtf_document["representations"]["plain_text"]


@pytest.mark.asyncio
async def test_rtf_auto_route_direct_and_complex_conversion(tmp_path: Path) -> None:
    simple = tmp_path / "simple.rtf"
    complex_rtf = tmp_path / "table.rtf"
    simple.write_text(r"{\rtf1\ansi Simple direct RTF text}", encoding="utf-8")
    complex_rtf.write_text(r"{\rtf1\ansi\trowd\cellx2000\cellx4000 Name\cell Value\cell\row\trowd\cellx2000\cellx4000 Alpha\cell 1\cell\row}", encoding="utf-8")

    direct = await request("POST", "/api/v1/parse/rtf", json={"file_id": "rtf-direct", "path": str(simple)})
    assert direct.status_code == 200, direct.text
    direct_payload = direct.json()
    assert direct_payload["status"] == "success"
    assert direct_payload["data"]["rtf_routing"]["selected_path"] == "direct_text"
    assert direct_payload["data"]["rtf_routing"]["direct_parse_attempted"] is True

    converted = await request("POST", "/api/v1/parse/rtf", json={"file_id": "rtf-complex", "path": str(complex_rtf)})
    assert converted.status_code == 200, converted.text
    converted_payload = converted.json()
    assert converted_payload["status"] == "success"
    routing = converted_payload["data"]["rtf_routing"]
    assert routing["selected_path"] == "convert_to_docx"
    assert "rtf_table" in routing["signals"]
    assert converted_payload["data"]["conversion"]["target_format"] == "docx"
    assert converted_payload["data"]["document"]["source_file"]["path"] == str(complex_rtf)


@pytest.mark.asyncio
async def test_optional_api_key_protects_api_routes() -> None:
    original = settings.api_key
    settings.api_key = "test-key"
    try:
        denied = await request("GET", "/api/v1/skills")
        assert denied.status_code == 401
        accepted = await request("GET", "/api/v1/skills", headers={"X-API-Key": "test-key"})
        assert accepted.status_code == 200
        health = await request("GET", "/health")
        assert health.status_code == 200
    finally:
        settings.api_key = original
