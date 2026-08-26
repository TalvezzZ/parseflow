import asyncio
import csv
from html import escape
from pathlib import Path
from typing import Any

from app.documents.models import DocumentBlock, DocumentResult, ParseContext, ProviderError, SkillResult
from app.skills.base import Skill, SkillManifest
from app.skills.providers import Provider, ProviderManifest, ProviderResult, ProviderRegistry
from app.skills.text_formats import RAW_SOURCE_SUFFIXES, TEXT_SUFFIXES


NON_FALLBACK_ERROR_CODES = {
    "file_size_exceeded",
    "binary_content_rejected",
    "path_not_found",
    "permission_denied",
    "xml_unsafe_or_invalid",
    "xml_resource_limit",
}


def source_markdown(raw: str, language: str = "text") -> str:
    """Wrap source in a fence that cannot be closed by its own contents."""
    current = maximum = 0
    for character in raw:
        current = current + 1 if character == "`" else 0
        maximum = max(maximum, current)
    fence = "`" * max(3, maximum + 1)
    return f"{fence}{language}\n{raw}\n{fence}" if raw else ""


class TextReadError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def looks_binary(payload: bytes) -> bool:
    if not payload:
        return False
    if b"\x00" in payload:
        return True
    sample = payload[:8192]
    control_bytes = sum(byte < 32 and byte not in {9, 10, 12, 13} for byte in sample)
    return control_bytes / len(sample) > 0.05


def read_text_source(source: Path, max_bytes: int, reject_binary: bool = True) -> tuple[str, int]:
    try:
        with source.open("rb") as stream:
            payload = stream.read(max_bytes + 1)
    except FileNotFoundError as exc:
        raise TextReadError("path_not_found", f"文件不存在: {source}") from exc
    except PermissionError as exc:
        raise TextReadError("permission_denied", f"无法读取文件: {source}") from exc
    except OSError as exc:
        raise TextReadError("provider_failed", str(exc)) from exc
    if len(payload) > max_bytes:
        raise TextReadError("file_size_exceeded", f"文本文件超过 {max_bytes // 1024 // 1024} MB 限制")
    if reject_binary and looks_binary(payload):
        raise TextReadError("binary_content_rejected", "文件包含二进制内容，不能使用文本解析")
    return payload.decode("utf-8-sig", errors="replace"), len(payload)


class PlainTextProvider(Provider):
    manifest = ProviderManifest(name="text.normal.stdlib", version="0.8.0", kind="text_parser", modes=["normal"],
                                capabilities=["text", "markdown", "html", "xml", "rtf", "csv", "tsv", "tables"])

    def __init__(self, max_file_size_mb: int = 20, max_table_rows: int = 100_000, max_table_columns: int = 1_000,
                 max_xml_nodes: int = 100_000, max_xml_depth: int = 128) -> None:
        self.max_file_size_bytes = max_file_size_mb * 1024 * 1024
        self.max_table_rows = max_table_rows
        self.max_table_columns = max_table_columns
        self.max_xml_nodes = max_xml_nodes
        self.max_xml_depth = max_xml_depth

    async def parse(self, context: ParseContext) -> ProviderResult:
        return await asyncio.to_thread(self._parse_sync, context)

    def _parse_sync(self, context: ParseContext) -> ProviderResult:
        source = Path(context.file.path)
        suffix = source.suffix.lower()
        try:
            raw, size_bytes = read_text_source(source, self.max_file_size_bytes)
        except TextReadError as exc:
            return self._failed(exc.code, str(exc))
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
                                  metadata={"encoding": "utf-8", "size_bytes": size_bytes})
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
        for element in soup(["script", "style", "noscript", "object", "embed", "iframe", "base", "meta", "link", "form", "input", "button", "textarea", "select", "svg", "math"]):
            element.decompose()
        for element in soup.find_all(True):
            for attribute in list(element.attrs):
                name = attribute.lower()
                value = str(element.attrs.get(attribute, "")).strip().lower()
                if name.startswith("on") or name in {"style", "src", "srcset", "srcdoc"} or (name == "href" and value.startswith(("javascript:", "data:"))):
                    del element.attrs[attribute]
        plain_text = soup.get_text("\n", strip=True)
        links = [{"text": item.get_text(" ", strip=True), "href": item.get("href")} for item in soup.find_all("a", href=True)]
        sanitized_html = str(soup)
        markdown = markdownify(sanitized_html, heading_style="ATX")
        document = DocumentResult(document_id=context.file.file_id, source_file=context.file, document_type="html",
                                  blocks=[DocumentBlock(kind="text", text=plain_text)] if plain_text else [],
                                  representations={"plain_text": plain_text, "markdown": markdown, "html": sanitized_html},
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
        stack: list[tuple[Any, str, int]] = [(root, "", 1)]
        visited = 0
        while stack:
            element, path, depth = stack.pop()
            visited += 1
            if visited > self.max_xml_nodes or depth > self.max_xml_depth:
                return self._failed("xml_resource_limit", "XML 节点数量或嵌套深度超过限制")
            tag = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else str(element.tag)
            current_path = f"{path}/{tag}" if path else f"/{tag}"
            text = (element.text or "").strip()
            attributes = {key.rsplit("}", 1)[-1]: value for key, value in element.attrib.items()}
            if text or attributes:
                nodes.append({"path": current_path, "tag": tag, "text": text, "attributes": attributes})
                attribute_text = " ".join(f"{key}={value}" for key, value in attributes.items())
                lines.append(f"{current_path}: {text}" + (f" ({attribute_text})" if attribute_text else ""))
            stack.extend((child, current_path, depth + 1) for child in reversed(list(element)))
        root_tag = root.tag.rsplit("}", 1)[-1]
        markdown = source_markdown(raw, "xml")
        document = DocumentResult(document_id=context.file.file_id, source_file=context.file, document_type="xml",
                                  blocks=[DocumentBlock(kind="text", text=raw)] if raw else [],
                                  representations={"plain_text": raw, "markdown": markdown, "html": self._xml_to_html(root)},
                                  metadata={"root_tag": root_tag, "node_count": len(nodes), "extraction_mode": "raw_source"},
                                  extensions={"xml": {"root_tag": root_tag, "nodes": nodes, "semantic_lines": lines}},
                                  parser={"name": self.manifest.name, "version": self.manifest.version})
        return ProviderResult(status="success", provider_name=self.manifest.name, document=document)

    @staticmethod
    def _xml_to_html(root: Any) -> str:
        """Render a safe, readable HTML tree without embedding untrusted XML."""
        def render(element: Any) -> str:
            tag = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else str(element.tag)
            attributes = " ".join(f'<span class="xml-attr">{escape(str(key).rsplit("}", 1)[-1])}={escape(str(value))}</span>' for key, value in element.attrib.items())
            text = escape((element.text or "").strip())
            children = "".join(render(child) for child in element)
            return f'<li><code>&lt;{escape(tag)}&gt;</code>{(" " + attributes) if attributes else ""}{("<p>" + text + "</p>") if text else ""}{("<ul>" + children + "</ul>") if children else ""}</li>'
        return '<!doctype html><html><head><meta charset="utf-8"><style>body{font:14px system-ui;padding:16px;color:#173f50}ul{margin:6px 0;padding-left:22px}li{margin:6px 0}code{color:#08779b}.xml-attr{margin-left:7px;color:#725b1b}p{margin:4px 0;white-space:pre-wrap}</style></head><body><ul>' + render(root) + '</ul></body></html>'

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


class RawSourceProvider(Provider):
    """Read allowlisted text-like files without interpreting their contents."""
    manifest = ProviderManifest(name="text.raw.source", version="0.8.0", kind="text_parser", modes=["normal", "fallback"],
                                capabilities=["raw_source", "text", "markdown"])

    def __init__(self, max_file_size_mb: int = 20) -> None:
        self.max_file_size_bytes = max_file_size_mb * 1024 * 1024

    async def parse(self, context: ParseContext) -> ProviderResult:
        return await asyncio.to_thread(self._parse_sync, context)

    def _parse_sync(self, context: ParseContext) -> ProviderResult:
        source = Path(context.file.path)
        try:
            raw, size_bytes = read_text_source(source, self.max_file_size_bytes)
        except TextReadError as exc:
            return self._failed(exc.code, str(exc))
        extraction_mode = "raw_source_fallback" if context.metadata.get("raw_source_fallback") else "raw_source"
        document = DocumentResult(document_id=context.file.file_id, source_file=context.file,
                                  document_type=source.suffix.lower().lstrip(".") or "text",
                                  blocks=[DocumentBlock(kind="text", text=raw)] if raw else [],
                                  representations={"plain_text": raw, "markdown": source_markdown(raw, source.suffix.lower().lstrip(".") or "text")},
                                  metadata={"encoding": "utf-8", "size_bytes": size_bytes, "extraction_mode": extraction_mode},
                                  parser={"name": self.manifest.name, "version": self.manifest.version})
        return ProviderResult(status="success", provider_name=self.manifest.name, document=document)

    def _failed(self, code: str, message: str) -> ProviderResult:
        return ProviderResult(status="failed", provider_name=self.manifest.name,
                              error=ProviderError(code=code, message=message, retryable=False))


class TextParseSkill(Skill):
    name = "text.parse"
    version = "0.8.0"
    suffixes = TEXT_SUFFIXES

    def __init__(self, providers: ProviderRegistry, normal_provider: str = "text.normal.stdlib", raw_provider: str = "text.raw.source") -> None:
        self.providers = providers
        self.normal_provider = normal_provider
        self.raw_provider = raw_provider

    @property
    def manifest(self) -> SkillManifest:
        return SkillManifest(name=self.name, version=self.version, kind="document_parser", input_types=sorted(self.suffixes),
                             capabilities=["text", "markdown", "tables", "links", "xml_structure", "rtf_text"], providers=[item["name"] for item in self.providers.list_manifests()])

    async def execute(self, context: ParseContext) -> SkillResult:
        if Path(context.file.path).suffix.lower() not in self.suffixes:
            return SkillResult(status="failed", skill_name=self.name,
                               error=ProviderError(code="unsupported_format", message="text.parse 不支持该文件格式", retryable=False))
        suffix = Path(context.file.path).suffix.lower()
        explicit_provider = context.options.provider
        if explicit_provider == self.raw_provider and suffix not in RAW_SOURCE_SUFFIXES:
            return SkillResult(status="failed", skill_name=self.name,
                               error=ProviderError(code="provider_not_allowed", message="该格式不允许绕过专用文本解析器", retryable=False))
        if suffix in RAW_SOURCE_SUFFIXES and explicit_provider not in {None, self.raw_provider}:
            return SkillResult(status="failed", skill_name=self.name,
                               error=ProviderError(code="provider_not_allowed", message="该格式仅允许安全原文解析器", retryable=False))
        provider_name = explicit_provider or (self.raw_provider if suffix in RAW_SOURCE_SUFFIXES else self.normal_provider)
        try:
            provider = self.providers.get(provider_name)
        except KeyError as exc:
            return SkillResult(status="failed", skill_name=self.name,
                               error=ProviderError(code="provider_not_found", message=str(exc), retryable=False))
        result = await provider.parse(context)
        can_fallback = (
            explicit_provider is None
            and context.options.fallback_enabled
            and provider.manifest.name != self.raw_provider
            and result.status == "failed"
            and (result.error is None or result.error.code not in NON_FALLBACK_ERROR_CODES)
        )
        if can_fallback:
            fallback_context = context.model_copy(deep=True)
            fallback_context.metadata["raw_source_fallback"] = True
            result = await self.providers.get(self.raw_provider).parse(fallback_context)
        return SkillResult(status=result.status, skill_name=self.name,
                           data={"document": result.document.model_dump()} if result.document else result.data,
                           warnings=result.warnings, metrics=result.metrics, error=result.error)
