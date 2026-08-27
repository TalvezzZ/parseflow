from app.documents.models import ParseContext, ProviderError, SkillResult
from app.skills.base import Skill, SkillManifest
from app.skills.providers import PdfInspector, ProviderRegistry


class PdfParseSkill(Skill):
    """PDF 统一入口；内部负责检测、Provider 选择、校验和降级。"""

    name = "pdf.parse"
    version = "0.1.0"

    def __init__(
        self,
        inspector: PdfInspector,
        providers: ProviderRegistry,
        normal_provider: str | None = None,
        fallback_providers: list[str] | None = None,
        ocr_provider: str | None = None,
    ) -> None:
        self.inspector = inspector
        self.providers = providers
        self.normal_provider = normal_provider
        self.fallback_providers = fallback_providers or []
        self.ocr_provider = ocr_provider

    @property
    def manifest(self) -> SkillManifest:
        return SkillManifest(
            name=self.name,
            version=self.version,
            kind="document_parser",
            input_types=["application/pdf"],
            capabilities=["pdf_detection", "normal_parse", "markdown"],
            providers=[manifest["name"] for manifest in self.providers.list_manifests()],
        )

    async def execute(self, context: ParseContext) -> SkillResult:
        try:
            inspection = await self.inspector.inspect(context)
        except Exception as exc:
            return SkillResult(
                status="failed",
                skill_name=self.name,
                error={"code": "inspection_failed", "message": str(exc), "retryable": False},
            )

        context.inspection = inspection
        requires_ocr = inspection.pdf_type != "text_based" or inspection.ocr_recommended
        requested_ocr = context.options.strategy == "ocr_first" and self.ocr_provider is not None
        mode = "ocr" if requires_ocr or requested_ocr else "normal"
        provider_names = self._provider_names(context, mode)
        attempts: list[dict] = []

        for provider_name in provider_names:
            try:
                provider = self.providers.get(provider_name)
            except KeyError as exc:
                return SkillResult(
                    status="failed",
                    skill_name=self.name,
                    data={"inspection": inspection.model_dump(), "attempts": attempts},
                    error={"code": "provider_not_found", "message": str(exc), "retryable": False},
                )

            if mode not in provider.manifest.modes:
                return SkillResult(
                    status="failed",
                    skill_name=self.name,
                    data={"inspection": inspection.model_dump(), "attempts": attempts},
                    error={
                        "code": "provider_unsupported",
                        "message": f"Provider {provider_name} 不支持解析模式: {mode}",
                        "retryable": False,
                    },
                )

            try:
                result = await provider.parse(context)
            except Exception as exc:  # 第三方 Provider 异常必须进入统一 fallback 流程
                result = None
                attempts.append({
                    "provider": provider_name,
                    "status": "failed",
                    "error": ProviderError(code="provider_exception", message=str(exc), retryable=True),
                })
                if not context.options.fallback_enabled:
                    break
                continue
            attempts.append({"provider": provider_name, "status": result.status, "error": result.error})
            if result.status in {"success", "partial"} and result.document is not None:
                result.document.provenance["inspection"] = inspection.model_dump()
                result.document.provenance["attempts"] = attempts
                result.document.provenance["strategy"] = context.options.strategy
                return SkillResult(
                    status=result.status,
                    skill_name=self.name,
                    data={"document": result.document.model_dump(), "inspection": inspection.model_dump()},
                    warnings=result.warnings,
                    metrics=result.metrics,
                )

            if not context.options.fallback_enabled:
                break

        last_error = attempts[-1].get("error") if attempts else None
        unavailable_error = {"code": "ocr_not_configured", "message": "该 PDF 需要 OCR，但本地 PaddleOCR Provider 未启用。请设置 PDF_OCR_ENABLED=true 并安装 OCR 依赖。", "retryable": False} if mode == "ocr" else {"code": "parse_failed", "message": "没有可用的 Provider", "retryable": False}
        return SkillResult(
            status="failed",
            skill_name=self.name,
            data={"inspection": inspection.model_dump(), "attempts": attempts},
            error=last_error or unavailable_error,
        )

    def _provider_names(self, context: ParseContext, mode: str) -> list[str]:
        explicit = context.options.provider
        if explicit:
            return [explicit]
        if mode == "ocr":
            return [self.ocr_provider] if self.ocr_provider else []
        names = [self.normal_provider] if self.normal_provider else []
        names.extend(self.fallback_providers)
        if not names:
            names.append(self.providers.first_for(mode).manifest.name)
        return list(dict.fromkeys(name for name in names if name))
