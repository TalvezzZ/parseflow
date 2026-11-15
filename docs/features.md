# ParseFlow v1.6.0 功能说明

本次统一结果文件存储改造已完成本地实现与验证；版本为1.6.0，尚未生产部署或创建远端Release。历史任务保留读取兼容。

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

- PDF：文本型 PDF 的普通解析可由 `PDF_NORMAL_PROVIDER` 在 pdf-inspector 与 PyMuPDF 之间切换；PyMuPDF 路径可无 OCR 提取嵌入栅格图片并记录页码、坐标，扫描 PDF 仍走 OCR/增强解析路径。
- Word 与 RTF：DOC/DOCX、复杂 RTF 的转换和结构化结果。
- 表格：XLS/XLSX/XLSM、CSV、TSV，包含工作表、稀疏单元格、公式、合并单元格、表格和图片信息。
- 演示：PPT/PPTX，提取幻灯片文本、表格、图片和组合形状。
- 纯文本与结构化文本：TXT、Markdown、JSON、YAML、INI、配置文件、日志、SQL、HTML、XML、CSS、JavaScript/TypeScript 等。
- 图片：PNG、JPG、WebP、BMP、TIFF 等，并支持 OCR Provider。
- 音视频：MP3、WAV、M4A、AAC、FLAC、OGG、MP4、MOV、MKV、AVI、WebM 的受控媒体预处理；当前不提供语音转写。

所有解析结果收敛为统一文档结果：正文、Markdown、页面/块、表格、图片、警告、执行步骤、指标和 artifact。

## 4. 结果与 Artifact

- 新任务的完整结构化内容保存到 `result/document.json`，`result/manifest.json` 描述单元和数据块；任务详情仅返回有界 `document` 预览与 `result_ref`，不再承载完整正文。
- 任务元数据继续使用JSON文件持久化，没有新增SQL任务仓库。相对存储引用和opaque ID用于访问，不接受任意本地路径，也不泄露绝对路径。
- `GET /api/v1/tasks/{task_id}/result` 获取结果目录；`GET /api/v1/tasks/{task_id}/result/units/{unit_id}/records?offset=0&limit=100` 按单元读取记录。完整文档通过artifact下载。
- 页面、幻灯片、块、表格等按Provider真实结构拆分；Word不强行按不可靠页码拆分，音视频仍只保存已有预处理结果。
- 旧任务内嵌文档和旧Excel SQLite结果继续可读，不自动批量迁移。预览缩减与实际解析截断分别标记；仅预览缩减不意味着任务 `partial`。
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

## 8. 质量报告与结果导出

v1.2.0 在任务结果中增加可选的 `quality` 与 `provenance`：输入分类、页/表格/图片数量、截断状态、可靠来源置信度、Provider/fallback 链路和解析器信息。未知置信度保持 `null`，不会根据启发式结果伪造模型置信度。

完整结果文件和可选导出使用独立预算。新任务的权威结构化数据不依赖任务预览，也不因可选导出超限而丢失；JSON、Markdown、纯文本和表格导出的完整性应以manifest及预览标记为准：

- 完整文档：`result/document.json` 与单元数据块；任务详情中的正文/表格只是有界预览；
- 可选导出：Markdown、纯文本以及格式适用的CSV/JSON/Markdown/XLSX，受独立导出预算约束；
- 文件通过opaque artifact ID受控下载，完整结果访问使用任务与单元ID，公开响应不包含服务器绝对路径；
- Web 工作台展示输入分类、Provider链路、实际截断状态，并区分预览与完整结果访问。

## 9. 任务工作台与浏览器质量门禁

v1.3.0 起，服务端任务 API 保持为唯一事实来源，并持续增强日常审核体验：

- 状态、文件类型、日期、Provider、错误码和文本筛选，支持 cursor 分页与 URL 恢复；
- capability 端点驱动上传后缀和大小提示，上传支持进度与取消；
- 轮询按任务时长退避，页面恢复可见时立即刷新，网络失败明确标记 stale；
- 图片 artifact 懒加载预览，大表格分页，JSON/详情支持搜索、折叠和复制；
- Tabs 支持方向键、Home/End，状态和错误使用 aria-live 与焦点管理；
- Vitest/RTL 作为前端单元门禁，Playwright Chromium 使用真实后端验证上传、轮询、深链接、下载、重试和删除；
- axe 对核心浏览器流程执行 critical/serious 可访问性检查。

### 9.1 OCR 页面预览（v1.3.1）

- 扫描 PDF 与独立图片的 OCR 结果在页面级记录 `width`、`height` 和 `preview`，用于前端版面校对；
- 渲染出的页面图像作为受控 artifact 随任务发布，与结果导出共用同一存储边界；
- 前端“OCR 校对”页签并列展示原始图像（含文本坐标标注）与重建版面，支持文本块选择与置信度查看。

### 9.2 OCR 校对补完与存储治理（v1.4.0）

- OCR 校对入口不再限定图片类型：扫描 PDF 的页面级尺寸与预览同样驱动校对视图；
- 多页结果提供页切换，切换时重置文本块选择，并优先使用已发布的预览 artifact；
- 页面缺少预览图或尺寸时给出明确说明，而不是渲染空白区域；
- 预览元数据只保留展示所需的 `filename` 与 `content_type`，内部存储键不进入公开结果；
- 解析方式选择器补齐键盘操作：方向键、Home/End、Escape 关闭并回焦；
- 删除终态任务时同步回收该任务的 artifact 与上传源文件；被重试任务复用的源文件不会被误删；
- 后台清理会保守回收无任务记录的孤儿目录，并以 `STORAGE_ORPHAN_MIN_AGE_SECONDS` 作为安全阈值；
- 公开文档与事件详情不再包含 `artifact_dir` 等服务器路径字段。

## 10. Excel文件存储与容量边界（v1.6.0，本地验证完成）

- 新Excel结果使用 `workbook/manifest.json` schema 2，按Sheet保存gzip JSONL块；共享字符串、样式及公式查询所需SQLite仅作为私有临时数据。
- XLSX/XLSM行列坐标默认支持1,048,576行、16,384列（XFD），保持行列1-based与Sheet ID 0-based；稀疏数据不展开中间空白矩形。
- 有效单元格总数仍默认10,000,000；文件大小、解压大小、XML大小、完整输出、分块、耗时和预览分别设预算。旧XLS受自身格式边界约束。
- 保留既有Sheet行游标接口与旧SQLite读取；分页要么返回完整行，要么明确拒绝超大行，不静默漏格。
- 巨大空白跨度的CSV导出有单独预算，完整坐标支持不意味着无条件生成巨大矩形文件。
- 新格式千万级数值样本完整保存成功，约135秒、解析进程峰值约56MiB、压缩产物约50MiB；兼容与生命周期回归通过，条件与边界见 [验证记录](file-result-storage-validation-1.6.0.md)。

## 11. 稳定契约

v1.0.0 冻结 `/api/v1`、Remote MCP 工具、opaque ID 前缀和稳定任务状态。v1.x 可增加可选字段或能力，但不会移除/重命名公开 endpoint、稳定工具、必填响应字段、稳定状态、错误码或 ID 语义。

详细接口和升级说明见：

- [REST API v1](api/v1.md)
- [Remote MCP v1](mcp.md)
- [v0.x 到 v1.0 迁移](migration/v0-to-v1.md)
- [部署与安全说明](../README.md)
