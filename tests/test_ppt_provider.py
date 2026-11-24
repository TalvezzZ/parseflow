"""PPT Provider 的表示输出必须与 1.6.0 的既有拼装字节一致。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pptx import Presentation
from pptx.util import Inches

from app.documents.models import FileInput, ParseContext
from app.skills.ppt.adapters.python_pptx import PythonPptxProvider


def _build_presentation(path: Path) -> None:
    presentation = Presentation()
    first = presentation.slides.add_slide(presentation.slide_layouts[6])  # 空白版式
    box = first.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    box.text_frame.text = "Hello"
    presentation.slides.add_slide(presentation.slide_layouts[6])  # 空幻灯片
    presentation.save(path)


@pytest.mark.asyncio
async def test_ppt_representations_match_legacy_layout(tmp_path: Path) -> None:
    source = tmp_path / "deck.pptx"
    _build_presentation(source)

    result = await PythonPptxProvider().parse(
        ParseContext(
            file=FileInput(file_id="deck-1", path=str(source)),
            metadata={"artifact_dir": str(tmp_path / "artifacts")},
        )
    )

    assert result.status == "success"
    document = result.document
    assert document is not None
    assert len(document.pages) == 2

    # 逐字节比对 1.6.0 的拼装方式：标题 "## Slide N" + 空行 + 页内块；空页保留分隔符。
    assert document.representations["markdown"] == "## Slide 1\n\nHello\n\n## Slide 2\n\n"
    # 纯文本不跳过空页，因此末尾保留分隔符。
    assert document.representations["plain_text"] == "Hello\n\n"
