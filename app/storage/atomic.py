from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, payload: Any) -> None:
    """Atomically replace a UTF-8 JSON file and fsync the written data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some filesystems do not support directory fsync; file fsync remains valid.
            pass
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
