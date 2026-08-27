from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from app.version import __version__


class HttpMetrics:
    """In-process, low-cardinality metrics suitable for the single-process deployment."""

    def __init__(self) -> None:
        self.requests_total = 0
        self.status_counts: Counter[str] = Counter()
        self.route_counts: Counter[tuple[str, str, str]] = Counter()
        self.route_duration_seconds: Counter[str] = Counter()
        self.duration_ms_total = 0

    def record(self, status_code: int, duration_ms: int, *, method: str = "UNKNOWN", route: str = "unknown") -> None:
        status = str(status_code)
        contains_id = re.search(r"(?:task|file|artifact)_[0-9a-f]{32}", route) is not None
        safe_route = route if route.startswith("/") and not contains_id else "unknown"
        self.requests_total += 1
        self.status_counts[status] += 1
        self.route_counts[(method.upper(), safe_route, status)] += 1
        self.route_duration_seconds[safe_route] += duration_ms / 1000
        self.duration_ms_total += duration_ms

    def snapshot(self) -> dict:
        return {"requests_total": self.requests_total, "status_counts": dict(self.status_counts),
                "duration_ms_total": self.duration_ms_total}

    def prometheus(self, *, task_counts: dict[str, int] | None = None, queue_depth: int = 0,
                   storage_bytes: int = 0) -> str:
        lines = ["# HELP parseflow_http_requests_total HTTP requests handled", "# TYPE parseflow_http_requests_total counter"]
        lines.extend(f'parseflow_http_requests_total{{method="{method}",route="{route}",status="{status}"}} {count}'
                     for (method, route, status), count in sorted(self.route_counts.items()))
        lines.extend(["# HELP parseflow_http_duration_seconds_total Aggregate request duration", "# TYPE parseflow_http_duration_seconds_total counter"])
        lines.extend(f'parseflow_http_duration_seconds_total{{route="{route}"}} {seconds:.6f}'
                     for route, seconds in sorted(self.route_duration_seconds.items()))
        lines.extend(["# HELP parseflow_tasks_current Current tasks by state", "# TYPE parseflow_tasks_current gauge"])
        lines.extend(f'parseflow_tasks_current{{status="{status}"}} {count}' for status, count in sorted((task_counts or {}).items()))
        lines.extend(["# HELP parseflow_task_queue_depth Queued task count", "# TYPE parseflow_task_queue_depth gauge",
                      f"parseflow_task_queue_depth {queue_depth}",
                      "# HELP parseflow_storage_bytes Bytes stored under the data root", "# TYPE parseflow_storage_bytes gauge",
                      f"parseflow_storage_bytes {storage_bytes}"])
        return "\n".join(lines) + "\n"


class JsonFormatter(logging.Formatter):
    """Structured formatter with explicit fields and no arbitrary request payload serialization."""

    FIELDS = ("request_id", "method", "route", "task_id", "file_id", "skill", "provider", "status", "duration_ms", "error_code")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {"timestamp": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                                   "service_version": __version__, "message": record.getMessage()}
        for field in self.FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, ensure_ascii=False)


logger = logging.getLogger("parse_agent.http")
