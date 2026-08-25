# ParseFlow 0.9.0 技术方案：任务中心、前端质量与可观测性

## 1. 目标与前置条件

`0.9.0` 基于 `0.8.x` 的文件流 API、统一结果 envelope、本地任务持久化和受控 artifact。目标是把当前单页演示工作台升级为可长期使用的任务中心，并建立浏览器测试与运行可观测性。

前置条件：任务状态以服务端文件仓库为事实来源；前端 localStorage 只能保存 UI 偏好，不能保存权威任务数据。

## 2. 后端任务查询能力

新增/稳定：

```http
GET    /api/v1/tasks?status=&query=&cursor=&limit=&sort=
GET    /api/v1/tasks/{task_id}
GET    /api/v1/tasks/{task_id}/events
POST   /api/v1/tasks/{task_id}/cancel
POST   /api/v1/tasks/{task_id}/retry
DELETE /api/v1/tasks/{task_id}
```

### 列表索引

保持文件存储模型，在 `FileTaskRepository` 后建立可重建内存索引：

```text
created_at → task_id
status → task_id set
file_id → task_id set
data_id → task_id set
```

启动时从任务 JSON 重建。cursor 使用稳定的 `created_at + task_id`，不暴露磁盘偏移。达到文件规模阈值后再评估 SQLite，不在本版本隐式切换。

### 事件时间线

每个任务可保存轻量追加事件：

```json
{
  "sequence": 4,
  "type": "step.started",
  "at": "...",
  "step": "pdf.parse",
  "message": "开始解析 PDF"
}
```

事件类型包括 created、queued、planning、step started/finished、warning、artifact created、cancel requested 和 terminal。事件不能包含服务器路径。首版使用轮询 `GET /events?after=`，不要求 WebSocket/SSE。

### 删除与保留

`DELETE` 首先写 tombstone/soft delete，UI 可区分 deleted、expired、missing input、missing artifact 和 corrupt record。后台清理在保留期后物理删除。活动任务不能删除。

### 重试

重试创建新 task_id，引用原 file_id 和 `retry_of`；原任务不可变。源文件已过期时返回稳定 `source_file_expired`。

## 3. 前端架构

从单个 `web/src/main.tsx` 拆分：

```text
web/src/
  app/App.tsx
  api/client.ts
  api/tasks.ts
  api/files.ts
  types/contracts.ts
  features/upload/UploadPanel.tsx
  features/tasks/TaskList.tsx
  features/tasks/TaskFilters.tsx
  features/tasks/TaskDetail.tsx
  features/tasks/TaskTimeline.tsx
  features/results/ResultTabs.tsx
  features/results/DocumentOverview.tsx
  features/results/TableViewer.tsx
  features/results/JsonViewer.tsx
  features/results/HtmlPreview.tsx
  features/results/ArtifactList.tsx
  hooks/useTaskPolling.ts
  hooks/useTaskList.ts
  lib/result-normalizer.ts
```

API types从 OpenAPI 生成或由共享契约生成，避免手写类型漂移。组件不读取后端嵌套原始结构，只接收统一 TaskPublic/ResultEnvelope。

## 4. 状态管理和轮询

首版使用 React hooks + Context 或轻量 server-state 库，避免自建复杂全局 store。

- 任务列表和详情独立缓存。
- 运行任务 1 秒轮询，长任务逐步退避到 3–5 秒。
- 页面不可见时降低频率；重新可见立即刷新。
- terminal 停止轮询。
- 网络错误保留上次成功数据并显示“状态可能已过期”。
- 页面刷新根据 URL `/tasks/{task_id}` 恢复详情。
- localStorage 仅保存 active tab、表格分页等偏好。

## 5. 上传体验

前端从 capability/配置端点读取支持后缀和最大文件大小，用于即时提示；服务端仍为最终校验方。

状态：选择、预检、上传中、服务器接收、排队。支持拖拽、文件选择、上传进度和取消上传；成功后导航到任务详情。错误按稳定 code 映射用户文案，并保留 request_id。

## 6. 结果展示

### 状态语义

- succeeded：完整成功。
- partial：结果可用，但 warnings 固定置顶。
- failed：不显示成功时间线。
- cancelled：明确区分用户取消。
- interrupted：提供重试操作。
- expired/deleted/corrupt：提供不同恢复说明。

### Artifact

使用 `artifact_id` 和 `download_url`。展示名称、类型、大小、hash、来源步骤；图片可懒加载预览，其他文件下载。下载失败区分 artifact 过期和任务无权限/不存在。

### 表格与结构化结果

- 表格分页或虚拟滚动，不固定丢弃 50 行。
- 支持 CSV/JSON 导出；大型导出由后端 artifact 生成。
- JSON 折叠、搜索、复制。
- HTML/XML 继续使用 `sandbox=""` iframe，不把内容注入主 DOM。
- warnings、Provider、plan、steps、metrics 有独立执行详情区。

## 7. 可访问性

- 上传、错误、复制和任务终态使用 `aria-live`。
- 新任务创建后焦点移动到任务标题；失败后聚焦错误摘要。
- Tabs 实现 `tablist/tab/tabpanel`，支持左右键、Home/End。
- 状态不只依赖颜色，所有图标有文本。
- Dialog、菜单和 Toast 具备焦点恢复。
- 表格有 caption、列头和键盘可达导出操作。
- 移动端不隐藏关键服务/任务状态。

目标：核心流程自动 axe 检查无 critical/serious 问题。

## 8. 测试体系

### 单元/组件

引入 Vitest、React Testing Library、MSW、jest-dom：

- API 错误归一化。
- result normalizer。
- 各任务状态和时间线。
- partial/warnings。
- Artifact 下载。
- XML/HTML sandbox。
- Tabs 键盘行为和 aria-live。
- 表格分页/导出。

### 浏览器 E2E

Playwright 使用真实后端：

1. 上传 TXT/CSV/XML/PDF。
2. 轮询至成功并验证正文/表格/HTML。
3. Office Pipeline 结果。
4. Artifact 下载。
5. failed/partial/cancelled/interrupted。
6. 刷新和深链接恢复。
7. 服务端任务列表、筛选和分页。
8. 重试和软删除。
9. Chromium 必跑；Firefox/WebKit 可 nightly。

CI 保存失败截图、trace 和后端日志。

## 9. 可观测性设计

### 结构化日志

JSON 字段：timestamp、level、service_version、request_id、task_id、file_id、skill、provider、status、duration_ms、error_code。禁止记录 API Key、原文内容和服务器路径；文件名按配置决定是否记录。

### Prometheus 指标

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

标签不得包含 task_id/file_id/filename，避免高基数。

### Health/Ready

- `/health`：进程存活。
- `/ready`：Repository 可读取、存储可写、Worker 存活、Registry 加载、生产所需二进制可用、恢复完成、磁盘高于拒绝阈值。
- Readiness 不初始化或下载 OCR 模型。

## 10. 实施顺序

1. 增加任务列表、事件、重试和软删除后端 API。
2. 完成结果 envelope 与 OpenAPI TypeScript 类型生成。
3. 拆分前端 API、任务列表、任务详情和结果组件。
4. 完成上传进度、轮询、深链接和错误状态。
5. 完成 artifact、表格、JSON、HTML/XML 体验。
6. 完成可访问性整改。
7. 引入 Vitest/RTL/MSW 和组件测试。
8. 引入 Playwright 实后端 E2E。
9. 上线 JSON 日志、Prometheus 和 readiness。
10. 将测试和指标文档纳入发布门禁。

## 11. 完成定义

- 任务历史来自服务端且支持分页、筛选、重试、取消和软删除。
- 页面刷新、深链接和服务重启后任务仍可正确显示。
- 所有终态、warnings 和 artifact 语义正确。
- 核心 UI 已模块化，不再由单个 main.tsx 承载。
- 核心流程有组件测试和真实 Playwright E2E。
- 关键交互满足键盘和自动可访问性检查。
- 日志、指标、health/readiness 可支持故障定位且不泄露路径或内容。
