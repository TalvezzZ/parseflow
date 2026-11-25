"""纯文本表示渲染。

纯文本是"把页面文本按分隔符拼起来"的最简投影；不做任何字符级改写。
"""

from __future__ import annotations

from typing import Any, Mapping

from app.render.contracts import DEFAULT_MEDIA_TYPES, RenderArtifact, RenderOptions


def _representation(document: Mapping[str, Any], kind: str) -> str:
    representations = document.get("representations")
    if not isinstance(representations, Mapping):
        return ""
    value = representations.get(kind)
    return value if isinstance(value, str) else ""


class PlainTextRenderer:
    """生成纯文本表示。"""

    kind = "plain_text"
    media_type = DEFAULT_MEDIA_TYPES["plain_text"]

    def render(self, document: Mapping[str, Any], options: RenderOptions) -> RenderArtifact:
        warnings: list[str] = []
        meta: dict[str, Any] = {}

        text = _representation(document, "plain_text") if options.prefer_document_representation else ""
        if text:
            renderer = "document"
        else:
            pages = document.get("pages")
            page_list = [page for page in pages if isinstance(page, Mapping)] if isinstance(pages, list) else []
            parts = [
                page.get("text")
                for page in page_list
                if isinstance(page.get("text"), str) and (page.get("text") or not options.plain_text_skip_empty)
            ]
            text = options.page_block_separator.join(parts)
            renderer = "pages" if text else "empty"
            meta["pages"] = len(page_list)
            if not text:
                warnings.append("plain_text_empty: 文档既无既有表示，也无页面文本")

        return RenderArtifact(
            kind=self.kind,
            media_type=self.media_type,
            text=text,
            renderer=renderer,
            warnings=warnings,
            meta=meta,
        )
