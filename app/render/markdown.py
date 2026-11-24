"""Markdown 表示渲染。

两种来源必须区分清楚：

1. 解析器已经产出 markdown（例如 pdf-inspector、Mammoth）——那是解析结果本身，
   直接复用，不能从页面重新拼装，否则会改变字节。
2. 只有页面/块结构（例如 PyMuPDF、PPTX）——按页模板生成。
"""

from __future__ import annotations

from typing import Any, Mapping

from app.render.contracts import DEFAULT_MEDIA_TYPES, RenderArtifact, RenderOptions
from app.render.text import strip_generated_cjk_spaces


def _representation(document: Mapping[str, Any], kind: str) -> str:
    representations = document.get("representations")
    if not isinstance(representations, Mapping):
        return ""
    value = representations.get(kind)
    return value if isinstance(value, str) else ""


def _page_text(page: Mapping[str, Any]) -> str:
    text = page.get("text")
    return text if isinstance(text, str) else ""


def _page_number(page: Mapping[str, Any], fallback: int) -> int:
    number = page.get("page_number")
    return number if isinstance(number, int) else fallback


def _pages(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    pages = document.get("pages")
    if not isinstance(pages, list):
        return []
    return [page for page in pages if isinstance(page, Mapping)]


def _blocks_text(document: Mapping[str, Any], separator: str) -> str:
    blocks = document.get("blocks")
    if not isinstance(blocks, list):
        return ""
    parts = [
        block.get("text")
        for block in blocks
        if isinstance(block, Mapping) and isinstance(block.get("text"), str) and block.get("text")
    ]
    return separator.join(parts)


class MarkdownRenderer:
    """生成 markdown 表示。"""

    kind = "markdown"
    media_type = DEFAULT_MEDIA_TYPES["markdown"]

    def render(self, document: Mapping[str, Any], options: RenderOptions) -> RenderArtifact:
        warnings: list[str] = []
        meta: dict[str, Any] = {}

        text = _representation(document, "markdown") if options.prefer_document_representation else ""
        if text:
            renderer = "document"
        else:
            text, page_count = self._from_pages(document, options)
            renderer = "pages"
            meta["pages"] = page_count
            if not text:
                text = _blocks_text(document, options.page_block_separator)
                renderer = "blocks" if text else "empty"
                if not text:
                    warnings.append("markdown_empty: 文档既无既有表示，也无页面或块文本")

        if options.cjk_generated_space_removal:
            text, removed = strip_generated_cjk_spaces(text)
            meta["cjk_space_removed"] = removed

        return RenderArtifact(
            kind=self.kind,
            media_type=self.media_type,
            text=text,
            renderer=renderer,
            warnings=warnings,
            meta=meta,
        )

    def _from_pages(self, document: Mapping[str, Any], options: RenderOptions) -> tuple[str, int]:
        pages = _pages(document)
        sections: list[str] = []
        for index, page in enumerate(pages, start=1):
            number = _page_number(page, index)
            text = _page_text(page)
            heading = options.page_heading_template.format(page=number)
            if text:
                sections.append(f"{heading}{options.page_block_separator}{text}")
            elif options.include_empty_pages:
                sections.append(heading)
        return options.page_separator.join(sections), len(pages)
