from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_max_concurrent_executions", 0),
        ("task_queue_max_size", 0),
        ("task_default_timeout_seconds", 0),
        ("task_cleanup_interval_seconds", 0),
        ("task_shutdown_grace_seconds", -1),
        ("task_shutdown_cancel_grace_seconds", -1),
    ],
)
def test_task_runtime_settings_reject_invalid_capacity_and_grace(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_task_runtime_settings_have_operable_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.task_max_concurrent_executions == 2
    assert settings.task_queue_max_size == 100
    assert settings.task_shutdown_grace_seconds == 30
    assert settings.task_shutdown_cancel_grace_seconds == 2
