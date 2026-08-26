from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

from pydantic import BaseModel, Field


class ParseBudget(BaseModel):
    """Bounded resources shared by public parsing paths."""

    max_input_bytes: int = Field(gt=0)
    max_output_bytes: int = Field(gt=0)
    timeout_seconds: int = Field(gt=0)
    max_archive_entries: int | None = Field(default=None, gt=0)
    max_uncompressed_bytes: int | None = Field(default=None, gt=0)
    max_pages: int | None = Field(default=None, gt=0)
    max_nodes: int | None = Field(default=None, gt=0)
    max_pixels: int | None = Field(default=None, gt=0)


class ResourceBudgetExceeded(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class BudgetDeadline:
    expires_at: float

    @classmethod
    def start(cls, timeout_seconds: int) -> "BudgetDeadline":
        return cls(monotonic() + timeout_seconds)

    def check(self) -> None:
        if monotonic() > self.expires_at:
            raise ResourceBudgetExceeded("execution_timeout", "解析执行超过资源预算时间限制")


class BudgetRegistry:
    """Minimal policy registry; specialized providers can request a named budget."""

    def __init__(self, *, default: ParseBudget, archive: ParseBudget, image: ParseBudget) -> None:
        self.default = default
        self.archive = archive
        self.image = image

    def for_suffix(self, suffix: str) -> ParseBudget:
        suffix = suffix.lower()
        if suffix in {".docx", ".xlsx", ".xlsm", ".pptx", ".odt", ".ods"}:
            return self.archive
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}:
            return self.image
        return self.default


def validate_output_size(value: str | bytes, budget: ParseBudget) -> None:
    size = len(value.encode("utf-8")) if isinstance(value, str) else len(value)
    if size > budget.max_output_bytes:
        raise ResourceBudgetExceeded("output_limit_exceeded", "解析输出超过资源预算限制")
