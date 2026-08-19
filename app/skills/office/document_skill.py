from pathlib import Path

from app.documents.models import ParseContext, ProviderError, SkillResult
from app.skills.base import Skill, SkillManifest
from app.skills.office.adapters.libreoffice import LibreOfficeProvider
from app.skills.providers import ProviderRegistry


class OfficeDocumentParseSkill(Skill):
    """统一的 Excel/PPT 解析入口，负责旧格式预转换和 Provider 调用。"""

    def __init__(self, name: str, direct_suffixes: set[str], legacy_targets: dict[str, str], providers: ProviderRegistry,
                 normal_provider: str, converter: LibreOfficeProvider):
        self.name = name
        self.direct_suffixes = direct_suffixes
        self.legacy_targets = legacy_targets
        self.providers = providers
        self.normal_provider = normal_provider
        self.converter = converter

    @property
    def manifest(self) -> SkillManifest:
        return SkillManifest(name=self.name, version="0.4.0", kind="document_parser",
                              input_types=sorted(self.direct_suffixes | set(self.legacy_targets)),
                              capabilities=["normal_parse", "tables", "images", "markdown"],
                              providers=[m["name"] for m in self.providers.list_manifests()])

    async def execute(self, context: ParseContext) -> SkillResult:
        source = Path(context.file.path)
        suffix = source.suffix.lower()
        conversion = None
        if suffix in self.legacy_targets:
            target = self.legacy_targets[suffix]
            convert_context = context.model_copy(deep=True)
            convert_context.metadata["target_format"] = target
            convert_context.metadata["output_dir"] = context.metadata.get("output_dir")
            converted = await self.converter.parse(convert_context)
            if converted.status != "success":
                return SkillResult(status="failed", skill_name=self.name, data={"conversion": converted.data}, warnings=converted.warnings, error=converted.error)
            conversion = converted.data.get("conversion")
            context.file.path = conversion["target_path"]
            context.metadata["converted_from"] = str(source)
        elif suffix not in self.direct_suffixes:
            return SkillResult(status="failed", skill_name=self.name,
                               error=ProviderError(code="unsupported_format", message=f"{self.name} 不支持格式: {suffix}", retryable=False))
        try:
            provider = self.providers.get(context.options.provider or self.normal_provider)
        except KeyError as exc:
            return SkillResult(status="failed", skill_name=self.name,
                               error=ProviderError(code="provider_not_found", message=str(exc), retryable=False))
        if "normal" not in provider.manifest.modes:
            return SkillResult(status="failed", skill_name=self.name,
                               error=ProviderError(code="provider_unsupported", message=f"Provider 不支持普通解析: {provider.manifest.name}", retryable=False))
        result = await provider.parse(context)
        if result.document is not None:
            result.document.source_file.path = str(source)
            result.document.provenance["source_path"] = str(source)
            if conversion:
                result.document.provenance["conversion"] = conversion
        if result.document is None:
            return SkillResult(status=result.status, skill_name=self.name, data={**result.data, "conversion": conversion} if conversion else result.data,
                               warnings=result.warnings, metrics=result.metrics, error=result.error)
        return SkillResult(status=result.status, skill_name=self.name,
                           data={"document": result.document.model_dump(), "conversion": conversion} if conversion else {"document": result.document.model_dump()},
                           warnings=result.warnings, metrics=result.metrics, error=result.error)
