from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from spark_keeper.paths import AppPaths
from spark_keeper.scheduler import TaskScheduler, current_user_sid

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows-only integration")


def test_current_user_sid_is_valid() -> None:
    assert current_user_sid().startswith("S-")


def test_task_scheduler_create_query_delete_cycle() -> None:
    paths = AppPaths.discover()
    scheduler = TaskScheduler(paths)
    send_time = (datetime.now().astimezone() + timedelta(minutes=30)).strftime("%H:%M")
    try:
        scheduler.install(send_time)
        assert scheduler.exists()
    finally:
        scheduler.remove()
    assert not scheduler.exists()
