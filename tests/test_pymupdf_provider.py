from __future__ import annotations

import io
from pathlib import Path

import pymupdf
from PIL import Image
import pytest

from app.documents.models import FileInput, ParseContext
from app.skills.pdf.adapters.pymupdf import PyMuPdfParserProvider


@pytest.mark.asyncio
async def test_pymupdf_provider_extracts_text_and_images_without_ocr(tmp_path: Path) -> None:
    image_stream = io.BytesIO()
    Image.new("RGB", (20, 10), (220, 40, 30)).save(image_stream, format="PNG")
    source = tmp_path / "illustrated.pdf"
    document = pymupdf.open()
    page = document.new_page(width=300, height=300)
    page.insert_text((20, 30), "Native PDF text")
    page.insert_image(pymupdf.Rect(20, 50, 120, 100), stream=image_stream.getvalue())
    document.save(source)
    document.close()

    artifact_dir = tmp_path / "artifacts"
    result = await PyMuPdfParserProvider().parse(
        ParseContext(
            file=FileInput(file_id="file-1", path=str(source)),
            metadata={"artifact_dir": str(artifact_dir)},
        )
    )

    assert result.status == "success"
    assert result.document is not None
    assert "Native PDF text" in result.document.representations["plain_text"]
    assert result.document.parser["name"] == "pdf.normal.pymupdf"
    assert result.metrics["images"] == 1
    assert result.metrics["image_occurrences"] == 1
    image = result.document.images[0]
    image_path = Path(image["path"])
    assert image_path.is_file() and image_path.read_bytes()
    assert image["mime_type"] == "image/png"
    assert image["occurrences"][0] == {"page_number": 1, "bbox": [20.0, 50.0, 120.0, 100.0]}
    assert any(block.kind == "image" and block.page_number == 1 for block in result.document.blocks)
    assert any(block.kind == "text" and "Native PDF text" in block.text for block in result.document.blocks)


@pytest.mark.asyncio
async def test_pymupdf_provider_extracts_more_than_200_distinct_images(tmp_path: Path) -> None:
    source = tmp_path / "many-images.pdf"
    document = pymupdf.open()
    page = document.new_page(width=250, height=250)
    for index in range(201):
        image_stream = io.BytesIO()
        Image.new("RGB", (3, 3), (index % 256, index // 256, 177)).save(image_stream, format="PNG")
        x = 5 + (index % 20) * 10
        y = 5 + (index // 20) * 10
        page.insert_image(pymupdf.Rect(x, y, x + 5, y + 5), stream=image_stream.getvalue())
    document.save(source)
    document.close()

    result = await PyMuPdfParserProvider().parse(
        ParseContext(
            file=FileInput(file_id="file-many-images", path=str(source)),
            metadata={"artifact_dir": str(tmp_path / "many-artifacts")},
        )
    )

    assert result.status == "success"
    assert result.document is not None
    assert len(result.document.images) == 201
    assert result.metrics["images"] == 201
    assert result.metrics["image_occurrences"] == 201


@pytest.mark.asyncio
async def test_pymupdf_representations_match_legacy_page_layout(tmp_path: Path) -> None:
    """渲染层接管后，逐页标题与纯文本拼接必须与 1.6.0 的表达式字节一致。"""
    source = tmp_path / "two-pages.pdf"
    document = pymupdf.open()
    first = document.new_page(width=300, height=300)
    first.insert_text((20, 30), "Page one text")
    document.new_page(width=300, height=300)  # 空白页：仍应输出页标题
    document.save(source)
    document.close()

    result = await PyMuPdfParserProvider().parse(
        ParseContext(
            file=FileInput(file_id="file-legacy", path=str(source)),
            metadata={"artifact_dir": str(tmp_path / "artifacts")},
        )
    )

    assert result.document is not None
    pages = result.document.pages
    expected_markdown = "\n\n---\n\n".join(
        f"## 第 {page.page_number} 页\n\n{page.text}" if page.text else f"## 第 {page.page_number} 页"
        for page in pages
    )
    expected_plain_text = "\n\n".join(page.text for page in pages if page.text)

    assert result.document.representations["markdown"] == expected_markdown
    assert result.document.representations["plain_text"] == expected_plain_text
    assert result.metrics["text_chars"] == len(expected_plain_text)
    assert result.document.quality["text_chars"] == len(expected_plain_text)
