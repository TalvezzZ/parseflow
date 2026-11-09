"""Private Excel worker entry point; never accepts requests over the network."""
from __future__ import annotations

import json
import sys
from time import perf_counter

try:
    import resource
except ImportError:  # Windows still supports parsing; RSS is optional telemetry.
    resource = None
from pathlib import Path

from app.documents.models import ParseContext, ProviderError
from app.skills.excel.adapters.openpyxl import OpenpyxlProvider
from app.skills.providers import ProviderResult


def main() -> None:
    request_path, response_path = map(Path, sys.argv[1:3])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    started = perf_counter()
    try:
        result = OpenpyxlProvider(**request["config"])._parse_sync(ParseContext.model_validate(request["context"]))
    except Exception as exc:
        result = ProviderResult(status="failed", provider_name="excel.normal.openpyxl",
                                error=ProviderError(code="excel_parse_failed", message=str(exc), retryable=False))
    if resource is not None:
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        result.metrics["worker_peak_rss_bytes"] = int(rss if sys.platform == "darwin" else rss * 1024)
    result.metrics["worker_duration_seconds"] = round(perf_counter() - started, 3)
    with response_path.open("w", encoding="utf-8") as handle:
        json.dump(result.model_dump(mode="json"), handle, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main()
