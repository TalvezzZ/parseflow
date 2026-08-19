from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from app.documents.models import DocumentResult, ParseContext, PdfInspectionResult, ProviderError


class ProviderManifest(BaseModel):
    """可插拔 Provider 的能力声明。"""

    name: str
    version: str = "0.1.0"
    kind: str
    modes: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)


class ProviderResult(BaseModel):
    status: str
    provider_name: str
    document: DocumentResult | None = None
    warnings: list[str] = Field(default_factory=list)
    metrics: dict[str, float | int | str] = Field(default_factory=dict)
    error: ProviderError | None = None
    data: dict = Field(default_factory=dict)


class Provider(ABC):
    """外部解析实现的统一适配接口。"""

    manifest: ProviderManifest

    @abstractmethod
    async def parse(self, context: ParseContext) -> ProviderResult:
        """调用外部实现并返回标准化结果。"""


class PdfInspector(ABC):
    @abstractmethod
    async def inspect(self, context: ParseContext) -> PdfInspectionResult:
        """检测 PDF 类型和 OCR 建议。"""


class ProviderRegistry:
    """Provider 注册、查询和按解析模式筛选。"""

    def __init__(self, providers: list[Provider] | None = None) -> None:
        self._providers: dict[str, Provider] = {}
        for provider in providers or []:
            self.register(provider)

    def register(self, provider: Provider) -> None:
        name = provider.manifest.name
        if name in self._providers:
            raise ValueError(f"Provider 已注册: {name}")
        self._providers[name] = provider

    def get(self, name: str) -> Provider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise KeyError(f"未找到 Provider: {name}") from exc

    def first_for(self, mode: str) -> Provider:
        for provider in self._providers.values():
            if mode in provider.manifest.modes:
                return provider
        raise KeyError(f"未找到支持解析模式的 Provider: {mode}")

    def list_manifests(self) -> list[dict]:
        return [provider.manifest.model_dump() for provider in self._providers.values()]
