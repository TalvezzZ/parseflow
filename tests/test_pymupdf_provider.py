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
