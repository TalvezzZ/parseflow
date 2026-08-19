from pathlib import Path
from types import SimpleNamespace

import pytest

from app.documents.models import FileConversionRequest, ParseContext
from app.skills.office.adapters.libreoffice import LibreOfficeProvider
from app.skills.office.skill import OfficeConvertSkill
from app.skills.providers import Provider, ProviderManifest, ProviderRegistry, ProviderResult


def test_libreoffice_provider_builds_conversion_result(monkeypatch, tmp_path) -> None:
    source = tmp_path / "legacy.doc"
    source.write_bytes(b"fake-doc")

    def fake_run(command, **kwargs):
        out_dir = Path(command[command.index("--outdir") + 1])
        (out_dir / "legacy.docx").write_bytes(b"converted-docx")
        return SimpleNamespace(returncode=0, stdout="convert ok", stderr="")

    monkeypatch.setattr("app.skills.office.adapters.libreoffice.shutil.which", lambda command: "/usr/bin/soffice")
    monkeypatch.setattr("app.skills.office.adapters.libreoffice.subprocess.run", fake_run)

    context = FileConversionRequest(
        file_id="file-1",
        path=str(source),
        target_format="docx",
        output_dir=str(tmp_path / "output"),
    ).to_context()
    result = LibreOfficeProvider()._convert_sync(context)

    assert result.status == "success"
    assert result.data["conversion"]["target_format"] == "docx"
    assert Path(result.data["conversion"]["target_path"]).read_bytes() == b"converted-docx"


class FakeConversionProvider(Provider):
    manifest = ProviderManifest(name="office.convert.test", kind="file_converter", modes=["convert"])

    async def parse(self, context: ParseContext) -> ProviderResult:
        return ProviderResult(
            status="success",
            provider_name=self.manifest.name,
            data={"conversion": {"target_format": context.metadata["target_format"]}},
        )


@pytest.mark.asyncio
async def test_office_convert_skill_returns_provider_data(tmp_path) -> None:
    source = tmp_path / "demo.docx"
    source.write_bytes(b"fake-docx")
    context = FileConversionRequest(
        file_id="file-1",
        path=str(source),
        target_format="pdf",
    ).to_context()
    skill = OfficeConvertSkill(
        providers=ProviderRegistry([FakeConversionProvider()]),
        default_provider="office.convert.test",
    )

    result = await skill.execute(context)

    assert result.status == "success"
    assert result.data["conversion"]["target_format"] == "pdf"
