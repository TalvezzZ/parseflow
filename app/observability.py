import logging
import time
from collections import Counter


class HttpMetrics:
    def __init__(self) -> None:
        self.requests_total = 0
        self.status_counts: Counter[str] = Counter()
        self.duration_ms_total = 0

    def record(self, status_code: int, duration_ms: int) -> None:
        self.requests_total += 1
        self.status_counts[str(status_code)] += 1
        self.duration_ms_total += duration_ms

    def snapshot(self) -> dict:
        return {"requests_total": self.requests_total, "status_counts": dict(self.status_counts),
                "duration_ms_total": self.duration_ms_total}

    def prometheus(self) -> str:
        lines = ["# HELP parseflow_http_requests_total HTTP requests handled", "# TYPE parseflow_http_requests_total counter"]
        lines.extend(f'parseflow_http_requests_total{{status="{status}"}} {count}' for status, count in sorted(self.status_counts.items()))
        lines.extend(["# HELP parseflow_http_duration_milliseconds_total Aggregate request duration", "# TYPE parseflow_http_duration_milliseconds_total counter", f"parseflow_http_duration_milliseconds_total {self.duration_ms_total}"])
        return "\n".join(lines) + "\n"


logger = logging.getLogger("parse_agent.http")
