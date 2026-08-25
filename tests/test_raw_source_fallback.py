from pathlib import Path

import pytest

from app.config import Settings
from app.documents.models import FileInput, ParseContext, ParseOptions
from app.planning.rule_planner import RuleBasedPlanner
from app.skills.providers import ProviderRegistry
from app.skills.registry import SkillRegistry
from app.skills.text import PlainTextProvider, RawSourceProvider, TextParseSkill
from app.skills.text_formats import RAW_SOURCE_SUFFIXES, TEXT_SUFFIXES


def _skill(max_file_size_mb: int = 20) -> TextParseSkill:
    return TextParseSkill(ProviderRegistry([
        PlainTextProvider(max_file_size_mb=max_file_size_mb),
        RawSourceProvider(max_file_size_mb=max_file_size_mb),
    ]))


def _context(path: Path) -> ParseContext:
    return ParseContext(file=FileInput(file_id="file_raw", path=str(path), filename=path.name))


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", sorted(RAW_SOURCE_SUFFIXES))
async def test_raw_source_suffixes_return_original_content(tmp_path: Path, suffix: str) -> None:
    source = tmp_path / f"sample{suffix}"
    content = "第一行\n<source>&原文\n"
    source.write_text(content, encoding="utf-8")

    result = await _skill().execute(_context(source))

    assert result.status == "success"
    document = result.data["document"]
    assert document["representations"]["plain_text"] == content
    assert document["metadata"]["extraction_mode"] == "raw_source"
    assert document["parser"]["name"] == "text.raw.source"
    assert document["representations"]["markdown"].startswith("```")
    assert document["representations"]["markdown"] != content


@pytest.mark.asyncio
async def test_raw_source_strips_utf8_bom_and_replaces_invalid_bytes(tmp_path: Path) -> None:
    source = tmp_path / "sample.log"
    source.write_bytes(b"\xef\xbb\xbfhello\xff")

    result = await _skill().execute(_context(source))

    assert result.status == "success"
    assert result.data["document"]["representations"]["plain_text"] == "hello\ufffd"


@pytest.mark.asyncio
async def test_raw_source_rejects_binary_content(tmp_path: Path) -> None:
    source = tmp_path / "sample.log"
    source.write_bytes(b"text\x00binary")

    result = await _skill().execute(_context(source))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "binary_content_rejected"


@pytest.mark.asyncio
async def test_normal_text_parser_rejects_binary_content(tmp_path: Path) -> None:
    source = tmp_path / "sample.txt"
    source.write_bytes(b"plain\x00binary")

    result = await _skill().execute(_context(source))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "binary_content_rejected"


@pytest.mark.asyncio
async def test_raw_source_enforces_file_size_limit(tmp_path: Path) -> None:
    source = tmp_path / "sample.log"
    source.write_bytes(b"x" * (1024 * 1024 + 1))

    result = await _skill(max_file_size_mb=1).execute(_context(source))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "file_size_exceeded"


@pytest.mark.asyncio
async def test_specialized_html_parser_takes_precedence_and_emits_sanitized_html(tmp_path: Path) -> None:
    source = tmp_path / "sample.html"
    source.write_text('<main onclick="alert(1)" style="background:url(https://example.test/x)"><h1>Hello</h1><img src="https://example.test/pixel"><script>alert(1)</script></main>', encoding="utf-8")

    result = await _skill().execute(_context(source))

    assert result.status == "success"
    document = result.data["document"]
    assert document["parser"]["name"] == "text.normal.stdlib"
    assert "Hello" in document["representations"]["html"]
    assert "script" not in document["representations"]["html"]
    assert "onclick" not in document["representations"]["html"]
    assert "style=" not in document["representations"]["html"]
    assert "src=" not in document["representations"]["html"]


@pytest.mark.asyncio
async def test_malformed_xml_falls_back_but_unsafe_xml_does_not(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.xml"
    malformed.write_text("<root><broken></root>", encoding="utf-8")
    malformed_result = await _skill().execute(_context(malformed))
    assert malformed_result.status == "success"
    assert malformed_result.data["document"]["metadata"]["extraction_mode"] == "raw_source_fallback"

    unsafe = tmp_path / "unsafe.xml"
    unsafe.write_text('<!DOCTYPE root [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><root>&xxe;</root>', encoding="utf-8")
    unsafe_result = await _skill().execute(_context(unsafe))
    assert unsafe_result.status == "failed"
    assert unsafe_result.error is not None
    assert unsafe_result.error.code == "xml_unsafe_or_invalid"


@pytest.mark.asyncio
async def test_raw_provider_cannot_bypass_specialized_xml_parser(tmp_path: Path) -> None:
    source = tmp_path / "sample.xml"
    source.write_text("<root />", encoding="utf-8")
    context = _context(source)
    context.options = ParseOptions(provider="text.raw.source")

    result = await _skill().execute(context)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "provider_not_allowed"


@pytest.mark.asyncio
async def test_xml_depth_budget_is_not_bypassed_by_fallback(tmp_path: Path) -> None:
    source = tmp_path / "deep.xml"
    source.write_text("<n>" * 6 + "value" + "</n>" * 6, encoding="utf-8")
    skill = TextParseSkill(ProviderRegistry([
        PlainTextProvider(max_xml_depth=5),
        RawSourceProvider(),
    ]))

    result = await skill.execute(_context(source))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "xml_resource_limit"


def test_text_suffixes_are_allowed_and_automatically_routed() -> None:
    allowed = {item.strip() for item in Settings().file_allowed_suffixes.split(",")}
    assert TEXT_SUFFIXES <= allowed

    skill = _skill()
    planner = RuleBasedPlanner(SkillRegistry([skill]))
    for suffix in TEXT_SUFFIXES - {".rtf"}:
        assert suffix in planner.routes
        assert planner.create(f"sample{suffix}").steps[0].skill_name == "text.parse"
    assert planner.routes[".rtf"][0] == "rtf.parse"
