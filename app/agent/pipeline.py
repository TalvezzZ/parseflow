from pathlib import Path
from time import perf_counter
from typing import Any

from app.agent.executor import SkillExecutor
from app.documents.models import ParseContext, PipelineResult, PipelineStep, ProviderError
from app.skills.office.adapters.libreoffice import LibreOfficeProvider


class OfficeParsePipeline:
    """执行 Office 预转换与对应解析 Skill，并返回可追踪步骤结果。"""

    routes = {
        ".doc": ("docx", "word.parse"),
        ".xls": ("xlsx", "excel.parse"),
        ".xlsm": ("xlsx", "excel.parse"),
        ".ppt": ("pptx", "ppt.parse"),
    }

    def __init__(self, executor: SkillExecutor, converter: LibreOfficeProvider) -> None:
        self.executor = executor
        self.converter = converter

    async def execute(self, context: ParseContext, *, convert_to_pdf: bool = False) -> PipelineResult:
        source = Path(context.file.path)
        steps: list[PipelineStep] = []
        started = perf_counter()
        current_path = source
        target_skill: str | None = None
        conversion: dict[str, Any] | None = None

        if convert_to_pdf:
            if "pdf" not in self.converter.supported_conversions.get(source.suffix.lower(), set()):
                return PipelineResult(status="failed", pipeline_name="office.pdf_parse", source_file=context.file,
                                      error=ProviderError(code="unsupported_conversion", message=f"不支持转换为 PDF: {source.suffix.lower()}"))
            target_format, target_skill = "pdf", "pdf.parse"
        if convert_to_pdf or source.suffix.lower() in self.routes:
            if not convert_to_pdf:
                target_format, target_skill = self.routes[source.suffix.lower()]
            conversion_context = context.model_copy(deep=True)
            conversion_context.metadata["target_format"] = target_format
            conversion_context.metadata["output_dir"] = context.metadata.get("output_dir")
            step_started = perf_counter()
            converted = await self.converter.parse(conversion_context)
            duration_ms = int((perf_counter() - step_started) * 1000)
            step = PipelineStep(name="office.convert", status=converted.status, duration_ms=duration_ms,
                                input_path=str(source), output_path=(converted.data.get("conversion") or {}).get("target_path"),
                                provider=converted.provider_name, warnings=converted.warnings, error=converted.error)
            steps.append(step)
            if converted.status != "success" or not converted.data.get("conversion"):
                return PipelineResult(status="failed", pipeline_name="office.pdf_parse" if convert_to_pdf else "office.parse", source_file=context.file,
                                      steps=steps, error=converted.error or ProviderError(code="conversion_failed", message="Office 转换失败"),
                                      duration_ms=int((perf_counter() - started) * 1000))
            conversion = converted.data["conversion"]
            current_path = Path(conversion["target_path"])
        else:
            target_skill = self._direct_skill(source.suffix.lower())

        parse_context = context.model_copy(deep=True)
        parse_context.file.path = str(current_path)
        step_started = perf_counter()
        parsed = await self.executor.execute(target_skill, parse_context)
        duration_ms = int((perf_counter() - step_started) * 1000)
        steps.append(PipelineStep(name=target_skill, status=parsed.status, duration_ms=duration_ms,
                                  input_path=str(current_path), provider=self._provider_from_result(parsed),
                                  warnings=parsed.warnings, error=parsed.error))
        data = dict(parsed.data)
        if conversion:
            data["conversion"] = conversion
        return PipelineResult(status=parsed.status, pipeline_name="office.pdf_parse" if convert_to_pdf else "office.parse", source_file=context.file,
                              steps=steps, result=data, warnings=parsed.warnings,
                              error=parsed.error, duration_ms=int((perf_counter() - started) * 1000))

    @staticmethod
    def _direct_skill(suffix: str) -> str:
        if suffix in {".docx"}:
            return "word.parse"
        if suffix in {".xlsx", ".xlsm"}:
            return "excel.parse"
        if suffix in {".pptx"}:
            return "ppt.parse"
        raise ValueError(f"不支持进入 Office 解析 Pipeline 的格式: {suffix}")

    @staticmethod
    def _provider_from_result(result) -> str | None:
        attempts = result.data.get("attempts") if isinstance(result.data, dict) else None
        if attempts and isinstance(attempts, list):
            return attempts[-1].get("provider")
        document = result.data.get("document") if isinstance(result.data, dict) else None
        return ((document or {}).get("parser") or {}).get("name")
