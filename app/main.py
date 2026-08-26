from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from shutil import disk_usage
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from mcp.server.transport_security import TransportSecuritySettings

from app.agent.executor import SkillExecutor
from app.agent.pipeline import OfficeParsePipeline
from app.config import get_settings
from app.documents.models import FileInput, ParseContext
from app.files import LocalFileStore, StoredFilePublic
from app.observability import HttpMetrics, logger
from app.planning.models import now
from app.planning.rule_planner import RuleBasedPlanner
from app.skills.office.adapters.libreoffice import LibreOfficeProvider
from app.skills.registry import create_default_registry
from app.tasks.artifacts import ArtifactRepository
from app.tasks.manager import PersistentTaskManager, TaskQueueFullError
from app.tasks.models import ArtifactListResponse, TaskListResponse, TaskRecord, TaskResultEnvelope, TaskSubmitResponse
from app.tasks.repository import FileTaskRepository
from app.version import __version__

settings = get_settings()
data_root = Path(settings.data_dir).resolve()
file_store = LocalFileStore(str(data_root / "files"), settings.file_max_size_mb, settings.file_allowed_suffixes,
                            min_free_mb=settings.storage_min_free_mb)
task_repository = FileTaskRepository(data_root / "tasks")
artifact_repository = ArtifactRepository(data_root / "artifacts")
http_metrics = HttpMetrics()
registry = create_default_registry()
planner = RuleBasedPlanner(registry)
executor = SkillExecutor(registry)
pipeline = OfficeParsePipeline(executor, LibreOfficeProvider(command=settings.office_converter_command, timeout_seconds=settings.office_converter_timeout_seconds))


def _public_document(value: object) -> dict | None:
    """Project the internal DocumentResult dump into a path-free public document."""
    if not isinstance(value, dict):
        return None
    document = dict(value)
    source = document.get("source_file")
    if isinstance(source, dict):
        document["source_file"] = {key: item for key, item in source.items() if key != "path"}
    return document


async def run_parse_intent(record: TaskRecord) -> TaskResultEnvelope:
    stored = file_store.get(record.file_id)
    source_path = file_store.source_path(record.file_id)
    if stored is None or source_path is None:
        return TaskResultEnvelope(status="failed", file_id=record.file_id, error={"code": "file_not_found", "message": "上传文件不存在", "retryable": False})
    plan = planner.create(stored.filename, record.goal)
    step = plan.steps[0]
    step.status, step.started_at = "running", now()
    def set_plan(current: TaskRecord) -> None:
        current.plan = plan.model_dump(mode="json")
    current = await task_repository.get(record.task_id)
    if current:
        await task_repository.update(current.task_id, current.revision, set_plan)
    context = ParseContext(file=FileInput(file_id=record.file_id, path=str(source_path), filename=stored.filename, mime_type=stored.content_type))
    workspace = artifact_repository.workspace(record.task_id)
    context.metadata["artifact_dir"] = str(workspace)
    context.metadata["output_dir"] = str(workspace)
    result = (await pipeline.execute(context)).model_dump() if step.skill_name == "office.parse_pipeline" else (await executor.execute(step.skill_name, context)).model_dump()
    step.finished_at = now()
    step.status = "succeeded" if result["status"] == "success" else "partial" if result["status"] == "partial" else "failed"
    step.error = result.get("error")
    payload = result.get("data") or result.get("result") or {}
    document = _public_document(payload.get("document") if isinstance(payload, dict) else None)
    step.result_summary = {"document_type": (document or {}).get("document_type"), "tables": len((document or {}).get("tables", []))}
    plan.status = "completed" if step.status in {"succeeded", "partial"} else "failed"
    current = await task_repository.get(record.task_id)
    if current:
        await task_repository.update(current.task_id, current.revision, lambda item: setattr(item, "plan", plan.model_dump(mode="json")))
    artifacts = artifact_repository.collect(record.task_id)
    public_artifacts = [item.model_dump(mode="json") | {"download_url": f"/api/v1/tasks/{record.task_id}/artifacts/{item.artifact_id}"} for item in artifacts]
    status = "succeeded" if result["status"] == "success" else "partial" if result["status"] == "partial" else "failed"
    return TaskResultEnvelope(status=status, file_id=record.file_id, document=document, artifacts=public_artifacts,
                              steps=[step.model_dump(mode="json")], warnings=list(result.get("warnings") or []),
                              metrics={}, error=result.get("error"))


task_manager = PersistentTaskManager(run_parse_intent, task_repository, max_concurrent_executions=settings.task_max_concurrent_executions,
                                     max_queue_size=settings.task_queue_max_size, default_timeout_seconds=settings.task_default_timeout_seconds,
                                     result_ttl_seconds=settings.task_result_ttl_seconds, cleanup_interval_seconds=settings.task_cleanup_interval_seconds)

from app.mcp_server import mcp


@asynccontextmanager
async def lifespan(_: FastAPI):
    await task_manager.start()
    async with mcp.session_manager.run():
        try:
            yield
        finally:
            await task_manager.stop()


app = FastAPI(title="Parse Agent", version=__version__, description="基于 LangChain 和 Skill 的文档解析 Agent", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"], allow_credentials=False, allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"])


@app.middleware("http")
async def observe_and_authenticate(request: Request, call_next):
    request_id, started = request.headers.get("X-Request-ID") or uuid4().hex, perf_counter()
    is_remote_mcp = request.url.path == "/mcp" or request.url.path.startswith("/mcp/")
    if is_remote_mcp and not settings.mcp_http_enabled:
        response = JSONResponse(status_code=404, content={"detail": {"code": "mcp_disabled", "message": "远程 MCP 未启用。"}})
    elif is_remote_mcp and not settings.api_key:
        response = JSONResponse(status_code=503, content={"detail": {"code": "mcp_api_key_required", "message": "远程 MCP 必须配置 API_KEY。"}})
    elif (request.url.path.startswith("/api/") or is_remote_mcp) and settings.api_key and request.headers.get("X-API-Key") != settings.api_key:
        response = JSONResponse(status_code=401, content={"detail": {"code": "unauthorized", "message": "缺少或无效的 API Key。"}})
    else:
        response = await call_next(request)
    duration_ms = int((perf_counter() - started) * 1000)
    response.headers["X-Request-ID"] = request_id
    http_metrics.record(response.status_code, duration_ms)
    logger.info("http_request method=%s path=%s status=%s duration_ms=%s request_id=%s", request.method, request.url.path, response.status_code, duration_ms, request_id)
    return response


mcp_allowed_hosts = [item.strip() for item in settings.mcp_allowed_hosts.split(",") if item.strip()]
app.mount("/mcp", mcp.streamable_http_app(streamable_http_path="/", stateless_http=True, transport_security=TransportSecuritySettings(allowed_hosts=mcp_allowed_hosts)), name="mcp")


@app.get("/health", tags=["系统"])
async def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}


@app.get("/ready", tags=["系统"])
async def ready() -> dict[str, str]:
    """Check local persistence and worker availability without probing parsers."""
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        probe = data_root / ".ready-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        if disk_usage(data_root).free < settings.storage_min_free_mb * 1024 * 1024:
            raise OSError("storage capacity below safety threshold")
        if not task_manager._started or not task_manager._workers:
            raise OSError("task worker unavailable")
    except OSError as exc:
        raise HTTPException(status_code=503, detail={"code": "not_ready", "message": str(exc)}) from exc
    return {"status": "ready", "service": settings.app_name}


@app.post("/api/v1/files", response_model=StoredFilePublic, status_code=201, tags=["文件"])
async def upload_file(file: UploadFile = File(...)) -> StoredFilePublic:
    try:
        record = await file_store.save(file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "upload_rejected", "message": str(exc)}) from exc
    return file_store.public(record.file_id)  # type: ignore[return-value]


@app.get("/api/v1/files/{file_id}", response_model=StoredFilePublic, tags=["文件"])
async def get_file(file_id: str) -> StoredFilePublic:
    record = file_store.public(file_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"code": "file_not_found", "message": "未找到文件"})
    return record


@app.get("/api/v1/files/{file_id}/content", tags=["文件"])
async def download_file(file_id: str) -> FileResponse:
    record, source = file_store.get(file_id), file_store.source_path(file_id)
    if record is None or source is None:
        raise HTTPException(status_code=404, detail={"code": "file_not_found", "message": "未找到文件"})
    return FileResponse(source, filename=record.filename, media_type=record.content_type)


@app.post("/api/v1/tasks/parse", response_model=TaskSubmitResponse, status_code=202, tags=["任务"])
async def submit_parse_task(file: UploadFile = File(...), goal: str | None = Form(default=None), data_id: str | None = Form(default=None, max_length=128)) -> TaskSubmitResponse:
    try:
        stored = await file_store.save(file)
        return await task_manager.submit_parse(stored.file_id, goal or None, data_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "upload_rejected", "message": str(exc)}) from exc
    except TaskQueueFullError as exc:
        file_store.delete(stored.file_id)
        raise HTTPException(status_code=429, detail={"code": "task_queue_full", "message": str(exc)}) from exc


@app.get("/api/v1/tasks", response_model=TaskListResponse, tags=["任务"])
async def list_tasks(status: str | None = None, cursor: str | None = None, limit: int = Query(default=50, ge=1, le=100)) -> TaskListResponse:
    items, next_cursor = await task_manager.list(status=status, cursor=cursor, limit=limit)
    return TaskListResponse(items=items, next_cursor=next_cursor)


@app.get("/api/v1/tasks/{task_id}", response_model=TaskRecord, tags=["任务"])
async def get_task(task_id: str) -> TaskRecord:
    task = await task_manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail={"code": "task_not_found", "message": "未找到任务"})
    return task


@app.post("/api/v1/tasks/{task_id}/cancel", response_model=TaskRecord, tags=["任务"])
async def cancel_task(task_id: str) -> TaskRecord:
    task = await task_manager.cancel(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail={"code": "task_not_found", "message": "未找到任务"})
    return task


@app.get("/api/v1/tasks/{task_id}/artifacts", response_model=ArtifactListResponse, tags=["任务"])
async def list_artifacts(task_id: str) -> ArtifactListResponse:
    if await task_manager.get(task_id) is None:
        raise HTTPException(status_code=404, detail={"code": "task_not_found", "message": "未找到任务"})
    return ArtifactListResponse(task_id=task_id, artifacts=artifact_repository.list(task_id))


@app.get("/api/v1/tasks/{task_id}/artifacts/{artifact_id}", tags=["任务"])
async def download_artifact(task_id: str, artifact_id: str) -> FileResponse:
    path = artifact_repository.download_path(task_id, artifact_id)
    if path is None:
        raise HTTPException(status_code=404, detail={"code": "artifact_not_found", "message": "未找到 artifact"})
    return FileResponse(path, filename=path.name)


@app.get("/api/v1/metrics", tags=["系统"])
async def metrics() -> dict:
    return {"http": http_metrics.snapshot(), "tasks": (await task_manager.metrics()).model_dump()}


@app.get("/api/v1/skills", tags=["Skill"])
async def list_skills() -> list[dict]:
    return registry.list_manifests()
