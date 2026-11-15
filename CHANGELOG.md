# Changelog

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
