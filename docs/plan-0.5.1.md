# Parse Agent 方案 0.5.1：无模型平台可用性加固

## 目标

在不引入数据库、Redis、OCR 或 ASR 模型的条件下，提升现有解析平台的接入体验、运行可观测性与非模型格式覆盖。

## 交付一：受控本地文件与 artifact

- `POST /api/v1/files` 接受 multipart 上传，保存到 `FILE_STORAGE_DIR`；
- 上传后返回稳定 `file_id`、服务端解析路径与文件元数据；
- `GET /api/v1/files/{file_id}` 查询本地 sidecar JSON 元数据；
- `GET /api/v1/files/{file_id}/content` 下载原文件；
- 每个上传文件拥有受控 `artifacts/` 目录；
- `GET /api/v1/files/{file_id}/artifacts/{artifact_path}` 仅允许下载该目录内的相对路径，拒绝路径穿越；
- 已上传文件进入同步解析或异步任务时自动将 `artifact_dir` 指向该文件专属目录。

文件元数据使用同目录 JSON sidecar，不作为数据库替代；文件和元数据会跨进程重启保留，但查询能力仅限按 `file_id`。

## 交付二：可观测性和安全边界

- HTTP middleware 生成或透传 `X-Request-ID`；
- 记录请求方法、路径、状态码、耗时、request_id 的结构化日志；
- 内存 HTTP 指标：请求总数、状态码分布、累计耗时；
- `GET /api/v1/metrics` 返回应用与任务队列指标；
- `API_KEY` 为空时不启用鉴权；设置后，所有 `/api/` 请求须提供 `X-API-Key`，`/health`、`/docs`、`/openapi.json` 保持可访问；
- 文件上传限制 `FILE_MAX_SIZE_MB` 和允许后缀 `FILE_ALLOWED_SUFFIXES`。

## 交付三：轻量格式解析

新增顶层 `text.parse` Skill，支持：

```text
.txt  .md  .markdown  .csv  .tsv  .html  .htm
```

输出统一 `DocumentResult`：

- TXT：原始文本和 Markdown；
- Markdown：原始 Markdown 与纯文本；
- CSV/TSV：表格、行块、Markdown 表格；
- HTML：提取正文纯文本、Markdown 与链接信息；
- 文件大小和表格行/列数限制，超限返回 `partial`。

同步 REST：

```text
POST /api/v1/parse/text
```

异步任务通过已有 `skill.execute` 使用：

```text
skill_name=text.parse
```

MCP `execute_skill` 不需要新增底层 Tool，即可访问该新顶层 Skill。

## 验收

- 上传后可用返回路径调用同步或异步解析；
- artifact 下载不允许访问存储根目录外文件；
- 设置 `API_KEY` 后，无 Key 的 API 请求被拒绝；
- 每个 API 响应具有 `X-Request-ID`，指标可查询；
- TXT/Markdown/CSV/TSV/HTML 真实 API 解析通过；
- 既有 API、MCP、Pipeline、任务与新集成测试全量通过。
