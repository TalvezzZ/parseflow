from app.documents.models import ParseContext, SkillResult
from app.skills.base import Skill, SkillManifest
from app.skills.providers import ProviderRegistry


class WordParseSkill(Skill):
    """Word 文档统一入口；0.2.0 支持 DOCX。"""

    name = "word.parse"
    version = "0.2.0"

    def __init__(
        self,
        providers: ProviderRegistry,
        normal_provider: str | None = None,
        fallback_providers: list[str] | None = None,
    ) -> None:
        self.providers = providers
        self.normal_provider = normal_provider
        self.fallback_providers = fallback_providers or []

    @property
    def manifest(self) -> SkillManifest:
        return SkillManifest(
            name=self.name,
            version=self.version,
            kind="document_parser",
            input_types=["application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
            capabilities=["text", "html", "markdown", "tables", "images"],
            providers=[manifest["name"] for manifest in self.providers.list_manifests()],
        )

    async def execute(self, context: ParseContext) -> SkillResult:
        if not context.file.path.lower().endswith(".docx"):
            return SkillResult(
                status="failed",
                skill_name=self.name,
                error={"code": "unsupported_format", "message": "0.2.0 只支持 .docx 文件", "retryable": False},
            )

        names = self._provider_names(context)
        attempts: list[dict] = []
        for name in names:
            try:
                provider = self.providers.get(name)
            except KeyError as exc:
                return SkillResult(
                    status="failed",
                    skill_name=self.name,
                    data={"attempts": attempts},
                    error={"code": "provider_not_found", "message": str(exc), "retryable": False},
                )
            if "normal" not in provider.manifest.modes:
                return SkillResult(
                    status="failed",
                    skill_name=self.name,
                    error={"code": "provider_unsupported", "message": f"Provider {name} 不支持普通解析", "retryable": False},
                )
            try:
                result = await provider.parse(context)
            except Exception as exc:
                result = None
                attempts.append({"provider": name, "status": "failed", "error": str(exc)})
                if context.options.fallback_enabled:
                    continue
                break
            attempts.append({"provider": name, "status": result.status, "error": result.error})
            if result.status in {"success", "partial"} and result.document is not None:
                result.document.provenance["attempts"] = attempts
                return SkillResult(
                    status=result.status,
                    skill_name=self.name,
                    data={"document": result.document.model_dump()},
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
            error=last_error or {"code": "parse_failed", "message": "没有可用的 Word Provider", "retryable": False},
        )

    def _provider_names(self, context: ParseContext) -> list[str]:
        if context.options.provider:
            return [context.options.provider]
        names = [self.normal_provider] if self.normal_provider else []
        names.extend(self.fallback_providers)
        if not names:
            names.append(self.providers.first_for("normal").manifest.name)
        return list(dict.fromkeys(name for name in names if name))
