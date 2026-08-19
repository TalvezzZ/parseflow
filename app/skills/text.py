import asyncio
import csv
from pathlib import Path
from typing import Any

from app.documents.models import DocumentBlock, DocumentResult, ParseContext, ProviderError, SkillResult
from app.skills.base import Skill, SkillManifest
from app.skills.providers import Provider, ProviderManifest, ProviderResult, ProviderRegistry


class PlainTextProvider(Provider):
    manifest = ProviderManifest(name="text.normal.stdlib", version="0.5.1", kind="text_parser", modes=["normal"],
                                capabilities=["text", "markdown", "html", "xml", "rtf", "csv", "tsv", "tables"])

    def __init__(self, max_file_size_mb: int = 20, max_table_rows: int = 100_000, max_table_columns: int = 1_000) -> None:
        self.max_file_size_bytes = max_file_size_mb * 1024 * 1024
        self.max_table_rows = max_table_rows
        self.max_table_columns = max_table_columns

    async def parse(self, context: ParseContext) -> ProviderResult:
        return await asyncio.to_thread(self._parse_sync, context)

    def _parse_sync(self, context: ParseContext) -> ProviderResult:
        source = Path(context.file.path)
        suffix = source.suffix.lower()
        if source.stat().st_size > self.max_file_size_bytes:
            return self._failed("file_size_exceeded", f"文本文件超过 {self.max_file_size_bytes // 1024 // 1024} MB 限制")
        try:
            raw = source.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            return self._failed("provider_failed", str(exc))
        if suffix in {".csv", ".tsv"}:
            return self._parse_delimited(context, raw, "\t" if suffix == ".tsv" else ",")
        if suffix in {".html", ".htm"}:
            return self._parse_html(context, raw)
        if suffix == ".xml":
            return self._parse_xml(context, raw)
        if suffix == ".rtf":
            return self._parse_rtf(context, raw)
        plain_text = self._markdown_to_text(raw) if suffix in {".md", ".markdown"} else raw
        block_kind = "heading" if suffix in {".md", ".markdown"} else "text"
        document = DocumentResult(document_id=context.file.file_id, source_file=context.file, document_type=suffix.lstrip("."),
                                  blocks=[DocumentBlock(kind=block_kind, text=plain_text)],
                                  representations={"plain_text": plain_text, "markdown": raw if suffix in {".md", ".markdown"} else plain_text},
                                  parser={"name": self.manifest.name, "version": self.manifest.version},
                                  metadata={"encoding": "utf-8", "size_bytes": source.stat().st_size})
        return ProviderResult(status="success", provider_name=self.manifest.name, document=document)

    def _parse_delimited(self, context: ParseContext, raw: str, delimiter: str) -> ProviderResult:
        reader = csv.reader(raw.splitlines(), delimiter=delimiter)
        rows: list[list[str]] = []
        truncated = False
        for row in reader:
            if len(rows) >= self.max_table_rows:
                truncated = True
                break
            rows.append(row[:self.max_table_columns])
            truncated = truncated or len(row) > self.max_table_columns
        markdown = self._table_markdown(rows)
        plain_text = "\n".join(" | ".join(row) for row in rows)
        blocks = [DocumentBlock(kind="table", text=plain_text)] if rows else []
        document = DocumentResult(document_id=context.file.file_id, source_file=context.file, document_type="tsv" if delimiter == "\t" else "csv",
                                  blocks=blocks, tables=[{"rows": rows, "delimiter": delimiter}] if rows else [],
                                  representations={"plain_text": plain_text, "markdown": markdown},
                                  parser={"name": self.manifest.name, "version": self.manifest.version},
                                  quality={"rows": len(rows), "truncated": str(truncated)})
        warning = ["text_table_truncated"] if truncated else []
        return ProviderResult(status="partial" if truncated else "success", provider_name=self.manifest.name, document=document, warnings=warning)

    def _parse_html(self, context: ParseContext, raw: str) -> ProviderResult:
        try:
            from bs4 import BeautifulSoup
            from markdownify import markdownify
        except ImportError:
            return self._failed("provider_unavailable", "未安装 HTML 解析依赖")
        soup = BeautifulSoup(raw, "html.parser")
        for element in soup(["script", "style", "noscript"]):
            element.decompose()
        plain_text = soup.get_text("\n", strip=True)
        links = [{"text": item.get_text(" ", strip=True), "href": item.get("href")} for item in soup.find_all("a", href=True)]
        markdown = markdownify(str(soup), heading_style="ATX")
        document = DocumentResult(document_id=context.file.file_id, source_file=context.file, document_type="html",
                                  blocks=[DocumentBlock(kind="text", text=plain_text)],
                                  representations={"plain_text": plain_text, "markdown": markdown},
                                  metadata={"links": links}, parser={"name": self.manifest.name, "version": self.manifest.version})
        return ProviderResult(status="success", provider_name=self.manifest.name, document=document)

    def _parse_xml(self, context: ParseContext, raw: str) -> ProviderResult:
        """使用 defusedxml 拒绝外部实体和实体展开风险。"""
        try:
            from defusedxml import ElementTree as SafeElementTree
            from xml.etree.ElementTree import ParseError
        except ImportError:
            return self._failed("provider_unavailable", "未安装 defusedxml")
        try:
            root = SafeElementTree.fromstring(raw)
        except ParseError as exc:
            return self._failed("xml_parse_failed", str(exc))
        except Exception as exc:
            return self._failed("xml_unsafe_or_invalid", str(exc))
        nodes: list[dict[str, Any]] = []
        lines: list[str] = []

        def visit(element, path: str) -> None:
            tag = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else str(element.tag)
            current_path = f"{path}/{tag}" if path else f"/{tag}"
            text = (element.text or "").strip()
            attributes = {key.rsplit("}", 1)[-1]: value for key, value in element.attrib.items()}
            if text or attributes:
                nodes.append({"path": current_path, "tag": tag, "text": text, "attributes": attributes})
                attribute_text = " ".join(f"{key}={value}" for key, value in attributes.items())
                lines.append(f"{current_path}: {text}" + (f" ({attribute_text})" if attribute_text else ""))
            for child in element:
                visit(child, current_path)

        visit(root, "")
        plain_text = "\n".join(lines)
        markdown = "# " + root.tag.rsplit("}", 1)[-1] + "\n\n" + "\n".join(f"- {line}" for line in lines)
        document = DocumentResult(document_id=context.file.file_id, source_file=context.file, document_type="xml",
                                  blocks=[DocumentBlock(kind="text", text=plain_text)] if plain_text else [],
                                  representations={"plain_text": plain_text, "markdown": markdown},
                                  metadata={"root_tag": root.tag.rsplit("}", 1)[-1], "node_count": len(nodes)},
                                  extensions={"xml": {"root_tag": root.tag.rsplit("}", 1)[-1], "nodes": nodes}},
                                  parser={"name": self.manifest.name, "version": self.manifest.version})
        if not nodes:
            return ProviderResult(status="failed", provider_name=self.manifest.name, document=document,
                                  error=ProviderError(code="quality_insufficient", message="XML 未提取到语义文本或属性", retryable=False))
        return ProviderResult(status="success", provider_name=self.manifest.name, document=document)

    def _parse_rtf(self, context: ParseContext, raw: str) -> ProviderResult:
        try:
            from striprtf.striprtf import rtf_to_text
        except ImportError:
            return self._failed("provider_unavailable", "未安装 striprtf")
        try:
            plain_text = rtf_to_text(raw).strip()
        except Exception as exc:
            return self._failed("rtf_parse_failed", str(exc))
        if not plain_text:
            return self._failed("quality_insufficient", "RTF 未提取到文本内容")
        document = DocumentResult(document_id=context.file.file_id, source_file=context.file, document_type="rtf",
                                  blocks=[DocumentBlock(kind="text", text=plain_text)],
                                  representations={"plain_text": plain_text, "markdown": plain_text},
                                  parser={"name": self.manifest.name, "version": self.manifest.version},
                                  metadata={"extraction_mode": "direct_text"})
        return ProviderResult(status="success", provider_name=self.manifest.name, document=document)

    @staticmethod
    def _markdown_to_text(value: str) -> str:
        try:
            from bs4 import BeautifulSoup
            from markdownify import markdownify
            return BeautifulSoup(markdownify(value), "html.parser").get_text("\n", strip=True) or value
        except ImportError:
            return value

    @staticmethod
    def _table_markdown(rows: list[list[str]]) -> str:
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        normalized = [row + [""] * (width - len(row)) for row in rows]
        lines = ["| " + " | ".join(normalized[0]) + " |"]
        if len(normalized) > 1:
            lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
            lines.extend("| " + " | ".join(row) + " |" for row in normalized[1:])
        return "\n".join(lines)

    def _failed(self, code: str, message: str) -> ProviderResult:
        return ProviderResult(status="failed", provider_name=self.manifest.name,
                              error=ProviderError(code=code, message=message, retryable=False))


class TextParseSkill(Skill):
    name = "text.parse"
    version = "0.5.1"
    suffixes = {".txt", ".md", ".markdown", ".csv", ".tsv", ".html", ".htm", ".xml", ".rtf"}

    def __init__(self, providers: ProviderRegistry, normal_provider: str = "text.normal.stdlib") -> None:
        self.providers = providers
        self.normal_provider = normal_provider

    @property
    def manifest(self) -> SkillManifest:
        return SkillManifest(name=self.name, version=self.version, kind="document_parser", input_types=sorted(self.suffixes),
                             capabilities=["text", "markdown", "tables", "links", "xml_structure", "rtf_text"], providers=[item["name"] for item in self.providers.list_manifests()])

    async def execute(self, context: ParseContext) -> SkillResult:
        if Path(context.file.path).suffix.lower() not in self.suffixes:
            return SkillResult(status="failed", skill_name=self.name,
                               error=ProviderError(code="unsupported_format", message="text.parse 不支持该文件格式", retryable=False))
        try:
            provider = self.providers.get(context.options.provider or self.normal_provider)
        except KeyError as exc:
            return SkillResult(status="failed", skill_name=self.name,
                               error=ProviderError(code="provider_not_found", message=str(exc), retryable=False))
        result = await provider.parse(context)
        return SkillResult(status=result.status, skill_name=self.name,
                           data={"document": result.document.model_dump()} if result.document else result.data,
                           warnings=result.warnings, metrics=result.metrics, error=result.error)
