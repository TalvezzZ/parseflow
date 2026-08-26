from __future__ import annotations

import json
from pathlib import Path

from app.main import app


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {"path", "output_dir", "callback", "source_path", "target_path", "artifact_path"}


def test_openapi_v1_snapshot_is_current_and_has_no_private_contract_fields() -> None:
    snapshot = json.loads((ROOT / "openapi/openapi-v1.json").read_text(encoding="utf-8"))
    assert snapshot == app.openapi()
    # Route parameter metadata legitimately contains `"in": "path"`; only payload fields are prohibited.
    serialized = json.dumps(snapshot).replace('"in": "path"', '"in": "route_parameter"')
    for field in FORBIDDEN:
        assert f'"{field}"' not in serialized
    assert all(path.startswith("/api/v1/") or path in {"/health", "/ready", "/metrics"} for path in snapshot["paths"])


def test_stable_contract_snapshots_declare_opaque_ids_and_remote_tools() -> None:
    task = json.loads((ROOT / "schemas/task-record-v1.json").read_text())
    mcp = json.loads((ROOT / "schemas/mcp-remote-v1.json").read_text())
    assert task["properties"]["task_id"]["pattern"] == "^task_[0-9a-f]{32}$"
    assert mcp["contract_version"] == "1.0"
    assert mcp["tools"] == ["list_skills", "preview_parse_plan", "submit_file_id", "get_task", "cancel_task", "list_artifacts"]
