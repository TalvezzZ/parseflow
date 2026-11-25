# Changelog

## 1.6.1 — 2026-11-25

PATCH release. Internal restructuring only: no new output format, endpoint, configuration
default or result field semantics. Markdown and plain-text output is byte-identical to
1.6.0 for every migrated provider, verified by regression tests.

### Render layer

- Result representation generation moved out of providers into `app/render/`: contracts,
  a per-kind renderer registry, and three built-in renderers (markdown, plain_text, json).
- Renderers only read the document mapping; they hold no Skill, Provider or file handle
  and perform no filesystem writes.
- `generate_document_exports` now delegates layout to the render layer and keeps its byte
  budget, staging directory, atomic publication and killable worker path unchanged.
- Providers migrated with byte-identical output: PyMuPDF, PaddleOCR, PPT (python-pptx),
  Excel (openpyxl). Providers whose representation is the parse result itself
  (pdf-inspector, Mammoth, text/raw, single-image OCR, FFmpeg media) intentionally keep
  producing it inline.

### Additions

- Page-level asset links: `RenderOptions.page_asset_links` groups images by page and
  appends references at page end, reproducing the existing PaddleOCR layout. Images
  without a relative path are skipped with a warning instead of leaking absolute paths.
- Workbook sheet grouping: `body_source="sheet"` renders `## {sheet}` plus `- {row}`
  lines, skips empty sheets, and derives grouping from `blocks[].metadata.sheet`.
- `result/manifest.json` now declares `schema` (`parseflow.result`) and `schema_version`.
  `result_ref` declares the same protocol. Manifests without `schema` remain readable.
- `schemas/result-manifest-v1.json` snapshot plus a consistency test.

### Rejected

- CJK generated-space removal was implemented, measured on 144k characters of real
  Chinese PDFs (96.2% false-deletion rate: table column boundaries and cross-line
  character streams were indistinguishable from generated spaces), and then removed
  entirely, including its option, config entry and helper module.

### Validation

Measured on the release commit, not on a working tree with unrelated in-progress work.

- Backend: 357 passed / 1 skipped.
- Frontend: 39 passed, production build passed.
- Image publication and production deployment were not performed.

## Unreleased — image-region extraction

- Add optional lightweight PP-DocLayout-M detection to PDF/image OCR, saving bounded region crops with page-pixel coordinates and file references. No full PP-StructureV3 or formula/vision model is loaded.
- Crop all qualifying nonduplicate image detections without a crop-count, candidate-box-count or prediction-item-count cap. Artifact pagination/preview limits do not truncate saved images; byte/disk errors remain explicit.
- Preserve formula content as ordinary OCR text; accept image-only results when crops are present. Existing native PDF image extraction remains separate.
- See `docs/ocr-layout-images.md` for scope, budgets and validation.

## Unreleased — video parsing phase 1

- Independently probe video metadata, extract structured text subtitles and optionally normalize audio; video auto no longer creates a WAV or fails solely because it has no audio track. Standalone audio auto keeps normalization.
- Add validated/persisted multipart media modes, subtitle language and stream indexes, retained on retry.
- Replace blocking-thread FFmpeg calls with cancellable subprocesses, shared deadline, bounded logs, runtime disk/output budgets and atomic media output publication.
- Add playback-relative subtitle timeline JSONL and composite-cursor paging, stage/content-scope overview, media upload controls and lazy timeline UI. No ASR, visual OCR, VTT or frame-analysis claim.
- Existing version/tag stays unchanged until an explicit release request. See `docs/video-parsing-phase1.md` for validation and scope.

## 1.6.0 — 2026-11-15

Implemented and validated locally on 2026-11-15; version metadata is aligned to 1.6.0. This version is prepared for the `v1.6.0` Git tag. GitHub Release creation, image publication and production deployment are separate operations. The reserved 1.5.0 reliability/supply-chain plan remains documented separately.

### Unified file-backed results

- Complete results for supported document, spreadsheet, image, text and existing media-preprocessing workflows are published as task-owned files. Task metadata remains local JSON; no SQL task repository is introduced.
- `result/manifest.json` describes units and chunks, while `result/document.json` retains the complete structured document. Task responses contain bounded document previews and a stable `result_ref` instead of the full result.
- Result discovery and bounded unit-record reads use `/api/v1/tasks/{id}/result` and `/api/v1/tasks/{id}/result/units/{unit_id}/records?offset=0&limit=100`. Complete structured document downloads use `/result/document`; manifest-listed resources use `/result/files/{storage_key}`. Excel complete cells are in the referenced workbook chunks. Task artifact previews are capped at 100, with a paginated artifact directory.
- Storage uses relative keys under the task workspace, with publication before task-reference updates. Deletion, retention, backup and orphan cleanup must include all referenced files.

### Excel

- New complete cell results use `workbook/manifest.json` schema 2 with separate Sheet directories and gzip JSONL chunks. Shared-string/style/formula SQLite indexes are private temporary helpers, not published cell stores.
- XLSX/XLSM coordinate defaults extend to 1,048,576 rows and 16,384 columns (XFD). Sparse far coordinates do not imply allocating or exporting the intervening blank rectangle.
- The default retained effective-cell budget remains 10,000,000. Input, decompressed archive, XML, complete output, chunk/file count, preview, export and runtime budgets are independent.

### Compatibility and upgrade

- Existing embedded task documents and legacy Excel SQLite results remain readable. No automatic destructive migration is required.
- New clients must treat task `document`, table cells and rendered text as previews, follow `result_ref` for complete data, and distinguish preview truncation from actual parsing truncation.
- Existing row/column environment overrides remain effective; review them if full Excel coordinates are desired. Do not remove old SQLite artifacts during upgrade.
- File-backed publication reduces persisted-record and response pressure; it does not make all third-party parsers internally streaming or guarantee throughput gains.

### Validation

Backend regression: 305 passed, 1 skipped. Frontend: 39 tests passed and production build passed. Version/OpenAPI checks passed. Built-asset mock-API browser smoke covered desktop/mobile and lazy unit pagination. New-format numeric benchmark retained 10 million cells in about 135 seconds with ~56 MiB parser-worker peak RSS and ~50 MiB compressed files. See `docs/file-result-storage-validation-1.6.0.md` for conditions and exclusions; no Docker or production-deployment verification is claimed.
