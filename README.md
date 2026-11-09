# ParseFlow

> A local-first intelligent document parsing workbench for turning files into structured, usable content.

ParseFlow accepts a single file, automatically selects an appropriate parsing workflow, and presents text, tables, Markdown, structured JSON, and generated artifacts in a web workbench. The backend is built with FastAPI and pluggable Skills; the frontend is a React/Vite application.

## Highlights

- **Automatic planning** — selects a registered Skill by file type; no model API key is required for the default rule-based workflow.
- **Selectable parsing tiers** — leave the default on **智能默认**, choose **标准解析** for native/fast extraction, or **增强解析** for OCR-first PDFs/images and Office-to-PDF layout parsing.
- **Multi-format parsing** — documents, Office files, structured text, spreadsheets, presentations, audio, and video.
- **Structured results** — normalized document model with blocks, tables, images, Markdown, plain text, provenance, warnings, and artifacts.
- **Safe spreadsheet handling** — XLSX/XLSM OOXML streams into a disk-backed SQLite store, with bounded previews, per-sheet row pagination, and killable parser/export workers.
- **Local upload boundaries** — controlled local storage, file-size and extension limits, optional `X-API-Key` authentication, and task metrics.
- **Developer integrations** — upload-only REST tasks, persistent polling, and opaque-ID MCP over local stdio or authenticated Streamable HTTP.

## Supported formats

| Category | Formats | Current behavior |
| --- | --- | --- |
| Documents | PDF, DOC, DOCX, RTF | Text PDFs use the configurable `pdf-inspector` or PyMuPDF provider; PyMuPDF extracts embedded raster images without OCR; scanned/image-based PDFs use optional local PaddleOCR; DOC/RTF may use LibreOffice conversion where required |
| Images | PNG, JPG, JPEG, WEBP, BMP, TIF, TIFF | Optional local PaddleOCR extracts text, Markdown, bounding boxes, and confidence from standalone images |
| Spreadsheets | XLS, XLSX, XLSM, CSV, TSV | Table and semantic cell extraction |
| Presentations | PPT, PPTX | Slide text, tables, and images |
| Text & structured data | TXT, MD, Markdown, HTML, HTM, XML, JSON, YAML, YML, INI, CFG, CONF, LOG, SQL, JS, TS, CSS | Specialized HTML/XML extraction where available; other allowlisted text formats preserve their source |
| Audio | MP3, WAV, M4A, AAC, FLAC, OGG | Metadata probing and audio normalization; no ASR yet |
| Video | MP4, MOV, MKV, AVI, WEBM | Metadata probing, audio extraction, and embedded-subtitle export when available; no ASR yet |

### Large Excel workbooks

Standard Excel extraction streams worksheet XML in workbook relationship order and writes retained cells to `workbook.sqlite`. It does not load a complete openpyxl workbook or keep a global cell-coordinate set. Shared strings and other lookup data are disk-backed; formulas retain expressions and available cached values. Hidden and very-hidden sheets are excluded. VBA is not executed.

Task JSON contains sheet summaries and bounded previews, not the full workbook. `tables[].cells` and `tables[].rows` are previews; consumers that previously assumed they contain every cell must use the row API or download the SQLite artifact. `quality.preview_truncated` is separate from actual source-data truncation (`quality.truncated` / task `partial`).

```http
GET /api/v1/tasks/{task_id}/sheets/{sheet_id}/rows?after_row=0&limit=100
```

Sheet IDs are zero-based, while row/column coordinates retain Excel's one-based numbering. The response contains complete nonempty rows and a `next_after_row` cursor (or `null`). Advance using the returned cursor; byte-limited pages may contain fewer rows than requested. Pages are capped at 200 rows / approximately 1 MB of cell payload. A single oversized row returns HTTP 413 with download guidance, without silently omitting cells.

Defaults: 10 million retained cells per workbook, 30 visible sheets, row numbers up to 100,000, column numbers up to 1,000, 100 MB input, 500 MB inflated archive, and 100 MB per worksheet XML. Full cells/images have a separate `EXCEL_MAX_STORAGE_MB=4096` disk budget. The preview is limited by `EXCEL_MAX_PREVIEW_CELLS=2000`, `EXCEL_MAX_PREVIEW_ROWS=100` (physical rows per sheet), and `EXCEL_MAX_PREVIEW_BYTES=1000000`; the byte reservation is conservative. XML records/text/tokens are additionally bounded at 4 MB and nesting at 128. Size and content budgets still apply independently: ten million arbitrary cells are not guaranteed to fit the default archive or disk limits.

The SQLite artifact is the complete retained data source. Optional CSV exports stream real row/column gaps and escape formula-like text for spreadsheet safety. Text and Markdown exports are explicitly previews. `TASK_MAX_RESULT_SIZE_MB` bounds the optional aggregate exports; exceeding it does not discard the SQLite artifact. Cancellation/timeout stops Excel parser/export subprocesses. Run `PYTHONPATH=. .venv/bin/python scripts/benchmark_excel.py --cells 1000000 10000000` for reproducible numeric-fixture RSS, time, and disk measurements; shared-string-heavy workbooks have different performance characteristics.

### Choose the standard PDF parser

Set `PDF_NORMAL_PROVIDER` in `.env` to select the parser for text-based PDFs:

- `pdf.normal.pdf-inspector` (default): text, Markdown, layout, and table extraction.
- `pdf.normal.pymupdf`: native text plus embedded raster-image extraction, without OCR.

Scanned/image-based PDFs continue to follow the OCR routing policy. PyMuPDF extracts every embedded raster image without an image-count or total-image-size limit.

## Prerequisites

### Required

- Python **3.12+**
- [uv](https://docs.astral.sh/uv/)
- Node.js **22+** and npm (for the web workbench)

### Optional capabilities

| Tool | Needed for |
| --- | --- |
| [LibreOffice](https://www.libreoffice.org/) (`soffice`) | Legacy Office conversion and complex RTF conversion |
| [FFmpeg](https://ffmpeg.org/) and `ffprobe` | Audio/video preparation |
| [PaddleOCR](https://www.paddleocr.ai/) optional Python group | Local OCR for scanned/image-based PDFs and standalone images |

ParseFlow starts without these optional tools, but the associated conversion or media workflows will report that the provider is unavailable.

### Enable local OCR for scanned PDFs and images

Install the optional local OCR group once (it includes the local CPU `paddlepaddle` inference engine, PaddleOCR, and PDF rendering support):

```bash
uv sync --group ocr
```

When ParseFlow first processes a scanned or image-based PDF, or a standalone supported image, the `pdf.ocr.paddle` or `image.ocr.paddle` provider automatically initializes PaddleOCR and downloads its required OCR model files into PaddleOCR's local cache (normally `~/.paddlex/official_models`). Later requests reuse that local cache and do not require a cloud OCR service.

When `PDF_NORMAL_PROVIDER=pdf.normal.pymupdf`, all embedded raster images are extracted without count or byte-size limits. Tune `PDF_OCR_MAX_PAGES`, `PDF_OCR_RENDER_SCALE`, and `PDF_OCR_MAX_PAGE_PIXELS` for scanned-PDF OCR, and `IMAGE_OCR_MAX_PIXELS` for standalone-image OCR. Set `PDF_OCR_ENABLED=false` or `IMAGE_OCR_ENABLED=false` to disable the corresponding OCR route.

## Quick start

```bash
# 1. Create the Python environment and install locked dependencies
uv sync

# 2. Configure optional runtime settings
cp .env.example .env

# 3. Start the FastAPI backend
uv run uvicorn app.main:app --reload
```

In another terminal:

```bash
cd web
npm ci
npm run dev
```

Open the web workbench at `http://127.0.0.1:5173`. Vite proxies `/api` and `/health` to the backend at `http://127.0.0.1:8000`.

Useful backend URLs:

- API documentation: `http://127.0.0.1:8000/docs`
- Health check: `http://127.0.0.1:8000/health`
- Available Skills: `http://127.0.0.1:8000/api/v1/skills`

## Docker deployment

ParseFlow uses one production Docker image: it packages the FastAPI backend and built React workbench together. Inside the container, Nginx serves the frontend and proxies `/api` to the local FastAPI process.

```bash
# Optional: copy settings and set a strong API key before Internet exposure.
cp .env.example .env
# Edit .env: set API_KEY=your-long-random-secret

# Build and start the full local parsing stack.
docker compose up -d --build

# Verify both services and open the workbench.
docker compose ps
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/ready
curl http://127.0.0.1:8080/metrics

# Full Linux runtime policy and upload/task smoke (requires Docker daemon).
./scripts/docker-runtime-smoke.sh
```

Open `http://127.0.0.1:8080`. Change the published port with `PARSEFLOW_PORT`, for example `PARSEFLOW_PORT=9000 docker compose up -d`.

The production API image includes LibreOffice, FFmpeg/FFprobe, CPU PaddleOCR, and Chinese fonts, so it supports the formats advertised by ParseFlow. It persists two named volumes:

- `parseflow-data` — uploaded files and generated artifacts;
- `parseflow-models` — downloaded PaddleOCR models. The first OCR request needs outbound access to download models; later requests reuse this volume.

Useful operations:

```bash
# Follow combined web and API logs.
docker compose logs -f parseflow

# Stop containers without deleting uploaded documents or OCR models.
docker compose down

# Stop and remove all ParseFlow runtime data (destructive).
docker compose down -v
```

> **Deployment notes:** Keep Uvicorn at one worker: the persistent task queue and repository are single-process; the service now acquires an exclusive lock on `DATA_DIR` and refuses a second process using the same data directory. Docker waits 45 seconds by default; keep `TASK_SHUTDOWN_GRACE_SECONDS` plus `TASK_SHUTDOWN_CANCEL_GRACE_SECONDS` at least 10 seconds below `PARSEFLOW_STOP_GRACE_PERIOD`. Runner cancellation is cooperative; a stuck Runner causes shutdown to fail while the data-directory lock remains held until process exit. Set `API_KEY` before exposing the service outside a trusted network. Public REST and remote MCP accept uploaded files or opaque IDs only; they never accept server-local paths, output directories, or callbacks.

## Configuration

Copy `.env.example` to `.env` and adjust values as needed. Important settings include:

- `DATA_DIR` — root directory for uploaded files, persistent task JSON, and task artifacts (defaults to `./data`)
- `FILE_MAX_SIZE_MB` and `FILE_ALLOWED_SUFFIXES` — upload boundaries
- `API_KEY` — when non-empty, API requests require `X-API-Key`
- `OFFICE_CONVERTER_COMMAND` — LibreOffice command, default `soffice`
- `MEDIA_FFMPEG_COMMAND` / `MEDIA_FFPROBE_COMMAND` — media tooling commands
- `TASK_MAX_CONCURRENT_EXECUTIONS`, `TASK_QUEUE_MAX_SIZE`, `TASK_SHUTDOWN_GRACE_SECONDS`, `TASK_SHUTDOWN_CANCEL_GRACE_SECONDS` — persistent task capacity and shutdown bounds
- `PARSEFLOW_STOP_GRACE_PERIOD` — Docker Compose stop timeout; leave at least 10 seconds beyond both task shutdown grace periods

For health checks, metrics, backup/restore, capacity tests, and release verification, see the [operations runbook](docs/operations.md).

> **Security note:** Do not commit `.env`, uploaded files, or generated artifacts. Runtime data under `data/` is intentionally excluded from Git.

## Use the API

Create an automatically planned parsing task by uploading a single file:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/tasks/parse \
  -F 'file=@./report.xlsx' \
  -F 'goal=提取表格并输出 Markdown' \
  -F 'parse_mode=enhanced'
```

The response contains a `task_id`. Query the task for the plan, step state, and final result:

```bash
curl http://127.0.0.1:8000/api/v1/tasks/{task_id}
```

For an authenticated deployment, pass the configured key:

```bash
curl -H 'X-API-Key: your-key' http://127.0.0.1:8000/api/v1/skills
```

## Development

Run backend tests:

```bash
uv run pytest
```

Build the production frontend bundle:

```bash
cd web
npm ci
npm run build
```

The generated frontend files are written to `web/dist/` and are not committed.

### MCP server

ParseFlow supports two MCP transports using the same tools and shared parsing runtime.

#### Local stdio MCP

Start the local stdio server with:

```bash
uv run parse-agent-mcp
```

A local client configuration is:

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

#### Docker / remote Streamable HTTP MCP

The Docker service can expose an authenticated MCP endpoint at:

```text
http://your-host:8080/mcp/
```

In the Docker `.env`, configure a strong key and explicitly enable MCP. `MCP_ALLOWED_HOSTS` must include the Host header used by clients; use the public hostname without a scheme, and include a port only when it is present in that Host header.

```env
API_KEY=replace-with-a-long-random-secret
MCP_HTTP_ENABLED=true
MCP_ALLOWED_HOSTS=parseflow.example.com
```

For local Docker testing on the default port, use:

```env
API_KEY=replace-with-a-long-random-secret
MCP_HTTP_ENABLED=true
MCP_ALLOWED_HOSTS=localhost,127.0.0.1
```

Restart the service after changing the settings:

```bash
docker compose up -d
curl -H 'X-API-Key: replace-with-a-long-random-secret' http://127.0.0.1:8080/mcp/
```

Configure a remote MCP client with the endpoint and request header. The exact configuration field varies by client; the transport contract is:

```json
{
  "url": "http://127.0.0.1:8080/mcp/",
  "headers": {
    "X-API-Key": "replace-with-a-long-random-secret"
  }
}
```

The endpoint is disabled by default and returns `404` until `MCP_HTTP_ENABLED=true`. It refuses MCP requests without `API_KEY`. Remote tools never accept server-local paths, output directories, or callbacks; they operate only on already-uploaded `file_id` and `task_id` values, backed by the same persistent task store as REST.

Available tools are `list_skills`, `preview_parse_plan`, `submit_file_id`, `get_task`, `cancel_task`, and `list_artifacts`.

## Version roadmap

The active roadmap and detailed release plans are maintained in:

- [`docs/roadmap.md`](docs/roadmap.md) — current baseline, confirmed architecture decisions, and release sequence;
- [`docs/plan-0.7.1.md`](docs/plan-0.7.1.md) — current capability cleanup;
- [`docs/plan-0.8.0.md`](docs/plan-0.8.0.md) — upload-only asynchronous API and local file task persistence;
- [`docs/plan-0.8.1.md`](docs/plan-0.8.1.md) — security and deployment hardening;
- [`docs/plan-0.9.0.md`](docs/plan-0.9.0.md) — task center, frontend tests, accessibility, and observability;
- [`docs/plan-1.0.0.md`](docs/plan-1.0.0.md) — stable API, MCP, data, and release contracts.

## Architecture

- `app/main.py` — FastAPI application and REST endpoints
- `app/agent/` — automatic planning and task execution
- `app/skills/` — Skill abstraction, providers, and parsers
- `app/documents/` — normalized document/result models
- `app/tasks/` — persistent task lifecycle, recovery, and artifact manifests
- `web/` — React/Vite ParseFlow workbench
- `docs/` — architecture and iteration plans

## v0.8.1 security and deployment

- Public parsing is protected by upload size limits, XML budgets, OOXML ZIP directory checks, task timeouts, and a storage free-space threshold.
- Docker binds to loopback by default and uses a read-only root filesystem, dropped Linux capabilities, PID/CPU/memory limits, writable `/data`, and a writable OCR model cache.
- `/health` reports process liveness; `/ready` additionally requires writable storage, safe free capacity, and an active persistent task worker.
- Production deployments should preload OCR models before restricting outbound network access.

## Current limitations

- Task records are persisted locally and recover queued work after restart; active work is marked interrupted and must be explicitly resubmitted.
- Uploaded files and artifacts use local storage; object storage and distributed queues are not included.
- Audio/video workflows prepare media only; they do not yet produce speech-to-text output.
- Deleting a terminal task reclaims its artifacts and its upload unless another task still references that file. Orphaned directories left by an interrupted delete are reclaimed by the background janitor after `STORAGE_ORPHAN_MIN_AGE_SECONDS`.

## License

[MIT](LICENSE)
