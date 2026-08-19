from app.skills.base import EchoSkill, Skill
from app.skills.pdf.adapters.inspector import PdfInspectorAdapter, PdfInspectorParserProvider
from app.skills.pdf.skill import PdfParseSkill
from app.skills.word.adapters.mammoth import MammothProvider
from app.skills.word.skill import WordParseSkill
from app.skills.office.adapters.libreoffice import LibreOfficeProvider
from app.skills.office.skill import OfficeConvertSkill
from app.skills.media.adapters.ffmpeg import FfmpegMediaPrepareProvider
from app.skills.media.skill import MediaPrepareSkill
from app.skills.excel.adapters.openpyxl import OpenpyxlProvider
from app.skills.ppt.adapters.python_pptx import PythonPptxProvider
from app.skills.office.document_skill import OfficeDocumentParseSkill
from app.skills.text import PlainTextProvider, TextParseSkill
from app.skills.rtf import RtfParseSkill
from app.skills.providers import ProviderRegistry
from app.config import get_settings


class SkillRegistry:
    """Skill 注册表，负责发现和获取可执行能力。"""

    def __init__(self, skills: list[Skill] | None = None) -> None:
        self._skills: dict[str, Skill] = {}
        for skill in skills or []:
            self.register(skill)

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            raise ValueError(f"Skill 已注册: {skill.name}")
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise KeyError(f"未找到 Skill: {name}") from exc

    def list_manifests(self) -> list[dict]:
        return [dict(skill.manifest) for skill in self._skills.values()]


def create_default_registry() -> SkillRegistry:
    settings = get_settings()
    providers = ProviderRegistry([PdfInspectorParserProvider()])
    word_providers = ProviderRegistry([MammothProvider()])
    office_providers = ProviderRegistry([
        LibreOfficeProvider(
            command=settings.office_converter_command,
            timeout_seconds=settings.office_converter_timeout_seconds,
        ),
    ])
    media_providers = ProviderRegistry([
        FfmpegMediaPrepareProvider(
            ffmpeg_command=settings.media_ffmpeg_command,
            ffprobe_command=settings.media_ffprobe_command,
            timeout_seconds=settings.media_timeout_seconds,
            max_duration_seconds=settings.media_max_duration_seconds,
            max_file_size_mb=settings.media_max_file_size_mb,
        ),
    ])
    excel_providers = ProviderRegistry([OpenpyxlProvider(
        max_sheets=settings.excel_max_sheets,
        max_semantic_cells=settings.excel_max_semantic_cells,
        max_sheet_rows=settings.excel_max_sheet_rows,
        max_columns=settings.excel_max_columns,
        max_file_size_mb=settings.excel_max_file_size_mb,
        max_uncompressed_size_mb=settings.excel_max_uncompressed_size_mb,
        max_zip_entries=settings.excel_max_zip_entries,
        max_sheet_xml_size_mb=settings.excel_max_sheet_xml_size_mb,
    )])
    ppt_providers = ProviderRegistry([PythonPptxProvider()])
    text_providers = ProviderRegistry([PlainTextProvider(
        max_file_size_mb=settings.text_max_file_size_mb,
        max_table_rows=settings.text_max_table_rows,
        max_table_columns=settings.text_max_table_columns,
    )])
    text_skill = TextParseSkill(providers=text_providers)
    word_skill = WordParseSkill(
        providers=word_providers,
        normal_provider=settings.word_normal_provider,
        fallback_providers=[name.strip() for name in settings.word_fallback_providers.split(",") if name.strip()],
    )
    rtf_skill = RtfParseSkill(
        text_skill=text_skill,
        word_skill=word_skill,
        converter=LibreOfficeProvider(command=settings.office_converter_command, timeout_seconds=settings.office_converter_timeout_seconds),
        routing_mode=settings.rtf_routing_mode,
        max_complexity_score=settings.rtf_direct_max_complexity_score,
        direct_max_size_mb=settings.rtf_direct_max_size_mb,
        direct_min_text_ratio=settings.rtf_direct_min_text_ratio,
    )
    return SkillRegistry([
        EchoSkill(),
        text_skill,
        rtf_skill,
        PdfParseSkill(
            inspector=PdfInspectorAdapter(),
            providers=providers,
            normal_provider=settings.pdf_normal_provider,
            fallback_providers=[
                name.strip()
                for name in settings.pdf_fallback_providers.split(",")
                if name.strip()
            ],
        ),
        word_skill,
        OfficeConvertSkill(
            providers=office_providers,
            default_provider=settings.office_converter_provider,
        ),
        MediaPrepareSkill(
            name="audio.prepare",
            media_kind="audio",
            suffixes={".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"},
            providers=media_providers,
            default_provider=settings.media_prepare_provider,
        ),
        MediaPrepareSkill(
            name="video.prepare",
            media_kind="video",
            suffixes={".mp4", ".mov", ".mkv", ".avi", ".webm"},
            providers=media_providers,
            default_provider=settings.media_prepare_provider,
        ),
        OfficeDocumentParseSkill(
            name="excel.parse",
            direct_suffixes={".xlsx", ".xlsm"},
            legacy_targets={".xls": "xlsx", ".xlsm": "xlsx"},
            providers=excel_providers,
            normal_provider=settings.excel_normal_provider,
            converter=LibreOfficeProvider(command=settings.office_converter_command, timeout_seconds=settings.office_converter_timeout_seconds),
        ),
        OfficeDocumentParseSkill(
            name="ppt.parse",
            direct_suffixes={".pptx"},
            legacy_targets={".ppt": "pptx"},
            providers=ppt_providers,
            normal_provider=settings.ppt_normal_provider,
            converter=LibreOfficeProvider(command=settings.office_converter_command, timeout_seconds=settings.office_converter_timeout_seconds),
        ),
    ])
