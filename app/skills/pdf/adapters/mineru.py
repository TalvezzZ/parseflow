from collections.abc import Awaitable, Callable
from typing import Any

from app.documents.models import ParseContext, ProviderError
from app.skills.providers import Provider, ProviderManifest, ProviderResult


class MineruProvider(Provider):
    """MinerU 集成边界；具体部署方式通过 runner 注入。"""

    manifest = ProviderManifest(
        name="pdf.ocr.mineru",
        kind="pdf_parser",
        modes=["ocr"],
        capabilities=["text", "pages", "tables", "images", "layout", "ocr"],
    )

    def __init__(self, runner: Callable[[ParseContext], Awaitable[Any]] | None = None) -> None:
        self.runner = runner

    async def parse(self, context: ParseContext) -> ProviderResult:
        if self.runner is None:
            return ProviderResult(
                status="failed",
                provider_name=self.manifest.name,
                error=ProviderError(
                    code="provider_unconfigured",
                    message="MinerU Provider 尚未配置 runner 或服务连接",
                    retryable=False,
                ),
            )
        try:
            result = await self.runner(context)
            return ProviderResult.model_validate(result)
        except Exception as exc:
            return ProviderResult(
                status="failed",
                provider_name=self.manifest.name,
                error=ProviderError(code="provider_failed", message=str(exc), retryable=True),
            )
