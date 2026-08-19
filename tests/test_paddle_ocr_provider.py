from pathlib import Path

import pytest

from app.documents.models import FileInput, ParseContext
from app.skills.pdf.adapters.paddle_ocr import PaddleOcrPdfProvider


class FakePaddleOcr:
    def predict(self, _: str):
        return [{
            "rec_texts": ["扫描件标题", "这是 OCR 提取的正文"],
            "rec_scores": [0.99, 0.95],
            "rec_boxes": [[0, 0, 100, 24], [0, 40, 240, 68]],
        }]


def fake_renderer(_: Path, __: int, ___: float, ____: int) -> list[tuple[int, Path]]:
    return [(1, Path("/tmp/page-0001.png"))]


@pytest.mark.asyncio
async def test_paddle_ocr_provider_normalizes_local_result(tmp_path: Path) -> None:
    source = tmp_path / "scanned.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    provider = PaddleOcrPdfProvider(ocr_factory=FakePaddleOcr, renderer=fake_renderer)

    result = await provider.parse(ParseContext(file=FileInput(file_id="file-1", path=str(source))))

    assert result.status == "success"
    assert result.document is not None
    assert result.document.representations["plain_text"] == "扫描件标题\n这是 OCR 提取的正文"
    assert result.document.representations["markdown"].startswith("## 第 1 页")
    assert result.document.blocks[0].bbox == [0.0, 0.0, 100.0, 24.0]
    assert result.document.blocks[0].metadata["confidence"] == 0.99


@pytest.mark.asyncio
async def test_paddle_ocr_provider_reports_missing_dependency(tmp_path: Path) -> None:
    source = tmp_path / "scanned.pdf"
    source.write_bytes(b"%PDF-1.4\n")

    def unavailable_factory():
        raise ImportError("paddleocr")

    result = await PaddleOcrPdfProvider(ocr_factory=unavailable_factory).parse(
        ParseContext(file=FileInput(file_id="file-1", path=str(source)))
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "provider_unavailable"
