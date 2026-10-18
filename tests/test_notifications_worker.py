from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest

import spark_keeper.worker as worker_module
from spark_keeper import maintenance
from spark_keeper.clock import BEIJING_TZ
from spark_keeper.database import Database
from spark_keeper.models import AttemptStatus, BatchMode, BatchResult, BatchStatus, TargetResult
from spark_keeper.mutex import WindowsTaskMutex
from spark_keeper.notifications import NotificationResult, notify_batch
from spark_keeper.paths import AppPaths
from spark_keeper.scheduler import SchedulerError
from spark_keeper.worker import main


@pytest.fixture(autouse=True)
def isolated_maintenance_gate(tmp_path, monkeypatch):
    name = rf"Global\SparkKeeper.Maintenance.Test.{uuid.uuid4().hex}"
    monkeypatch.setattr(maintenance, "maintenance_mutex_name", lambda: name)
    monkeypatch.setenv("SPARK_KEEPER_ROOT", str(tmp_path))
    monkeypatch.setattr(
        worker_module.TaskScheduler, "has_beijing_daily_trigger", Mock(return_value=True)
    )


def test_batch_notification_contains_counts_not_private_content(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    class FakeToast:
        text_fields: list[str]

    class FakeToaster:
        def __init__(self, _name: str) -> None:
            pass

        def show_toast(self, toast: FakeToast) -> None:
            captured["text"] = toast.text_fields

    monkeypatch.setattr("spark_keeper.notifications.Toast", FakeToast)
    monkeypatch.setattr("spark_keeper.notifications.WindowsToaster", FakeToaster)
    result = BatchResult(
        "batch-id",
        BatchMode.SCHEDULED,
        BatchStatus.PARTIAL,
        (
            TargetResult(1, "真实好友昵称（ID: 1）", AttemptStatus.SUCCESS, detail="消息正文"),
            TargetResult(2, "另一位好友（ID: 2）", AttemptStatus.FAILED),
        ),
    )
    delivered = notify_batch(result)
    assert delivered.delivered
    rendered = " ".join(captured["text"])
    assert "成功 1" in rendered
    assert "失败 1" in rendered
    assert "真实好友昵称" not in rendered
    assert "消息正文" not in rendered


def test_worker_requires_internal_scheduled_flag() -> None:
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code == 2


@pytest.mark.asyncio
async def test_scheduled_worker_skips_when_plan_is_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SPARK_KEEPER_ROOT", str(tmp_path))
    paths = AppPaths.discover()
    paths.ensure_runtime_dirs()
    database = Database(paths.database)

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("停用计划不得启动批次")

    monkeypatch.setattr(worker_module.BatchService, "run_batch", fail_if_called)
    monkeypatch.setattr(
        worker_module.TaskScheduler,
        "has_beijing_daily_trigger",
        Mock(side_effect=AssertionError("停用计划不得核验或重建系统任务")),
    )

    assert await worker_module.run_scheduled() == 0
    assert any(event["category"] == "schedule_disabled" for event in database.list_events(limit=20))


@pytest.mark.asyncio
async def test_scheduled_worker_uses_beijing_date_when_pacific_date_is_previous(
    tmp_path, monkeypatch
) -> None:
    pacific_now = datetime(2026, 9, 4, 21, 30, tzinfo=timezone(timedelta(hours=-7)))
    beijing_now = pacific_now.astimezone(BEIJING_TZ)
    assert pacific_now.date() != beijing_now.date()
    monkeypatch.setattr(worker_module, "now_beijing", lambda: beijing_now)
    paths = AppPaths.discover()
    paths.ensure_runtime_dirs()
    database = Database(paths.database)
    database.save_plan(enabled=True, send_time="12:00", message_text="不发送", confirmed=True)
    plan = database.get_plan()
    result = BatchResult("offline-batch", BatchMode.SCHEDULED, BatchStatus.SKIPPED, ())
    run_batch = AsyncMock(return_value=result)
    notify = Mock(return_value=NotificationResult(True))
    monkeypatch.setattr(worker_module.BatchService, "run_batch", run_batch)
    monkeypatch.setattr(worker_module, "notify_batch", notify)

    assert await worker_module.run_scheduled() == 0

    run_batch.assert_awaited_once_with(
        BatchMode.SCHEDULED, scheduled_for="2026-09-05T12:00:00+08:00"
    )
    notify.assert_called_once_with(result)
    assert database.get_plan() == plan
    assert database.list_batches() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("query_fails", [False, True], ids=["legacy-task", "query-failure"])
async def test_scheduled_worker_skips_unverified_beijing_task_without_mutating_plan(
    tmp_path, monkeypatch, query_fails
) -> None:
    paths = AppPaths.discover()
    database = Database(paths.database)
    database.save_plan(enabled=True, send_time="12:00", message_text="不发送", confirmed=True)
    before = database.get_plan()
    guard = Mock(return_value=False)
    if query_fails:
        guard.side_effect = SchedulerError("系统任务查询失败")
    service = Mock(side_effect=AssertionError("未核验北京时间任务不得构造发送服务"))
    notify = Mock()
    monkeypatch.setattr(worker_module.TaskScheduler, "has_beijing_daily_trigger", guard)
    monkeypatch.setattr(worker_module, "BatchService", service)
    monkeypatch.setattr(worker_module, "notify_batch", notify)

    assert await worker_module.run_scheduled() == 0

    guard.assert_called_once_with("12:00")
    service.assert_not_called()
    notify.assert_not_called()
    assert database.get_plan() == before
    assert database.list_batches() == []
    event = next(
        event for event in database.list_events()
        if event["category"] == "schedule_timezone_unverified"
    )
    assert event["level"] == "WARNING"
    assert "重新保存每日计划" in event["message"]
    if query_fails:
        assert "系统任务查询失败" in event["message"]



@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "category"),
    [(ValueError, "configuration_invalid"), (RuntimeError, "internal_error")],
)
async def test_scheduled_worker_preserves_original_errors(
    tmp_path, monkeypatch, error_type, category
) -> None:
    monkeypatch.setenv("SPARK_KEEPER_ROOT", str(tmp_path))
    paths = AppPaths.discover()
    paths.ensure_runtime_dirs()
    database = Database(paths.database)
    database.save_plan(enabled=True, send_time="09:00", message_text="测试消息", confirmed=True)
    original = "worker 原始错误 storage_state\n" + "完整详情" * 100

    async def fail_batch(*_args, **_kwargs):
        try:
            raise OSError("worker 底层原因")
        except OSError as cause:
            error = error_type(original)
            error.add_note("worker diagnostic: scheduled_for=2026-09-08")
            raise error from cause

    monkeypatch.setattr(worker_module.BatchService, "run_batch", fail_batch)

    assert await worker_module.run_scheduled() == 2
    event = next(event for event in database.list_events() if event["category"] == category)
    assert f"{error_type.__name__}: {original}" in event["message"]
    assert "OSError: worker 底层原因" in event["message"]
    assert "worker diagnostic: scheduled_for=2026-09-08" in event["message"]
    assert "direct cause" in event["message"]
    assert "Traceback (most recent call last)" in event["message"]
    assert "in fail_batch" in event["message"]


def test_notification_failure_preserves_original_error_for_event_log(monkeypatch) -> None:
    class BrokenToaster:
        def __init__(self, _name: str) -> None:
            try:
                raise OSError("Windows 原始系统错误")
            except OSError as cause:
                raise RuntimeError("通知初始化失败：authorization 原文") from cause

    monkeypatch.setattr("spark_keeper.notifications.WindowsToaster", BrokenToaster)
    result = notify_batch(BatchResult("batch", BatchMode.SCHEDULED, BatchStatus.SUCCESS, ()))
    assert not result.delivered
    assert "OSError: Windows 原始系统错误" in result.error
    assert "RuntimeError: 通知初始化失败：authorization 原文" in result.error
    assert "Traceback" in result.error


@pytest.mark.asyncio
async def test_worker_future_schema_reports_error_without_writes_or_batch(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("SPARK_KEEPER_ROOT", str(tmp_path))
    paths = AppPaths.discover()
    paths.ensure_runtime_dirs()
    with sqlite3.connect(paths.database) as connection:
        connection.executescript(
            "CREATE TABLE app_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO app_meta VALUES ('schema_version', '999');"
        )
    before = paths.database.read_bytes()

    def reject_service(*_args, **_kwargs):
        pytest.fail("数据库初始化失败不能构造批次服务")

    monkeypatch.setattr(worker_module, "BatchService", reject_service)
    assert await worker_module.run_scheduled() == 2
    message = capsys.readouterr().err
    assert "定时任务启动失败，未执行发送" in message
    assert "999" in message
    assert paths.database.read_bytes() == before
    assert not (paths.data.parent / "backups").exists()


@pytest.mark.asyncio
async def test_worker_migration_failure_preserves_original_error_and_never_starts_batch(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("SPARK_KEEPER_ROOT", str(tmp_path))
    paths = AppPaths.discover()
    database = Database(paths.database)
    database.set_meta("schema_version", "2")
    database.save_plan(enabled=True, send_time="09:00", message_text="不发送", confirmed=True)
    before = database.get_plan()

    def fail_migration(connection):
        connection.execute("UPDATE plan SET message_text = '应回滚'")
        raise sqlite3.OperationalError("迁移原始故障")

    def reject_service(*_args, **_kwargs):
        pytest.fail("数据库迁移失败不能构造批次服务")

    monkeypatch.setattr(Database, "_migrate_schema", staticmethod(fail_migration))
    monkeypatch.setattr(worker_module, "BatchService", reject_service)
    assert await worker_module.run_scheduled() == 2
    message = capsys.readouterr().err
    assert "迁移原始故障" in message
    assert "事务已回滚" in message
    assert database.get_plan() == before
    assert database.get_meta("schema_version") == "2"
    assert len(list((paths.data.parent / "backups").glob("*.sqlite3"))) == 1


@pytest.mark.asyncio
async def test_worker_does_not_recover_active_send(tmp_path, monkeypatch) -> None:
    from spark_keeper.models import FriendCandidate

    monkeypatch.setenv("SPARK_KEEPER_ROOT", str(tmp_path))
    paths = AppPaths.discover()
    database = Database(paths.database)
    account = database.save_account("offline-account", "离线账号")
    target = database.add_target(
        FriendCandidate(
            stable_key="offline-friend",
            display_name="离线好友",
            profile_url="https://www.douyin.com/user/offline-friend",
        ),
        "离线好友",
    )
    batch = database.start_batch(BatchMode.SCHEDULED, target_count=1)
    reservation = database.reserve_attempt(
        batch_id=batch,
        account_key=account.platform_user_id,
        target_id=target.id,
        run_date="2026-09-04",
        message_text="离线记录，不发送",
        manual_override=False,
    )
    before = database.get_attempt(reservation.attempt_id)
    with WindowsTaskMutex():
        assert await worker_module.run_scheduled() == 0
    assert database.get_attempt(reservation.attempt_id) == before
    assert database.has_daily_guard(account.platform_user_id, target.id, "2026-09-04")
