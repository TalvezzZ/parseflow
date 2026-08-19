# ParseFlow

> A local-first intelligent document parsing workbench for turning files into structured, usable content.

ParseFlow accepts a single file, automatically selects an appropriate parsing workflow, and presents text, tables, Markdown, structured JSON, and generated artifacts in a web workbench. The backend is built with FastAPI and pluggable Skills; the frontend is a React/Vite application.

## Highlights

- **Automatic planning** — selects a registered Skill by file type; no model API key is required for the default rule-based workflow.
- **Multi-format parsing** — documents, Office files, structured text, spreadsheets, presentations, audio, and video.
- **Structured results** — normalized document model with blocks, tables, images, Markdown, plain text, provenance, warnings, and artifacts.
- **Safe spreadsheet handling** — XLSX/XLSM semantic-cell scanning avoids traversing oversized, style-polluted used ranges.
- **Local upload boundaries** — controlled local storage, file-size and extension limits, optional `X-API-Key` authentication, and task metrics.
- **Developer integrations** — REST API, asynchronous task status, callbacks, and an MCP stdio server.

## Supported formats

| Category | Formats | Current behavior |
| --- | --- | --- |
| Documents | PDF, DOC, DOCX, RTF | Text PDF parsing; scanned/image-based PDFs use optional local PaddleOCR; DOC/RTF may use LibreOffice conversion where required |
| Images | PNG, JPG, JPEG, WEBP, BMP, TIF, TIFF | Optional local PaddleOCR extracts text, Markdown, bounding boxes, and confidence from standalone images |
| Spreadsheets | XLS, XLSX, XLSM, CSV, TSV | Table and semantic cell extraction |
| Presentations | PPT, PPTX | Slide text, tables, and images |
| Text & structured data | TXT, MD, Markdown, HTML, HTM, XML | Text extraction; XML uses safe structured extraction |
| Audio | MP3, WAV, M4A, AAC, FLAC, OGG | Metadata probing and audio normalization; no ASR yet |
| Video | MP4, MOV, MKV, AVI, WEBM | Metadata probing, audio extraction, and embedded-subtitle export when available; no ASR yet |

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

The default CPU configuration is intended for local deployment. Tune `PDF_OCR_MAX_PAGES`, `PDF_OCR_RENDER_SCALE`, and `PDF_OCR_MAX_PAGE_PIXELS` for PDF OCR, and `IMAGE_OCR_MAX_PIXELS` for image OCR, in `.env` to control runtime and memory usage. Set `PDF_OCR_ENABLED=false` or `IMAGE_OCR_ENABLED=false` to disable the corresponding route.

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

## Configuration

Copy `.env.example` to `.env` and adjust values as needed. Important settings include:

- `FILE_STORAGE_DIR` — local directory for uploaded files and generated artifacts (defaults to `./data/files`)
- `FILE_MAX_SIZE_MB` and `FILE_ALLOWED_SUFFIXES` — upload boundaries
- `API_KEY` — when non-empty, API requests require `X-API-Key`
- `OFFICE_CONVERTER_COMMAND` — LibreOffice command, default `soffice`
- `MEDIA_FFMPEG_COMMAND` / `MEDIA_FFPROBE_COMMAND` — media tooling commands
- `TASK_MAX_CONCURRENT_EXECUTIONS`, `TASK_QUEUE_MAX_SIZE` — in-memory task queue limits

> **Security note:** Do not commit `.env`, uploaded files, or generated artifacts. Runtime data under `data/` is intentionally excluded from Git.

## Use the API

Create an automatically planned parsing task by uploading a single file:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/parse \
  -F 'file=@./report.xlsx' \
  -F 'goal=提取表格并输出 Markdown'
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

Start the MCP stdio server with:

```bash
uv run parse-agent-mcp
```

It exposes `list_skills`, `execute_skill`, and `parse_office_pipeline`.

## Architecture

- `app/main.py` — FastAPI application and REST endpoints
- `app/agent/` — automatic planning and task execution
- `app/skills/` — Skill abstraction, providers, and parsers
- `app/documents/` — normalized document/result models
- `app/tasks/` — in-memory task lifecycle and callbacks
- `web/` — React/Vite ParseFlow workbench
- `docs/` — architecture and iteration plans

## Current limitations

- The task queue is in-process memory; task state does not survive a service restart.
- Uploaded files and artifacts use local storage; object storage and distributed queues are not included.
- Audio/video workflows prepare media only; they do not yet produce speech-to-text output.

## License

[MIT](LICENSE)
