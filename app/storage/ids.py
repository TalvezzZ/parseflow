from __future__ import annotations

import re
import uuid


_PATTERNS = {
    "file": re.compile(r"^file_[0-9a-f]{32}$"),
    "task": re.compile(r"^task_[0-9a-f]{32}$"),
    "artifact": re.compile(r"^artifact_[0-9a-f]{32}$"),
}


def is_valid_id(kind: str, value: str) -> bool:
    pattern = _PATTERNS[kind]
    return bool(pattern.fullmatch(value))


def require_id(kind: str, value: str) -> str:
    if not is_valid_id(kind, value):
        raise ValueError(f"无效的 {kind}_id")
    return value


def new_id(kind: str) -> str:
    if kind not in _PATTERNS:
        raise ValueError(f"未知 ID 类型: {kind}")
    return f"{kind}_{uuid.uuid4().hex}"
