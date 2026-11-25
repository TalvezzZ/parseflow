"""中英 README 的结构一致性。

README.md 是默认入口，README_zh-CN.md 是中文版。两份手工维护，必须有机械校验，
否则英文版改了章节、中文版忘记跟，读者会看到互相矛盾的内容。

这里校验的是**结构**（章节存在性、顺序、数量），不是译文质量。
新增或重命名章节时，同步更新下面的映射表即可。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EN = ROOT / "README.md"
ZH = ROOT / "README_zh-CN.md"

# 英文标题 -> 中文标题。顺序即期望顺序。
SECTION_MAP: dict[int, dict[str, str]] = {
    2: {
        "Highlights": "核心特性",
        "Supported formats": "支持的格式",
        "Prerequisites": "前置条件",
        "Quick start": "快速开始",
        "Docker deployment": "Docker 部署",
        "Configuration": "配置",
        "Use the API": "使用 API",
        "Development": "开发",
        "Version roadmap": "版本路线图",
        "Architecture": "架构",
        "v0.8.1 security and deployment": "v0.8.1 安全与部署",
        "Current limitations": "当前限制",
        "License": "许可证",
    },
    3: {
        "Video and audio stages": "视频与音频阶段",
        "Unified result-file storage (1.6.0)": "统一结果文件存储（1.6.0）",
        "Large Excel workbooks": "大型 Excel 工作簿",
        "Choose the standard PDF parser": "选择标准 PDF 解析器",
        "Required": "必需",
        "Optional capabilities": "可选能力",
        "Enable local OCR for scanned PDFs and images": "为扫描 PDF 与图片启用本地 OCR",
        "MCP server": "MCP 服务",
    },
    4: {
        "Local stdio MCP": "本地 stdio MCP",
        "Docker / remote Streamable HTTP MCP": "Docker / 远程 Streamable HTTP MCP",
    },
}


def _read(path: Path) -> str:
    assert path.is_file(), f"缺少 {path.name}"
    return path.read_text(encoding="utf-8")


def _headings(text: str, level: int) -> list[str]:
    """按出现顺序取出指定层级的标题文本（忽略代码块内的 # 行）。"""
    pattern = re.compile(rf"^{'#' * level} (.+)$", re.MULTILINE)
    headings: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = pattern.match(line)
        if match:
            headings.append(match.group(1).strip())
    return headings


@pytest.mark.parametrize("level", [2, 3, 4])
def test_headings_match_mapping_in_order(level: int) -> None:
    """两种语言的同层级标题必须按同一顺序出现，且数量相等。"""
    expected = SECTION_MAP[level]
    en = _headings(_read(EN), level)
    zh = _headings(_read(ZH), level)

    assert len(en) == len(zh), (
        f"H{level} 标题数量不一致：英文 {len(en)} 个，中文 {len(zh)} 个。\n"
        f"英文：{en}\n中文：{zh}\n请同步 README_zh-CN.md 与 SECTION_MAP。"
    )
    for index, (english, chinese) in enumerate(zip(en, zh)):
        assert english in expected, (
            f"README.md 第 {index + 1} 个 H{level} 标题 {english!r} 未登记在 SECTION_MAP，"
            f"请在 tests/test_readme_parity.py 中补上其中文标题。"
        )
        assert expected[english] == chinese, (
            f"H{level} 第 {index + 1} 个标题不匹配：英文 {english!r} 期望中文 "
            f"{expected[english]!r}，实际 {chinese!r}。"
        )


@pytest.mark.parametrize("level", [2, 3, 4])
def test_mapping_has_no_stale_entries(level: int) -> None:
    """映射表不能残留已从英文版删除的章节，否则它会掩盖真实差异。"""
    expected = SECTION_MAP[level]
    en = set(_headings(_read(EN), level))

    stale = sorted(set(expected) - en)
    assert not stale, (
        f"SECTION_MAP 的 H{level} 条目在 README.md 中已不存在：{stale}。"
        f"请删除这些条目，避免映射表与实际文档脱节。"
    )


def test_code_block_counts_match() -> None:
    """代码块数量一致，避免某一版漏掉示例。"""
    en = _read(EN).count("```")
    zh = _read(ZH).count("```")

    assert en == zh, f"代码围栏数量不一致：英文 {en}，中文 {zh}"
    assert en % 2 == 0, "代码围栏必须成对出现"


def test_table_rows_match() -> None:
    en = sum(1 for line in _read(EN).splitlines() if line.startswith("| "))
    zh = sum(1 for line in _read(ZH).splitlines() if line.startswith("| "))

    assert en == zh, f"表格行数量不一致：英文 {en}，中文 {zh}"


def test_language_switches_point_at_each_other() -> None:
    assert "[简体中文](README_zh-CN.md)" in _read(EN), "英文版缺少指向中文版的链接"
    assert "[English](README.md)" in _read(ZH), "中文版缺少指向英文版的链接"


def test_relative_links_resolve() -> None:
    """两版的相对链接都必须指向真实文件，避免文档漂移。"""
    broken: list[str] = []
    for path in (EN, ZH):
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", _read(path)):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            relative = target.split("#")[0]
            if relative and not (ROOT / relative).exists():
                broken.append(f"{path.name} -> {target}")
    assert not broken, "存在断链：\n" + "\n".join(broken)
