from app.documents.models import (
    DocumentBlock,
    DocumentPage,
    DocumentResult,
    ParseContext,
    PdfInspectionResult,
    ProviderError,
)
from app.skills.providers import PdfInspector, Provider, ProviderManifest, ProviderResult


def _load_pdf_inspector():
    try:
        import pdf_inspector
    except ImportError as exc:
        raise RuntimeError("未安装 pdf-inspector，请执行 uv sync") from exc
    return pdf_inspector


class PdfInspectorAdapter(PdfInspector):
    """pdf-inspector 的延迟导入适配器。"""

    async def inspect(self, context: ParseContext) -> PdfInspectionResult:
        pdf_inspector = _load_pdf_inspector()
        result = pdf_inspector.detect_pdf(context.file.path)
        pdf_type = getattr(result.pdf_type, "value", result.pdf_type)
        reasons = []
        for item in getattr(result, "ocr_reasons_by_page", []) or []:
            reasons.extend(list(getattr(item, "reasons", []) or []))
        return PdfInspectionResult(
            pdf_type=str(pdf_type),
            confidence=getattr(result, "confidence", None),
            ocr_recommended=bool(getattr(result, "ocr_recommended", False)),
            pages_needing_ocr=list(getattr(result, "pages_needing_ocr", []) or []),
            reasons=reasons or list(getattr(result, "reasons", []) or []),
        )


class PdfInspectorParserProvider(Provider):
    """使用 pdf-inspector 完成文本型 PDF 的普通解析。"""

    manifest = ProviderManifest(
        name="pdf.normal.pdf-inspector",
        kind="pdf_parser",
        modes=["normal"],
        capabilities=["text", "markdown", "pages", "layout", "tables"],
    )

    async def parse(self, context: ParseContext) -> ProviderResult:
        try:
            pdf_inspector = _load_pdf_inspector()
            result = pdf_inspector.process_pdf(context.file.path)
            markdown = str(getattr(result, "markdown", None) or "")
            pages = self._extract_pages(pdf_inspector, context.file.path)
        except Exception as exc:
            return ProviderResult(
                status="failed",
                provider_name=self.manifest.name,
                error=ProviderError(code="provider_failed", message=str(exc), retryable=True),
            )

        if not pages and markdown:
            pages = [
                DocumentPage(
                    page_number=1,
                    text=markdown,
                    blocks=[DocumentBlock(kind="text", text=markdown, page_number=1)],
                )
            ]
        if not markdown:
            markdown = "\n\n".join(page.text for page in pages if page.text)
        if not markdown.strip():
            return ProviderResult(
                status="failed",
                provider_name=self.manifest.name,
                error=ProviderError(code="quality_insufficient", message="普通解析未提取到文本", retryable=False),
            )

        blocks = [block for page in pages for block in page.blocks]
        metrics: dict[str, float | int | str] = {
            "pages": int(getattr(result, "page_count", len(pages)) or len(pages)),
            "text_chars": len(markdown),
        }
        for key in ("processing_time_ms", "pages_with_tables", "pages_with_columns"):
            value = getattr(result, key, None)
            if isinstance(value, (int, float, str)):
                metrics[key] = value

        document = DocumentResult(
            document_id=context.file.file_id,
            source_file=context.file,
            document_type="pdf",
            pages=pages,
            blocks=blocks,
            parser={"name": self.manifest.name, "version": self.manifest.version},
            metadata={"title": getattr(result, "title", None)},
            quality=metrics,
        )
        return ProviderResult(
            status="success",
            provider_name=self.manifest.name,
            document=document,
            metrics=metrics,
        )

    @staticmethod
    def _extract_pages(pdf_inspector, path: str) -> list[DocumentPage]:
        """使用按页 Markdown API 保留页面边界；旧版本不支持时由调用方回退。"""

        extraction = pdf_inspector.extract_pages_markdown(path)
        pages: list[DocumentPage] = []
        for index, item in enumerate(getattr(extraction, "pages", []) or []):
            page_number = int(getattr(item, "page", index)) + 1
            text = str(getattr(item, "markdown", "") or "")
            pages.append(
                DocumentPage(
                    page_number=page_number,
                    text=text,
                    blocks=[DocumentBlock(kind="text", text=text, page_number=page_number)],
                )
            )
        return pages
