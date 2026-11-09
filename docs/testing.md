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
- PDF provider selection via `PDF_NORMAL_PROVIDER`, text parsing, PyMuPDF no-OCR embedded-image extraction, and scanned-PDF PaddleOCR routing;
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

## Browser E2E gate

Playwright drives a real browser against a real backend and a real Vite frontend. `web/playwright.config.ts` starts both services itself with an isolated `DATA_DIR=/tmp/parseflow-e2e`, so no manual setup is required.

```bash
cd web
npx playwright install --with-deps chromium   # first run only
npm run test:e2e
```

The suite covers:

- upload, background polling, deep-link refresh, artifact download, retry, and delete;
- an axe scan asserting no `critical` or `serious` accessibility violations;
- a real enhanced-mode OCR parse verifying coordinate overlays and page previews.

This layer catches what the unit tests cannot: real layout, computed colors, and rendering. Both color-contrast defects fixed in v1.4.0 were found by the axe scan, not by jsdom.

### Installing Chromium behind a proxy

Playwright downloads Chrome for Testing from `cdn.playwright.dev`. On a machine whose system proxy is not inherited by the downloader, `npx playwright install chromium` can stall indefinitely without printing an error. Pass the proxy explicitly:

```bash
HTTPS_PROXY=http://127.0.0.1:7897 HTTP_PROXY=http://127.0.0.1:7897 npx playwright install chromium
```

To confirm connectivity before a long install, request the exact artifact URL from `web/node_modules/playwright-core/browsers.json` (`browserVersion`) rather than guessing the path — macOS arm64 uses Chrome-for-Testing under `builds/cft/<version>/mac-arm64/`, not `builds/chromium/<revision>/`:

```bash
curl -sSL -o /dev/null -w '%{http_code}\n' -r 0-2000 \
  --proxy http://127.0.0.1:7897 \
  "https://cdn.playwright.dev/builds/cft/151.0.7922.34/mac-arm64/chrome-mac-arm64.zip"
```

A `206` means the download will work. If the proxy is unavailable, `PARSEFLOW_USE_SYSTEM_CHROME=1 npm run test:e2e` runs against an installed Google Chrome instead; CI uses the bundled browser, so prefer the proxy path for local parity.

## Environment-dependent tests

Some integration tests detect tools and skip their tool-specific assertions when unavailable:

- LibreOffice (`soffice`) for legacy Office and complex RTF conversion;
- FFmpeg/FFprobe for media preparation;
- optional PaddleOCR runtime for live OCR smoke testing.

For a release candidate, run the default gate on a workstation or CI image containing LibreOffice and FFmpeg/FFprobe. The current macOS release validation environment provides these tools.

## Manual UI smoke checks

The Vite build validates TypeScript and production bundling. Before tagging a stable release, also manually check the running workbench:

1. Upload a CSV/XLSX and open **表格**; string and 2D table rows must render without a white screen.
2. Set `PDF_NORMAL_PROVIDER=pdf.normal.pymupdf` and open a text PDF with an embedded raster image; **概览** must count the image, and **产物** must provide its preview/download without OCR.
3. Open a scanned PDF after installing the OCR group; its task must finish through `pdf.ocr.paddle`, and **OCR 校对** must show the page image with coordinate overlays.
4. Open an existing task and use **返回任务列表**.
5. Confirm supported-format guidance and upload keyboard activation using Enter/Space.
6. Delete a terminal task and confirm its artifact and upload directories are gone from `DATA_DIR`.

## Known scope limits

- OCR bounding-box overlays render in the **OCR 校对** tab for images and scanned PDFs. They are not drawn onto the original PDF file itself.
- The E2E suite requires a browser download on first run; see the proxy note above.
- Direct server-path parsing endpoints are intended for trusted deployments. Public deployments should enforce `API_KEY` and restrict network exposure.
