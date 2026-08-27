# ParseFlow 1.1.0 技术方案：事件时间线、可观测性与运行验证

## 1. 目标

`v1.1.0` 不扩张公开解析格式或改变已冻结的 v1 REST/MCP 契约。目标是将既有异步解析能力升级为可诊断、可运维、可验证的单机/可信内网服务。

核心原则：

- 保持 `/api/v1`、Remote MCP v1、opaque ID、任务状态语义和 schema v1 兼容；
- 不记录 API Key、原文内容、服务器路径或高基数标签；
- 事件日志和指标只能辅助诊断，不能替代任务 JSON 作为事实来源；
- Docker 的安全姿态保持本地默认绑定、最小权限和受控可写目录。

## 2. 任务事件时间线

### 2.1 API

新增稳定接口：

```text
GET /api/v1/tasks/{task_id}/events?after=&limit=
```

返回按 sequence 升序的轻量事件列表和下一游标。事件不包含服务器路径、文件内容或凭据。

### 2.2 事件结构

```json
{
  "sequence": 4,
  "type": "step.started",
  "at": "2026-01-01T00:00:00Z",
  "task_id": "task_<32hex>",
  "step": "pdf.parse",
  "message": "开始解析 PDF",
  "details": {"provider": "pdf.normal.inspector"}
}
```

`details` 是可选低敏感业务元数据；禁止 `path`、`output_dir`、`source_path`、文件名全文和内容片段。

### 2.3 固定事件类型

```text
created
queued
planning.started
step.started
step.finished
warning
artifact.created
cancel.requested
cancelled
succeeded
partial
failed
interrupted
```

事件在任务工作目录或受控 repository 下按 task ID 保存，并采用 append-safe/原子写入策略。任务删除或 TTL 物理清理时一并删除事件。损坏事件文件隔离，不阻塞任务查询或服务启动。

## 3. 结构化日志

### 3.1 格式与字段

新增可配置 JSON 日志模式，最小字段：

```text
timestamp, level, service_version, request_id,
task_id, file_id, skill, provider, status,
duration_ms, error_code, message
```

- `task_id`、`file_id` 仅在日志中作为字段，不能作为 Prometheus label；
- 默认不记录文件名；如未来增加文件名诊断开关，必须默认关闭；
- 对异常对象实施字段过滤，避免路径、请求 body、凭据泄露；
- 保留当前人类可读日志模式，避免破坏本地开发体验。

### 3.2 请求关联

HTTP middleware 生成或接受安全格式的 request ID，并将其放入响应头和结构化日志。后台任务事件/日志关联 task ID 与执行 provider。

## 4. Prometheus 指标

保持 `/metrics` 的 Prometheus exposition，补全如下低基数指标：

```text
parseflow_http_requests_total{method,route,status}
parseflow_http_duration_seconds{route}
parseflow_tasks_total{status,type}
parseflow_tasks_current{status}
parseflow_task_queue_depth
parseflow_task_wait_seconds
parseflow_task_duration_seconds{skill,status}
parseflow_provider_calls_total{provider,status}
parseflow_provider_duration_seconds{provider}
parseflow_artifact_bytes_total
parseflow_storage_bytes
parseflow_storage_cleanup_bytes_total
parseflow_task_recovery_total{outcome}
parseflow_external_process_total{tool,outcome}
```

要求：

- 不使用 task ID、file ID、文件名、请求 ID、错误 message 作为 label；
- 直方图 bucket 明确并在文档说明；
- `/api/v1/metrics` 可继续保留 JSON 诊断视图；
- `/metrics` 只用于 Prometheus 文本格式。

## 5. Readiness 与运行诊断

扩展 `/ready` 的只读检查：

- task repository 可读；
- data root 与 artifact root 可写；
- 可用磁盘空间高于 hard threshold；
- persistent task worker 已启动；
- Skill registry 已加载；
- 必需的外部二进制（如配置启用的 LibreOffice/FFmpeg）存在；
- 启动恢复已完成。

禁止 readiness 下载 OCR 模型、执行真实解析或修改任务记录。

可选新增受 API Key 保护的诊断视图，提供 worker、队列、存储和 provider availability 摘要，但不暴露本地路径。

## 6. Web 工作台

在不改变 API 核心结果结构的前提下补齐任务中心：

- `TaskTimeline` 使用 `/events` 轮询；
- 运行任务 1 秒轮询，长时间任务退避到 3–5 秒；页面重新可见立即刷新；
- 显示事件、Provider、warning、error code 和 artifact 创建信息；
- 明确区分 failed、partial、cancelled、interrupted；
- 在详情中提供 cancel/retry/delete，并执行终态/活动状态约束；
- 网络错误保留上次成功数据，标记“状态可能已过期”；
- 支持 `/tasks/{task_id}` 深链接和刷新恢复；
- 继续保证 keyboard、aria-live、tablist 和焦点恢复。

## 7. Docker 与运行时 Smoke

新增可重复的运行时验证脚本或测试：

1. 构建 `linux/amd64` 镜像；
2. 使用 Compose 启动，确认 loopback 映射；
3. 验证容器非 root、`read_only`、capabilities dropped、PID/CPU/内存限制；
4. 验证 `/health`、`/ready` 和 `/metrics`；
5. 上传一个 TXT/CSV fixture，轮询任务至终态，下载 artifact；
6. 检查服务响应和日志未出现服务器绝对路径；
7. 停止容器并清理临时数据。

本地无法运行 Docker 时，CI 必须在 Linux runner 执行此门禁。

## 8. 测试与发布门禁

### 后端

- 任务事件写入、顺序、分页、重启、损坏隔离、删除/TTL 清理；
- 每个任务状态对应正确 terminal 事件；
- JSON 日志脱敏与 request ID；
- 指标名、label 集、无高基数约束；
- readiness 在 repository、worker、存储和二进制缺失时返回正确结果。

### 前端

- Timeline、warning/error 状态、取消/重试/删除操作；
- 轮询退避、visibility 恢复、请求失败时的 stale UI；
- keyboard 和 aria-live 回归；
- Playwright Chromium 真实后端主流程：上传、任务完成、深链接、retry、delete、artifact。

### Release

- 后端完整 pytest；
- Web TypeScript/build 和组件测试；
- OpenAPI/MCP/schema drift；
- Docker Compose config；
- Linux AMD64 Docker runtime smoke；
- wheel/sdist 干净环境安装和 `parse-agent-mcp` 入口检查；
- `git diff --check` 与文档链接检查。

## 9. 非目标

以下不纳入 `v1.1.0`：

- 多租户、细粒度 RBAC、对象存储或分布式队列；
- 自动 ASR、知识库/RAG、工作流编排；
- 对互联网公开暴露的安全承诺；
- WebSocket/SSE 实时推送（事件首版使用轮询）。

## 10. 完成定义

- 任务具有可靠、无路径泄露的事件时间线；
- 运维人员可凭结构化日志、低基数指标、ready 检查定位任务与资源问题；
- Web 可展示任务执行过程并安全控制终态任务；
- Linux Docker 运行时 smoke 被实际执行或在 CI 强制执行；
- v1 契约、版本、文档和发布门禁保持兼容并通过全量验证。
