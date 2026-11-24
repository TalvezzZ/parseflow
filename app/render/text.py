"""纯文本表示渲染与中文排版规则。

`is_generated_cjk_space` 的判据参考 PDF 文本抽取的实际情况：中日韩文本在排版上
不需要词间空格，抽取器却常插入空格。只在该空格夹在两个 CJK 字符之间时判定为
生成空格；真实空格、英文词间空格、标点相邻的空格一律保留。
"""

from __future__ import annotations

from typing import Any, Mapping

from app.render.contracts import DEFAULT_MEDIA_TYPES, RenderArtifact, RenderOptions

# 常见 CJK 区段：扩展 A、基本区、兼容表意、假名、谚文。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x2E80, 0x2EFF),
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xAC00, 0xD7AF),
)


def is_cjk_char(char: str) -> bool:
    if len(char) != 1:
        return False
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def is_generated_cjk_space(left: str, space: str, right: str) -> bool:
    """判断一个空格是否是抽取器在 CJK 字符之间生成的。"""
    return space == " " and len(left) == 1 and len(right) == 1 and is_cjk_char(left) and is_cjk_char(right)


def strip_generated_cjk_spaces(text: str) -> tuple[str, int]:
    """移除 CJK 之间的生成空格，返回 (新文本, 移除数量)。"""
    if " " not in text:
        return text, 0
    kept: list[str] = []
    removed = 0
    for index, char in enumerate(text):
        if char == " " and 0 < index < len(text) - 1 and is_generated_cjk_space(text[index - 1], char, text[index + 1]):
            removed += 1
            continue
        kept.append(char)
    return "".join(kept), removed


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
