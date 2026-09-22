from typing import Any, Literal

from pydantic import BaseModel, Field


class FileInput(BaseModel):
    """待处理文件的最小描述。"""

    file_id: str = Field(description="文件唯一标识")
    path: str = Field(description="解析服务可访问的本地文件路径")
    filename: str | None = Field(default=None, description="原始文件名")
    mime_type: str | None = Field(default=None, description="文件 MIME 类型")


class ParseOptions(BaseModel):
    """解析请求中的可选策略。"""

    provider: str | None = Field(default=None, description="显式指定 Provider")
    fallback_enabled: bool = Field(default=True, description="主 Provider 失败时是否允许降级")
    strategy: Literal["auto", "text_first", "table_first", "ocr_first"] = Field(default="auto", description="服务端批准的受控解析目标")


class PdfParseRequest(BaseModel):
    """0.1.0 本地 PDF 解析接口请求。"""

    file_id: str = Field(description="文件唯一标识")
    path: str = Field(description="解析服务可访问的本地 PDF 路径")
    filename: str | None = None
    mime_type: str = "application/pdf"
    provider: str | None = Field(default=None, description="可选的 Provider 覆盖")
    fallback_enabled: bool = True

    def to_context(self) -> "ParseContext":
        return ParseContext(
            file=FileInput(
                file_id=self.file_id,
                path=self.path,
                filename=self.filename,
                mime_type=self.mime_type,
            ),
            options=ParseOptions(provider=self.provider, fallback_enabled=self.fallback_enabled),
        )


class DocumentParseRequest(BaseModel):
    """通用本地文档解析请求；0.2.0 首先支持 DOCX。"""

    file_id: str = Field(description="文件唯一标识")
    path: str = Field(description="解析服务可访问的本地文档路径")
    filename: str | None = None
    mime_type: str | None = None
    provider: str | None = Field(default=None, description="可选的 Provider 覆盖")
    fallback_enabled: bool = True

    def to_context(self) -> "ParseContext":
        return ParseContext(
            file=FileInput(
                file_id=self.file_id,
                path=self.path,
                filename=self.filename,
                mime_type=self.mime_type,
            ),
            options=ParseOptions(provider=self.provider, fallback_enabled=self.fallback_enabled),
        )


class OfficePipelineRequest(BaseModel):
    """Office 转换后自动解析的 Pipeline 请求。"""

    file_id: str = Field(description="文件唯一标识")
    path: str = Field(description="解析服务可访问的本地 Office 文件路径")
    filename: str | None = None
    mime_type: str | None = None
    output_dir: str | None = Field(default=None, description="转换产物输出目录")
    timeout_seconds: int = Field(default=300, ge=1, le=3600)

    def to_context(self) -> "ParseContext":
        return ParseContext(
            file=FileInput(file_id=self.file_id, path=self.path, filename=self.filename, mime_type=self.mime_type),
            metadata={"output_dir": self.output_dir, "timeout_seconds": self.timeout_seconds},
        )


class MediaPrepareRequest(BaseModel):
    """音频或视频预处理请求；本版本不执行 ASR。"""

    file_id: str = Field(description="文件唯一标识")
    path: str = Field(description="解析服务可访问的本地媒体文件路径")
    filename: str | None = None
    mime_type: str | None = None
    output_dir: str | None = Field(default=None, description="产物输出目录；未指定时使用临时目录")
    audio_stream_index: int | None = Field(default=None, ge=0, description="可选的 FFprobe 音轨全局索引")
    subtitle_stream_index: int | None = Field(default=None, ge=0, description="可选的 FFprobe 字幕轨全局索引")
    provider: str | None = Field(default=None, description="可选的 Provider 覆盖")
    fallback_enabled: bool = True
    timeout_seconds: int = Field(default=300, ge=1, le=3600)

    def to_context(self) -> "ParseContext":
        return ParseContext(
            file=FileInput(
                file_id=self.file_id,
                path=self.path,
                filename=self.filename,
                mime_type=self.mime_type,
            ),
            options=ParseOptions(provider=self.provider, fallback_enabled=self.fallback_enabled),
            metadata={
                "output_dir": self.output_dir,
                "audio_stream_index": self.audio_stream_index,
                "subtitle_stream_index": self.subtitle_stream_index,
                "timeout_seconds": self.timeout_seconds,
            },
        )


class FileConversionRequest(BaseModel):
    """文件格式转换请求。"""

    file_id: str = Field(description="文件唯一标识")
    path: str = Field(description="解析服务可访问的本地文件路径")
    target_format: str = Field(description="目标格式，例如 docx、xlsx、pptx、pdf")
    filename: str | None = None
    mime_type: str | None = None
    output_dir: str | None = Field(default=None, description="输出目录；未指定时使用临时目录")
    provider: str | None = Field(default=None, description="可选的 Provider 覆盖")
    timeout_seconds: int = Field(default=300, ge=1, le=3600)

    def to_context(self) -> "ParseContext":
        return ParseContext(
            file=FileInput(
                file_id=self.file_id,
                path=self.path,
                filename=self.filename,
                mime_type=self.mime_type,
            ),
            options=ParseOptions(provider=self.provider),
            metadata={
                "target_format": self.target_format.lower().lstrip("."),
                "output_dir": self.output_dir,
                "timeout_seconds": self.timeout_seconds,
            },
        )


class PdfInspectionResult(BaseModel):
    """pdf-inspector 的稳定化输出。"""

    pdf_type: Literal["text_based", "scanned", "image_based", "mixed", "unknown"]
    confidence: float | None = None
    ocr_recommended: bool = False
    pages_needing_ocr: list[int] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class DocumentBlock(BaseModel):
    """统一文档中的一个内容块。"""

    kind: Literal["text", "heading", "list", "table", "image", "unknown"] = "text"
    text: str = ""
    page_number: int | None = None
    bbox: list[float] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentPage(BaseModel):
    page_number: int
    text: str = ""
    blocks: list[DocumentBlock] = Field(default_factory=list)
    width: int | None = None
    height: int | None = None
    preview: dict[str, Any] | None = None


class DocumentResult(BaseModel):
    """所有文档解析 Provider 都需要产出的统一结果。"""

    document_id: str
    source_file: FileInput
    document_type: str = "document"
    pages: list[DocumentPage] = Field(default_factory=list)
    blocks: list[DocumentBlock] = Field(default_factory=list)
    tables: list[dict[str, Any]] = Field(default_factory=list)
    images: list[dict[str, Any]] = Field(default_factory=list)
    representations: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    parser: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    quality: dict[str, float | int | str] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)


class ConversionResult(BaseModel):
    """文件转换的统一结果。"""

    source_file: FileInput
    source_format: str
    target_format: str
    target_path: str
    size_bytes: int
    duration_ms: int
    provider: dict[str, str] = Field(default_factory=dict)


class PipelineStep(BaseModel):
    """Pipeline 中一个可追踪执行步骤。"""

    name: str
    status: str
    duration_ms: int = 0
    input_path: str | None = None
    output_path: str | None = None
    provider: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: "ProviderError | None" = None


class PipelineResult(BaseModel):
    """Office 转换与解析串联后的统一结果。"""

    status: Literal["success", "partial", "failed"]
    pipeline_name: str
    source_file: FileInput
    steps: list[PipelineStep] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: "ProviderError | None" = None
    duration_ms: int = 0


class ProviderError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class ParseContext(BaseModel):
    """Skill 执行上下文。"""

    file: FileInput
    goal: str = Field(default="解析文件", description="本次解析目标")
    options: ParseOptions = Field(default_factory=ParseOptions)
    metadata: dict[str, Any] = Field(default_factory=dict)
    inspection: PdfInspectionResult | None = None


class SkillResult(BaseModel):
    """Skill 的统一执行结果。"""

    status: Literal["success", "partial", "failed"]
    skill_name: str
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    metrics: dict[str, float | int | str] = Field(default_factory=dict)
    error: ProviderError | None = None
