from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime

from .automation.service import BatchService
from .database import Database
from .dpapi import DpapiJsonStore
from .logging_safe import format_error
from .maintenance import (
    MaintenanceBusy,
    MaintenanceLease,
    MaintenanceRequired,
    assert_maintenance_clear,
)
from .models import BatchMode, BatchStatus, ErrorCode
from .notifications import notify_batch
from .paths import AppPaths
from .scheduler import scheduled_datetime

_MAINTENANCE_WAIT_SECONDS = 5.0


async def _acquire_maintenance_lease(lease: MaintenanceLease) -> None:
    # Restoring Enabled may trigger this worker just before the installer releases
    # its gate. Wait briefly for that handoff, never retry the batch itself.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _MAINTENANCE_WAIT_SECONDS
    while True:
        try:
            lease.acquire()
            return
        except MaintenanceBusy:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise
        await asyncio.sleep(min(0.1, remaining))


async def run_scheduled() -> int:
    try:
        lease = MaintenanceLease()
        await _acquire_maintenance_lease(lease)
    except Exception as exc:  # noqa: BLE001
        print(f"定时任务启动失败，未执行发送：{format_error(exc)}", file=sys.stderr)
        return 2
    try:
        try:
            assert_maintenance_clear()
        except MaintenanceRequired as exc:
            print(f"定时任务启动失败，未执行发送：{format_error(exc)}", file=sys.stderr)
            return 2
        return await _run_scheduled_under_maintenance()
    finally:
        # Keep the gate through recovery, batch finalization, notification and error logging.
        lease.release()


async def _run_scheduled_under_maintenance() -> int:
    try:
        paths = AppPaths.discover()
        paths.ensure_runtime_dirs()
        database = Database(paths.database)
        recovered = database.recover_inflight_if_idle()
        if recovered:
            database.record_event(
                "WARNING",
                ErrorCode.SEND_UNKNOWN.value,
                f"worker 启动时恢复 {recovered} 条未完成尝试为结果不确定",
            )
        plan = database.get_plan()
    except Exception as exc:  # noqa: BLE001
        # The database may be incompatible or unavailable; do not try to log
        # back into it, construct the service, or continue a scheduled batch.
        print(f"定时任务启动失败，未执行发送：{format_error(exc)}", file=sys.stderr)
        return 2
    if not plan.enabled or not plan.confirmed_at:
        database.record_event("INFO", "schedule_disabled", "每日计划未启用，本次任务跳过")
        return 0
    service = BatchService(
        database,
        DpapiJsonStore(paths.auth_state),
    )
    scheduled_for = scheduled_datetime(plan, datetime.now().astimezone().date()).isoformat(
        timespec="seconds"
    )
    try:
        result = await service.run_batch(
            BatchMode.SCHEDULED,
            scheduled_for=scheduled_for,
        )
    except ValueError as exc:
        database.record_event(
            "ERROR",
            ErrorCode.CONFIGURATION_INVALID.value,
            format_error(exc),
        )
        return 2
    except Exception as exc:  # noqa: BLE001
        database.record_event(
            "ERROR",
            ErrorCode.INTERNAL_ERROR.value,
            format_error(exc),
        )
        return 2

    notification = notify_batch(result)
    if not notification.delivered:
        database.record_event(
            "WARNING",
            "notification_failed",
            f"Windows 通知发送失败（{notification.error}）",
            batch_id=result.batch_id,
        )
    if result.status in {BatchStatus.SUCCESS, BatchStatus.SKIPPED}:
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="续火花助手内部定时 worker")
    parser.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.scheduled:
        parser.error("worker 仅允许由已确认的 Windows 计划任务启动")
    return asyncio.run(run_scheduled())


if __name__ == "__main__":
    raise SystemExit(main())
