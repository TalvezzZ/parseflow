# ParseFlow v1.1.0 功能说明

ParseFlow 是一个面向单机或可信内网环境的异步文档解析平台。用户上传文件后，服务端选择合适的解析能力，任务在后台持久化执行，并通过统一的任务、结果和 artifact 接口提供进度与下载。

## 1. 文件上传与异步任务

- 通过 `POST /api/v1/tasks/parse` 上传文件并创建解析任务。
- 服务端返回不暴露本地路径的 `task_id`、`file_id` 和轮询链接。
- 支持 `goal` 作为可选解析目标，用于影响自动规划。
- 支持大文件类型、后缀和磁盘余量限制；不允许客户端传入本地 `path`、`output_dir` 或 `callback`。
- 任务状态：`queued`、`planning`、`running`、`cancelling`、`succeeded`、`partial`、`failed`、`cancelled`、`interrupted`。

## 2. 持久化任务中心

- 任务、上传文件元数据和 artifact manifest 均保存于本地受控目录，采用原子写入。
- 服务重启后：排队任务会恢复；运行中的任务标记为 `interrupted`，不会被自动重跑。
- 可查询任务详情、历史列表、状态筛选、文本查询、排序和 cursor 分页：
  - `GET /api/v1/tasks`
  - `GET /api/v1/tasks/{task_id}`
- 通过 `GET /api/v1/tasks/{task_id}/events` 查询持久化、可分页的执行时间线，包括规划、步骤、warning、artifact 和终态事件。
- 支持取消、显式重试和删除终态任务：
  - `POST /api/v1/tasks/{task_id}/cancel`
  - `POST /api/v1/tasks/{task_id}/retry`
  - `DELETE /api/v1/tasks/{task_id}`
- 重试总是生成新的任务，并使用 `retry_of` 关联原任务。

## 3. 文档格式与自动解析

平台按文件类型自动选择已注册 Skill/Provider，覆盖：

- PDF：文本、页面、表格和 OCR/增强解析路径。
- Word 与 RTF：DOC/DOCX、复杂 RTF 的转换和结构化结果。
- 表格：XLS/XLSX/XLSM、CSV、TSV，包含工作表、稀疏单元格、公式、合并单元格、表格和图片信息。
- 演示：PPT/PPTX，提取幻灯片文本、表格、图片和组合形状。
- 纯文本与结构化文本：TXT、Markdown、JSON、YAML、INI、配置文件、日志、SQL、HTML、XML、CSS、JavaScript/TypeScript 等。
- 图片：PNG、JPG、WebP、BMP、TIFF 等，并支持 OCR Provider。
- 音视频：MP3、WAV、M4A、AAC、FLAC、OGG、MP4、MOV、MKV、AVI、WebM 的受控媒体预处理；当前不提供语音转写。

所有解析结果收敛为统一文档结果：正文、Markdown、页面/块、表格、图片、警告、执行步骤、指标和 artifact。

## 4. 结果与 Artifact

- 结果通过任务详情返回，页面不会暴露源文件或服务器工作目录路径。
- Artifact 使用 `artifact_id` 管理，按任务进行受控下载：
  - `GET /api/v1/tasks/{task_id}/artifacts`
  - `GET /api/v1/tasks/{task_id}/artifacts/{artifact_id}`
- 前端支持结果概览、表格、结构化 JSON、文本/Markdown、HTML/XML 安全预览、执行计划与 artifact 列表。
- HTML/XML 预览使用 sandboxed iframe，不将不可信内容注入主页面 DOM。

## 5. Web 工作台

React/Vite 工作台提供：

- 文件选择、拖放上传和可选解析目标；
- 后台任务轮询与状态展示；
- 服务端任务历史加载；
- 终态结果、警告、artifact 和执行步骤展示；
- 可访问的基础交互，包括键盘文件选择、ARIA tab/tabpanel 和状态文本提示。

## 6. Remote MCP

Remote MCP 使用 opaque ID，不接受服务器路径。稳定工具包括：

- `list_skills`
- `preview_parse_plan`
- `submit_file_id`
- `get_task`
- `cancel_task`
- `list_artifacts`

启用 Remote MCP 时需要 `MCP_HTTP_ENABLED=true` 和非空 `API_KEY`，适用于可信部署环境。

## 7. 安全与运行保障

- 上传类型、大小和存储可用空间阈值控制；
- XML 节点/深度控制和安全 XML 处理；
- ZIP/OOXML 路径穿越、条目数、解压大小和异常压缩比防护；
- 受控 artifact 工作目录和任务结果大小限制；
- 文件任务记录原子写入、损坏记录隔离；
- Storage Janitor 可清理过期终态 artifact 和陈旧临时文件；
- `/health` 供存活探测，`/ready` 校验本地存储、可用空间和任务 Worker；
- `/metrics` 输出低基数 Prometheus 兼容 HTTP 指标；
- Docker Compose 默认仅绑定 loopback，并启用只读根文件系统、能力收缩与 CPU/内存/PID 限制。

## 8. 稳定契约

v1.0.0 冻结 `/api/v1`、Remote MCP 工具、opaque ID 前缀和稳定任务状态。v1.x 可增加可选字段或能力，但不会移除/重命名公开 endpoint、稳定工具、必填响应字段、稳定状态、错误码或 ID 语义。

详细接口和升级说明见：

- [REST API v1](api/v1.md)
- [Remote MCP v1](mcp.md)
- [v0.x 到 v1.0 迁移](migration/v0-to-v1.md)
- [部署与安全说明](../README.md)
