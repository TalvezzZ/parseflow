"""资产投递：为解析结果中的图片签发有时效的访问地址。

设计约束（见 docs/plan-1.8.0.md）：

- 结果里只放短 URL 与 `asset_id`，**任何 base64 都不得进入结果或持久化文件**；
- 签名 URL 无状态，靠 HMAC + 过期时间校验，不引入会话；
- 对象存储是可选实现，本模块只定义协议并给出本地签名实现。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from pathlib import Path
from typing import Protocol

ASSET_ID_PREFIX = "sha256-"
SIGNATURE_LENGTH = 32
ASSET_PATH_TEMPLATE = "/api/v1/tasks/{task_id}/assets/{asset_id}"


class AssetError(Exception):
    """资产投递错误；携带对外错误码与 HTTP 状态码。"""

    def __init__(self, code: str, message: str, status_code: int = 403) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(f"{code}: {message}")


def asset_id_from_sha256(digest: str) -> str:
    """把 sha256 转成稳定的资产 ID。"""
    return f"{ASSET_ID_PREFIX}{digest.lower()}"


def sha256_from_asset_id(asset_id: str) -> str:
    """解析资产 ID；格式不合法时抛 AssetError。"""
    if not asset_id.startswith(ASSET_ID_PREFIX):
        raise AssetError("asset_id_invalid", "资产 ID 格式不正确", status_code=400)
    digest = asset_id[len(ASSET_ID_PREFIX) :].lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise AssetError("asset_id_invalid", "资产 ID 格式不正确", status_code=400)
    return digest


class AssetSigner:
    """无状态签名器：HMAC-SHA256 覆盖任务、资产与过期时间。"""

    def __init__(self, secret: str | None, ttl_seconds: int = 900) -> None:
        self.secret = secret or ""
        self.ttl_seconds = max(1, int(ttl_seconds))

    @property
    def available(self) -> bool:
        """没有密钥时不得签发 URL，也不得降级为无签名地址。"""
        return bool(self.secret)

    def _payload(self, task_id: str, asset_id: str, expires_at: int) -> bytes:
        return f"{task_id}|{asset_id}|{expires_at}".encode("utf-8")

    def sign(self, task_id: str, asset_id: str, expires_at: int) -> str:
        digest = hmac.new(self.secret.encode("utf-8"), self._payload(task_id, asset_id, expires_at), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:SIGNATURE_LENGTH]

    def verify(self, task_id: str, asset_id: str, expires_at: int, signature: str, *, now: int | None = None) -> None:
        if not self.available:
            raise AssetError("asset_url_unavailable", "未配置资产签名密钥", status_code=503)
        current = int(time.time()) if now is None else now
        if expires_at <= current:
            raise AssetError("asset_url_expired", "资产地址已过期")
        expected = self.sign(task_id, asset_id, expires_at)
        if not hmac.compare_digest(expected, signature or ""):
            raise AssetError("asset_signature_invalid", "资产地址签名无效")

    def signed_url(self, task_id: str, asset_id: str, *, inline: bool = False) -> tuple[str, int]:
        """返回 (相对地址, 过期时间戳)。"""
        if not self.available:
            raise AssetError("asset_url_unavailable", "未配置资产签名密钥", status_code=503)
        expires_at = int(time.time()) + self.ttl_seconds
        signature = self.sign(task_id, asset_id, expires_at)
        path = ASSET_PATH_TEMPLATE.format(task_id=task_id, asset_id=asset_id)
        query = f"exp={expires_at}&sig={signature}"
        if inline:
            query += "&format=data-uri"
        return f"{path}?{query}", expires_at


class AssetUrlProvider(Protocol):
    """资产地址提供者；对象存储实现只需满足该协议。"""

    def url_for(self, task_id: str, asset_id: str, *, inline: bool = False) -> tuple[str, int]:
        """返回 (地址, 过期时间戳)。"""


class SignedLocalProvider:
    """本地文件 + 签名地址：不依赖对象存储，服务可达即可用。"""

    def __init__(self, signer: AssetSigner) -> None:
        self.signer = signer

    def url_for(self, task_id: str, asset_id: str, *, inline: bool = False) -> tuple[str, int]:
        return self.signer.signed_url(task_id, asset_id, inline=inline)


def encode_data_uri(path: Path, content_type: str, max_bytes: int) -> dict[str, object]:
    """把图片编码成 data URI；超出预算必须显式拒绝，不得截断。"""
    size = path.stat().st_size
    if size > max_bytes:
        raise AssetError(
            "asset_too_large_for_inline",
            f"图片 {size} 字节超过内联预算 {max_bytes} 字节",
            status_code=413,
        )
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    media_type = content_type or "application/octet-stream"
    return {
        "data_uri": f"data:{media_type};base64,{payload}",
        "content_type": media_type,
        "size_bytes": size,
    }
