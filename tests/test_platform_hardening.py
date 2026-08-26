from __future__ import annotations

import asyncio

import httpx
import pytest

from app.main import app


async def request(method: str, path: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


async def parse_upload(filename: str, payload: bytes, media_type: str = "application/octet-stream") -> dict:
    created = await request("POST", "/api/v1/tasks/parse", files={"file": (filename, payload, media_type)})
    assert created.status_code == 202, created.text
    task_id = created.json()["task_id"]
    for _ in range(100):
        task = (await request("GET", f"/api/v1/tasks/{task_id}")).json()
        if task["status"] in {"succeeded", "partial", "failed", "cancelled", "interrupted"}:
            return task
        await asyncio.sleep(.02)
    raise AssertionError("task did not finish")


@pytest.mark.asyncio
async def test_upload_then_parse_csv_returns_path_free_envelope() -> None:
    task = await parse_upload("report.csv", b"name,value\nalpha,1\n", "text/csv")
    assert task["status"] == "succeeded"
    assert task["result"]["document"]["tables"][0]["rows"][1] == ["alpha", "1"]
    assert "path" not in str(task)


@pytest.mark.asyncio
async def test_html_and_xml_uploads_produce_safe_representations() -> None:
    html = await parse_upload("safe.html", b"<main onclick='x()'><h1>Title</h1><script>bad()</script></main>", "text/html")
    xml = await parse_upload("order.xml", b"<order><customer>Alice</customer></order>", "application/xml")
    assert html["status"] == "succeeded"
    assert "script" not in html["result"]["document"]["representations"]["html"]
    assert "onclick" not in html["result"]["document"]["representations"]["html"]
    assert xml["result"]["document"]["representations"]["plain_text"] == "<order><customer>Alice</customer></order>"
    assert xml["result"]["document"]["extensions"]["xml"]["root_tag"] == "order"


@pytest.mark.asyncio
async def test_legacy_path_routes_are_not_public() -> None:
    for route in ("/api/v1/parse/text", "/api/v1/parse/rtf", "/api/v1/tasks/skill", "/api/v1/convert/office"):
        response = await request("POST", route, json={"path": "/tmp/secret", "callback": "https://example.test"})
        assert response.status_code in {404, 405}
