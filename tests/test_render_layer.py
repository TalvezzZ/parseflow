import json

import pytest

from app.documents.models import DocumentBlock, DocumentPage, DocumentResult, FileInput
from app.render import RenderOptions, default_registry, render_representations
from app.render.registry import RenderRegistry
from app.render.text import is_generated_cjk_space, strip_generated_cjk_spaces


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


def test_cjk_generated_space_removal_is_opt_in():
    pages = [{"page_number": 1, "text": "中 文 字 and hello world 第 1 页"}]

    default = render_representations(_document(pages), kinds=["markdown"]).get("markdown")
    assert default is not None
    assert "中 文 字" in default.text

    cleaned = render_representations(
        _document(pages),
        kinds=["markdown"],
        options=RenderOptions(cjk_generated_space_removal=True),
    ).get("markdown")
    assert cleaned is not None
    assert "中文字" in cleaned.text
    assert "hello world" in cleaned.text  # 英文词间空格保留
    assert "第 1 页" in cleaned.text  # 数字相邻空格保留
    assert cleaned.meta["cjk_space_removed"] == 2


@pytest.mark.parametrize(
    ("left", "space", "right", "expected"),
    [
        ("中", " ", "文", True),
        ("あ", " ", "い", True),
        ("a", " ", "b", False),
        ("中", "  ", "文", False),
        ("中", "\t", "文", False),
        ("中", " ", "1", False),
        ("。", " ", "文", False),
    ],
)
def test_is_generated_cjk_space(left, space, right, expected):
    assert is_generated_cjk_space(left, space, right) is expected


def test_strip_generated_cjk_spaces_counts_removals():
    text, removed = strip_generated_cjk_spaces("这 是 测试，hello world")
    assert text == "这是测试，hello world"
    assert removed == 2


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
