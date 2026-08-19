from app.documents.models import ParseContext, SkillResult
from app.skills.base import Skill, SkillManifest
from app.skills.providers import ProviderRegistry


class OfficeConvertSkill(Skill):
    """Office 文件转换统一入口。"""

    name = "office.convert"
    version = "0.3.0"

    def __init__(
        self,
        providers: ProviderRegistry,
        default_provider: str | None = None,
        fallback_providers: list[str] | None = None,
    ) -> None:
        self.providers = providers
        self.default_provider = default_provider
        self.fallback_providers = fallback_providers or []

    @property
    def manifest(self) -> SkillManifest:
        return SkillManifest(
            name=self.name,
            version=self.version,
            kind="file_converter",
            input_types=["office_document"],
            capabilities=["doc_to_docx", "xls_to_xlsx", "ppt_to_pptx", "office_to_pdf"],
            providers=[manifest["name"] for manifest in self.providers.list_manifests()],
        )

    async def execute(self, context: ParseContext) -> SkillResult:
        provider_names = self._provider_names(context)
        attempts: list[dict] = []
        for name in provider_names:
            try:
                provider = self.providers.get(name)
            except KeyError as exc:
                return SkillResult(
                    status="failed",
                    skill_name=self.name,
                    data={"attempts": attempts},
                    error={"code": "provider_not_found", "message": str(exc), "retryable": False},
                )
            if "convert" not in provider.manifest.modes:
                return SkillResult(
                    status="failed",
                    skill_name=self.name,
                    error={"code": "provider_unsupported", "message": f"Provider {name} 不支持文件转换", "retryable": False},
                )
            try:
                result = await provider.parse(context)
            except Exception as exc:
                attempts.append({"provider": name, "status": "failed", "error": str(exc)})
                if context.options.fallback_enabled:
                    continue
                break
            attempts.append({"provider": name, "status": result.status, "error": result.error})
            if result.status in {"success", "partial"}:
                return SkillResult(
                    status=result.status,
                    skill_name=self.name,
                    data={**result.data, "attempts": attempts},
                    warnings=result.warnings,
                    metrics=result.metrics,
                )
            if not context.options.fallback_enabled:
                break

        last_error = attempts[-1].get("error") if attempts else None
        return SkillResult(
            status="failed",
            skill_name=self.name,
            data={"attempts": attempts},
            error=last_error or {"code": "conversion_failed", "message": "没有可用的转换 Provider", "retryable": False},
        )

    def _provider_names(self, context: ParseContext) -> list[str]:
        if context.options.provider:
            return [context.options.provider]
        names = [self.default_provider] if self.default_provider else []
        names.extend(self.fallback_providers)
        if not names:
            names.append(self.providers.first_for("convert").manifest.name)
        return list(dict.fromkeys(name for name in names if name))
