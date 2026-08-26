# Migration: ParseFlow 0.x to 1.0

## Public API

Path-based parsing and callback APIs were removed in 0.8.0. Use this workflow instead:

```text
upload file -> POST /api/v1/tasks/parse -> poll task_id -> artifact_id download
```

Do not send `path`, `output_dir`, or `callback`. Keep only opaque `file_id`, `task_id`, and `artifact_id` values in integrations.

## Tasks

Task records are file-backed. Queued tasks recover after restart; work active during restart becomes `interrupted` and requires an explicit retry. Retry creates a distinct task with `retry_of`.

## Compatibility

1.0 freezes the current `/api/v1` endpoint set and remote MCP tool list. Future v1.x versions may add optional fields but will not remove or rename stable endpoints, tools, status values, error codes, or ID meanings.
