import io
import sys
from types import SimpleNamespace

import pytest

from app.documents.models import FileInput, ParseContext
from app.skills.word.adapters.mammoth import MammothProvider


class FakeImage:
    content_type = "image/png"
    description = "logo"

    def open(self):
        return io.BytesIO(b"fake-png")


def fake_mammoth_module():
    def convert_to_html(docx_file, convert_image):
        image = convert_image(FakeImage())
        html = (
            "<h1>产品说明</h1>"
            "<p>这是正文。</p>"
            "<ul><li>第一项</li></ul>"
            "<table><tr><th>名称</th><th>数量</th></tr>"
            "<tr><td>产品 A</td><td>2</td></tr></table>"
            f"<p><img src=\"{image['src']}\" alt=\"{image['alt']}\"></p>"
        )
        return SimpleNamespace(value=html, messages=[])

    return SimpleNamespace(
        convert_to_html=convert_to_html,
        images=SimpleNamespace(img_element=lambda callback: callback),
    )


@pytest.mark.asyncio
async def test_mammoth_provider_returns_unified_docx_result(monkeypatch, tmp_path) -> None:
    monkeypatch.setitem(sys.modules, "mammoth", fake_mammoth_module())
    docx_path = tmp_path / "demo.docx"
    docx_path.write_bytes(b"fake-docx")

    context = ParseContext(
        file=FileInput(file_id="file-1", path=str(docx_path)),
        metadata={"artifact_dir": str(tmp_path / "artifacts")},
    )
    result = await MammothProvider().parse(context)

    assert result.status == "success"
    assert result.document is not None
    assert result.document.representations["markdown"]
    assert result.document.blocks[0].kind == "heading"
    assert result.document.tables[0]["rows"][1] == ["产品 A", "2"]
    assert result.document.images[0]["filename"] == "image-0001.png"
    assert (tmp_path / "artifacts" / "images" / "image-0001.png").read_bytes() == b"fake-png"
