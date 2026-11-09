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

    METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"})

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
        safe_method = method.upper()
        if safe_method not in self.METHODS:
            safe_method = "OTHER"
        self.requests_total += 1
        self.status_counts[status] += 1
        self.route_counts[(safe_method, safe_route, status)] += 1
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


class _Histogram:
    BUCKETS = (0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0, 300.0, 900.0, 1800.0)

    def __init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.bucket_counts = {bucket: 0 for bucket in self.BUCKETS}

    def observe(self, value: float) -> None:
        value = max(0.0, value)
        self.count += 1
        self.total += value
        for bucket in self.BUCKETS:
            if value <= bucket:
                self.bucket_counts[bucket] += 1

    @staticmethod
    def _label_text(labels: dict[str, str]) -> str:
        return "{" + ",".join(f'{key}="{value}"' for key, value in labels.items()) + "}" if labels else ""

    def render_samples(self, name: str, labels: dict[str, str] | None = None) -> list[str]:
        base_labels = labels or {}
        lines: list[str] = []
        for bucket in self.BUCKETS:
            rendered = dict(base_labels) | {"le": str(bucket)}
            lines.append(f"{name}_bucket{self._label_text(rendered)} {self.bucket_counts[bucket]}")
        rendered = dict(base_labels) | {"le": "+Inf"}
        lines.extend([f"{name}_bucket{self._label_text(rendered)} {self.count}",
                      f"{name}_sum{self._label_text(base_labels)} {self.total:.6f}",
                      f"{name}_count{self._label_text(base_labels)} {self.count}"])
        return lines

    def render(self, name: str, help_text: str) -> list[str]:
        return [f"# HELP {name} {help_text}", f"# TYPE {name} histogram", *self.render_samples(name)]


class TaskExecutionMetrics:
    """Low-cardinality task metrics for the supported single-process runtime."""

    TERMINAL_STATUSES = ("succeeded", "partial", "failed", "cancelled", "interrupted", "other")

    def __init__(self) -> None:
        self.queue_wait = _Histogram()
        self.duration_by_status = {status: _Histogram() for status in self.TERMINAL_STATUSES}
        self.queue_rejections: Counter[str] = Counter({"full": 0, "other": 0})
        self.recoveries: Counter[str] = Counter({"queued": 0, "interrupted": 0, "other": 0})

    def observe_queue_wait(self, seconds: float) -> None:
        self.queue_wait.observe(seconds)

    def observe_duration(self, seconds: float, status: str) -> None:
        safe_status = status if status in self.TERMINAL_STATUSES else "other"
        self.duration_by_status.setdefault(safe_status, _Histogram()).observe(seconds)

    def record_queue_rejection(self, reason: str) -> None:
        self.queue_rejections[reason if reason == "full" else "other"] += 1

    def record_recovery(self, outcome: str) -> None:
        safe_outcome = outcome if outcome in {"queued", "interrupted"} else "other"
        self.recoveries[safe_outcome] += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "queue_wait_count": self.queue_wait.count,
            "queue_wait_seconds_total": self.queue_wait.total,
            "task_duration_count": {status: histogram.count for status, histogram in self.duration_by_status.items()},
            "queue_rejections": dict(self.queue_rejections),
            "recoveries": dict(self.recoveries),
        }

    def prometheus(self) -> str:
        lines = self.queue_wait.render("parseflow_task_queue_wait_seconds", "Time spent waiting before task execution")
        lines.extend(["# HELP parseflow_task_duration_seconds Task execution duration",
                      "# TYPE parseflow_task_duration_seconds histogram"])
        for status, histogram in sorted(self.duration_by_status.items()):
            lines.extend(histogram.render_samples("parseflow_task_duration_seconds", {"status": status}))
        if self.queue_rejections:
            lines.extend(["# HELP parseflow_task_queue_rejections_total Task submissions rejected by the queue",
                          "# TYPE parseflow_task_queue_rejections_total counter"])
            lines.extend(f'parseflow_task_queue_rejections_total{{reason="{reason}"}} {count}'
                         for reason, count in sorted(self.queue_rejections.items()))
        if self.recoveries:
            lines.extend(["# HELP parseflow_task_recoveries_total Task records recovered during process startup",
                          "# TYPE parseflow_task_recoveries_total counter"])
            lines.extend(f'parseflow_task_recoveries_total{{outcome="{outcome}"}} {count}'
                         for outcome, count in sorted(self.recoveries.items()))
        return "\n".join(lines) + "\n"
