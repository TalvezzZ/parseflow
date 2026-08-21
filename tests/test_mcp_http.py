from __future__ import annotations

import json

import httpx
import pytest

from app.main import app, settings


async def request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


def initialize_payload() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "parseflow-test", "version": "1.0"},
        },
    }


@pytest.mark.asyncio
async def test_remote_mcp_is_disabled_by_default() -> None:
    original = settings.mcp_http_enabled
    settings.mcp_http_enabled = False
    try:
        response = await request("POST", "/mcp/", json=initialize_payload())
    finally:
        settings.mcp_http_enabled = original

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "mcp_disabled"
    assert response.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_remote_mcp_requires_api_key_when_enabled() -> None:
    original_enabled = settings.mcp_http_enabled
    original_key = settings.api_key
    settings.mcp_http_enabled = True
    settings.api_key = None
    try:
        response = await request("POST", "/mcp/", json=initialize_payload())
    finally:
        settings.mcp_http_enabled = original_enabled
        settings.api_key = original_key

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "mcp_api_key_required"


@pytest.mark.asyncio
async def test_remote_mcp_rejects_missing_key_and_initializes_with_valid_key() -> None:
    original_enabled = settings.mcp_http_enabled
    original_key = settings.api_key
    settings.mcp_http_enabled = True
    settings.api_key = "mcp-test-key"
    try:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
                denied = await client.post("/mcp/", json=initialize_payload())
                accepted = await client.post(
                    "/mcp/",
                    headers={"X-API-Key": "mcp-test-key", "Accept": "application/json, text/event-stream"},
                    json=initialize_payload(),
                )
    finally:
        settings.mcp_http_enabled = original_enabled
        settings.api_key = original_key

    assert denied.status_code == 401
    assert denied.json()["detail"]["code"] == "unauthorized"
    assert accepted.status_code == 200, accepted.text
    assert accepted.headers["content-type"].startswith("text/event-stream")
    payload = json.loads(accepted.text.split("data: ", 1)[1].strip())
    assert payload["result"]["serverInfo"]["name"] == "parse-agent"
    assert "protocolVersion" in payload["result"]
