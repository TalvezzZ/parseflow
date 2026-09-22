import asyncio
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image

from app.documents.models import DocumentBlock, DocumentPage, DocumentResult, ParseContext, ProviderError
from app.skills.providers import Provider, ProviderManifest, ProviderResult


class PaddleOcrImageProvider(Provider):
    """Recognize text in one standalone image with PaddleOCR."""

    manifest = ProviderManifest(
        name="image.ocr.paddle",
        version="0.1.0",
        kind="image_ocr",
        modes=["ocr"],
        capabilities=["local_ocr", "image", "text", "markdown", "bbox", "confidence"],
    )

    def __init__(
        self,
        device: str = "cpu",
        lang: str = "ch",
        max_pixels: int = 20_000_000,
        ocr_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.device = device
        self.lang = lang
        self.max_pixels = max_pixels
        self._ocr_factory = ocr_factory

    async def parse(self, context: ParseContext) -> ProviderResult:
        return await asyncio.to_thread(self._parse_sync, context)

    def _parse_sync(self, context: ParseContext) -> ProviderResult:
        source = Path(context.file.path)
        if not source.is_file():
            return self._failed("source_not_found", f"图片文件不存在: {source}")
        try:
            with Image.open(source) as image:
                width, height = image.size
        except Exception as exc:
            return self._failed("invalid_image", f"无法读取图片: {exc}")
        if width * height > self.max_pixels:
            return self._failed("ocr_input_limit_exceeded", f"图片像素超过 OCR 上限 {self.max_pixels:,}")
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
            blocks = self._recognize_image(ocr, source)
        except Exception as exc:
            return self._failed("provider_failed", f"PaddleOCR 识别失败: {exc}", retryable=True)
        plain_text = "\n".join(block.text for block in blocks if block.text)
        if not plain_text.strip():
            return self._failed("quality_insufficient", "PaddleOCR 未提取到可用文本")

        preview: dict[str, Any] | None = None
        artifact_dir_value = str(context.metadata.get("artifact_dir") or "")
        if artifact_dir_value:
            artifact_dir = Path(artifact_dir_value)
            preview_dir = artifact_dir / "ocr-pages"
            preview_dir.mkdir(parents=True, exist_ok=True)
            extension = source.suffix.lower() or ".png"
            artifact_name = f"ocr-page-0001{extension}"
            preview_path = preview_dir / artifact_name
            shutil.copyfile(source, preview_path)
            preview = {"filename": artifact_name, "relative_path": preview_path.relative_to(artifact_dir).as_posix(), "content_type": context.file.mime_type or "image/png"}
        page = DocumentPage(page_number=1, text=plain_text, blocks=blocks, width=width, height=height, preview=preview)
        document = DocumentResult(
            document_id=context.file.file_id,
            source_file=context.file,
            document_type="image",
            pages=[page],
            blocks=blocks,
            metadata={"width": width, "height": height, "mime_type": context.file.mime_type},
            representations={"plain_text": plain_text, "markdown": plain_text},
            parser={"name": self.manifest.name, "version": self.manifest.version},
            quality={"pages": 1, "text_chars": len(plain_text), "blocks": len(blocks)},
            provenance={"ocr": {"engine": "PaddleOCR", "device": self.device, "lang": self.lang}},
        )
        return ProviderResult(status="success", provider_name=self.manifest.name, document=document, metrics=document.quality)

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

    def _recognize_image(self, ocr: Any, image_path: Path) -> list[DocumentBlock]:
        if hasattr(ocr, "predict"):
            result = list(ocr.predict(str(image_path)))
            return self._blocks_from_predict_result(result[0] if result else None)
        if hasattr(ocr, "ocr"):
            return self._blocks_from_legacy_result(ocr.ocr(str(image_path), cls=True))
        raise TypeError("PaddleOCR 实例未提供 predict 或 ocr 方法")

    def _blocks_from_predict_result(self, result: Any) -> list[DocumentBlock]:
        data = self._as_mapping(result)
        payload = data.get("res", data)
        texts = self._first_value(payload, "rec_texts", "texts")
        scores = self._first_value(payload, "rec_scores", "scores")
        boxes = self._first_value(payload, "rec_boxes", "rec_polys", "dt_polys")
        return [
            DocumentBlock(
                kind="text",
                text=str(text),
                page_number=1,
                bbox=self._bbox(boxes[index]) if index < len(boxes) else None,
                metadata={"confidence": float(scores[index]) if index < len(scores) else None, "ocr": "paddleocr"},
            )
            for index, text in enumerate(texts)
            if str(text).strip()
        ]

    def _blocks_from_legacy_result(self, result: Any) -> list[DocumentBlock]:
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
                blocks.append(DocumentBlock(kind="text", text=text, page_number=1, bbox=self._bbox(line[0]), metadata={"confidence": float(text_score[1]) if len(text_score) > 1 else None, "ocr": "paddleocr"}))
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
