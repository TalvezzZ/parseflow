import pytest

from app.documents.models import (
    DocumentResult,
    FileInput,
    ParseContext,
    PdfInspectionResult,
    ProviderError,
)
from app.skills.pdf.skill import PdfParseSkill
from app.skills.providers import PdfInspector, Provider, ProviderManifest, ProviderRegistry, ProviderResult


class FakeInspector(PdfInspector):
    def __init__(self, pdf_type: str) -> None:
        self.pdf_type = pdf_type

    async def inspect(self, context: ParseContext) -> PdfInspectionResult:
        return PdfInspectionResult(pdf_type=self.pdf_type, confidence=0.99)


class FakeProvider(Provider):
    def __init__(self, name: str, mode: str, status: str = "success") -> None:
        self.manifest = ProviderManifest(name=name, kind="test", modes=[mode])
        self.status = status
        self.called = 0

    async def parse(self, context: ParseContext) -> ProviderResult:
        self.called += 1
        if self.status == "failed":
            return ProviderResult(
                status="failed",
                provider_name=self.manifest.name,
                error=ProviderError(code="test_failure", message="模拟失败"),
            )
        document = DocumentResult(document_id=context.file.file_id, source_file=context.file)
        return ProviderResult(status="success", provider_name=self.manifest.name, document=document)


def make_context() -> ParseContext:
    return ParseContext(file=FileInput(file_id="file-1", path="/tmp/demo.pdf"))


@pytest.mark.asyncio
async def test_text_pdf_uses_normal_provider() -> None:
    normal = FakeProvider("normal.test", "normal")
    ocr = FakeProvider("ocr.test", "ocr")
    skill = PdfParseSkill(
        inspector=FakeInspector("text_based"),
        providers=ProviderRegistry([normal, ocr]),
        normal_provider="normal.test",
    )

    result = await skill.execute(make_context())

    assert result.status == "success"
    assert normal.called == 1
    assert ocr.called == 0
    assert result.data["inspection"]["pdf_type"] == "text_based"


@pytest.mark.asyncio
async def test_scanned_pdf_returns_explicit_ocr_not_supported() -> None:
    normal = FakeProvider("normal.test", "normal")
    ocr = FakeProvider("ocr.test", "ocr")
    skill = PdfParseSkill(
        inspector=FakeInspector("scanned"),
        providers=ProviderRegistry([normal, ocr]),
        normal_provider="normal.test",
    )

    result = await skill.execute(make_context())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "ocr_not_supported"
    assert normal.called == 0
    assert ocr.called == 0


@pytest.mark.asyncio
async def test_failed_primary_provider_falls_back() -> None:
    primary = FakeProvider("normal.primary", "normal", status="failed")
    fallback = FakeProvider("normal.fallback", "normal")
    skill = PdfParseSkill(
        inspector=FakeInspector("text_based"),
        providers=ProviderRegistry([primary, fallback]),
        normal_provider="normal.primary",
        fallback_providers=["normal.fallback"],
    )

    result = await skill.execute(make_context())

    assert result.status == "success"
    assert primary.called == 1
    assert fallback.called == 1
    assert result.data["document"]["provenance"]["attempts"][0]["provider"] == "normal.primary"


@pytest.mark.asyncio
async def test_explicit_provider_can_switch_implementation() -> None:
    normal = FakeProvider("normal.test", "normal")
    alternative = FakeProvider("normal.alternative", "normal")
    context = make_context()
    context.options.provider = "normal.alternative"
    skill = PdfParseSkill(
        inspector=FakeInspector("text_based"),
        providers=ProviderRegistry([normal, alternative]),
        normal_provider="normal.test",
    )

    result = await skill.execute(context)

    assert result.status == "success"
    assert normal.called == 0
    assert alternative.called == 1
