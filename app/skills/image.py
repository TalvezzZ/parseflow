from app.documents.models import ParseContext, ProviderError, SkillResult
from app.skills.base import Skill, SkillManifest
from app.skills.providers import ProviderRegistry


class ImageParseSkill(Skill):
    """Standalone image OCR entrypoint with a configurable provider."""

    name = "image.parse"
    version = "0.1.0"

    def __init__(self, providers: ProviderRegistry, ocr_provider: str | None = None) -> None:
        self.providers = providers
        self.ocr_provider = ocr_provider

    @property
    def manifest(self) -> SkillManifest:
        return SkillManifest(
            name=self.name,
            version=self.version,
            kind="document_parser",
            input_types=["image/png", "image/jpeg", "image/webp", "image/bmp", "image/tiff"],
            capabilities=["local_ocr", "text", "markdown", "bbox", "confidence"],
            providers=[manifest["name"] for manifest in self.providers.list_manifests()],
        )

    async def execute(self, context: ParseContext) -> SkillResult:
        provider_name = context.options.provider or self.ocr_provider
        if not provider_name:
            return self._failed("ocr_not_configured", "图片 OCR Provider 未启用。请设置 IMAGE_OCR_ENABLED=true 并安装 OCR 依赖。")
        try:
            provider = self.providers.get(provider_name)
        except KeyError as exc:
            return self._failed("provider_not_found", str(exc))
        if "ocr" not in provider.manifest.modes:
            return self._failed("provider_unsupported", f"Provider {provider_name} 不支持 OCR 模式")

        try:
            result = await provider.parse(context)
        except Exception as exc:
            return self._failed("provider_exception", str(exc), retryable=True)
        if result.status not in {"success", "partial"} or result.document is None:
            return SkillResult(
                status="failed",
                skill_name=self.name,
                data={"attempts": [{"provider": provider_name, "status": result.status, "error": result.error}]},
                error=result.error or ProviderError(code="parse_failed", message="图片 OCR 未返回解析结果"),
                warnings=result.warnings,
                metrics=result.metrics,
            )

        result.document.provenance["attempts"] = [{"provider": provider_name, "status": result.status, "error": result.error}]
        return SkillResult(
            status=result.status,
            skill_name=self.name,
            data={"document": result.document.model_dump()},
            warnings=result.warnings,
            metrics=result.metrics,
        )

    def _failed(self, code: str, message: str, retryable: bool = False) -> SkillResult:
        return SkillResult(
            status="failed",
            skill_name=self.name,
            error={"code": code, "message": message, "retryable": retryable},
        )
