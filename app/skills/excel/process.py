"""Run Excel extraction in a process that can actually be stopped."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.documents.models import ParseContext, ProviderError
from app.skills.providers import ProviderResult


def _failure(code: str, message: str) -> ProviderResult:
    return ProviderResult(status="failed", provider_name="excel.normal.openpyxl",
                          error=ProviderError(code=code, message=message, retryable=False))


def remove_excel_outputs(workspace: Path) -> None:
    """Only remove known generated outputs under this task's exact workspace."""
    root = workspace.resolve()
    for name in ("workbook.sqlite", "images", "exports", ".exports.tmp"):
        target = root / name
        if target.is_symlink():
            target.unlink()
        elif target.resolve().parent != root:
            raise ValueError("Invalid Excel cleanup target")
        elif target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)


async def export_in_worker(document: dict, workspace: Path, *, max_bytes: int, cancel_path: Path) -> str:
    """Bounded CSV export must stop before the API marks a task cancelled."""
    staging = Path(tempfile.mkdtemp(prefix=".excel-export-", dir=workspace.parent))
    request_path, response_path = staging / "request.json", staging / "response.txt"
    request_path.write_text(json.dumps({"document": document, "workspace": str(workspace), "max_bytes": max_bytes},
                                       ensure_ascii=False), encoding="utf-8")
    process = None
    completed = False
    try:
        if cancel_path.exists():
            return "cancelled"
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.skills.excel.export_worker", str(request_path), str(response_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        while process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=0.25)
            except TimeoutError:
                if cancel_path.exists():
                    process.kill()
                    await process.wait()
                    return "cancelled"
        if cancel_path.exists():
            return "cancelled"
        if process.returncode != 0 or not response_path.is_file():
            return "failed"
        status = response_path.read_text(encoding="utf-8")
        completed = status == "success"
        return status
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        if not completed:
            root = workspace.resolve()
            for name in (".exports.tmp", "exports"):
                target = root / name
                if target.resolve().parent == root and target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
        shutil.rmtree(staging, ignore_errors=True)


async def parse_in_worker(context: ParseContext, config: dict[str, Any]) -> ProviderResult:
    workspace = Path(str(context.metadata.get("artifact_dir") or tempfile.mkdtemp(prefix="parse-agent-excel-")))
    workspace.mkdir(parents=True, exist_ok=True)
    context = context.model_copy(deep=True)
    # Isolate unpublished DB/images from API downloads and interrupted jobs.
    staging = Path(tempfile.mkdtemp(prefix=".excel-worker-", dir=workspace.parent))
    context.metadata["artifact_dir"] = str(staging)
    request_path = staging / "request.json"
    response_path = staging / "response.json"
    cancel_path = context.metadata.get("excel_cancel_path")
    request_path.write_text(json.dumps({"config": config, "context": context.model_dump(mode="json")}), encoding="utf-8")
    process: asyncio.subprocess.Process | None = None
    try:
        if cancel_path and Path(str(cancel_path)).exists():
            return _failure("task_cancelled", "Excel 解析已取消")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.skills.excel.worker", str(request_path), str(response_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        while process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=0.25)
            except TimeoutError:
                if cancel_path and Path(str(cancel_path)).exists():
                    process.kill()
                    await process.wait()
                    return _failure("task_cancelled", "Excel 解析已取消")
        if process.returncode != 0 or not response_path.is_file():
            return _failure("excel_worker_failed", "Excel 解析进程异常退出")
        # Result is a bounded preview, never a full cell dump.
        response_limit = max(8 * 1024 * 1024, int(config.get("max_preview_bytes", 1_000_000)) * 16)
        if response_path.stat().st_size > response_limit:
            return _failure("excel_preview_limit_exceeded", "Excel 预览结果超过大小预算")
        result = ProviderResult.model_validate_json(response_path.read_text(encoding="utf-8"))
        if result.status == "failed":
            return result
        if cancel_path and Path(str(cancel_path)).exists():
            return _failure("task_cancelled", "Excel 解析已取消")
        # Publish only parser outputs, never request/response or partial SQLite files.
        for source in staging.iterdir():
            if source.name in {"request.json", "response.json"} or source.name.startswith("."):
                continue
            destination = workspace / source.name
            if source.is_dir():
                if destination.exists():
                    raise FileExistsError(destination)
                os.replace(source, destination)
            else:
                os.replace(source, destination)
        if result.document:
            result.document.metadata["artifact_dir"] = str(workspace)
            for image in result.document.images:
                relative = image.get("relative_path")
                if relative:
                    image["path"] = str(workspace / relative)
        return result
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        shutil.rmtree(staging, ignore_errors=True)
