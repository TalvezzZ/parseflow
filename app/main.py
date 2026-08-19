from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.files import LocalFileStore, StoredFile
from app.observability import HttpMetrics, logger
from app.planning.rule_planner import RuleBasedPlanner
from app.planning.models import now

from app.agent.executor import SkillExecutor
from app.agent.pipeline import OfficeParsePipeline
from app.config import get_settings
from app.documents.models import DocumentParseRequest, FileConversionRequest, MediaPrepareRequest, OfficePipelineRequest, PdfParseRequest, PipelineResult, SkillResult
from app.tasks.manager import InMemoryTaskManager, TaskQueueFullError
from app.tasks.models import TaskMetrics, TaskOfficePipelineRequest, TaskRecord, TaskSkillRequest, TaskSubmitResponse
from app.skills.registry import create_default_registry
from app.skills.office.adapters.libreoffice import LibreOfficeProvider


settings = get_settings()
file_store = LocalFileStore(settings.file_storage_dir, settings.file_max_size_mb, settings.file_allowed_suffixes)
http_metrics = HttpMetrics()
registry = create_default_registry()
planner = RuleBasedPlanner(registry)
executor = SkillExecutor(registry)
pipeline = OfficeParsePipeline(
    executor,
    LibreOfficeProvider(command=settings.office_converter_command, timeout_seconds=settings.office_converter_timeout_seconds),
)


async def run_task(record: TaskRecord) -> dict:
    """复用现有确定性执行器；任务层不会直接调用 Provider。"""
    if record.task_type == "skill.execute":
        request = TaskSkillRequest.model_validate(record.request)
        context = request.to_context()
        if artifact_dir := file_store.artifact_dir(request.file_id):
            context.metadata["artifact_dir"] = str(artifact_dir)
        return (await executor.execute(request.skill_name, context)).model_dump()
    if record.task_type == "office.parse_pipeline":
        request = TaskOfficePipelineRequest.model_validate(record.request)
        context = request.to_context()
        if artifact_dir := file_store.artifact_dir(request.file_id):
            context.metadata["output_dir"] = str(artifact_dir)
            context.metadata["artifact_dir"] = str(artifact_dir)
        return (await pipeline.execute(context)).model_dump()
    if record.task_type == "parse.intent":
        return await run_parse_intent(record)
    raise ValueError(f"未知任务类型: {record.task_type}")


async def run_parse_intent(record: TaskRecord) -> dict:
    """任务 Worker 内部完成规划、策略校验和唯一顶层步骤执行。"""
    record.status = "planning"
    request = record.request
    plan = planner.create(str(request["path"]), request.get("goal"))
    record.plan = plan.model_dump(mode="json")
    step = plan.steps[0]
    step.status = "running"
    step.started_at = now()
    plan.status = "running"
    record.plan = plan.model_dump(mode="json")
    from app.documents.models import FileInput, ParseContext
    context = ParseContext(file=FileInput(file_id=str(request["file_id"]), path=str(request["path"]), filename=request.get("filename"), mime_type=request.get("mime_type")))
    if artifact_dir := file_store.artifact_dir(str(request["file_id"])):
        context.metadata["artifact_dir"] = str(artifact_dir)
        context.metadata["output_dir"] = str(artifact_dir)
    record.status = "running"
    if step.skill_name == "office.parse_pipeline":
        result = (await pipeline.execute(context)).model_dump()
    else:
        result = (await executor.execute(step.skill_name, context)).model_dump()
    step.finished_at = now()
    step.status = "succeeded" if result["status"] == "success" else "partial" if result["status"] == "partial" else "failed"
    step.error = result.get("error")
    document = (result.get("data") or result.get("result") or {}).get("document", {})
    step.result_summary = {"document_type": document.get("document_type"), "tables": len(document.get("tables", [])), "images": len(document.get("images", []))}
    plan.status = "completed" if step.status in {"succeeded", "partial"} else "failed"
    record.plan = plan.model_dump(mode="json")
    return {"status": result["status"], "plan": record.plan, "result": result, "warnings": plan.warnings, "error": result.get("error")}


task_manager = InMemoryTaskManager(
    run_task,
    max_concurrent_executions=settings.task_max_concurrent_executions,
    max_queue_size=settings.task_queue_max_size,
    default_timeout_seconds=settings.task_default_timeout_seconds,
    result_ttl_seconds=settings.task_result_ttl_seconds,
    cleanup_interval_seconds=settings.task_cleanup_interval_seconds,
    callback_timeout_seconds=settings.task_callback_timeout_seconds,
)

@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        yield
    finally:
        await task_manager.stop()


app = FastAPI(title="Parse Agent", version="0.6.0", description="基于 LangChain 和 Skill 的文档解析 Agent", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def observe_and_authenticate(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid4().hex
    started = perf_counter()
    if settings.api_key and request.url.path.startswith("/api/") and request.headers.get("X-API-Key") != settings.api_key:
        response = JSONResponse(status_code=401, content={"detail": {"code": "unauthorized", "message": "缺少或无效的 API Key。"}})
    else:
        response = await call_next(request)
    duration_ms = int((perf_counter() - started) * 1000)
    response.headers["X-Request-ID"] = request_id
    http_metrics.record(response.status_code, duration_ms)
    logger.info("http_request method=%s path=%s status=%s duration_ms=%s request_id=%s", request.method, request.url.path, response.status_code, duration_ms, request_id)
    return response


@app.get("/health", tags=["系统"])
async def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}


@app.post("/api/v1/files", response_model=StoredFile, status_code=201, tags=["文件"])
async def upload_file(file: UploadFile = File(...)) -> StoredFile:
    """上传文件到受控本地目录，返回可直接传给解析 API 的路径。"""
    try:
        return await file_store.save(file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "upload_rejected", "message": str(exc)}) from exc


@app.post("/api/v1/parse", response_model=TaskSubmitResponse, status_code=202, tags=["智能解析"])
async def submit_automatic_parse(
    file: UploadFile = File(...),
    goal: str | None = Form(default=None),
    data_id: str | None = Form(default=None, max_length=128),
    callback: str | None = Form(default=None),
) -> TaskSubmitResponse:
    """用户只需上传一个文件；Worker 会在任务内自动规划并执行。"""
    if not settings.task_queue_enabled:
        raise HTTPException(status_code=503, detail={"code": "task_queue_disabled", "message": "任务队列当前未启用。"})
    if callback and not callback.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail={"code": "invalid_callback", "message": "callback 必须是 HTTP 或 HTTPS URL。"})
    try:
        stored = await file_store.save(file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "upload_rejected", "message": str(exc)}) from exc
    request = {"file_id": stored.file_id, "path": stored.path, "filename": stored.filename,
               "mime_type": stored.content_type, "goal": goal or None}
    try:
        return await task_manager.submit("parse.intent", request, data_id, callback)
    except TaskQueueFullError as exc:
        raise HTTPException(status_code=429, detail={"code": "task_queue_full", "message": "当前待处理任务较多，请稍后重试。", "queued_tasks": exc.queued_tasks, "max_queue_size": exc.max_queue_size}) from exc


@app.get("/api/v1/files/{file_id}", response_model=StoredFile, tags=["文件"])
async def get_file(file_id: str) -> StoredFile:
    record = file_store.get(file_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"未找到文件: {file_id}")
    return record


@app.get("/api/v1/files/{file_id}/content", tags=["文件"])
async def download_file(file_id: str) -> FileResponse:
    record = file_store.get(file_id)
    if record is None or not Path(record.path).is_file():
        raise HTTPException(status_code=404, detail=f"未找到文件: {file_id}")
    return FileResponse(record.path, filename=record.filename, media_type=record.content_type)


@app.get("/api/v1/files/{file_id}/artifacts/{artifact_path:path}", tags=["文件"])
async def download_artifact(file_id: str, artifact_path: str) -> FileResponse:
    path = file_store.artifact_path(file_id, artifact_path)
    if path is None:
        raise HTTPException(status_code=404, detail="未找到 artifact")
    return FileResponse(path, filename=path.name)


@app.get("/api/v1/metrics", tags=["系统"])
async def metrics() -> dict:
    """返回轻量应用指标和当前任务队列指标。"""
    return {"http": http_metrics.snapshot(), "tasks": task_manager.metrics().model_dump()}


@app.post("/api/v1/tasks/skill", response_model=TaskSubmitResponse, status_code=202, tags=["任务"])
async def submit_skill_task(request: TaskSkillRequest) -> TaskSubmitResponse:
    """异步排队执行指定顶层 Skill。"""
    if not settings.task_queue_enabled:
        raise HTTPException(status_code=503, detail={"code": "task_queue_disabled", "message": "任务队列当前未启用。"})
    if not Path(request.path).is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {request.path}")
    try:
        return await task_manager.submit("skill.execute", request.model_dump(mode="json"), request.data_id, str(request.callback) if request.callback else None)
    except TaskQueueFullError as exc:
        raise HTTPException(status_code=429, detail={"code": "task_queue_full", "message": "当前待处理任务较多，请稍后重试。", "queued_tasks": exc.queued_tasks, "max_queue_size": exc.max_queue_size}) from exc


@app.post("/api/v1/tasks/office-pipeline", response_model=TaskSubmitResponse, status_code=202, tags=["任务"])
async def submit_office_pipeline_task(request: TaskOfficePipelineRequest) -> TaskSubmitResponse:
    """异步排队执行 Office 转换后自动解析 Pipeline。"""
    if not settings.task_queue_enabled:
        raise HTTPException(status_code=503, detail={"code": "task_queue_disabled", "message": "任务队列当前未启用。"})
    if not Path(request.path).is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {request.path}")
    try:
        return await task_manager.submit("office.parse_pipeline", request.model_dump(mode="json"), request.data_id, str(request.callback) if request.callback else None)
    except TaskQueueFullError as exc:
        raise HTTPException(status_code=429, detail={"code": "task_queue_full", "message": "当前待处理任务较多，请稍后重试。", "queued_tasks": exc.queued_tasks, "max_queue_size": exc.max_queue_size}) from exc


@app.get("/api/v1/tasks/metrics", response_model=TaskMetrics, tags=["任务"])
async def task_metrics() -> TaskMetrics:
    """查看当前进程中的队列与执行统计。"""
    return task_manager.metrics()


@app.get("/api/v1/tasks/{task_id}", response_model=TaskRecord, tags=["任务"])
async def get_task(task_id: str) -> TaskRecord:
    """查询内存任务的当前快照；服务重启或 TTL 过期后不可查询。"""
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"未找到任务: {task_id}")
    return task


@app.post("/api/v1/tasks/{task_id}/cancel", response_model=TaskRecord, tags=["任务"])
async def cancel_task(task_id: str) -> TaskRecord:
    """仅取消尚未被 Worker 领取的排队任务。"""
    task = task_manager.cancel(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"未找到任务: {task_id}")
    if task.status != "cancelled":
        raise HTTPException(status_code=409, detail="仅允许取消排队中的任务")
    return task


@app.get("/api/v1/skills", tags=["Skill"])
async def list_skills() -> list[dict]:
    return registry.list_manifests()


@app.post("/api/v1/parse/rtf", response_model=SkillResult, tags=["解析"])
async def parse_rtf(request: DocumentParseRequest) -> SkillResult:
    """按复杂度自动直接解析 RTF，或转换成 DOCX 后增强解析。"""
    path = Path(request.path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {request.path}")
    context = request.to_context()
    if artifact_dir := file_store.artifact_dir(request.file_id):
        context.metadata["artifact_dir"] = str(artifact_dir)
        context.metadata["output_dir"] = str(artifact_dir)
    return await executor.execute("rtf.parse", context)


@app.post("/api/v1/parse/text", response_model=SkillResult, tags=["解析"])
async def parse_text(request: DocumentParseRequest) -> SkillResult:
    """解析 TXT、Markdown、CSV/TSV 或 HTML 文件。"""
    path = Path(request.path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {request.path}")
    context = request.to_context()
    if artifact_dir := file_store.artifact_dir(request.file_id):
        context.metadata["artifact_dir"] = str(artifact_dir)
    return await executor.execute("text.parse", context)


@app.post("/api/v1/parse/pdf", response_model=SkillResult, tags=["解析"])
async def parse_pdf(request: PdfParseRequest) -> SkillResult:
    """解析一个服务本地可访问的 PDF 文件。"""

    path = Path(request.path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {request.path}")

    return await executor.execute("pdf.parse", request.to_context())


@app.post("/api/v1/parse/docx", response_model=SkillResult, tags=["解析"])
async def parse_docx(request: DocumentParseRequest) -> SkillResult:
    """解析一个服务本地可访问的 DOCX 文件。"""

    path = Path(request.path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {request.path}")

    return await executor.execute("word.parse", request.to_context())


@app.post("/api/v1/convert/office", response_model=SkillResult, tags=["转换"])
async def convert_office(request: FileConversionRequest) -> SkillResult:
    """将本地 Office 文件转换为指定格式，不覆盖源文件。"""

    path = Path(request.path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {request.path}")

    return await executor.execute("office.convert", request.to_context())


@app.post("/api/v1/parse/excel", response_model=SkillResult, tags=["解析"])
async def parse_excel(request: DocumentParseRequest) -> SkillResult:
    """解析 XLSX/XLSM，旧版 XLS 会先转换为 XLSX。"""
    path = Path(request.path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {request.path}")
    return await executor.execute("excel.parse", request.to_context())


@app.post("/api/v1/parse/ppt", response_model=SkillResult, tags=["解析"])
async def parse_ppt(request: DocumentParseRequest) -> SkillResult:
    """解析 PPTX，旧版 PPT 会先转换为 PPTX。"""
    path = Path(request.path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {request.path}")
    return await executor.execute("ppt.parse", request.to_context())


@app.post("/api/v1/pipeline/parse-office", response_model=PipelineResult, tags=["Pipeline"])
async def parse_office_pipeline(request: OfficePipelineRequest) -> PipelineResult:
    """转换旧版 Office 并自动调用对应解析 Skill。"""
    path = Path(request.path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {request.path}")
    try:
        return await pipeline.execute(request.to_context())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v1/prepare/audio", response_model=SkillResult, tags=["媒体"])
async def prepare_audio(request: MediaPrepareRequest) -> SkillResult:
    """探测并标准化音频，产出供后续 ASR 使用的 WAV 文件，不执行语音识别。"""

    path = Path(request.path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {request.path}")
    return await executor.execute("audio.prepare", request.to_context())


@app.post("/api/v1/prepare/video", response_model=SkillResult, tags=["媒体"])
async def prepare_video(request: MediaPrepareRequest) -> SkillResult:
    """探测视频、提取默认音轨并尝试导出字幕，不执行语音识别。"""

    path = Path(request.path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {request.path}")
    return await executor.execute("video.prepare", request.to_context())
