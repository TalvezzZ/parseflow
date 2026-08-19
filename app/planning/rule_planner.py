import uuid
from pathlib import Path

from app.planning.models import ParsePlan, PlanStep
from app.skills.registry import SkillRegistry


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
        ".mp3": ("audio.prepare", "识别为音频，当前执行媒体预处理"),
        ".wav": ("audio.prepare", "识别为音频，当前执行媒体预处理"),
        ".m4a": ("audio.prepare", "识别为音频，当前执行媒体预处理"),
        ".mp4": ("video.prepare", "识别为视频，当前执行媒体预处理"),
        ".mov": ("video.prepare", "识别为视频，当前执行媒体预处理"),
        ".mkv": ("video.prepare", "识别为视频，当前执行媒体预处理"),
        ".avi": ("video.prepare", "识别为视频，当前执行媒体预处理"),
        ".webm": ("video.prepare", "识别为视频，当前执行媒体预处理"),
    }

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    def create(self, path: str, goal: str | None = None) -> ParsePlan:
        suffix = Path(path).suffix.lower()
        route = self.routes.get(suffix)
        if route is None:
            raise ValueError(f"当前系统无法自动规划该文件格式: {suffix or '<无后缀>'}")
        skill_name, reason = route
        self.registry.get(skill_name)
        warnings = []
        if skill_name in {"audio.prepare", "video.prepare"}:
            warnings.append("当前未配置 ASR Provider，将完成媒体预处理但不会生成语音转录文本。")
        return ParsePlan(plan_id=f"plan_{uuid.uuid4().hex}", goal=goal or "自动提取文件中的可用结构化内容", steps=[
            PlanStep(step_id="step-1", skill_name=skill_name, reason=reason),
        ], warnings=warnings)
