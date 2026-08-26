# Release Testing Guide

This guide defines the automated regression gate for a ParseFlow stable release.

## Default release gate

Run the backend regression suite and production web build:

```bash
uv run pytest
cd web && npm run build
```

The backend suite covers:

- automatic planning for every publicly allowed upload extension;
- PDF text parsing and scanned-PDF PaddleOCR routing;
- OCR provider normalization, input limits, empty output, and unavailable dependencies;
- PDF, Word, Excel, PowerPoint, RTF, CSV/TSV, HTML, XML, Office conversion, and media preparation paths;
- upload/download lifecycle, extension rejection, artifact traversal protection, request IDs, API-key protection, and CORS preflight;
- persistent automatic task planning/execution, queue capacity, cancellation, restart recovery, expiry cleanup, metrics, and normalized task result contracts;
- Excel sparse-range hardening and legacy Office conversion where local LibreOffice is available.

## Optional local OCR smoke test

The mock-based OCR tests run in the default suite. To validate real local OCR and first-use model download, install the optional dependencies:

```bash
uv sync --group ocr
```

Then submit a non-sensitive scanned PDF through the UI or API. Verify that the task selects `pdf.ocr.paddle`, produces pages, blocks, `plain_text`, and Markdown. PaddleOCR downloads models on first use and reuses its local cache afterward.

## Environment-dependent tests

Some integration tests detect tools and skip their tool-specific assertions when unavailable:

- LibreOffice (`soffice`) for legacy Office and complex RTF conversion;
- FFmpeg/FFprobe for media preparation;
- optional PaddleOCR runtime for live OCR smoke testing.

For a release candidate, run the default gate on a workstation or CI image containing LibreOffice and FFmpeg/FFprobe. The current macOS release validation environment provides these tools.

## Manual UI smoke checks

The Vite build validates TypeScript and production bundling. Before tagging a stable release, also manually check the running workbench:

1. Upload a CSV/XLSX and open **表格**; string and 2D table rows must render without a white screen.
2. Open a text PDF; **概览**, **正文**, and **Markdown** must show extracted content.
3. Open a scanned PDF after installing the OCR group; its task must finish through `pdf.ocr.paddle`.
4. Open an existing task and use **返回任务列表**.
5. Confirm supported-format guidance and upload keyboard activation using Enter/Space.

## Known scope limits

- OCR visual bounding-box overlays are preserved in backend JSON blocks but are not yet rendered as a PDF overlay in the web UI.
- No browser-component test runner is installed yet; the manual UI checks above remain a release requirement.
- Direct server-path parsing endpoints are intended for trusted deployments. Public deployments should enforce `API_KEY` and restrict network exposure.
