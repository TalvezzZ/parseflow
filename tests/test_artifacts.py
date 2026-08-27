from __future__ import annotations

import json
from pathlib import Path

from app.tasks.artifacts import ArtifactRepository


TASK_ID = "task_" + "1" * 32


def test_nested_artifact_download_uses_internal_storage_key(tmp_path: Path) -> None:
    repository = ArtifactRepository(tmp_path)
    nested = repository.workspace(TASK_ID) / "exports" / "document.json"
    nested.parent.mkdir(parents=True)
    nested.write_text("{}")
    artifact = repository.collect(TASK_ID)[0]
    assert artifact.filename == "document.json"
    assert repository.download_path(TASK_ID, artifact.artifact_id) == nested.resolve()
    assert "storage_key" not in artifact.model_dump()


def test_artifact_storage_key_cannot_escape_workspace(tmp_path: Path) -> None:
    repository = ArtifactRepository(tmp_path)
    artifact_id = "artifact_" + "2" * 32
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    repository.workspace(TASK_ID)
    repository._manifest_path(TASK_ID).write_text(json.dumps({"artifacts": [{"artifact_id": artifact_id, "filename": "secret.txt", "storage_key": "../../../secret.txt"}]}))
    assert repository.download_path(TASK_ID, artifact_id) is None
