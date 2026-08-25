from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.skills.text_formats import TEXT_SUFFIXES


class Settings(BaseSettings):
    """应用配置。"""

    app_name: str = "parse-agent"
    app_env: str = "dev"
    log_level: str = "INFO"
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_model: str | None = None
    pdf_normal_provider: str = "pdf.normal.pdf-inspector"
    pdf_fallback_providers: str = ""
    pdf_ocr_provider: str = "pdf.ocr.paddle"
    pdf_ocr_enabled: bool = True
    pdf_ocr_device: str = "cpu"
    pdf_ocr_lang: str = "ch"
    pdf_ocr_max_pages: int = 50
    pdf_ocr_render_scale: float = 2.0
    pdf_ocr_max_page_pixels: int = 20_000_000
    image_ocr_provider: str = "image.ocr.paddle"
    image_ocr_enabled: bool = True
    image_ocr_device: str = "cpu"
    image_ocr_lang: str = "ch"
    image_ocr_max_pixels: int = 20_000_000
    word_normal_provider: str = "word.normal.mammoth"
    word_fallback_providers: str = ""
    office_converter_provider: str = "office.convert.libreoffice"
    office_converter_command: str = "soffice"
    office_converter_timeout_seconds: int = 300
    media_prepare_provider: str = "media.prepare.ffmpeg"
    media_ffmpeg_command: str = "ffmpeg"
    media_ffprobe_command: str = "ffprobe"
    media_timeout_seconds: int = 300
    media_max_duration_seconds: int = 14400
    media_max_file_size_mb: int = 2048
    excel_normal_provider: str = "excel.normal.openpyxl"
    excel_max_sheets: int = 30
    excel_max_semantic_cells: int = 1000000
    excel_max_sheet_rows: int = 100000
    excel_max_columns: int = 1000
    excel_max_file_size_mb: int = 100
    excel_max_uncompressed_size_mb: int = 500
    excel_max_zip_entries: int = 10000
    excel_max_sheet_xml_size_mb: int = 100
    ppt_normal_provider: str = "ppt.normal.python-pptx"
    task_queue_enabled: bool = True
    task_max_concurrent_executions: int = 2
    task_queue_max_size: int = 100
    task_default_timeout_seconds: int = 1800
    task_result_ttl_seconds: int = 86400
    task_cleanup_interval_seconds: int = 300
    task_callback_timeout_seconds: int = 15
    file_storage_dir: str = "./data/files"
    file_max_size_mb: int = 100
    file_allowed_suffixes: str = ",".join(sorted({
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".ppt", ".pptx",
        ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff",
        ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
        ".mp4", ".mov", ".mkv", ".avi", ".webm",
    } | set(TEXT_SUFFIXES)))
    api_key: str | None = None
    # Remote MCP exposes server-local file paths; require API_KEY when enabled.
    mcp_http_enabled: bool = False
    # Comma-separated Host headers accepted by MCP DNS-rebinding protection.
    mcp_allowed_hosts: str = "localhost,127.0.0.1,localhost:*,127.0.0.1:*,[::1]:*"
    text_max_file_size_mb: int = 20
    text_max_table_rows: int = 100000
    text_max_table_columns: int = 1000
    text_max_xml_nodes: int = 100000
    text_max_xml_depth: int = 128
    rtf_routing_mode: Literal["auto", "direct", "convert"] = "auto"
    rtf_direct_max_complexity_score: int = 5
    rtf_direct_max_size_mb: int = 2
    rtf_direct_min_text_ratio: float = 0.01

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
