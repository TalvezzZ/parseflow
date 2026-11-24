import asyncio
import json
import math
import tempfile
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from app.documents.models import DocumentBlock, DocumentPage, DocumentResult, ParseContext, ProviderError
from app.render import RenderOptions, render_representations
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
        layout_enabled: bool = False,
        layout_model_name: str = "PP-DocLayout-M",
        layout_threshold: float = .5,
        layout_max_total_bytes: int = 50 * 1024 * 1024,
        layout_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.device = device
        self.lang = lang
        self.max_pages = max_pages
        self.render_scale = render_scale
        self.max_page_pixels = max_page_pixels
        self._ocr_factory = ocr_factory
        self._renderer = renderer
        self.layout_enabled = layout_enabled
        self.layout_model_name = layout_model_name
        self.layout_threshold = layout_threshold
        self.layout_max_total_bytes = layout_max_total_bytes
        self._layout_factory = layout_factory

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

        warnings: list[str] = []
        images: list[dict[str, Any]] = []
        document_pages: list[DocumentPage] = []
        blocks: list[DocumentBlock] = []
        artifact_value = str(context.metadata.get("artifact_dir") or "")
        if self.layout_enabled and not artifact_value:
            artifact_value = str(Path(tempfile.mkdtemp(prefix="parseflow-pdf-layout-")).resolve())
            context.metadata["artifact_dir"] = artifact_value
        artifact_dir = Path(artifact_value) if artifact_value else None
        if artifact_dir is not None:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        layout = None
        if self.layout_enabled:
            try:
                layout = self._create_layout()
            except Exception as exc:
                warnings.append(f"layout_images_failed: 版面图片提取初始化失败: {str(exc)[:512]}")
        try:
            with tempfile.TemporaryDirectory(prefix=f"parseflow-{context.file.file_id}-ocr-") as temporary:
                pages = iter(self._render_pages(source, Path(temporary)))
                try:
                    while True:
                        if self._cancelled(context):
                            return self._failed("task_cancelled", "PDF OCR 已取消")
                        try:
                            page_number, image_path = next(pages)
                        except StopIteration:
                            break
                        if self._cancelled(context):
                            return self._failed("task_cancelled", "PDF OCR 已取消")
                        page_blocks = self._recognize_page(ocr, image_path, page_number)
                        page_text = "\n".join(block.text for block in page_blocks if block.text)
                        width, height = self._image_size(image_path) if image_path.is_file() else (0, 0)
                        page_images = []
                        if layout is not None:
                            try:
                                page_images, image_blocks, layout_warnings = layout.extract(image_path, artifact_dir, page_number)
                                page_blocks.extend(image_blocks)
                                images.extend(page_images)
                                warnings.extend(layout_warnings)
                            except Exception as exc:
                                warnings.append(f"layout_images_failed: 第 {page_number} 页图片区域提取失败: {str(exc)[:512]}")
                        preview: dict[str, Any] | None = None
                        if artifact_dir is not None and image_path.is_file():
                            preview_dir = artifact_dir / "ocr-pages"
                            preview_dir.mkdir(parents=True, exist_ok=True)
                            artifact_name = f"ocr-page-{page_number:04d}.png"
                            shutil.copyfile(image_path, preview_dir / artifact_name)
                            preview = {"filename": artifact_name, "content_type": "image/png"}
                        document_pages.append(DocumentPage(page_number=page_number, text=page_text, blocks=page_blocks,
                            width=width, height=height, preview=preview))
                        blocks.extend(page_blocks)
                finally:
                    close = getattr(pages, "close", None)
                    if close:
                        close()
        except ValueError as exc:
            return self._failed("ocr_input_limit_exceeded", str(exc))
        except Exception as exc:
            return self._failed("provider_failed", f"PaddleOCR 识别失败: {exc}", retryable=True)

        if not document_pages:
            return self._failed("quality_insufficient", "PDF 未包含可供 OCR 的页面")
        plain_text = "\n\n".join(page.text for page in document_pages if page.text)
        if not plain_text.strip() and not images:
            failed = self._failed("quality_insufficient", "PaddleOCR 未提取到可用文本或图片区域")
            failed.warnings = warnings
            return failed
        blank_pages = [page.page_number for page in document_pages if not page.text.strip()
                       and not any(block.kind == "image" for block in page.blocks)]
        warnings.extend(f"OCR 未在第 {page} 页识别到文本，可能为空白页或清晰度不足" for page in blank_pages)
        document = DocumentResult(
            document_id=context.file.file_id, source_file=context.file, document_type="pdf",
            pages=document_pages, blocks=blocks, images=images,

            metadata={"artifact_dir": str(artifact_dir)} if artifact_dir is not None else {},
            parser={"name": self.manifest.name, "version": self.manifest.version}, warnings=warnings,
            quality={"pages": len(document_pages), "text_chars": len(plain_text), "blocks": len(blocks),
                     "blank_pages": len(blank_pages), "images": len(images)},
            provenance={"ocr": {"engine": "PaddleOCR", "device": self.device, "lang": self.lang,
                "render_scale": self.render_scale, "orientation_correction": False}, "layout": {**self._layout_provenance(), "images_count": len(images),
                    "result_status": "disabled" if not self.layout_enabled else "warning" if any("layout" in warning for warning in warnings) else "completed"}},
        )
        # 表示由渲染层派生；页面级图片引用保持 1.6.0 的页尾追加布局。
        rendered = render_representations(
            document,
            kinds=["plain_text", "markdown"],
            options=RenderOptions(page_asset_links=True),
        )
        document.representations = rendered.as_representations()
        return ProviderResult(status="partial" if warnings else "success", provider_name=self.manifest.name,
                              document=document, warnings=warnings, metrics=document.quality)

    @staticmethod
    def _cancelled(context: ParseContext) -> bool:
        value = context.metadata.get("task_cancel_path") or context.metadata.get("excel_cancel_path")
        return bool(value and Path(str(value)).exists())

    def _create_layout(self):
        if not self.layout_enabled:
            return None
        from app.skills.layout_images import LayoutImageExtractor
        return LayoutImageExtractor(device=self.device, model_name=self.layout_model_name,
            threshold=self.layout_threshold,
            max_total_bytes=self.layout_max_total_bytes, max_pixels=self.max_page_pixels,
            detector_factory=self._layout_factory)

    def _layout_provenance(self) -> dict[str, Any]:
        return {"enabled": self.layout_enabled, "model": self.layout_model_name if self.layout_enabled else None,
                "coordinate_system": "page_pixels", "formula_mode": "text_ocr"}

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

    def _render_pages(self, source: Path, temporary: Path) -> Iterator[tuple[int, Path]]:
        if self._renderer is not None:
            yield from self._renderer(source, self.max_pages, self.render_scale, self.max_page_pixels)
            return
        import pypdfium2 as pdfium
        if not math.isfinite(self.render_scale) or self.render_scale <= 0:
            raise ValueError("PDF 渲染比例必须是有限正数")
        document = pdfium.PdfDocument(str(source))
        try:
            if len(document) > self.max_pages:
                raise ValueError(f"PDF 页数 {len(document)} 超过 OCR 上限 {self.max_pages}")
            for index in range(len(document)):
                page = document[index]
                bitmap = image = None
                try:
                    width, height = page.get_size()
                    pixel_width, pixel_height = math.ceil(width * self.render_scale), math.ceil(height * self.render_scale)
                    if pixel_width <= 0 or pixel_height <= 0 or pixel_width * pixel_height > self.max_page_pixels:
                        raise ValueError(f"第 {index + 1} 页渲染像素超过上限 {self.max_page_pixels:,}")
                    bitmap = page.render(scale=self.render_scale)
                    image = bitmap.to_pil()
                    if image.width * image.height > self.max_page_pixels:
                        raise ValueError(f"第 {index + 1} 页渲染像素超过上限 {self.max_page_pixels:,}")
                    image_path = temporary / f"page-{index + 1:04d}.png"
                    image.save(image_path, "PNG")
                finally:
                    if image is not None:
                        image.close()
                    if bitmap is not None:
                        bitmap.close()
                    page.close()
                try:
                    yield index + 1, image_path
                finally:
                    image_path.unlink(missing_ok=True)
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
