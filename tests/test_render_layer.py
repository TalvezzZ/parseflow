import json

import pytest

from app.documents.models import DocumentBlock, DocumentPage, DocumentResult, FileInput
from app.render import RenderOptions, default_registry, render_representations
from app.render.registry import RenderRegistry


def _document(pages, representations=None, blocks=None) -> dict:
    return {
        "document_id": "doc-1",
        "document_type": "pdf",
        "pages": pages,
        "blocks": blocks or [],
        "representations": representations or {},
    }


def test_markdown_prefers_document_representation():
    """解析器产出的 markdown 是解析结果本身，必须原样复用。"""
    document = _document(
        pages=[{"page_number": 1, "text": "page text"}],
        representations={"markdown": "# 标题\n\n正文", "plain_text": "标题\n正文"},
    )
    artifact = render_representations(document, kinds=["markdown"]).get("markdown")

    assert artifact is not None
    assert artifact.renderer == "document"
    assert artifact.text == "# 标题\n\n正文"


def test_markdown_from_pages_matches_pymupdf_convention():
    """只有页面结构时按页模板生成，必须与 PyMuPDF provider 的既有输出一致。"""
    pages = [
        {"page_number": 1, "text": "第一页正文"},
        {"page_number": 2, "text": ""},
        {"page_number": 3, "text": "第三页正文"},
    ]
    expected = "\n\n---\n\n".join(
        f"## 第 {page['page_number']} 页\n\n{page['text']}" if page["text"] else f"## 第 {page['page_number']} 页"
        for page in pages
    )

    artifact = render_representations(_document(pages), kinds=["markdown"]).get("markdown")

    assert artifact is not None
    assert artifact.renderer == "pages"
    assert artifact.text == expected
    assert artifact.meta["pages"] == 3


def test_markdown_skips_empty_page_heading_when_disabled():
    pages = [{"page_number": 1, "text": ""}, {"page_number": 2, "text": "有内容"}]
    artifact = render_representations(
        _document(pages),
        kinds=["markdown"],
        options=RenderOptions(include_empty_pages=False),
    ).get("markdown")

    assert artifact is not None
    assert artifact.text == "## 第 2 页\n\n有内容"


def test_plain_text_joins_page_texts():
    pages = [{"page_number": 1, "text": "A"}, {"page_number": 2, "text": "B"}, {"page_number": 3, "text": ""}]
    artifact = render_representations(_document(pages), kinds=["plain_text"]).get("plain_text")

    assert artifact is not None
    assert artifact.text == "A\n\nB"
    assert artifact.renderer == "pages"


def test_json_matches_existing_export_encoding():
    document = _document(
        pages=[{"page_number": 1, "text": "中文"}],
        representations={"markdown": "# 中文"},
    )
    expected = "".join(json.JSONEncoder(ensure_ascii=False, indent=2).iterencode(document))

    artifact = render_representations(document, kinds=["json"]).get("json")

    assert artifact is not None
    assert artifact.text == expected
    assert "\\u4e2d" not in artifact.text


def test_unknown_kind_is_reported_not_silently_skipped():
    result = render_representations(_document([]), kinds=["markdown", "docx"])

    assert result.missing == ["docx"]
    assert [artifact.kind for artifact in result.artifacts] == ["markdown"]


def test_empty_document_warns_instead_of_silent_empty_string():
    artifact = render_representations(_document([]), kinds=["markdown"]).get("markdown")

    assert artifact is not None
    assert artifact.text == ""
    assert artifact.renderer == "empty"
    assert any(warning.startswith("markdown_empty") for warning in artifact.warnings)


def test_blocks_fallback_when_no_pages():
    document = _document([], blocks=[{"kind": "text", "text": "块一"}, {"kind": "text", "text": "块二"}])
    artifact = render_representations(document, kinds=["markdown"]).get("markdown")

    assert artifact is not None
    assert artifact.renderer == "blocks"
    assert artifact.text == "块一\n\n块二"


def test_document_result_model_is_accepted():
    document = DocumentResult(
        document_id="doc-2",
        source_file=FileInput(file_id="f-1", path="/tmp/x.pdf"),
        pages=[DocumentPage(page_number=1, text="模型输入", blocks=[DocumentBlock(kind="text", text="模型输入")])],
    )
    artifact = render_representations(document, kinds=["markdown"]).get("markdown")

    assert artifact is not None
    assert artifact.text == "## 第 1 页\n\n模型输入"


def test_duplicate_renderer_rejected():
    registry = default_registry()
    with pytest.raises(ValueError):
        registry.register(registry.get("markdown"))


def test_registry_lists_all_builtin_kinds():
    assert set(default_registry().available()) == {"markdown", "plain_text", "json"}


def test_render_returns_all_kinds_by_default():
    result = default_registry().render(_document([{"page_number": 1, "text": "x"}]))

    assert {artifact.kind for artifact in result.artifacts} == {"markdown", "plain_text", "json"}
    assert result.missing == []


def test_as_representations_maps_kind_to_text():
    result = render_representations(_document([{"page_number": 1, "text": "x"}]), kinds=["markdown"])
    assert result.as_representations() == {"markdown": "## 第 1 页\n\nx"}


def test_artifact_size_bytes_is_utf8_length():
    artifact = render_representations(_document([], representations={"markdown": "中文"}), kinds=["markdown"]).get(
        "markdown"
    )
    assert artifact is not None
    assert artifact.size_bytes == len("中文".encode("utf-8"))


def _paged_document(pages, images=None) -> dict:
    return {
        "document_id": "doc-assets",
        "document_type": "pdf",
        "pages": pages,
        "blocks": [],
        "representations": {},
        "images": images or [],
    }


def test_page_asset_links_follow_page_and_order():
    """图片引用按页归组，并保持 images 列表原顺序。"""
    document = _paged_document(
        pages=[{"page_number": 1, "text": "第一页"}, {"page_number": 2, "text": "第二页"}],
        images=[
            {"page_number": 1, "relative_path": "images/a.png"},
            {"page_number": 2, "relative_path": "images/b.png"},
            {"page_number": 1, "relative_path": "images/c.png"},
        ],
    )
    artifact = render_representations(
        document, kinds=["markdown"], options=RenderOptions(page_asset_links=True)
    ).get("markdown")

    assert artifact is not None
    assert artifact.text == (
        "## 第 1 页\n\n第一页\n\n![第 1 页图片](images/a.png)\n\n![第 1 页图片](images/c.png)"
        "\n\n---\n\n"
        "## 第 2 页\n\n第二页\n\n![第 2 页图片](images/b.png)"
    )


def test_page_asset_links_reproduce_empty_body_layout():
    """空正文 + 有图：复现 1.6.0 的前导分隔符（字节等价要求）。"""
    document = _paged_document(
        pages=[{"page_number": 1, "text": ""}],
        images=[{"page_number": 1, "relative_path": "images/only.png"}],
    )
    artifact = render_representations(
        document, kinds=["markdown"], options=RenderOptions(page_asset_links=True)
    ).get("markdown")

    assert artifact is not None
    assert artifact.text == "## 第 1 页\n\n\n\n![第 1 页图片](images/only.png)"


def test_page_asset_links_use_occurrences_when_page_number_absent():
    document = _paged_document(
        pages=[{"page_number": 1, "text": "正文"}],
        images=[{"occurrences": [{"page_number": 1, "bbox": [0, 0, 1, 1]}], "relative_path": "images/occ.png"}],
    )
    artifact = render_representations(
        document, kinds=["markdown"], options=RenderOptions(page_asset_links=True)
    ).get("markdown")

    assert artifact is not None
    assert artifact.text.endswith("![第 1 页图片](images/occ.png)")


def test_page_asset_links_skip_images_without_relative_path():
    """只有绝对路径的图片不得写入结果，且必须留下警告。"""
    document = _paged_document(
        pages=[{"page_number": 1, "text": "正文"}],
        images=[{"page_number": 1, "path": "/abs/secret.png"}],
    )
    artifact = render_representations(
        document, kinds=["markdown"], options=RenderOptions(page_asset_links=True)
    ).get("markdown")

    assert artifact is not None
    assert "/abs/secret.png" not in artifact.text
    assert any(warning.startswith("asset_link_skipped") for warning in artifact.warnings)


def test_page_asset_links_disabled_by_default():
    document = _paged_document(
        pages=[{"page_number": 1, "text": "正文"}],
        images=[{"page_number": 1, "relative_path": "images/a.png"}],
    )
    artifact = render_representations(document, kinds=["markdown"]).get("markdown")

    assert artifact is not None
    assert "![" not in artifact.text


def test_markdown_matches_paddle_legacy_layout():
    """与 PaddleOCR 1.6.0 的页尾图片引用拼装逐字节一致。"""
    pages = [
        {"page_number": 1, "text": "第一页正文"},
        {"page_number": 2, "text": ""},
        {"page_number": 3, "text": "第三页正文"},
    ]
    images = [
        {"page_number": 1, "relative_path": "images/p1-a.png"},
        {"page_number": 1, "relative_path": "images/p1-b.png"},
        {"page_number": 3, "relative_path": "images/p3-a.png"},
    ]

    by_page: dict[int, list[dict]] = {}
    for image in images:
        by_page.setdefault(image["page_number"], []).append(image)
    parts = []
    for page in pages:
        body = page["text"]
        for image in by_page.get(page["page_number"], []):
            body += f"\n\n![第 {page['page_number']} 页图片]({image['relative_path']})"
        parts.append(f"## 第 {page['page_number']} 页\n\n{body}" if body else f"## 第 {page['page_number']} 页")
    expected = "\n\n---\n\n".join(parts)

    artifact = render_representations(
        _paged_document(pages, images), kinds=["markdown"], options=RenderOptions(page_asset_links=True)
    ).get("markdown")

    assert artifact is not None
    assert artifact.text == expected
