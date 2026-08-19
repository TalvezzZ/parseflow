from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl

from app.documents.models import ParseContext


TaskStatus = Literal["queued", "planning", "running", "succeeded", "partial", "failed", "cancelled"]
CallbackStatus = Literal["not_requested", "pending", "succeeded", "failed"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskSkillRequest(BaseModel):
    """异步执行一个顶层 Skill 的请求。"""

    skill_name: str = Field(description="顶层 Skill 名称，例如 excel.parse")
    file_id: str
    path: str
    filename: str | None = None
    mime_type: str | None = None
    provider: str | None = None
    fallback_enabled: bool = True
    data_id: str | None = Field(default=None, max_length=128, description="调用方业务标识，将原样回传")
    callback: HttpUrl | None = Field(default=None, description="终态通知 URL；为空时请轮询任务")
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)

    def to_context(self) -> ParseContext:
        from app.documents.models import FileInput, ParseOptions
        return ParseContext(file=FileInput(file_id=self.file_id, path=self.path, filename=self.filename, mime_type=self.mime_type),
                            options=ParseOptions(provider=self.provider, fallback_enabled=self.fallback_enabled))


class TaskOfficePipelineRequest(BaseModel):
    """异步执行 Office 转换和解析 Pipeline 的请求。"""

    file_id: str
    path: str
    filename: str | None = None
    mime_type: str | None = None
    output_dir: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    data_id: str | None = Field(default=None, max_length=128, description="调用方业务标识，将原样回传")
    callback: HttpUrl | None = Field(default=None, description="终态通知 URL；为空时请轮询任务")

    def to_context(self) -> ParseContext:
        from app.documents.models import FileInput
        return ParseContext(file=FileInput(file_id=self.file_id, path=self.path, filename=self.filename, mime_type=self.mime_type),
                            metadata={"output_dir": self.output_dir, "timeout_seconds": self.timeout_seconds} if self.timeout_seconds else {"output_dir": self.output_dir})


class TaskRecord(BaseModel):
    """单进程生命周期内保存的任务快照。"""

    task_id: str
    task_type: Literal["skill.execute", "office.parse_pipeline", "parse.intent"]
    status: TaskStatus = "queued"
    data_id: str | None = None
    callback: str | None = None
    callback_status: CallbackStatus = "not_requested"
    callback_status_code: int | None = None
    callback_error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    request: dict[str, Any] = Field(default_factory=dict, exclude=True)


class TaskSubmitResponse(BaseModel):
    task_id: str
    status: Literal["queued"]
    queue_position: int
    created_at: datetime


class TaskMetrics(BaseModel):
    queued_tasks: int
    running_tasks: int
    completed_tasks: int
    max_concurrent_executions: int
    max_queue_size: int
