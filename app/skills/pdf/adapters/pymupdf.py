from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
import tempfile
from pathlib import Path
from typing import Any

from app.documents.models import DocumentBlock, DocumentPage, DocumentResult, ParseContext, ProviderError
from app.skills.providers import Provider, ProviderManifest, ProviderResult


class PyMuPdfParserProvider(Provider):
    """用 PyMuPDF 解析 PDF 原生文本和嵌入图片，不执行 OCR。"""

    manifest = ProviderManifest(
        name="pdf.normal.pymupdf",
        kind="pdf_parser",
        modes=["normal"],
        capabilities=["text", "markdown", "pages", "layout", "images"],
    )

    async def parse(self, context: ParseContext) -> ProviderResult:
        return await asyncio.to_thread(self._parse_sync, context)

    def _parse_sync(self, context: ParseContext) -> ProviderResult:
        try:
            import pymupdf
        except ImportError:
            return self._failed(
                "provider_unavailable",
                "未安装 PyMuPDF Provider。请执行 `uv sync` 后再将 PDF_NORMAL_PROVIDER 设为 pdf.normal.pymupdf。",
            )

        artifact_dir = self._artifact_dir(context)
        images_dir = artifact_dir / "images"
        images: list[dict[str, Any]] = []
        images_by_digest: dict[str, dict[str, Any]] = {}
        pages: list[DocumentPage] = []
        warnings: list[str] = []
        image_occurrences = 0

        try:
            with pymupdf.open(context.file.path) as document:
                metadata = document.metadata or {}
                for page_index in range(document.page_count):
                    page_number = page_index + 1
                    page_blocks: list[DocumentBlock] = []
                    text_parts: list[str] = []
                    try:
                        layout = document.load_page(page_index).get_text("dict")
                    except Exception:
                        warnings.append(f"第 {page_number} 页读取失败；其余页面结果仍可用")
                        pages.append(DocumentPage(page_number=page_number))
                        continue

                    for raw_block in layout.get("blocks", []):
                        block_type = raw_block.get("type")
                        if block_type == 0:
                            text = self._text_from_block(raw_block)
                            if not text:
                                continue
                            text_parts.append(text)
                            page_blocks.append(
                                DocumentBlock(
                                    kind="text",
                                    text=text,
                                    page_number=page_number,
                                    bbox=self._bbox(raw_block.get("bbox")),
                                )
                            )
                            continue
                        if block_type != 1:
                            continue

                        image_data = raw_block.get("image")
                        if not isinstance(image_data, (bytes, bytearray, memoryview)) or not image_data:
                            continue
                        image_bytes = bytes(image_data)
                        digest = hashlib.sha256(image_bytes).hexdigest()
                        image = images_by_digest.get(digest)
                        if image is None:
                            extension = str(raw_block.get("ext") or "bin").lower().lstrip(".")
                            if not re.fullmatch(r"[a-z0-9]{1,8}", extension):
                                extension = "bin"
                            filename = f"image-{len(images) + 1:04d}.{extension}"
                            relative_path = f"images/{filename}"
                            image_path = images_dir / filename
                            try:
                                images_dir.mkdir(parents=True, exist_ok=True)
                                image_path.write_bytes(image_bytes)
                            except OSError:
                                warnings.append("图片工件写入失败；文本解析结果仍可用")
                                continue

                            image = {
                                "filename": filename,
                                "path": str(image_path),
                                "relative_path": relative_path,
                                "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
                                "width": int(raw_block["width"]) if raw_block.get("width") is not None else None,
                                "height": int(raw_block["height"]) if raw_block.get("height") is not None else None,
                                "size_bytes": len(image_bytes),
                                "sha256": digest,
                                "occurrences": [],
                            }
                            images_by_digest[digest] = image
                            images.append(image)

                        bbox = self._bbox(raw_block.get("bbox"))
                        image["occurrences"].append({"page_number": page_number, "bbox": bbox})
                        image_occurrences += 1
                        page_blocks.append(
                            DocumentBlock(
                                kind="image",
                                page_number=page_number,
                                bbox=bbox,
                                metadata={
                                    "filename": image["filename"],
                                    "relative_path": image["relative_path"],
                                    "mime_type": image["mime_type"],
                                    "width": image["width"],
                                    "height": image["height"],
                                    "sha256": digest,
                                },
                            )
                        )

                    page_text = "\n\n".join(text_parts)
                    pages.append(DocumentPage(page_number=page_number, text=page_text, blocks=page_blocks))
        except Exception as exc:
            return self._failed("provider_failed", str(exc), retryable=True)

        plain_text = "\n\n".join(page.text for page in pages if page.text)
        if not plain_text.strip() and not images:
            return self._failed("quality_insufficient", "PyMuPDF 未提取到文本或图片", retryable=False)
        markdown = "\n\n---\n\n".join(
            f"## 第 {page.page_number} 页\n\n{page.text}" if page.text else f"## 第 {page.page_number} 页"
            for page in pages
        )
        blocks = [block for page in pages for block in page.blocks]
        metrics: dict[str, float | int | str] = {
            "pages": len(pages),
            "text_chars": len(plain_text),
            "images": len(images),
            "image_occurrences": image_occurrences,
        }
        document = DocumentResult(
            document_id=context.file.file_id,
            source_file=context.file,
            document_type="pdf",
            pages=pages,
            blocks=blocks,
            images=images,
            parser={"name": self.manifest.name, "version": self.manifest.version},
            metadata={"title": metadata.get("title")},
            representations={"plain_text": plain_text, "markdown": markdown},
            quality=metrics,
            warnings=warnings,
        )
        return ProviderResult(
            status="partial" if warnings else "success",
            provider_name=self.manifest.name,
            document=document,
            warnings=warnings,
            metrics=metrics,
        )

    @staticmethod
    def _artifact_dir(context: ParseContext) -> Path:
        configured = context.metadata.get("artifact_dir")
        directory = Path(str(configured)) if configured else Path(tempfile.mkdtemp(prefix=f"parse-agent-{context.file.file_id}-pdf-"))
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @staticmethod
    def _text_from_block(block: dict[str, Any]) -> str:
        lines: list[str] = []
        for line in block.get("lines", []):
            text = "".join(str(span.get("text", "")) for span in line.get("spans", []))
            if text.strip():
                lines.append(text)
        return "\n".join(lines).strip()

    @staticmethod
    def _bbox(value: Any) -> list[float] | None:
        if not isinstance(value, (list, tuple)) or len(value) < 4:
            return None
        try:
            return [round(float(item), 3) for item in value[:4]]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _failed(code: str, message: str, retryable: bool = False) -> ProviderResult:
        return ProviderResult(
            status="failed",
            provider_name=PyMuPdfParserProvider.manifest.name,
            error=ProviderError(code=code, message=message, retryable=retryable),
        )
