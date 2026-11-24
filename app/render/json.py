"""结构化 JSON 表示渲染。

序列化参数必须与既有导出保持一致（`ensure_ascii=False`、`indent=2`），
否则同一任务的结果文件字节会变化。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from app.render.contracts import DEFAULT_MEDIA_TYPES, RenderArtifact, RenderOptions


def dumps_document(document: Mapping[str, Any]) -> str:
    """与 app/tasks/exports.py 相同的 JSON 编码参数。"""
    return "".join(json.JSONEncoder(ensure_ascii=False, indent=2).iterencode(dict(document)))


class JsonRenderer:
    """生成结构化 JSON 表示。"""

    kind = "json"
    media_type = DEFAULT_MEDIA_TYPES["json"]

    def render(self, document: Mapping[str, Any], options: RenderOptions) -> RenderArtifact:
        warnings: list[str] = []
        try:
            text = dumps_document(document)
            renderer = "document"
        except (TypeError, ValueError) as exc:  # 非 JSON 可序列化内容必须显式失败
            raise ValueError(f"document_not_json_serializable: {exc}") from exc
        return RenderArtifact(
            kind=self.kind,
            media_type=self.media_type,
            text=text,
            renderer=renderer,
            warnings=warnings,
            meta={"bytes": len(text.encode("utf-8"))},
        )
