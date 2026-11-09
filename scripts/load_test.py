#!/usr/bin/env python3
"""Measure ParseFlow task throughput against a running API using one fixture file."""

from __future__ import annotations

import argparse
import asyncio
import math
import mimetypes
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx

TERMINAL = {"succeeded", "partial", "failed", "cancelled", "interrupted"}


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percent / 100 * len(ordered)) - 1))
    return ordered[index]


def seconds_between(later: str | None, earlier: str | None) -> float | None:
    if not later or not earlier:
        return None
    try:
        return max(0.0, (datetime.fromisoformat(later.replace("Z", "+00:00")) -
                         datetime.fromisoformat(earlier.replace("Z", "+00:00"))).total_seconds())
    except ValueError:
        return None


async def run(args: argparse.Namespace) -> int:
    fixture = Path(args.file).expanduser().resolve()
    if not fixture.is_file():
        raise SystemExit(f"Fixture does not exist: {fixture}")
    file_size = fixture.stat().st_size
    content = fixture.read_bytes()
    content_type = mimetypes.guess_type(fixture.name)[0] or "application/octet-stream"
    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    limits = httpx.Limits(max_connections=max(args.submit_concurrency, 8), max_keepalive_connections=8)
    timeout = httpx.Timeout(args.request_timeout)
    submitted: set[str] = set()
    rejected: Counter[str] = Counter()
    results: dict[str, dict] = {}
    started_wall = time.monotonic()

    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), headers=headers,
                                 timeout=timeout, limits=limits) as client:
        semaphore = asyncio.Semaphore(args.submit_concurrency)

        async def submit(index: int) -> tuple[str, str | None]:
            async with semaphore:
                try:
                    response = await client.post(
                        "/api/v1/tasks/parse",
                        files={"file": (fixture.name, content, content_type)},
                    )
                except httpx.HTTPError as exc:
                    return str(index), f"request_error:{type(exc).__name__}"
            if response.status_code != 202:
                try:
                    body = response.json()
                    detail = body.get("detail", {})
                    code = detail.get("code", f"http_{response.status_code}") if isinstance(detail, dict) else f"http_{response.status_code}"
                except ValueError:
                    code = f"http_{response.status_code}"
                return str(index), code
            task_id = response.json()["task_id"]
            submitted.add(task_id)
            return task_id, None

        submit_results = await asyncio.gather(*(submit(index) for index in range(args.jobs)))
        pending: set[str] = set()
        for task_id, error in submit_results:
            if error:
                rejected[error] += 1
            else:
                pending.add(task_id)

        deadline = time.monotonic() + args.task_timeout
        while pending and time.monotonic() < deadline:
            async def poll(task_id: str) -> tuple[str, dict | None, str | None]:
                try:
                    response = await client.get(f"/api/v1/tasks/{task_id}")
                    if response.status_code != 200:
                        return task_id, None, f"http_{response.status_code}"
                    return task_id, response.json(), None
                except httpx.HTTPError as exc:
                    return task_id, None, f"request_error:{type(exc).__name__}"

            polled = await asyncio.gather(*(poll(task_id) for task_id in tuple(pending)))
            for task_id, task, error in polled:
                if error:
                    rejected[error] += 1
                    continue
                if task and task.get("status") in TERMINAL:
                    results[task_id] = task
                    pending.discard(task_id)
            if pending:
                await asyncio.sleep(args.poll_interval)

    elapsed = max(0.001, time.monotonic() - started_wall)
    states = Counter(task.get("status", "unknown") for task in results.values())
    queue_wait = [value for task in results.values()
                  if (value := seconds_between(task.get("started_at"), task.get("created_at"))) is not None]
    durations = [task["duration_ms"] / 1000 for task in results.values() if task.get("duration_ms") is not None]
    terminal_count = len(results)

    print(f"API: {args.base_url}")
    print(f"Fixture: {fixture.name} ({file_size:,} bytes)")
    print(f"Submitted: {args.jobs}; accepted: {len(submitted)}; terminal: {terminal_count}; still pending: {len(pending)}")
    print(f"Elapsed: {elapsed:.2f}s; completed throughput: {terminal_count / elapsed:.3f} tasks/s")
    print(f"Terminal statuses: {dict(sorted(states.items()))}")
    print(f"Submission/poll errors: {dict(sorted(rejected.items()))}")
    if queue_wait:
        print(f"Queue wait P50/P95: {percentile(queue_wait, 50):.3f}s / {percentile(queue_wait, 95):.3f}s")
    if durations:
        print(f"Execution P50/P95: {percentile(durations, 50):.3f}s / {percentile(durations, 95):.3f}s")
    if pending:
        print("Timed-out task IDs: " + ", ".join(sorted(pending)))
    return 1 if pending or rejected or any(status in {"failed", "interrupted"} for status in states) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080", help="ParseFlow API base URL")
    parser.add_argument("--file", required=True, help="Non-sensitive representative fixture file")
    parser.add_argument("--jobs", type=int, default=8, help="Total parsing tasks to submit")
    parser.add_argument("--submit-concurrency", type=int, default=4, help="Concurrent upload requests")
    parser.add_argument("--api-key", default=None, help="Optional X-API-Key value")
    parser.add_argument("--request-timeout", type=float, default=60.0, help="HTTP request timeout in seconds")
    parser.add_argument("--task-timeout", type=float, default=3600.0, help="Maximum time to poll tasks")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="Task status polling interval")
    args = parser.parse_args()
    if args.jobs < 1 or args.submit_concurrency < 1:
        parser.error("--jobs and --submit-concurrency must be positive")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
