# Parse Agent 方案 0.5.0：轻量任务化服务

## 1. 目标与边界

将当前同步、服务端本地路径调用方式扩展为单进程内可排队的异步任务服务，以保护 LibreOffice、FFmpeg 和大文件解析对本机资源的占用。

一期明确不引入：

```text
数据库、Redis、消息队列、分布式 Worker、对象存储、宕机恢复、回调签名、回调重试、运行中任务强制取消
```

提供：

```text
内存任务记录、FIFO 排队、最大并发、提交/查询/取消、TTL 清理、callback 回调
```

## 2. 执行模型

```text
POST task API
  → InMemoryTaskManager
  → asyncio.Queue（FIFO）
  → N 个 Worker（N=TASK_MAX_CONCURRENT_EXECUTIONS）
  → SkillExecutor / OfficeParsePipeline
  → 内存状态与可选 callback
```

Worker 数等于最大并发执行数；队列只保存尚未开始的任务。

配置：

```env
TASK_QUEUE_ENABLED=true
TASK_MAX_CONCURRENT_EXECUTIONS=2
TASK_QUEUE_MAX_SIZE=100
TASK_DEFAULT_TIMEOUT_SECONDS=1800
TASK_RESULT_TTL_SECONDS=86400
TASK_CLEANUP_INTERVAL_SECONDS=300
TASK_CALLBACK_TIMEOUT_SECONDS=15
```

队列已满时拒绝任务：

```http
429 Too Many Requests
```

```json
{
  "detail": {
    "code": "task_queue_full",
    "message": "当前待处理任务较多，请稍后重试。",
    "queued_tasks": 100,
    "max_queue_size": 100
  }
}
```

## 3. 状态模型

执行状态：

```text
queued → running → succeeded | failed
queued → cancelled
```

回调状态独立：

```text
not_requested | pending | succeeded | failed
```

`callback_failed` 不改变实际执行结果。例如解析成功但回调返回 502 时，任务仍然是 `succeeded`。

进程重启时，内存中的 queued/running/已完成任务都会丢失；这是本阶段有意接受的边界。

## 4. API

```text
POST /api/v1/tasks/skill
POST /api/v1/tasks/office-pipeline
GET  /api/v1/tasks/{task_id}
POST /api/v1/tasks/{task_id}/cancel
GET  /api/v1/tasks/metrics
```

支持任务类型：

| 类型 | 内部执行 |
|---|---|
| `skill.execute` | `SkillExecutor.execute()` |
| `office.parse_pipeline` | `OfficeParsePipeline.execute()` |

## 5. callback 协议

参考 MinerU 的异步模型，任务请求支持：

```json
{
  "data_id": "customer-file-001",
  "callback": "https://example.com/hooks/parse-agent"
}
```

- `data_id` 为调用方业务标识，将在查询结果与 callback 中原样返回；
- `callback` 为空时，调用方使用任务查询接口轮询；
- callback 必须是 HTTP/HTTPS URL；
- 系统使用 UTF-8 JSON `POST` 请求发送终态任务快照；
- 仅 HTTP 200 视为 callback 成功；
- 非 200、网络错误或超时记录为 callback 失败；
- 一期不签名、不重试，且 callback 失败不影响任务执行结果。

回调示例：

```json
{
  "task_id": "task_01JQ...",
  "data_id": "customer-file-001",
  "task_type": "skill.execute",
  "status": "succeeded",
  "created_at": "2026-03-17T10:00:00Z",
  "started_at": "2026-03-17T10:00:03Z",
  "finished_at": "2026-03-17T10:00:08Z",
  "duration_ms": 5000,
  "result": {}
}
```

## 6. 验收

- 同时执行数永远不超过配置值；
- 超额任务按 FIFO 排队；
- 队列满返回友好 `429`；
- 任务可查询，排队任务可取消；
- callback 可收到成功或失败终态 JSON；
- callback 非 200 不改变任务实际执行状态；
- 既有解析、Pipeline、MCP 与任务测试全量通过。
