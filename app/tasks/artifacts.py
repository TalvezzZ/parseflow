from __future__ import annotations

import mimetypes
from pathlib import Path

from app.storage.atomic import atomic_write_json
from app.storage.ids import new_id, require_id
from app.tasks.models import ArtifactRecord


class ArtifactRepository:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def workspace(self, task_id: str) -> Path:
        require_id("task", task_id)
        path = self.root / task_id / "files"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _manifest_path(self, task_id: str) -> Path:
        require_id("task", task_id)
        return self.root / task_id / "manifest.json"

    def collect(self, task_id: str) -> list[ArtifactRecord]:
        workspace = self.workspace(task_id)
        artifacts: list[ArtifactRecord] = []
        for path in sorted(workspace.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            artifacts.append(ArtifactRecord(
                artifact_id=new_id("artifact"), filename=path.name,
                content_type=mimetypes.guess_type(path.name)[0], size_bytes=path.stat().st_size,
                kind="generated_file",
            ))
        atomic_write_json(self._manifest_path(task_id), {"task_id": task_id, "artifacts": [item.model_dump(mode="json") for item in artifacts]})
        return artifacts

    def list(self, task_id: str) -> list[ArtifactRecord]:
        path = self._manifest_path(task_id)
        if not path.is_file():
            return []
        try:
            import json
            return [ArtifactRecord.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8")).get("artifacts", [])]
        except Exception:
            return []

    def download_path(self, task_id: str, artifact_id: str) -> Path | None:
        require_id("artifact", artifact_id)
        artifacts = self.list(task_id)
        item = next((item for item in artifacts if item.artifact_id == artifact_id), None)
        if item is None:
            return None
        candidate = (self.workspace(task_id) / item.filename).resolve()
        try:
            candidate.relative_to(self.workspace(task_id).resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() and not candidate.is_symlink() else None
