from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


TaskStatus = Literal[
    "queued", "planning", "running", "cancelling", "succeeded", "partial", "failed", "cancelled", "interrupted"
]
TerminalTaskStatus = Literal["succeeded", "partial", "failed", "cancelled", "interrupted"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskResultEnvelope(BaseModel):
    """Stable public result shape, independent of the executed Skill/Pipeline."""

    status: Literal["succeeded", "partial", "failed"]
    file_id: str
    document: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    conversion: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    quality: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None


class TaskRecord(BaseModel):
    """Durable internal task record. `request` contains opaque IDs only."""

    schema_version: Literal[1] = 1
    revision: int = 0
    task_id: str
    task_type: Literal["parse.intent"] = "parse.intent"
    file_id: str
    status: TaskStatus = "queued"
    goal: str | None = None
    data_id: str | None = Field(default=None, max_length=128)
    retry_of: str | None = None
    cancel_requested: bool = False
    plan: dict[str, Any] | None = None
    result: TaskResultEnvelope | None = None
    error: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None


class TaskSubmitResponse(BaseModel):
    task_id: str
    file_id: str
    status: Literal["queued"]
    queue_position: int
    created_at: datetime
    links: dict[str, str]


class TaskListResponse(BaseModel):
    items: list[TaskRecord]
    next_cursor: str | None = None


class TaskMetrics(BaseModel):
    queued_tasks: int
    running_tasks: int
    completed_tasks: int
    max_concurrent_executions: int
    max_queue_size: int


class ArtifactRecord(BaseModel):
    artifact_id: str
    filename: str
    content_type: str | None = None
    size_bytes: int
    created_at: datetime = Field(default_factory=utc_now)
    kind: str = "file"


class ArtifactListResponse(BaseModel):
    task_id: str
    artifacts: list[ArtifactRecord]


TERMINAL_STATUSES = {"succeeded", "partial", "failed", "cancelled", "interrupted"}
ACTIVE_STATUSES = {"queued", "planning", "running", "cancelling"}
"""States which must never be deleted while referenced by a running worker."""
