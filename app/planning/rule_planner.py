import uuid
from pathlib import Path
from typing import Literal

from app.planning.models import ParsePlan, PlanStep
from app.skills.registry import SkillRegistry
from app.skills.text_formats import TEXT_SUFFIXES


class RuleBasedPlanner:
    """无模型的受控文件类型路由器，只生成已注册的顶层能力。"""

    routes = {
        ".pdf": ("pdf.parse", "识别为 PDF，使用 PDF 解析 Skill"),
        ".docx": ("word.parse", "识别为 DOCX，使用 Word 解析 Skill"),
        ".doc": ("office.parse_pipeline", "识别为旧版 DOC，需要转换后解析"),
        ".xlsx": ("excel.parse", "识别为 XLSX，使用 Excel 解析 Skill"),
        ".xlsm": ("excel.parse", "识别为 XLSM，使用 Excel 解析 Skill"),
        ".xls": ("office.parse_pipeline", "识别为旧版 XLS，需要转换后解析"),
        ".pptx": ("ppt.parse", "识别为 PPTX，使用 PowerPoint 解析 Skill"),
        ".ppt": ("office.parse_pipeline", "识别为旧版 PPT，需要转换后解析"),
        ".rtf": ("rtf.parse", "识别为 RTF，由 RTF Skill 自动选择直接或 DOCX 转换路径"),
        ".txt": ("text.parse", "识别为文本文件"),
        ".md": ("text.parse", "识别为 Markdown 文件"),
        ".markdown": ("text.parse", "识别为 Markdown 文件"),
        ".csv": ("text.parse", "识别为 CSV 表格"),
        ".tsv": ("text.parse", "识别为 TSV 表格"),
        ".html": ("text.parse", "识别为 HTML 文件"),
        ".htm": ("text.parse", "识别为 HTML 文件"),
        ".xml": ("text.parse", "识别为 XML 文件"),
        ".png": ("image.parse", "识别为图片，使用本地 OCR 解析 Skill"),
        ".jpg": ("image.parse", "识别为图片，使用本地 OCR 解析 Skill"),
        ".jpeg": ("image.parse", "识别为图片，使用本地 OCR 解析 Skill"),
        ".webp": ("image.parse", "识别为图片，使用本地 OCR 解析 Skill"),
        ".bmp": ("image.parse", "识别为图片，使用本地 OCR 解析 Skill"),
        ".tif": ("image.parse", "识别为图片，使用本地 OCR 解析 Skill"),
        ".tiff": ("image.parse", "识别为图片，使用本地 OCR 解析 Skill"),
        ".mp3": ("audio.prepare", "识别为音频，当前执行媒体预处理"),
        ".wav": ("audio.prepare", "识别为音频，当前执行媒体预处理"),
        ".m4a": ("audio.prepare", "识别为音频，当前执行媒体预处理"),
        ".aac": ("audio.prepare", "识别为音频，当前执行媒体预处理"),
        ".flac": ("audio.prepare", "识别为音频，当前执行媒体预处理"),
        ".ogg": ("audio.prepare", "识别为音频，当前执行媒体预处理"),
        ".mp4": ("video.prepare", "识别为视频，当前执行媒体预处理"),
        ".mov": ("video.prepare", "识别为视频，当前执行媒体预处理"),
        ".mkv": ("video.prepare", "识别为视频，当前执行媒体预处理"),
        ".avi": ("video.prepare", "识别为视频，当前执行媒体预处理"),
        ".webm": ("video.prepare", "识别为视频，当前执行媒体预处理"),
    }
    for _text_suffix in TEXT_SUFFIXES - {".rtf"}:
        routes.setdefault(_text_suffix, ("text.parse", f"识别为 {_text_suffix.lstrip('.').upper()} 文本文件"))
    del _text_suffix

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    def create(self, path: str, goal: str | None = None,
               parse_mode: Literal["auto", "standard", "enhanced"] = "auto") -> ParsePlan:
        """Create a safe plan for the requested quality tier.

        ``standard`` preserves the fast native route. ``enhanced`` normalizes
        PDF/image-like Office documents through LibreOffice into PDF before OCR
        or layout-aware parsing. ``auto`` stays compatible with prior routing
        while using enhanced normalization for legacy Office formats.
        """
        suffix = Path(path).suffix.lower()
        if parse_mode not in {"auto", "standard", "enhanced"}:
            raise ValueError(f"不支持的解析模式: {parse_mode}")
        route = self.routes.get(suffix)
        if route is None:
            raise ValueError(f"当前系统无法自动规划该文件格式: {suffix or '<无后缀>'}")
        skill_name, reason = route
        warnings = []

        # Enhanced parsing is intentionally limited to document formats where
        # PDF normalization improves visual-layout/OCR fidelity. Native Office
        # extractors remain the default for structured spreadsheets/slides.
        enhanced_office = {".doc", ".docx", ".rtf", ".odt", ".ppt", ".pptx", ".odp", ".xls", ".xlsx", ".xlsm", ".ods"}
        if parse_mode == "enhanced" and suffix in enhanced_office:
            skill_name = "office.pdf_parse_pipeline"
            reason = "已选择增强解析：先转换为 PDF，再按版面和 OCR 路径解析"
        elif parse_mode == "enhanced" and suffix in {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}:
            reason += "（增强模式：优先 OCR/版面解析）"
        elif parse_mode == "standard":
            reason += "（标准模式：使用原生快速解析路径）"

        if skill_name not in {"office.parse_pipeline", "office.pdf_parse_pipeline"}:
            self.registry.get(skill_name)
        if skill_name in {"audio.prepare", "video.prepare"}:
            warnings.append("当前未配置 ASR Provider，将完成媒体预处理但不会生成语音转录文本。")
        return ParsePlan(plan_id=f"plan_{uuid.uuid4().hex}", goal=goal or "自动提取文件中的可用结构化内容",
                         parse_mode=parse_mode, steps=[PlanStep(step_id="step-1", skill_name=skill_name, reason=reason)],
                         warnings=warnings)
