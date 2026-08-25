from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.version import __version__


def now() -> datetime:
    return datetime.now(timezone.utc)


class PlanStep(BaseModel):
    step_id: str
    skill_name: str
    reason: str
    status: Literal["pending", "running", "succeeded", "partial", "failed", "skipped"] = "pending"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result_summary: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None


class ParsePlan(BaseModel):
    plan_id: str
    goal: str
    planner: dict[str, str] = Field(default_factory=lambda: {"name": "rule-based", "version": __version__})
    status: Literal["planned", "running", "completed", "failed"] = "planned"
    created_at: datetime = Field(default_factory=now)
    steps: list[PlanStep]
    warnings: list[str] = Field(default_factory=list)
