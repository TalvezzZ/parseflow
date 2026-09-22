import asyncio
import json
import tempfile
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.documents.models import DocumentBlock, DocumentPage, DocumentResult, ParseContext, ProviderError
from app.skills.providers import Provider, ProviderManifest, ProviderResult


class PaddleOcrPdfProvider(Provider):
    """Render scanned PDFs locally and recognize each page with PaddleOCR.

    PaddleOCR downloads its model files into its cache on first use.  The heavy
    dependencies are intentionally optional, so importing this module never
    prevents text-based PDF parsing from starting.
    """

    manifest = ProviderManifest(
        name="pdf.ocr.paddle",
        version="0.1.0",
        kind="pdf_ocr",
        modes=["ocr"],
        capabilities=["scanned_pdf", "local_ocr", "pages", "text", "markdown", "bbox", "confidence"],
    )

    def __init__(
        self,
        device: str = "cpu",
        lang: str = "ch",
        max_pages: int = 50,
        render_scale: float = 2.0,
        max_page_pixels: int = 20_000_000,
        ocr_factory: Callable[[], Any] | None = None,
        renderer: Callable[[Path, int, float, int], list[tuple[int, Path]]] | None = None,
    ) -> None:
        self.device = device
        self.lang = lang
        self.max_pages = max_pages
        self.render_scale = render_scale
        self.max_page_pixels = max_page_pixels
        self._ocr_factory = ocr_factory
        self._renderer = renderer

    async def parse(self, context: ParseContext) -> ProviderResult:
        return await asyncio.to_thread(self._parse_sync, context)

    def _parse_sync(self, context: ParseContext) -> ProviderResult:
        source = Path(context.file.path)
        if not source.is_file():
            return self._failed("source_not_found", f"PDF 文件不存在: {source}")
        try:
            ocr = self._create_ocr()
        except ImportError:
            return self._failed(
                "provider_unavailable",
                "未安装 PaddleOCR OCR 依赖。请执行 `uv sync --group ocr` 后重试。",
            )
        except Exception as exc:
            return self._failed("ocr_initialization_failed", f"PaddleOCR 初始化失败: {exc}")

        try:
            with tempfile.TemporaryDirectory(prefix=f"parseflow-{context.file.file_id}-ocr-") as temporary:
                pages = self._render_pages(source, Path(temporary))
                if not pages:
                    return self._failed("quality_insufficient", "PDF 未包含可供 OCR 的页面")
                document_pages: list[DocumentPage] = []
                blocks: list[DocumentBlock] = []
                markdown_parts: list[str] = []
                artifact_dir = Path(str(context.metadata.get("artifact_dir") or ""))
                for page_number, image_path in pages:
                    page_blocks = self._recognize_page(ocr, image_path, page_number)
                    page_text = "\n".join(block.text for block in page_blocks if block.text)
                    width, height = self._image_size(image_path) if image_path.is_file() else (0, 0)
                    preview: dict[str, Any] | None = None
                    if str(context.metadata.get("artifact_dir") or "") and image_path.is_file():
                        preview_dir = artifact_dir / "ocr-pages"
                        preview_dir.mkdir(parents=True, exist_ok=True)
                        artifact_name = f"ocr-page-{page_number:04d}.png"
                        preview_path = preview_dir / artifact_name
                        shutil.copyfile(image_path, preview_path)
                        preview = {"filename": artifact_name, "relative_path": preview_path.relative_to(artifact_dir).as_posix(), "content_type": "image/png"}
                    document_pages.append(DocumentPage(page_number=page_number, text=page_text, blocks=page_blocks, width=width, height=height, preview=preview))
                    blocks.extend(page_blocks)
                    markdown_parts.append(f"## 第 {page_number} 页\n\n{page_text}" if page_text else f"## 第 {page_number} 页")
        except ValueError as exc:
            return self._failed("ocr_input_limit_exceeded", str(exc))
        except Exception as exc:
            return self._failed("provider_failed", f"PaddleOCR 识别失败: {exc}", retryable=True)

        plain_text = "\n\n".join(page.text for page in document_pages if page.text)
        if not plain_text.strip():
            return self._failed("quality_insufficient", "PaddleOCR 未提取到可用文本")
        blank_pages = [page.page_number for page in document_pages if not page.text.strip()]
        warnings = [f"OCR 未在第 {page} 页识别到文本，可能为空白页或清晰度不足" for page in blank_pages]
        document = DocumentResult(
            document_id=context.file.file_id,
            source_file=context.file,
            document_type="pdf",
            pages=document_pages,
            blocks=blocks,
            representations={"plain_text": plain_text, "markdown": "\n\n---\n\n".join(markdown_parts)},
            parser={"name": self.manifest.name, "version": self.manifest.version},
            warnings=warnings,
            quality={"pages": len(document_pages), "text_chars": len(plain_text), "blocks": len(blocks), "blank_pages": len(blank_pages)},
            provenance={"ocr": {"engine": "PaddleOCR", "device": self.device, "lang": self.lang, "render_scale": self.render_scale,
                                    "orientation_correction": False}},
        )
        return ProviderResult(status="partial" if warnings else "success", provider_name=self.manifest.name, document=document,
                              warnings=warnings, metrics=document.quality)

    def _create_ocr(self) -> Any:
        if self._ocr_factory is not None:
            return self._ocr_factory()
        from paddleocr import PaddleOCR
        return PaddleOCR(
            lang=self.lang,
            device=self.device,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )

    def _render_pages(self, source: Path, temporary: Path) -> list[tuple[int, Path]]:
        if self._renderer is not None:
            return self._renderer(source, self.max_pages, self.render_scale, self.max_page_pixels)
        import pypdfium2 as pdfium
        document = pdfium.PdfDocument(str(source))
        try:
            if len(document) > self.max_pages:
                raise ValueError(f"PDF 页数 {len(document)} 超过 OCR 上限 {self.max_pages}")
            rendered: list[tuple[int, Path]] = []
            for index in range(len(document)):
                page = document[index]
                try:
                    bitmap = page.render(scale=self.render_scale)
                    image = bitmap.to_pil()
                    if image.width * image.height > self.max_page_pixels:
                        raise ValueError(f"第 {index + 1} 页渲染像素超过上限 {self.max_page_pixels:,}")
                    image_path = temporary / f"page-{index + 1:04d}.png"
                    image.save(image_path, "PNG")
                    rendered.append((index + 1, image_path))
                finally:
                    page.close()
            return rendered
        finally:
            document.close()

    @staticmethod
    def _image_size(image_path: Path) -> tuple[int, int]:
        from PIL import Image
        with Image.open(image_path) as image:
            return image.width, image.height

    def _recognize_page(self, ocr: Any, image_path: Path, page_number: int) -> list[DocumentBlock]:
        if hasattr(ocr, "predict"):
            result = list(ocr.predict(str(image_path)))
            return self._blocks_from_predict_result(result[0] if result else None, page_number)
        if hasattr(ocr, "ocr"):
            result = ocr.ocr(str(image_path), cls=True)
            return self._blocks_from_legacy_result(result, page_number)
        raise TypeError("PaddleOCR 实例未提供 predict 或 ocr 方法")

    def _blocks_from_predict_result(self, result: Any, page_number: int) -> list[DocumentBlock]:
        data = self._as_mapping(result)
        payload = data.get("res", data)
        texts = self._first_value(payload, "rec_texts", "texts")
        scores = self._first_value(payload, "rec_scores", "scores")
        boxes = self._first_value(payload, "rec_boxes", "rec_polys", "dt_polys")
        return [
            DocumentBlock(
                kind="text",
                text=str(text),
                page_number=page_number,
                bbox=self._bbox(boxes[index]) if index < len(boxes) else None,
                metadata={"confidence": float(scores[index]) if index < len(scores) else None, "ocr": "paddleocr"},
            )
            for index, text in enumerate(texts)
            if str(text).strip()
        ]

    def _blocks_from_legacy_result(self, result: Any, page_number: int) -> list[DocumentBlock]:
        lines = result[0] if isinstance(result, list) and result else []
        blocks: list[DocumentBlock] = []
        for line in lines or []:
            if not isinstance(line, (list, tuple)) or len(line) < 2:
                continue
            text_score = line[1]
            if not isinstance(text_score, (list, tuple)) or not text_score:
                continue
            text = str(text_score[0])
            if text.strip():
                blocks.append(DocumentBlock(kind="text", text=text, page_number=page_number, bbox=self._bbox(line[0]), metadata={"confidence": float(text_score[1]) if len(text_score) > 1 else None, "ocr": "paddleocr"}))
        return blocks

    @staticmethod
    def _first_value(payload: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = payload.get(key)
            if value is not None:
                return value
        return []

    @staticmethod
    def _as_mapping(result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return result
        value = getattr(result, "json", None)
        value = value() if callable(value) else value
        if isinstance(value, str):
            value = json.loads(value)
        if isinstance(value, dict):
            return value
        value = getattr(result, "res", None)
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _bbox(value: Any) -> list[float] | None:
        if hasattr(value, "tolist"):
            value = value.tolist()
        if not isinstance(value, (list, tuple)):
            return None
        flattened: list[float] = []
        for item in value:
            if isinstance(item, (list, tuple)):
                flattened.extend(float(number) for number in item)
            else:
                flattened.append(float(item))
        return flattened or None

    def _failed(self, code: str, message: str, retryable: bool = False) -> ProviderResult:
        return ProviderResult(status="failed", provider_name=self.manifest.name, error=ProviderError(code=code, message=message, retryable=retryable))
