"""Excel Provider 的表示输出必须与 1.6.0 的既有拼装字节一致。"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from app.documents.models import FileInput, ParseContext
from app.skills.excel.adapters.openpyxl import OpenpyxlProvider


def _build_workbook(path: Path) -> None:
    workbook = Workbook()
    first = workbook.active
    first.title = "人员"
    first.append(["姓名", "年龄"])
    first.append(["张三", 30])
    second = workbook.create_sheet("产品")
    second.append(["名称", "单价"])
    second.append(["键盘", 199])
    workbook.create_sheet("空白表")  # 无内容 sheet
    workbook.save(path)


@pytest.mark.asyncio
async def test_excel_representations_match_legacy_layout(tmp_path: Path) -> None:
    source = tmp_path / "book.xlsx"
    _build_workbook(source)

    result = await OpenpyxlProvider().parse(
        ParseContext(
            file=FileInput(file_id="book-1", path=str(source)),
            metadata={"artifact_dir": str(tmp_path / "artifacts")},
        )
    )

    assert result.status == "success"
    document = result.document
    assert document is not None

    # 复现 1.6.0 拼装：每 sheet "## 名称" + 空行 + 每行 "- 行"；空 sheet 跳过。
    expected = (
        "## 人员\n\n- 姓名 | 年龄\n- 张三 | 30\n\n"
        "## 产品\n\n- 名称 | 单价\n- 键盘 | 199"
    )
    assert document.representations["markdown"] == expected
    assert "空白表" not in document.representations["markdown"]

    # 纯文本不分组、不加前缀，按块顺序用换行拼接。
    assert document.representations["plain_text"].split("\n")[:3] == ["人员", "姓名 | 年龄", "张三 | 30"]
    assert "## 人员" not in document.representations["plain_text"]