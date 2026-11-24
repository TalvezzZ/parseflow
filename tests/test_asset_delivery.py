"""资产投递：签名、内联预算与结果注入。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.assets import (
    AssetError,
    AssetSigner,
    SignedLocalProvider,
    asset_id_from_sha256,
    encode_data_uri,
    sha256_from_asset_id,
)


def test_sign_and_verify_roundtrip():
    signer = AssetSigner("secret", 900)
    url, expires_at = signer.signed_url("task-1", "sha256-" + "a" * 64)
    signature = url.split("sig=")[1].split("&")[0]

    signer.verify("task-1", "sha256-" + "a" * 64, expires_at, signature, now=expires_at - 1)


def test_tampered_signature_rejected():
    signer = AssetSigner("secret", 900)
    _, expires_at = signer.signed_url("task-1", "sha256-" + "a" * 64)

    with pytest.raises(AssetError) as excinfo:
        signer.verify("task-1", "sha256-" + "b" * 64, expires_at, "deadbeef", now=expires_at - 1)
    assert excinfo.value.code == "asset_signature_invalid"


def test_expired_url_rejected():
    signer = AssetSigner("secret", 900)
    asset_id = "sha256-" + "a" * 64
    signature = signer.sign("task-1", asset_id, 1000)

    with pytest.raises(AssetError) as excinfo:
        signer.verify("task-1", asset_id, 1000, signature, now=1001)
    assert excinfo.value.code == "asset_url_expired"


def test_missing_secret_cannot_sign():
    signer = AssetSigner(None, 900)

    assert signer.available is False
    with pytest.raises(AssetError) as excinfo:
        signer.signed_url("task-1", "sha256-" + "a" * 64)
    assert excinfo.value.code == "asset_url_unavailable"


def test_inline_url_carries_format_parameter():
    signer = AssetSigner("secret", 900)
    url, _ = signer.signed_url("task-1", "sha256-" + "a" * 64, inline=True)

    assert "format=data-uri" in url


def test_asset_id_roundtrip_and_validation():
    digest = "c" * 64
    assert sha256_from_asset_id(asset_id_from_sha256(digest)) == digest
    for bad in ("deadbeef", "sha256-", "sha256-" + "z" * 64):
        with pytest.raises(AssetError):
            sha256_from_asset_id(bad)


def test_encode_data_uri_within_budget(tmp_path: Path):
    path = tmp_path / "a.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")

    payload = encode_data_uri(path, "image/png", 1024)

    assert payload["data_uri"].startswith("data:image/png;base64,")
    assert payload["size_bytes"] == 8
    assert payload["content_type"] == "image/png"


def test_encode_data_uri_rejects_oversized(tmp_path: Path):
    path = tmp_path / "big.png"
    path.write_bytes(b"x" * 2048)

    with pytest.raises(AssetError) as excinfo:
        encode_data_uri(path, "image/png", 1024)
    assert excinfo.value.code == "asset_too_large_for_inline"
    assert excinfo.value.status_code == 413


def test_signed_local_provider_delegates():
    provider = SignedLocalProvider(AssetSigner("secret", 900))
    url, expires_at = provider.url_for("task-1", "sha256-" + "a" * 64)

    assert url.startswith("/api/v1/tasks/task-1/assets/sha256-")
    assert "exp=" in url and "sig=" in url
    assert expires_at > 0


# ---------- 结果注入 ----------


def _document_with_images(count: int, digest: str = "a" * 64) -> dict:
    return {
        "document_id": "doc-1",
        "images": [
            {"filename": f"img-{index}.png", "relative_path": f"images/img-{index}.png",
             "sha256": digest, "mime_type": "image/png", "size_bytes": 1024}
            for index in range(count)
        ],
    }


def test_inject_adds_signed_urls_without_base64(monkeypatch):
    import app.main as main

    signer = AssetSigner("secret", 900)
    monkeypatch.setattr(main, "asset_url_provider", SignedLocalProvider(signer))
    monkeypatch.setattr(main.settings, "asset_delivery", "url", raising=False)
    monkeypatch.setattr(main.settings, "asset_list_max", 100, raising=False)

    document = main._inject_asset_urls(_document_with_images(2), "task-1")

    assert document is not None
    image = document["images"][0]
    assert image["asset_id"] == "sha256-" + "a" * 64
    assert image["url"].startswith("/api/v1/tasks/task-1/assets/")
    assert "format=data-uri" in image["inline_url"]
    assert image["url_expires_at"].endswith("Z")
    assert "base64" not in json.dumps(document)
    assert document["images_total"] == 2
    assert document["images_truncated"] is False


def test_inject_respects_delivery_none(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main.settings, "asset_delivery", "none", raising=False)

    document = main._inject_asset_urls(_document_with_images(1), "task-1")

    assert document is not None
    assert document["images"][0]["asset_id"] == "sha256-" + "a" * 64
    assert "url" not in document["images"][0]


def test_inject_truncates_and_reports_total(monkeypatch):
    import app.main as main

    signer = AssetSigner("secret", 900)
    monkeypatch.setattr(main, "asset_url_provider", SignedLocalProvider(signer))
    monkeypatch.setattr(main.settings, "asset_delivery", "url", raising=False)
    monkeypatch.setattr(main.settings, "asset_list_max", 2, raising=False)

    document = main._inject_asset_urls(_document_with_images(5), "task-1", total_images=137)

    assert document is not None
    assert len(document["images"]) == 2
    assert document["images_total"] == 137
    assert document["images_truncated"] is True


def test_inject_skips_images_without_sha256(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main.settings, "asset_delivery", "url", raising=False)
    document = main._inject_asset_urls({"images": [{"filename": "x.png", "relative_path": "images/x.png"}]}, "task-1")

    assert document is not None
    assert "asset_id" not in document["images"][0]


# ---------- 端点 ----------


def _prepare_workspace(root: Path, task_id: str, payload: bytes) -> tuple[Path, str]:
    workspace = root / task_id / "files"
    (workspace / "result").mkdir(parents=True)
    (workspace / "images").mkdir(parents=True)
    (workspace / "images" / "x.png").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (workspace / "result" / "document.json").write_text(
        json.dumps({"images": [{"sha256": digest, "relative_path": "images/x.png", "mime_type": "image/png"}]}),
        encoding="utf-8",
    )
    return workspace, digest


def _patched_main(monkeypatch, root: Path):
    import app.main as main

    signer = AssetSigner("secret", 900)
    monkeypatch.setattr(main.artifact_repository, "root", root)
    monkeypatch.setattr(main, "asset_signer", signer)
    monkeypatch.setattr(main, "asset_url_provider", SignedLocalProvider(signer))
    monkeypatch.setattr(main.settings, "asset_delivery", "url", raising=False)
    monkeypatch.setattr(main.settings, "asset_inline_max_bytes", 1024, raising=False)
    return main, signer


def test_endpoint_serves_binary(tmp_path: Path, monkeypatch):
    main, signer = _patched_main(monkeypatch, tmp_path / "artifacts")
    payload = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    workspace, digest = _prepare_workspace(tmp_path / "artifacts", "task-1", payload)
    asset_id = asset_id_from_sha256(digest)
    url, expires_at = signer.signed_url("task-1", asset_id)
    signature = url.split("sig=")[1]

    response = asyncio.run(main.get_task_asset("task-1", asset_id, expires_at, signature, "binary"))

    assert Path(response.path) == workspace / "images" / "x.png"
    assert response.media_type == "image/png"
    assert "private" in response.headers["cache-control"]


def test_endpoint_returns_data_uri(tmp_path: Path, monkeypatch):
    main, signer = _patched_main(monkeypatch, tmp_path / "artifacts")
    payload = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    _, digest = _prepare_workspace(tmp_path / "artifacts", "task-1", payload)
    asset_id = asset_id_from_sha256(digest)
    url, expires_at = signer.signed_url("task-1", asset_id, inline=True)
    signature = url.split("sig=")[1].split("&")[0]

    response = asyncio.run(main.get_task_asset("task-1", asset_id, expires_at, signature, "data-uri"))

    body = json.loads(response.body)
    assert body["data_uri"].startswith("data:image/png;base64,")


def test_endpoint_rejects_bad_signature(tmp_path: Path, monkeypatch):
    main, signer = _patched_main(monkeypatch, tmp_path / "artifacts")
    _, digest = _prepare_workspace(tmp_path / "artifacts", "task-1", b"payload")
    asset_id = asset_id_from_sha256(digest)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main.get_task_asset("task-1", asset_id, 4102444800, "bogus", "binary"))
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail["code"] == "asset_signature_invalid"


def test_endpoint_rejects_oversized_inline(tmp_path: Path, monkeypatch):
    main, signer = _patched_main(monkeypatch, tmp_path / "artifacts")
    _, digest = _prepare_workspace(tmp_path / "artifacts", "task-1", b"x" * 4096)
    asset_id = asset_id_from_sha256(digest)
    url, expires_at = signer.signed_url("task-1", asset_id, inline=True)
    signature = url.split("sig=")[1].split("&")[0]

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main.get_task_asset("task-1", asset_id, expires_at, signature, "data-uri"))
    assert excinfo.value.status_code == 413
    assert excinfo.value.detail["code"] == "asset_too_large_for_inline"


def test_endpoint_missing_asset_is_404(tmp_path: Path, monkeypatch):
    main, signer = _patched_main(monkeypatch, tmp_path / "artifacts")
    (tmp_path / "artifacts" / "task-1" / "files" / "result").mkdir(parents=True)
    asset_id = asset_id_from_sha256("d" * 64)
    url, expires_at = signer.signed_url("task-1", asset_id)
    signature = url.split("sig=")[1]

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main.get_task_asset("task-1", asset_id, expires_at, signature, "binary"))
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail["code"] == "asset_not_found"


def test_endpoint_only_serves_images_prefix(tmp_path: Path, monkeypatch):
    """资产端点不得被用来读取结果目录里的其他文件。"""
    main, signer = _patched_main(monkeypatch, tmp_path / "artifacts")
    workspace = tmp_path / "artifacts" / "task-1" / "files"
    (workspace / "result").mkdir(parents=True)
    payload = b'{"secret": "result-document"}'
    (workspace / "result" / "document.json").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (workspace / "result" / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "units": [], "resources": [
            {"storage_key": "result/document.json", "size_bytes": len(payload), "sha256": digest}]}),
        encoding="utf-8")
    # 伪造一份索引，声明 document.json 为资产
    (workspace / "result" / "document.json").write_text(json.dumps({
        "images": [{"sha256": digest, "relative_path": "result/document.json", "mime_type": "application/json"}]}),
        encoding="utf-8")
    asset_id = asset_id_from_sha256(digest)
    url, expires_at = signer.signed_url("task-1", asset_id)
    signature = url.split("sig=")[1]

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main.get_task_asset("task-1", asset_id, expires_at, signature, "binary"))
    assert excinfo.value.status_code == 404
