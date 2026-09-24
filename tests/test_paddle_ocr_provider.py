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
async def test_paddle_ocr_provider_publishes_per_page_previews(tmp_path: Path) -> None:
    """Scanned PDFs are document_type='pdf' but must still expose per-page proofing data."""

    from PIL import Image

    source = tmp_path / "scanned.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    rendered = tmp_path / "rendered"
    rendered.mkdir()
    page_paths = []
    for number in (1, 2):
        page = rendered / f"page-{number}.png"
        Image.new("RGB", (120 + number, 90 + number), "white").save(page)
        page_paths.append((number, page))

    def renderer(_: Path, __: int, ___: float, ____: int):
        return page_paths

    artifact_dir = tmp_path / "artifacts" / "task-1" / "files"
    artifact_dir.mkdir(parents=True)
    context = ParseContext(file=FileInput(file_id="file-1", path=str(source)))
    context.metadata["artifact_dir"] = str(artifact_dir)

    result = await PaddleOcrPdfProvider(ocr_factory=FakePaddleOcr, renderer=renderer).parse(context)

    assert result.status == "success"
    assert result.document.document_type == "pdf"
    assert [page.page_number for page in result.document.pages] == [1, 2]
    assert [(page.width, page.height) for page in result.document.pages] == [(121, 91), (122, 92)]
    assert result.document.pages[0].preview == {"filename": "ocr-page-0001.png", "content_type": "image/png"}
    assert result.document.pages[1].preview == {"filename": "ocr-page-0002.png", "content_type": "image/png"}
    for page in result.document.pages:
        assert "relative_path" not in page.preview
    assert (artifact_dir / "ocr-pages" / "ocr-page-0001.png").is_file()
    assert (artifact_dir / "ocr-pages" / "ocr-page-0002.png").is_file()


@pytest.mark.asyncio
async def test_paddle_ocr_provider_enforces_renderer_limits(tmp_path: Path) -> None:
    source = tmp_path / "scanned.pdf"
    source.write_bytes(b"%PDF-1.4\n")

    def over_limit_renderer(_: Path, __: int, ___: float, ____: int):
        raise ValueError("PDF 页数 51 超过 OCR 上限 50")

    result = await PaddleOcrPdfProvider(ocr_factory=FakePaddleOcr, renderer=over_limit_renderer).parse(
        ParseContext(file=FileInput(file_id="file-1", path=str(source)))
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "ocr_input_limit_exceeded"


@pytest.mark.asyncio
async def test_paddle_ocr_provider_maps_empty_text_to_quality_failure(tmp_path: Path) -> None:
    source = tmp_path / "scanned.pdf"
    source.write_bytes(b"%PDF-1.4\n")

    class EmptyOcr:
        def predict(self, _: str):
            return [{"rec_texts": [], "rec_scores": [], "rec_boxes": []}]

    result = await PaddleOcrPdfProvider(ocr_factory=EmptyOcr, renderer=fake_renderer).parse(
        ParseContext(file=FileInput(file_id="file-1", path=str(source)))
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "quality_insufficient"


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
