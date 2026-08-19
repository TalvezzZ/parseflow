from app.documents.models import ParseContext, SkillResult
from app.skills.base import Skill, SkillManifest
from app.skills.providers import ProviderRegistry


class MediaPrepareSkill(Skill):
    """音频或视频预处理入口；本版本只生成 ASR 前置产物，不执行语音识别。"""

    version = "0.5.0"

    def __init__(
        self,
        name: str,
        media_kind: str,
        suffixes: set[str],
        providers: ProviderRegistry,
        default_provider: str | None = None,
        fallback_providers: list[str] | None = None,
    ) -> None:
        self.name = name
        self.media_kind = media_kind
        self.suffixes = suffixes
        self.providers = providers
        self.default_provider = default_provider
        self.fallback_providers = fallback_providers or []

    @property
    def manifest(self) -> SkillManifest:
        return SkillManifest(
            name=self.name,
            version=self.version,
            kind="media_preprocessor",
            input_types=sorted(self.suffixes),
            capabilities=["media_probe", "audio_normalization", "asr_preparation"],
            providers=[manifest["name"] for manifest in self.providers.list_manifests()],
            transcript_available=False,
        )

    async def execute(self, context: ParseContext) -> SkillResult:
        suffix = context.file.path.lower().rsplit(".", 1)[-1] if "." in context.file.path else ""
        if f".{suffix}" not in self.suffixes:
            return SkillResult(
                status="failed",
                skill_name=self.name,
                error={"code": "unsupported_format", "message": f"{self.name} 不支持该文件格式", "retryable": False},
            )
        context.metadata["media_kind"] = self.media_kind
        attempts: list[dict] = []
        for provider_name in self._provider_names(context):
            try:
                provider = self.providers.get(provider_name)
            except KeyError as exc:
                return SkillResult(
                    status="failed",
                    skill_name=self.name,
                    data={"attempts": attempts},
                    error={"code": "provider_not_found", "message": str(exc), "retryable": False},
                )
            if "prepare" not in provider.manifest.modes:
                return SkillResult(
                    status="failed",
                    skill_name=self.name,
                    data={"attempts": attempts},
                    error={"code": "provider_unsupported", "message": f"Provider {provider_name} 不支持媒体预处理", "retryable": False},
                )
            try:
                result = await provider.parse(context)
            except Exception as exc:
                attempts.append({"provider": provider_name, "status": "failed", "error": str(exc)})
                if context.options.fallback_enabled:
                    continue
                break
            attempts.append({"provider": provider_name, "status": result.status, "error": result.error})
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
            error=last_error or {"code": "media_prepare_failed", "message": "没有可用的媒体预处理 Provider", "retryable": False},
        )

    def _provider_names(self, context: ParseContext) -> list[str]:
        if context.options.provider:
            return [context.options.provider]
        names = [self.default_provider] if self.default_provider else []
        names.extend(self.fallback_providers)
        if not names:
            names.append(self.providers.first_for("prepare").manifest.name)
        return list(dict.fromkeys(name for name in names if name))
