# ParseFlow

> 本地优先的智能文档解析工作台，把文件转成结构化、可用的内容。

ParseFlow 接收单个文件，自动选择合适的解析流程，并在 Web 工作台中展示文本、表格、Markdown、结构化 JSON 以及生成的产物。后端基于 FastAPI 与可插拔的 Skill；前端是 React/Vite 应用。

[English](README.md) · **简体中文**

当前工作版本为 **1.6.1**，这是一个 PATCH 版本，把结果表示的生成抽取到渲染层。输出格式、端点、默认 Provider 与结果字段语义均未改变；四个已迁移 Provider（PyMuPDF、PaddleOCR、PPT、Excel）的 markdown 与纯文本输出与 1.6.0 字节一致，并由回归测试把关。1.6.0 的统一结果文件存储保持不变。参见[渲染层方案](docs/plan-1.6.1.md)与[版本策略](docs/versioning.md)。

1.6.1 的验证在发布提交上执行：后端 357 passed / 1 skipped，前端 39 passed，生产构建通过。镜像发布与生产部署未执行。

## 核心特性

- **自动规划** —— 按文件类型选择已注册的 Skill；默认的规则式流程不需要任何模型 API Key。
- **可选解析档位** —— 默认停留在 **智能默认**；**标准解析** 走原生/快速抽取，**增强解析** 面向 OCR 优先的 PDF/图片与 Office 转 PDF 版面解析。
- **多格式解析** —— 文档、Office 文件、结构化文本、电子表格、演示文稿、音频与视频。
- **结构化结果** —— 归一化文档模型，含内容块、表格、图片、Markdown、纯文本、provenance、警告与产物。
- **文件承载结果** —— 完整结构化结果存放在任务自有文件中；有界任务预览与稳定结果引用让任务查询在各受支持文件类型下都保持轻量。
- **安全的表格处理** —— XLSX/XLSM 的 OOXML 流式写入按 Sheet 的 gzip 分块，保留完整 Excel 坐标、有界预览、行分页与可杀死的解析/导出 worker；旧 SQLite 结果仍可读取。
- **本地上传边界** —— 受控本地存储、文件大小与扩展名限制、可选 `X-API-Key` 鉴权与任务指标。
- **开发者集成** —— 仅上传的 REST 任务、持久轮询，以及通过本地 stdio 或带鉴权的 Streamable HTTP 提供的不透明 ID MCP。

## 支持的格式

| 类别 | 格式 | 当前行为 |
| --- | --- | --- |
| 文档 | PDF、DOC、DOCX、RTF | 文本型 PDF 使用可配置的 `pdf-inspector` 或 PyMuPDF Provider；PyMuPDF 抽取内嵌位图但不做 OCR；扫描/图片型 PDF 使用可选的本地 PaddleOCR；DOC/RTF 在需要时经 LibreOffice 转换 |
| 图片 | PNG、JPG、JPEG、WEBP、BMP、TIF、TIFF | 可选本地 PaddleOCR 从独立图片中抽取文本、Markdown、包围盒与置信度 |
| 电子表格 | XLS、XLSX、XLSM、CSV、TSV | 表格与语义单元格抽取 |
| 演示文稿 | PPT、PPTX | 幻灯片文本、表格与图片 |
| 文本与结构化数据 | TXT、MD、Markdown、HTML、HTM、XML、JSON、YAML、YML、INI、CFG、CONF、LOG、SQL、JS、TS、CSS | 可用时走专用 HTML/XML 抽取；其余白名单文本格式保留原文 |
| 音频 | MP3、WAV、M4A、AAC、FLAC、OGG | 元数据与可选音频归一化（auto 会归一化）；不做 ASR |
| 视频 | MP4、MOV、MKV、AVI、WEBM | 元数据、结构化内嵌文本字幕与播放相对时间轴；仅在请求时做音频归一化；不做 ASR 或视觉推断 |

### 视频与音频阶段

视频 `auto` 现在探测元数据并抽取一条合适的内嵌**文本字幕**轨，不再生成 WAV。静音视频可以仅凭元数据/字幕成功；仅元数据成功不代表其语音或画面内容已被识别。独立音频的 `auto` 为兼容性保留归一化。未启用 ASR、抽帧 OCR 或视觉模型。

`POST /api/v1/tasks/parse` 上的可选 multipart 字段：

- `media_audio_mode=auto|skip|normalize`：显式选择 `normalize` 会为视频生成单声道 16 kHz PCM WAV。
- `media_subtitle_mode=auto|skip|extract`：`extract` 请求字幕结果；请求的字幕不可用时产生 `partial` 并给出阶段原因。
- `audio_stream_index` / `subtitle_stream_index`：可选的 FFprobe 全局索引，会做校验而非静默回退。
- `subtitle_language`：可选的轨道语言标记（如 `zho` 或 `eng`）；精确的大小写不敏感匹配。显式轨道索引优先。

任务会保留这些选项以便重试。结果提供 probe/subtitles/audio 阶段状态、如实的 `content_scope`、选中轨道与 `subtitle_available`；`transcript_available=false` 仍然表示未做 ASR。转换后的 SRT、结构化 JSONL 时间轴、阶段记录与请求的 WAV 都是任务自有文件。SRT 是导出表示，不一定是源轨格式；本阶段不新增 VTT 输出。

```http
GET /api/v1/tasks/{task_id}/timeline?after_ms=0&after_index=-1&limit=100
```

请逐字使用 `next_cursor`。复合 `(start_ms, cue_index)` 游标可保留起始时间相同的重叠字幕。时间轴时间相对于播放位置，不是容器包的绝对 PTS；源起始时间与时长基准会被保留。完整资源仍可在结果清单中发现。位图/硬字幕需要未来的 OCR，会明确报告为不支持。

FFprobe/FFmpeg 是可取消的子进程，共享 `MEDIA_TIMEOUT_SECONDS` 截止时间，日志有界并使用私有暂存区。`MEDIA_MAX_OUTPUT_SIZE_MB=512`、`MEDIA_MIN_FREE_MB=128`、`MEDIA_NUM_THREADS=2` 与 `MEDIA_MAX_CONCURRENT_EXECUTIONS=1` 约束媒体工作。媒体并发上限位于既有全局任务队列内部，不引入独立的公平调度器。视频上传默认 **500 MiB**，由 `VIDEO_MAX_UPLOAD_SIZE_MB=500` 控制；其他文件（含音频）沿用 `FILE_MAX_SIZE_MB=100`。能力接口按后缀暴露上限，使工作台校验同一套策略。这些上传限制与 Provider 的 2 GiB 输入预算相互独立。未知/非有限时长会被拒绝；非常见容器可能报告保守的时长估计。长文件容量与分段续传不在短样本测试的承诺范围内。

参见[实现方案与验证](docs/video-parsing-phase1.md)。

### 统一结果文件存储（1.6.0）

新任务的元数据仍以 JSON 存放在 `DATA_DIR` 下；本次变更不引入 SQL 任务仓库。完整解析内容发布到受控 artifact 工作区：`result/manifest.json` 描述结果单元与数据块，`result/document.json` 保留完整结构化文档。页面、幻灯片、内容块、表格或其他 Provider 产出的单元各有独立数据块文件。媒体流程只发布既有的预处理输出；这不增加 ASR。

任务结果里的 `document` 是有界预览，`result_ref` 标识文件承载的结果。使用受鉴权的结果端点发现单元并读取记录，无需取回整个文档：

```http
GET /api/v1/tasks/{task_id}/result
GET /api/v1/tasks/{task_id}/result/units/{unit_id}/records?offset=0&limit=100
```

完整结构化文档可通过 `result_ref.document_url`（`/api/v1/tasks/{task_id}/result/document`）或其不透明 artifact ID 下载。清单列出的资源（含完整 Excel 分块）通过受控的 `/result/files/{storage_key}` 下载。任务 artifact 预览上限为 100 条；用 `result_ref.artifacts_url` 配合 `offset`/`limit` 获取其余目录。内部存储引用使用相对键；调用方不能提供任意服务器路径。新客户端不得把任务预览当作完整结果。预览截断与源数据截断不同：完整文件承载的解析结果可能有被缩短的预览，但并不因此是 `partial`。既有内嵌文档任务与旧 Excel SQLite artifact 仍可读取；不做自动批量重写或删除。

结果文件先暂存再发布，任务引用只在结果发布后才记录。备份、删除、过期与孤儿清理必须同时覆盖任务元数据与其整个 artifact 树。文件承载发布约束了持久记录与结果响应体量；它不会自动让每个第三方解析器内部变成流式。

### 大型 Excel 工作簿

标准 Excel 抽取按工作簿关系顺序流式处理工作表 XML。新的完整单元格输出使用 `workbook/manifest.json` **schema 2**、按 Sheet 独立目录与 gzip 压缩的 JSONL 分块。共享字符串与查找数据可使用私有临时 SQLite；辅助数据库不作为最终单元格存储发布。公式保留表达式与可用的缓存值。隐藏与极隐藏工作表被排除。VBA 不执行。

任务 `tables[].cells` 与 `tables[].rows` 是预览。保留行端点同时服务新的文件承载工作簿与旧的 `workbook.sqlite` 结果：

```http
GET /api/v1/tasks/{task_id}/sheets/{sheet_id}/rows?after_row=0&limit=100
```

Sheet ID 从 0 开始，而行列坐标沿用 Excel 的 1 起始编号。请使用 `next_after_row` 而非固定行增量；分页可能因字节上限而变短。超出单行预览预算的行必须显式拒绝，而不是静默丢格。完整内容仍可从已存分块与 artifact 获取。

XLSX/XLSM 的坐标默认上限扩展到 **1,048,576 行 × 16,384 列（XFD）**。稀疏的远端坐标不会展开中间的空白矩形。保留的语义单元格预算仍为**每工作簿 10,000,000**。输入大小、解压后归档大小、单 Sheet XML 大小、完整输出/存储大小、分块大小/数量、运行时与预览预算相互独立。`EXCEL_MAX_SHEET_ROWS` 与 `EXCEL_MAX_COLUMNS` 的既有部署覆盖继续生效；若需要完整坐标，请在升级时复核它们。支持完整坐标不等于承诺处理一个填满的 1,048,576 × 16,384 工作表。

CSV 生成只在其独立导出预算内保留真实坐标空隙；稀疏角单元格不得强制产生无界空白输出。可选导出限制不会删除权威的完整结果。[历史验证记录](docs/excel-streaming-validation.md)中的早期 SQLite 数值基准是另一套基线。新的 gzip 分块实测在约 135 秒内保留一千万单元格，worker 峰值 RSS 约 56 MiB、压缩存储约 50 MiB；参见 [v1.6.0 验证条件](docs/file-result-storage-validation-1.6.0.md)。

### 选择标准 PDF 解析器

在 `.env` 中设置 `PDF_NORMAL_PROVIDER` 来选择文本型 PDF 的解析器：

- `pdf.normal.pdf-inspector`（默认）：文本、Markdown、版面与表格抽取。
- `pdf.normal.pymupdf`：原生文本加内嵌位图抽取，不做 OCR。

扫描/图片型 PDF 继续遵循 OCR 路由策略。PyMuPDF 会抽取每一张内嵌位图，不设图片数量或总大小上限。

## 前置条件

### 必需

- Python **3.12+**
- [uv](https://docs.astral.sh/uv/)
- Node.js **22+** 与 npm（用于 Web 工作台）

### 可选能力

| 工具 | 用途 |
| --- | --- |
| [LibreOffice](https://www.libreoffice.org/)（`soffice`） | 旧版 Office 转换与复杂 RTF 转换 |
| [FFmpeg](https://ffmpeg.org/) 与 `ffprobe` | 音频/视频预处理 |
| [PaddleOCR](https://www.paddleocr.ai/) 可选 Python 组 | 扫描/图片型 PDF 与独立图片的本地 OCR |

缺少这些可选工具时 ParseFlow 仍可启动，但相关转换或媒体流程会报告 Provider 不可用。

### 为扫描 PDF 与图片启用本地 OCR

安装一次可选本地 OCR 组（含本地 CPU 的 `paddlepaddle` 推理引擎、PaddleOCR 与 PDF 渲染支持）：

```bash
uv sync --group ocr
```

ParseFlow 首次处理扫描/图片型 PDF 或受支持的独立图片时，`pdf.ocr.paddle` 或 `image.ocr.paddle` Provider 会自动初始化 PaddleOCR，并把所需模型文件下载到 PaddleOCR 本地缓存（通常为 `~/.paddlex/official_models`）。后续请求复用该缓存，不需要云端 OCR 服务。

当前工作树还在 OCR 路由中启用了图片区域抽取，使用独立的 `LayoutDetection` 模块与 `PP-DocLayout-M`（模型文件约 22.6 MB；运行时内存更大）。它**不会**初始化完整的 PP-StructureV3 流水线：不加载公式、表格结构、图表转数据或图片描述模型。公式区域仍是普通 OCR 文本框；不承诺数学保真或 LaTeX。

`OCR_LAYOUT_IMAGES_ENABLED=true` 控制该阶段。`OCR_LAYOUT_THRESHOLD=0.5` 选择有效检测；**没有裁剪数量上限**。`OCR_LAYOUT_MAX_IMAGES` 不再使用，包括旧的环境覆盖。每个符合条件的非重复检测都会被裁剪；候选框与预测项数量都不会静默截断检测器输出。`OCR_LAYOUT_MAX_TOTAL_SIZE_MB=50` 仍是独立的字节预算：磁盘/写入/字节失败产生警告与 partial 状态，而不是伪造完整结果。任务预览/artifact 分页不会限制已保存的完整图片集合。检测到的插图/照片/图表区域成为图片文件，带页面像素框与来源 `layout_crop`。图表裁剪是图片，不是重建的图表数据。有图片裁剪但无文本的页面也能产生有效结果。检测失败会保留可用文本并给出警告/partial 状态；将开关设为 false 可恢复纯文本 OCR。首次使用可能把版面模型下载到 Paddle 缓存。原生 PyMuPDF 图片路径保持独立。

参见[范围与验证](docs/ocr-layout-images.md)。

当 `PDF_NORMAL_PROVIDER=pdf.normal.pymupdf` 时，所有内嵌位图都会被抽取，不设数量或字节大小上限。扫描 PDF 的 OCR 可调 `PDF_OCR_MAX_PAGES`、`PDF_OCR_RENDER_SCALE`、`PDF_OCR_MAX_PAGE_PIXELS`，独立图片 OCR 可调 `IMAGE_OCR_MAX_PIXELS`。设置 `PDF_OCR_ENABLED=false` 或 `IMAGE_OCR_ENABLED=false` 可关闭对应 OCR 路由。

## 快速开始

```bash
# 1. 创建 Python 环境并安装锁定依赖
uv sync

# 2. 配置可选的运行时设置
cp .env.example .env

# 3. 启动 FastAPI 后端
uv run uvicorn app.main:app --reload
```

另开一个终端：

```bash
cd web
npm ci
npm run dev
```

打开 Web 工作台 `http://127.0.0.1:5173`。Vite 将 `/api` 与 `/health` 代理到 `http://127.0.0.1:8000` 的后端。

常用的后端地址：

- API 文档：`http://127.0.0.1:8000/docs`
- 健康检查：`http://127.0.0.1:8000/health`
- 可用 Skill：`http://127.0.0.1:8000/api/v1/skills`

## Docker 部署

ParseFlow 使用单个生产镜像：把 FastAPI 后端与构建后的 React 工作台打包在一起。容器内由 Nginx 提供前端并代理 `/api` 到本地 FastAPI 进程。

```bash
# 可选：复制设置并在暴露到互联网前设置强 API Key。
cp .env.example .env
# 编辑 .env：设置 API_KEY=your-long-random-secret

# 构建并启动完整的本地解析栈。
docker compose up -d --build

# 校验两个服务并打开工作台。
docker compose ps
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/ready
curl http://127.0.0.1:8080/metrics

# 完整的 Linux 运行时策略与上传/任务冒烟（需要 Docker daemon）。
./scripts/docker-runtime-smoke.sh
```

打开 `http://127.0.0.1:8080`。用 `PARSEFLOW_PORT` 改变发布端口，例如 `PARSEFLOW_PORT=9000 docker compose up -d`。

生产 API 镜像包含 LibreOffice、FFmpeg/FFprobe、CPU PaddleOCR 与中文字体，因此支持 ParseFlow 宣称的格式。它持久化两个命名卷：

- `parseflow-data` —— 上传文件与生成的 artifact；
- `parseflow-models` —— 下载的 PaddleOCR 模型。首次 OCR 请求需要出站访问来下载模型；后续请求复用该卷。

常用操作：

```bash
# 跟踪 Web 与 API 的合并日志。
docker compose logs -f parseflow

# 停止容器但保留已上传文档与 OCR 模型。
docker compose down

# 停止并删除全部 ParseFlow 运行时数据（破坏性）。
docker compose down -v
```

> **部署说明：** Uvicorn 保持单 worker：持久任务队列与仓库都是单进程；服务现在对 `DATA_DIR` 加独占锁，并拒绝第二个使用同一数据目录的进程。Docker 默认等待 45 秒；请让 `TASK_SHUTDOWN_GRACE_SECONDS` 加 `TASK_SHUTDOWN_CANCEL_GRACE_SECONDS` 至少比 `PARSEFLOW_STOP_GRACE_PERIOD` 低 10 秒。Runner 取消是协作式的；卡住的 Runner 会让关闭失败，而数据目录锁会一直持有到进程退出。在把服务暴露到可信网络之外前设置 `API_KEY`。公开 REST 与远程 MCP 只接受上传文件或不透明 ID；它们从不接受服务器本地路径、输出目录或回调。

## 配置

把 `.env.example` 复制为 `.env` 并按需调整。重要设置包括：

- `DATA_DIR` —— 上传文件、持久任务 JSON 与任务 artifact 的根目录（默认 `./data`）
- `FILE_MAX_SIZE_MB` 与 `FILE_ALLOWED_SUFFIXES` —— 上传边界
- `API_KEY` —— 非空时 API 请求需要 `X-API-Key`
- `OFFICE_CONVERTER_COMMAND` —— LibreOffice 命令，默认 `soffice`
- `MEDIA_FFMPEG_COMMAND` / `MEDIA_FFPROBE_COMMAND` —— 媒体工具命令
- `TASK_MAX_CONCURRENT_EXECUTIONS`、`TASK_QUEUE_MAX_SIZE`、`TASK_SHUTDOWN_GRACE_SECONDS`、`TASK_SHUTDOWN_CANCEL_GRACE_SECONDS` —— 持久任务容量与关闭边界
- `PARSEFLOW_STOP_GRACE_PERIOD` —— Docker Compose 停止超时；至少比两个任务关闭宽限期之和多留 10 秒

健康检查、指标、备份/恢复、容量测试与发布验证见[运维手册](docs/operations.md)。

> **安全提示：** 不要提交 `.env`、上传文件或生成的 artifact。`data/` 下的运行时数据有意排除在 Git 之外。

## 使用 API

上传单个文件创建自动规划的解析任务：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/tasks/parse \
  -F 'file=@./report.xlsx' \
  -F 'goal=提取表格并输出 Markdown' \
  -F 'parse_mode=enhanced'
```

响应包含 `task_id`。查询任务以获得计划、步骤状态与最终结果：

```bash
curl http://127.0.0.1:8000/api/v1/tasks/{task_id}
```

在启用鉴权的部署中传入配置的密钥：

```bash
curl -H 'X-API-Key: your-key' http://127.0.0.1:8000/api/v1/skills
```

## 开发

运行后端测试：

```bash
uv run pytest
```

构建生产前端包：

```bash
cd web
npm ci
npm run build
```

生成的前端文件写入 `web/dist/`，不提交到仓库。

### MCP 服务

ParseFlow 支持两种 MCP 传输，使用相同工具与共享的解析运行时。

#### 本地 stdio MCP

启动本地 stdio 服务：

```bash
uv run parse-agent-mcp
```

本地客户端配置示例：

```json
{
  "mcpServers": {
    "parseflow": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/parseflow", "run", "parse-agent-mcp"]
    }
  }
}
```

#### Docker / 远程 Streamable HTTP MCP

Docker 服务可暴露带鉴权的 MCP 端点：

```text
http://your-host:8080/mcp/
```

在 Docker 的 `.env` 中配置强密钥并显式启用 MCP。`MCP_ALLOWED_HOSTS` 必须包含客户端使用的 Host 头；使用不带协议的公开主机名，只有当该 Host 头中出现端口时才写端口。

```env
API_KEY=replace-with-a-long-random-secret
MCP_HTTP_ENABLED=true
MCP_ALLOWED_HOSTS=parseflow.example.com
```

在默认端口做本地 Docker 测试时使用：

```env
API_KEY=replace-with-a-long-random-secret
MCP_HTTP_ENABLED=true
MCP_ALLOWED_HOSTS=localhost,127.0.0.1
```

修改设置后重启服务：

```bash
docker compose up -d
curl -H 'X-API-Key: replace-with-a-long-random-secret' http://127.0.0.1:8080/mcp/
```

用端点与请求头配置远程 MCP 客户端。具体配置字段因客户端而异，传输契约是：

```json
{
  "url": "http://127.0.0.1:8080/mcp/",
  "headers": {
    "X-API-Key": "replace-with-a-long-random-secret"
  }
}
```

该端点默认关闭，在 `MCP_HTTP_ENABLED=true` 之前返回 `404`。缺少 `API_KEY` 的 MCP 请求会被拒绝。远程工具从不接受服务器本地路径、输出目录或回调；它们只对已上传的 `file_id` 与 `task_id` 操作，底层与 REST 共用同一持久任务存储。

可用工具为 `list_skills`、`preview_parse_plan`、`submit_file_id`、`get_task`、`cancel_task` 与 `list_artifacts`。

## 版本路线图

当前路线图与详细发布方案维护在：

- [`docs/roadmap.md`](docs/roadmap.md) —— 当前基线、已确认的架构决策与发布顺序；
- [`docs/plan-1.6.1.md`](docs/plan-1.6.1.md) —— 渲染层解耦（已完成并发布）；
- [`docs/plan-1.7.0.md`](docs/plan-1.7.0.md) —— 富格式导出与可选解析引擎（方案）；
- [`docs/plan-1.6.0.md`](docs/plan-1.6.0.md) —— 统一文件结果与完整 Excel 坐标（已发布）；
- [`docs/versioning.md`](docs/versioning.md) —— 版本号判定规则；
- [`CHANGELOG.md`](CHANGELOG.md) —— 未发布变更与兼容性说明；
- [`docs/plan-1.0.0.md`](docs/plan-1.0.0.md) —— 稳定 API、MCP、数据与发布契约。

## 架构

- `app/main.py` —— FastAPI 应用与 REST 端点
- `app/agent/` —— 自动规划与任务执行
- `app/render/` —— 渲染层：表示生成与排版（不写磁盘）
- `app/skills/` —— Skill 抽象、Provider 与解析器
- `app/documents/` —— 归一化文档/结果模型
- `app/tasks/` —— 持久任务生命周期、恢复与 artifact 清单
- `web/` —— React/Vite ParseFlow 工作台
- `docs/` —— 架构与迭代方案

## v0.8.1 安全与部署

- 公开解析受上传大小限制、XML 预算、OOXML ZIP 目录检查、任务超时与存储空闲空间阈值保护。
- Docker 默认绑定回环地址，使用只读根文件系统、丢弃 Linux capabilities、限制 PID/CPU/内存、可写 `/data` 与可写 OCR 模型缓存。
- `/health` 报告进程存活；`/ready` 额外要求存储可写、空闲容量安全且存在活跃的持久任务 worker。
- 生产部署应在限制出站网络访问前预加载 OCR 模型。

## 当前限制

- 任务记录本地持久化，重启后恢复排队工作；进行中的工作被标记为 interrupted，需显式重新提交。
- 上传文件与 artifact 使用本地存储；不含对象存储与分布式队列。
- 音视频流程只做媒体预处理，尚不产出语音转文字结果。
- 删除终态任务会回收其 artifact 与上传文件，除非其他任务仍引用该文件。中断删除留下的孤儿目录由后台 janitor 在 `STORAGE_ORPHAN_MIN_AGE_SECONDS` 后回收。

## 许可证

[MIT](LICENSE)
