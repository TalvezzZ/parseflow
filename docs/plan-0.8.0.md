# ParseFlow 0.8.0 技术方案：文件流、异步任务与本地持久化

## 1. 目标与不兼容变更

`0.8.0` 收敛公开 REST API 和远程 HTTP MCP 的安全边界：调用方只上传文件流、使用不透明 ID，并异步查询结果。

已确认决策：

- 公开接口不接受 `path`。
- 公开接口不接受 `output_dir`。
- 删除 callback，不执行任何任务完成出站请求。
- 所有解析和转换通过有界异步任务执行。
- 任务、结果和 artifact 使用本地文件持久化，不引入数据库、Redis 或分布式 Worker。

本版本允许移除旧公开接口，迁移方法必须在发布说明中列出。

## 2. 目标架构

```text
HTTP multipart upload
  → LocalFileRepository.save_stream()
  → StoredFilePublic(file_id, filename, size, media_type)
  → TaskService.create_parse_task(file_id, goal)
  → FileTaskRepository.create(task.json)
  → PersistentTaskQueue
  → Worker resolves file_id internally
  → Planner / Skill / Pipeline
  → ArtifactRepository writes manifest
  → FileTaskRepository atomically persists terminal result
  → Client polls GET /tasks/{task_id}
```

边界分层：

```text
API/MCP models       只含 ID 和公开元数据
Application services 负责状态机和工作区分配
Internal execution   可使用真实 Path，但不序列化给客户端
Repositories         负责文件、任务和 artifact 的受控磁盘访问
```

## 3. 模块拆分

```text
app/api/
  files.py            上传与受控下载路由
  tasks.py            创建、查询、取消任务
  models.py           公开请求/响应 DTO
app/storage/
  ids.py              file/task/artifact ID 校验
  atomic.py           原子 JSON 写入和 fsync
  files.py            LocalFileRepository
  tasks.py            FileTaskRepository
  artifacts.py        ArtifactRepository/manifest
  locks.py            单任务锁抽象
app/tasks/
  models.py           内部持久化记录和状态机
  service.py          TaskService
  queue.py            单进程持久队列适配
  worker.py           执行、恢复、取消、关闭
app/mcp/
  common_tools.py     无路径查询能力
  remote_tools.py     file_id/task_id 工具
  local_tools.py      可选 stdio 本机路径工具
```

`app/main.py` 只组装依赖和挂载 Router。

## 4. 公开 API 契约

### 4.1 创建解析任务

```http
POST /api/v1/tasks/parse
Content-Type: multipart/form-data
```

字段：`file`、可选 `goal`、可选 `data_id`。`data_id` 不参与路径构造。

成功返回 `202`：

```json
{
  "task_id": "task_...",
  "file_id": "file_...",
  "status": "queued",
  "queue_position": 1,
  "created_at": "2026-01-01T00:00:00Z",
  "links": {
    "self": "/api/v1/tasks/task_...",
    "file": "/api/v1/files/file_.../content"
  }
}
```

任务创建前失败时清理刚上传的文件；任务记录创建后发生错误则持久化 failed 状态。

### 4.2 查询、列出和取消

```http
GET  /api/v1/tasks/{task_id}
GET  /api/v1/tasks?status=&cursor=&limit=
POST /api/v1/tasks/{task_id}/cancel
```

状态机：

```text
queued → planning → running → succeeded | partial | failed
queued → cancelled
planning/running → cancelling → cancelled
running → interrupted（异常重启恢复）
```

非法迁移返回 `409 task_state_conflict`。

### 4.3 文件和 artifact

```http
GET /api/v1/files/{file_id}
GET /api/v1/files/{file_id}/content
GET /api/v1/tasks/{task_id}/artifacts
GET /api/v1/tasks/{task_id}/artifacts/{artifact_id}
```

公开文件模型删除 `path`，只返回元数据和下载 URL。Artifact manifest 使用服务端生成的 `artifact_id`，外部不再传相对路径。

## 5. 数据模型

### 5.1 公开与内部文件模型分离

```python
class StoredFilePublic(BaseModel):
    file_id: str
    filename: str
    content_type: str | None
    size_bytes: int
    created_at: datetime
    download_url: str

class StoredFileRecord(BaseModel):
    file_id: str
    filename: str
    content_type: str | None
    size_bytes: int
    created_at: datetime
    storage_name: str
    relative_path: str
    sha256: str
```

绝对路径不进入 API 响应、任务 request/result 或日志。

### 5.2 持久化任务

```python
class PersistedTaskRecord(BaseModel):
    schema_version: Literal[1] = 1
    revision: int
    task_id: str
    task_type: Literal["parse.intent"]
    file_id: str
    status: TaskStatus
    goal: str | None
    data_id: str | None
    retry_of: str | None
    cancel_requested: bool = False
    plan: dict | None
    result: TaskResultEnvelope | None
    error: ProviderError | None
    warnings: list[str]
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
```

### 5.3 统一结果 envelope

Worker 在落盘前统一 SkillResult/PipelineResult：

```json
{
  "status": "succeeded",
  "document": {},
  "artifacts": [],
  "steps": [],
  "conversion": null,
  "warnings": [],
  "metrics": {},
  "error": null
}
```

前端不再判断多层 `result.result.data.document`。

## 6. 磁盘布局与路径约束

```text
DATA_DIR/
├── files/file_<id>/metadata.json + source.<ext>
├── tasks/task_<id>.json
├── tasks/quarantine/
├── artifacts/task_<id>/manifest.json + files/
├── locks/
└── tmp/
```

- 磁盘文件名由服务端生成，原始文件名只用于展示和下载。
- ID 使用完整正则校验，例如 `^task_[0-9a-f]{32}$`。
- Repository 组合路径后执行 `resolve()` 和 `relative_to(root)`。
- 禁止符号链接逃逸。
- 临时文件与最终文件位于同一文件系统。

## 7. 原子写入与并发控制

`atomic_write_json()`：

1. 同目录创建随机 `.tmp`。
2. 写 UTF-8 JSON，flush 并 `os.fsync(file)`。
3. `os.replace(tmp, target)`。
4. 关键元数据创建时 fsync 目录。

任务每次更新增加 `revision`。更新接口接收 `expected_revision`，不匹配时抛出冲突。单进程用 `asyncio.Lock`，外层保留文件锁抽象；`0.8.0` 明确只支持一个 Uvicorn Worker。

Artifact 先写文件，再原子更新 manifest；任务成功前验证 manifest 实体存在。

## 8. 队列、恢复与关闭

### 启动恢复

1. 验证存储目录权限。
2. 扫描任务 JSON；损坏记录移入 quarantine。
3. `planning/running/cancelling` 转为 `interrupted`。
4. `queued` 按创建时间重新入队。
5. 启动 Worker 和清理任务。

首版不自动重跑 interrupted，避免转换副作用；显式重试创建新 task_id 并记录 `retry_of`。

### 优雅关闭

- 停止接收和领取新任务。
- 等待 grace period。
- 未结束任务持久化为 interrupted。
- 终止已登记的外部子进程。

### 取消

- queued：持久化 cancelled；遗留 queue item 被取出时跳过。
- running：设置 `cancel_requested`，Provider 检查 token。
- LibreOffice/FFmpeg 保留 subprocess handle，terminate 超时后 kill。

## 9. 移除 callback

删除：

- 公开请求和 MCP 的 callback 字段。
- `TaskRecord.callback*`。
- `InMemoryTaskManager._deliver_callback()`。
- callback timeout 配置和测试。

旧任务 JSON 中的 callback 字段可忽略读取，但绝不执行出站请求。

## 10. 旧路径 API 迁移

生产环境移除 `/tasks/skill`、`/tasks/office-pipeline`、各 `/parse/*`、`/convert/office`、`/pipeline/parse-office`、`/prepare/*` 路由。若开发测试仍需路径模式，移入未默认挂载的 `app/dev_api.py`。

```text
旧：上传 → 获取 path → 调用解析端点
新：POST /tasks/parse(file) → task_id → GET /tasks/{task_id}
```

## 11. MCP 分离

远程 HTTP 工具：

```text
list_skills
preview_parse_plan(filename_or_suffix, goal?)
submit_file_id(file_id, goal?, data_id?)
get_task(task_id)
cancel_task(task_id)
list_artifacts(task_id)
```

远程工具不得接受 path/output_dir/callback。`preview_parse_plan` 不访问文件系统。

本机 stdio 路径工具如保留，放入独立 MCP server/profile，并明确仅可信本机；远程挂载不注册它们。

## 12. 配置和 Docker

```text
DATA_DIR=./data
TASK_MAX_CONCURRENT_EXECUTIONS=2
TASK_QUEUE_MAX_SIZE=100
TASK_RESULT_TTL_SECONDS=86400
TASK_SHUTDOWN_GRACE_SECONDS=30
TASK_MAX_RESULT_SIZE_MB=20
STORAGE_MIN_FREE_MB=1024
```

移除 callback 配置。检测多 Uvicorn Worker 时拒绝启动或报致命错误。

Docker 默认：

```yaml
ports:
  - "${PARSEFLOW_BIND_HOST:-127.0.0.1}:${PARSEFLOW_PORT:-8080}:80"
```

Nginx 不覆盖客户端 `X-API-Key`。本版本定位本机/可信内网，不宣称多用户隔离。

## 13. 测试矩阵

### Repository

- ID、路径穿越、绝对路径和符号链接逃逸。
- 原子写入故障不破坏旧版本。
- revision 冲突、损坏 JSON quarantine。
- Artifact manifest 与文件一致。

### Task

- 所有合法/非法状态迁移。
- 重启恢复 queued，running 转 interrupted。
- 排队和运行中取消。
- 队列满、结果过大、磁盘空间不足。
- TTL 清理不删除活动任务引用文件。

### API/MCP

- OpenAPI 和远程 MCP schema 不含 path/output_dir/callback。
- 上传、轮询、下载端到端。
- JSON/日志不含 storage root。
- 旧路径路由生产环境 404。
- 远程 MCP 只能使用已有 file_id。

### 故障注入

- task JSON 写入前后崩溃。
- Worker 执行中重启。
- 临时文件残留。
- 外部进程超时/取消。
- 磁盘只读或空间不足。

## 14. 实施阶段

1. 建立公开/内部 DTO、ID 和 Repository。
2. 实现 FileTaskRepository、状态机、原子写入和恢复。
3. 以 TaskService 替换 InMemoryTaskManager，统一结果 envelope。
4. 上线新任务、列表、取消和 artifact API。
5. 前端切换新 API。
6. 拆分远程/本地 MCP。
7. 删除 callback 和生产路径型路由。
8. 调整 Docker/Nginx 并完成迁移文档。
9. 执行重启、故障注入、回归和 Docker smoke。

## 15. 风险与控制

- JSON 文件增长：首版用内存索引 + cursor；达到规模阈值再评估 SQLite。
- 多进程争用：强制单应用进程。
- 结果过大：设置 JSON 上限，大内容转 artifact。
- 重试副作用：interrupted 不自动重跑。
- 不兼容升级：发布迁移表和完整 OpenAPI 示例。

## 16. 完成定义

- 生产 REST/OpenAPI/远程 MCP 不存在 path、output_dir、callback。
- 所有耗时操作通过持久任务执行。
- 重启后 queued 和 terminal 可恢复，运行中状态真实。
- 文件和 artifact 只通过不透明 ID 访问。
- 结果 envelope 对所有格式一致。
- Docker 默认仅绑定本机，代理不注入全局 API Key。
- Repository、恢复、安全 schema 和端到端测试通过。
