from types import SimpleNamespace

import pytest

from app.documents.models import FileInput, ParseContext
from app.skills.pdf.adapters.inspector import PdfInspectorAdapter, PdfInspectorParserProvider


def fake_pdf_inspector() -> SimpleNamespace:
    return SimpleNamespace(
        detect_pdf=lambda path: SimpleNamespace(
            pdf_type="text_based",
            confidence=0.98,
            ocr_recommended=False,
            pages_needing_ocr=[],
            ocr_reasons_by_page=[],
        ),
        process_pdf=lambda path: SimpleNamespace(
            markdown="# Demo\n\nHello PDF",
            page_count=1,
            processing_time_ms=12,
            title="Demo",
        ),
        extract_pages_markdown=lambda path: SimpleNamespace(
            pages=[SimpleNamespace(page=0, markdown="# Demo\n\nHello PDF")]
        ),
    )


@pytest.mark.asyncio
async def test_pdf_inspector_adapter_detects_pdf(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "pdf_inspector", fake_pdf_inspector())

    result = await PdfInspectorAdapter().inspect(
        ParseContext(file=FileInput(file_id="file-1", path="/tmp/demo.pdf"))
    )

    assert result.pdf_type == "text_based"
    assert result.confidence == 0.98


@pytest.mark.asyncio
async def test_pdf_inspector_provider_returns_unified_document(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "pdf_inspector", fake_pdf_inspector())

    result = await PdfInspectorParserProvider().parse(
        ParseContext(file=FileInput(file_id="file-1", path="/tmp/demo.pdf"))
    )

    assert result.status == "success"
    assert result.document is not None
    assert result.document.pages[0].text == "# Demo\n\nHello PDF"
    assert result.document.parser["name"] == "pdf.normal.pdf-inspector"
