import re
from pathlib import Path
from typing import Any, Literal

from app.documents.models import ParseContext, ProviderError, SkillResult
from app.skills.base import Skill, SkillManifest
from app.skills.office.adapters.libreoffice import LibreOfficeProvider


class RtfParseSkill(Skill):
    """根据 RTF 特征和直接解析质量选择直接或转换增强路径。"""

    name = "rtf.parse"
    version = "0.5.3"
    signals = {
        "rtf_table": (r"\\(?:trowd|cell|row|intbl)\b", 5),
        "rtf_picture": (r"\\(?:pict|pngblip|jpegblip)\b", 5),
        "rtf_object": (r"\\(?:object|objdata|objclass)\b", 8),
        "rtf_field": (r"\\(?:field|fldinst|fldrslt)\b", 3),
        "rtf_shape": (r"\\(?:shp|shpinst)\b", 4),
        "rtf_header_footer": (r"\\(?:header|footer)\b", 2),
        "rtf_footnote_annotation": (r"\\(?:footnote|annotation)\b", 2),
    }

    def __init__(
        self,
        text_skill: Skill,
        word_skill: Skill,
        converter: LibreOfficeProvider,
        routing_mode: Literal["auto", "direct", "convert"] = "auto",
        max_complexity_score: int = 5,
        direct_max_size_mb: int = 2,
        direct_min_text_ratio: float = 0.01,
    ) -> None:
        self.text_skill = text_skill
        self.word_skill = word_skill
        self.converter = converter
        self.routing_mode = routing_mode
        self.max_complexity_score = max_complexity_score
        self.direct_max_size_bytes = direct_max_size_mb * 1024 * 1024
        self.direct_min_text_ratio = direct_min_text_ratio

    @property
    def manifest(self) -> SkillManifest:
        return SkillManifest(name=self.name, version=self.version, kind="document_parser", input_types=[".rtf"],
                             capabilities=["direct_text", "auto_route", "docx_fallback", "routing_provenance"],
                             providers=["text.normal.stdlib", "office.convert.libreoffice", "word.normal.mammoth"])

    async def execute(self, context: ParseContext) -> SkillResult:
        source = Path(context.file.path)
        if source.suffix.lower() != ".rtf":
            return SkillResult(status="failed", skill_name=self.name,
                               error=ProviderError(code="unsupported_format", message="rtf.parse 只支持 .rtf", retryable=False))
        try:
            raw = source.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            return SkillResult(status="failed", skill_name=self.name,
                               error=ProviderError(code="provider_failed", message=str(exc), retryable=False))
        routing = self._routing_evidence(raw, source.stat().st_size)
        should_convert = self.routing_mode == "convert" or (self.routing_mode == "auto" and routing["complexity_score"] >= self.max_complexity_score)
        if should_convert:
            return await self._convert_then_parse(context, routing)
        direct = await self.text_skill.execute(context)
        accepted, reason = self._direct_quality(direct, source.stat().st_size)
        routing["direct_parse_attempted"] = True
        routing["direct_parse_quality"] = {"accepted": accepted, "reason": reason, "text_length": self._text_length(direct)}
        if self.routing_mode == "auto" and not accepted:
            routing["fallback_reason"] = reason
            return await self._convert_then_parse(context, routing)
        return self._with_routing(direct, routing, "direct_text", source_path=str(source))

    def _routing_evidence(self, raw: str, size_bytes: int) -> dict[str, Any]:
        found: list[str] = []
        score = 0
        for name, (pattern, weight) in self.signals.items():
            if re.search(pattern, raw, re.IGNORECASE):
                found.append(name)
                score += weight
        if size_bytes > self.direct_max_size_bytes:
            found.append("rtf_large_file")
            score += 2
        return {"mode": self.routing_mode, "signals": found, "complexity_score": score,
                "threshold": self.max_complexity_score, "source_size_bytes": size_bytes}

    async def _convert_then_parse(self, context: ParseContext, routing: dict[str, Any]) -> SkillResult:
        source = Path(context.file.path)
        conversion_context = context.model_copy(deep=True)
        conversion_context.metadata["target_format"] = "docx"
        conversion_context.metadata["output_dir"] = context.metadata.get("output_dir") or context.metadata.get("artifact_dir")
        converted = await self.converter.parse(conversion_context)
        if converted.status != "success" or not converted.data.get("conversion"):
            return SkillResult(status="failed", skill_name=self.name, data={"rtf_routing": routing, "conversion": converted.data.get("conversion")},
                               warnings=converted.warnings, metrics=converted.metrics, error=converted.error)
        conversion = converted.data["conversion"]
        parse_context = context.model_copy(deep=True)
        parse_context.file.path = conversion["target_path"]
        parsed = await self.word_skill.execute(parse_context)
        return self._with_routing(parsed, routing, "convert_to_docx", conversion, source_path=str(source))

    def _with_routing(self, result: SkillResult, routing: dict[str, Any], selected_path: str,
                      conversion: dict[str, Any] | None = None, source_path: str | None = None) -> SkillResult:
        routing["selected_path"] = selected_path
        data = dict(result.data)
        if conversion:
            data["conversion"] = conversion
        data["rtf_routing"] = routing
        document = data.get("document")
        if document:
            if source_path:
                document["source_file"]["path"] = source_path
            document.setdefault("provenance", {})["rtf_routing"] = routing
            if conversion:
                document["provenance"]["conversion"] = conversion
        return SkillResult(status=result.status, skill_name=self.name, data=data, warnings=result.warnings,
                           metrics=result.metrics, error=result.error)

    def _direct_quality(self, result: SkillResult, source_size: int) -> tuple[bool, str | None]:
        if result.status not in {"success", "partial"}:
            return False, "direct_parse_failed"
        text_length = self._text_length(result)
        if text_length == 0:
            return False, "direct_parse_empty"
        if source_size and text_length / source_size < self.direct_min_text_ratio:
            return False, "direct_parse_low_text_ratio"
        return True, None

    @staticmethod
    def _text_length(result: SkillResult) -> int:
        document = result.data.get("document") if isinstance(result.data, dict) else None
        return len(((document or {}).get("representations") or {}).get("plain_text", ""))
