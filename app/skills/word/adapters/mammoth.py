import asyncio
import mimetypes
import tempfile
from pathlib import Path
from typing import Any

from app.documents.models import DocumentBlock, DocumentResult, ParseContext, ProviderError
from app.skills.providers import Provider, ProviderManifest, ProviderResult


class MammothProvider(Provider):
    """使用 Mammoth 将 DOCX 转为语义 HTML/Markdown，并提取图片。"""

    manifest = ProviderManifest(
        name="word.normal.mammoth",
        version="0.2.0",
        kind="word_parser",
        modes=["normal"],
        capabilities=["text", "html", "markdown", "headings", "tables", "images"],
    )

    async def parse(self, context: ParseContext) -> ProviderResult:
        return await asyncio.to_thread(self._parse_sync, context)

    def _parse_sync(self, context: ParseContext) -> ProviderResult:
        try:
            import mammoth
            from bs4 import BeautifulSoup
            from markdownify import markdownify
        except ImportError as exc:
            return ProviderResult(
                status="failed",
                provider_name=self.manifest.name,
                error=ProviderError(
                    code="provider_unavailable",
                    message="未安装 DOCX 解析依赖，请执行 uv sync",
                    retryable=False,
                ),
            )

        artifact_dir = self._artifact_dir(context)
        images_dir = artifact_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        assets: list[dict[str, Any]] = []
        image_count = 0

        def convert_image(image):
            nonlocal image_count
            image_count += 1
            content_type = str(getattr(image, "content_type", "application/octet-stream"))
            extension = mimetypes.guess_extension(content_type) or ".bin"
            if extension == ".jpe":
                extension = ".jpg"
            filename = f"image-{image_count:04d}{extension}"
            image_path = images_dir / filename
            with image.open() as image_file:
                image_path.write_bytes(image_file.read())
            relative_path = f"images/{filename}"
            assets.append(
                {
                    "filename": filename,
                    "path": str(image_path),
                    "relative_path": relative_path,
                    "mime_type": content_type,
                }
            )
            return {
                "src": relative_path,
                "alt": str(getattr(image, "description", "") or filename),
            }

        try:
            with open(context.file.path, "rb") as docx_file:
                conversion = mammoth.convert_to_html(
                    docx_file,
                    convert_image=mammoth.images.img_element(convert_image),
                )
            html = str(getattr(conversion, "value", "") or "")
            markdown = markdownify(html, heading_style="ATX", escape_underscores=False)
            soup = BeautifulSoup(html, "html.parser")
            blocks, tables = self._extract_blocks(soup)
        except Exception as exc:
            return ProviderResult(
                status="failed",
                provider_name=self.manifest.name,
                error=ProviderError(code="provider_failed", message=str(exc), retryable=True),
            )

        warnings = [self._message_text(message) for message in getattr(conversion, "messages", []) or []]
        text_chars = len(soup.get_text(" ", strip=True))
        if not html.strip() and not markdown.strip():
            return ProviderResult(
                status="failed",
                provider_name=self.manifest.name,
                warnings=warnings,
                error=ProviderError(code="quality_insufficient", message="DOCX 未提取到内容", retryable=False),
            )

        metrics: dict[str, float | int | str] = {
            "blocks": len(blocks),
            "text_chars": text_chars,
            "tables": len(tables),
            "images": len(assets),
        }
        document = DocumentResult(
            document_id=context.file.file_id,
            source_file=context.file,
            document_type="docx",
            blocks=blocks,
            tables=tables,
            images=assets,
            representations={"html": html, "markdown": markdown, "plain_text": soup.get_text("\n", strip=True)},
            metadata={"artifact_dir": str(artifact_dir)},
            parser={"name": self.manifest.name, "version": self.manifest.version},
            quality=metrics,
        )
        return ProviderResult(
            status="success",
            provider_name=self.manifest.name,
            document=document,
            warnings=warnings,
            metrics=metrics,
        )

    @staticmethod
    def _artifact_dir(context: ParseContext) -> Path:
        configured = context.metadata.get("artifact_dir")
        if configured:
            directory = Path(str(configured))
            directory.mkdir(parents=True, exist_ok=True)
            return directory
        return Path(tempfile.mkdtemp(prefix=f"parse-agent-{context.file.file_id}-"))

    @classmethod
    def _extract_blocks(cls, soup) -> tuple[list[DocumentBlock], list[dict[str, Any]]]:
        blocks: list[DocumentBlock] = []
        tables: list[dict[str, Any]] = []
        root = soup.body or soup

        def walk(parent) -> None:
            for element in parent.find_all(recursive=False):
                name = element.name.lower()
                if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                    text = element.get_text(" ", strip=True)
                    if text:
                        blocks.append(
                            DocumentBlock(
                                kind="heading",
                                text=text,
                                metadata={"level": int(name[1:])},
                            )
                        )
                elif name in {"p", "blockquote", "pre"}:
                    text = element.get_text(" ", strip=True)
                    if text:
                        blocks.append(DocumentBlock(kind="text", text=text))
                    for image in element.find_all("img"):
                        blocks.append(
                            DocumentBlock(kind="image", metadata={"src": image.get("src", "")})
                        )
                elif name in {"ul", "ol"}:
                    for item in element.find_all("li", recursive=False):
                        text = item.get_text(" ", strip=True)
                        if text:
                            blocks.append(DocumentBlock(kind="list", text=text))
                elif name == "table":
                    rows = [
                        [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"], recursive=False)]
                        for row in element.find_all("tr")
                    ]
                    table = {"rows": rows, "html": str(element)}
                    tables.append(table)
                    blocks.append(DocumentBlock(kind="table", text="\n".join(" | ".join(row) for row in rows), metadata=table))
                elif name == "img":
                    blocks.append(DocumentBlock(kind="image", metadata={"src": element.get("src", "")}))
                else:
                    walk(element)

        walk(root)
        return blocks, tables

    @staticmethod
    def _message_text(message: Any) -> str:
        return str(getattr(message, "message", message))
