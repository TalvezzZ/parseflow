from pathlib import Path

import pytest
from PIL import Image

from app.documents.models import FileInput, ParseContext
from app.skills.image_ocr import PaddleOcrImageProvider


class FakePaddleOcr:
    def predict(self, _: str):
        return [{
            "rec_texts": ["图片标题", "这是 OCR 提取的正文"],
            "rec_scores": [0.99, 0.95],
            "rec_boxes": [[0, 0, 100, 24], [0, 40, 240, 68]],
        }]


class EmptyPaddleOcr:
    def predict(self, _: str):
        return [{"rec_texts": [], "rec_scores": [], "rec_boxes": []}]


def make_image(tmp_path: Path, name: str = "sample.png", size: tuple[int, int] = (100, 80)) -> Path:
    source = tmp_path / name
    Image.new("RGB", size, "white").save(source)
    return source


@pytest.mark.asyncio
async def test_paddle_ocr_image_provider_normalizes_local_result(tmp_path: Path) -> None:
    source = make_image(tmp_path)
    provider = PaddleOcrImageProvider(ocr_factory=FakePaddleOcr)

    result = await provider.parse(ParseContext(file=FileInput(file_id="file-1", path=str(source), mime_type="image/png")))

    assert result.status == "success"
    assert result.document is not None
    assert result.document.document_type == "image"
    assert result.document.pages[0].page_number == 1
    assert result.document.representations["plain_text"] == "图片标题\n这是 OCR 提取的正文"
    assert result.document.representations["markdown"] == "图片标题\n这是 OCR 提取的正文"
    assert result.document.blocks[0].bbox == [0.0, 0.0, 100.0, 24.0]
    assert result.document.blocks[0].metadata["confidence"] == 0.99
    assert result.document.metadata == {"width": 100, "height": 80, "mime_type": "image/png"}
    assert result.document.images == []


@pytest.mark.asyncio
async def test_paddle_ocr_image_provider_rejects_oversized_image(tmp_path: Path) -> None:
    source = make_image(tmp_path, size=(4, 4))

    result = await PaddleOcrImageProvider(max_pixels=15, ocr_factory=FakePaddleOcr).parse(
        ParseContext(file=FileInput(file_id="file-1", path=str(source)))
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "ocr_input_limit_exceeded"


@pytest.mark.asyncio
async def test_paddle_ocr_image_provider_reports_missing_dependency(tmp_path: Path) -> None:
    source = make_image(tmp_path)

    def unavailable_factory():
        raise ImportError("paddleocr")

    result = await PaddleOcrImageProvider(ocr_factory=unavailable_factory).parse(
        ParseContext(file=FileInput(file_id="file-1", path=str(source)))
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "provider_unavailable"


@pytest.mark.asyncio
async def test_paddle_ocr_image_provider_reports_empty_result(tmp_path: Path) -> None:
    source = make_image(tmp_path)

    result = await PaddleOcrImageProvider(ocr_factory=EmptyPaddleOcr).parse(
        ParseContext(file=FileInput(file_id="file-1", path=str(source)))
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "quality_insufficient"


@pytest.mark.asyncio
async def test_paddle_ocr_image_provider_reports_missing_source() -> None:
    result = await PaddleOcrImageProvider(ocr_factory=FakePaddleOcr).parse(
        ParseContext(file=FileInput(file_id="file-1", path="/tmp/no-such-image.png"))
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "source_not_found"
