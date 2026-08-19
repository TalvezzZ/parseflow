import pytest

from app.documents.models import DocumentResult, FileInput, ParseContext, ProviderError
from app.skills.image import ImageParseSkill
from app.skills.providers import Provider, ProviderManifest, ProviderRegistry, ProviderResult


class FakeProvider(Provider):
    def __init__(self, name: str, mode: str = "ocr", status: str = "success") -> None:
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
        document = DocumentResult(document_id=context.file.file_id, source_file=context.file, document_type="image")
        return ProviderResult(status="success", provider_name=self.manifest.name, document=document)


def make_context() -> ParseContext:
    return ParseContext(file=FileInput(file_id="file-1", path="/tmp/demo.png"))


@pytest.mark.asyncio
async def test_image_skill_uses_configured_ocr_provider() -> None:
    provider = FakeProvider("image.ocr.test")
    skill = ImageParseSkill(ProviderRegistry([provider]), ocr_provider="image.ocr.test")

    result = await skill.execute(make_context())

    assert result.status == "success"
    assert provider.called == 1
    assert result.data["document"]["provenance"]["attempts"][0]["provider"] == "image.ocr.test"


@pytest.mark.asyncio
async def test_image_skill_honors_explicit_provider() -> None:
    default = FakeProvider("image.ocr.default")
    alternative = FakeProvider("image.ocr.alternative")
    context = make_context()
    context.options.provider = "image.ocr.alternative"
    skill = ImageParseSkill(ProviderRegistry([default, alternative]), ocr_provider="image.ocr.default")

    result = await skill.execute(context)

    assert result.status == "success"
    assert default.called == 0
    assert alternative.called == 1


@pytest.mark.asyncio
async def test_image_skill_without_provider_returns_setup_error() -> None:
    result = await ImageParseSkill(ProviderRegistry()).execute(make_context())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "ocr_not_configured"


@pytest.mark.asyncio
async def test_image_skill_rejects_provider_without_ocr_mode() -> None:
    provider = FakeProvider("image.other", mode="normal")

    result = await ImageParseSkill(ProviderRegistry([provider]), ocr_provider="image.other").execute(make_context())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "provider_unsupported"


@pytest.mark.asyncio
async def test_image_skill_propagates_provider_failure() -> None:
    provider = FakeProvider("image.ocr.test", status="failed")

    result = await ImageParseSkill(ProviderRegistry([provider]), ocr_provider="image.ocr.test").execute(make_context())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "test_failure"
    assert result.data["attempts"][0]["provider"] == "image.ocr.test"
