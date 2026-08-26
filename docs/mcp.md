# ParseFlow Remote MCP v1

Remote MCP is authenticated and uses opaque IDs only. It never accepts host paths, output directories, or callbacks.

Stable remote tools:

- `list_skills`
- `preview_parse_plan`
- `submit_file_id`
- `get_task`
- `cancel_task`
- `list_artifacts`

Tool names, required parameters, and their primary result shapes are stable for v1.x. `submit_file_id` returns an asynchronous task submission; clients poll `get_task` until a terminal status. Artifact download URLs are supplied by `list_artifacts` and remain controlled REST URLs.

Use `MCP_HTTP_ENABLED=true` and a non-empty `API_KEY` only on trusted deployments. The stdio profile uses the same opaque-ID contract.
