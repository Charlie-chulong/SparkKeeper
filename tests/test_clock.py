from __future__ import annotations

from datetime import datetime, timedelta, timezone

from spark_keeper.clock import BEIJING_TZ, display_beijing_time


def test_display_crosses_local_date_without_changing_the_instant() -> None:
    pacific = datetime(2026, 10, 17, 21, tzinfo=timezone(-timedelta(hours=7)))
    assert pacific.astimezone(BEIJING_TZ).isoformat() == "2026-10-18T12:00:00+08:00"
    assert display_beijing_time(pacific.isoformat()) == "2026-10-18 12:00:00"


def test_legacy_naive_timestamp_is_not_misrepresented_as_beijing_time() -> None:
    assert display_beijing_time("2026-09-01T09:00:00") == (
        "2026-09-01T09:00:00（原记录未标注时区）"
    )
