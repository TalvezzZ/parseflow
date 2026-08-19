import pytest

from app.documents.models import FileInput, ParseContext
from app.skills.registry import create_default_registry


def test_default_registry_exposes_echo_skill() -> None:
    registry = create_default_registry()

    assert registry.list_manifests()[0]["name"] == "system.echo"
    assert registry.get("pdf.parse").manifest["name"] == "pdf.parse"
    assert registry.get("word.parse").manifest["name"] == "word.parse"


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
