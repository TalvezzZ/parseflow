"""Markdown 表示渲染。

两种来源必须区分清楚：

1. 解析器已经产出 markdown（例如 pdf-inspector、Mammoth）——那是解析结果本身，
   直接复用，不能从页面重新拼装，否则会改变字节。
2. 只有页面/块结构（例如 PyMuPDF、PPTX）——按页模板生成。
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


def _page_text(page: Mapping[str, Any]) -> str:
    text = page.get("text")
    return text if isinstance(text, str) else ""


def _page_blocks_text(page: Mapping[str, Any], separator: str) -> str:
    """页内块文本，按块顺序拼接；无文本块（如纯图片块）跳过。"""
    blocks = page.get("blocks")
    if not isinstance(blocks, list):
        return ""
    parts = [
        block.get("text")
        for block in blocks
        if isinstance(block, Mapping) and isinstance(block.get("text"), str) and block.get("text")
    ]
    return separator.join(parts)


def _page_number(page: Mapping[str, Any], fallback: int) -> int:
    number = page.get("page_number")
    return number if isinstance(number, int) else fallback


def _pages(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    pages = document.get("pages")
    if not isinstance(pages, list):
        return []
    return [page for page in pages if isinstance(page, Mapping)]


def _asset_pages(image: Mapping[str, Any]) -> list[int]:
    """图片所属页：优先 page_number，其次 occurrences[].page_number（保序去重）。"""
    pages: list[int] = []
    single = image.get("page_number")
    if isinstance(single, int):
        pages.append(single)
    occurrences = image.get("occurrences")
    if isinstance(occurrences, list):
        for occurrence in occurrences:
            if isinstance(occurrence, Mapping):
                page = occurrence.get("page_number")
                if isinstance(page, int) and page not in pages:
                    pages.append(page)
    return pages


def _page_asset_links(document: Mapping[str, Any], options: RenderOptions) -> tuple[dict[int, list[str]], int]:
    """按页归组图片相对路径；返回 (页 -> 路径列表, 跳过数量)。"""
    if not options.page_asset_links:
        return {}, 0
    images = document.get("images")
    if not isinstance(images, list):
        return {}, 0
    grouped: dict[int, list[str]] = {}
    skipped = 0
    for image in images:
        if not isinstance(image, Mapping):
            continue
        path = image.get("relative_path")
        if not isinstance(path, str) or not path:
            skipped += 1
            continue
        for page in _asset_pages(image):
            grouped.setdefault(page, []).append(path)
    return grouped, skipped


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


def _sheet_sections(document: Mapping[str, Any], options: RenderOptions) -> str:
    """按块元数据的 sheet 名分组，复现工作簿的既有 markdown 布局。

    分组依据是 block.metadata.sheet（由 Excel Provider 写入），因此不需要
    给 DocumentResult 增加新字段；同一 sheet 名按首次出现顺序输出。
    """
    blocks = document.get("blocks")
    if not isinstance(blocks, list):
        return ""
    order: list[str] = []
    grouped: dict[str, list[str]] = {}
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        metadata = block.get("metadata")
        name = metadata.get("sheet") if isinstance(metadata, Mapping) else None
        text = block.get("text")
        if not isinstance(name, str) or not name:
            continue
        if name not in grouped:
            order.append(name)
            grouped[name] = []
        # heading 块承载 sheet 名（标题已由模板输出），不重复计入行。
        if block.get("kind") == "heading":
            continue
        if isinstance(text, str) and text:
            grouped[name].append(options.sheet_row_template.format(row=text))
    sections: list[str] = []
    for name in order:
        rows = grouped[name]
        if not rows and options.sheet_skip_empty:
            continue
        title = options.sheet_title_template.format(name=name)
        body = "\n".join(rows)
        sections.append(f"{title}{options.page_block_separator}{body}")
    # 工作簿分节用空行连接，与既有 Excel 拼装一致（不用页分隔符）。
    return options.page_block_separator.join(sections)


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
        elif options.body_source == "sheet":
            text = _sheet_sections(document, options)
            renderer = "sheets" if text else "empty"
            meta["sheets"] = len({str((block.get("metadata") or {}).get("sheet"))
                                  for block in (document.get("blocks") or [])
                                  if isinstance(block, Mapping) and isinstance(block.get("metadata"), Mapping)
                                  and (block.get("metadata") or {}).get("sheet")})
            if not text:
                warnings.append("markdown_empty: 工作簿没有可渲染的工作表内容")
        else:
            text, page_count = self._from_pages(document, options)
            renderer = "pages"
            meta["pages"] = page_count
            if not text:
                text = _blocks_text(document, options.page_block_separator)
                renderer = "blocks" if text else "empty"
                if not text:
                    warnings.append("markdown_empty: 文档既无既有表示，也无页面或块文本")

        asset_links, skipped_assets = _page_asset_links(document, options)
        if skipped_assets:
            warnings.append(f"asset_link_skipped: {skipped_assets} 张图片缺少相对路径，未写入 markdown")

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
        asset_links, _ = _page_asset_links(document, options)
        sections: list[str] = []
        for index, page in enumerate(pages, start=1):
            number = _page_number(page, index)
            heading = options.page_heading_template.format(page=number)
            if options.body_source == "blocks":
                # 块来源（幻灯片等）结构上总是"标题 + 正文"，空正文也保留分隔符。
                body = _page_blocks_text(page, options.page_block_separator)
                sections.append(f"{heading}{options.page_block_separator}{body}")
                continue
            body = _page_text(page)
            # 图片引用沿用既有拼装：每个引用前置分隔符。空正文 + 有图时因此保留前导分隔符，
            # 这是 1.6.0 的实际产物，为字节等价刻意复现；如需修正在 v1.7.0 统一处理。
            for path in asset_links.get(number, []):
                body += options.asset_link_separator + options.asset_link_template.format(page=number, path=path)
            if body:
                sections.append(f"{heading}{options.page_block_separator}{body}")
            elif options.include_empty_pages:
                sections.append(heading)
        return options.page_separator.join(sections), len(pages)
