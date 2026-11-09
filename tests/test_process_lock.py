from __future__ import annotations

import pytest

from app.storage.process_lock import DataDirectoryLock, ProcessLockError


def test_data_directory_lock_rejects_a_second_owner_and_releases(tmp_path) -> None:
    first = DataDirectoryLock(tmp_path)
    second = DataDirectoryLock(tmp_path)

    first.acquire()
    try:
        with pytest.raises(ProcessLockError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()
