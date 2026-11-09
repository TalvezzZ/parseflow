import pytest

from app.documents.models import FileInput, ParseContext
from app.skills.registry import create_default_registry


def test_default_registry_exposes_echo_skill() -> None:
    registry = create_default_registry()

    assert registry.list_manifests()[0]["name"] == "system.echo"
    assert registry.get("pdf.parse").manifest["name"] == "pdf.parse"
    assert registry.get("word.parse").manifest["name"] == "word.parse"
    assert registry.get("image.parse").manifest["name"] == "image.parse"
    pdf_skill = registry.get("pdf.parse")
    assert pdf_skill.normal_provider == "pdf.normal.pdf-inspector"
    assert {item["name"] for item in pdf_skill.providers.list_manifests()} >= {
        "pdf.normal.pdf-inspector",
        "pdf.normal.pymupdf",
    }


def test_pdf_normal_provider_setting_selects_pymupdf(monkeypatch) -> None:
    import app.skills.registry as registry_module
    from app.config import Settings

    settings = Settings(_env_file=None, pdf_normal_provider="pdf.normal.pymupdf", pdf_ocr_enabled=False)
    monkeypatch.setattr(registry_module, "get_settings", lambda: settings)

    registry = registry_module.create_default_registry()

    assert registry.get("pdf.parse").normal_provider == "pdf.normal.pymupdf"


@pytest.mark.asyncio
async def test_echo_skill_returns_unified_result() -> None:
    registry = create_default_registry()
    context = ParseContext(file=FileInput(file_id="file-1", path="/tmp/demo.pdf"))

    result = await registry.get("system.echo").execute(context)

    assert result.status == "success"
    assert result.data["file_id"] == "file-1"


def test_missing_skill_fails_loudly() -> None:
    registry = create_default_registry()

    with pytest.raises(KeyError, match="未找到 Skill"):
        registry.get("pdf.mineru")
