from __future__ import annotations

import sqlite3

import pytest

import spark_keeper.worker as worker_module
from spark_keeper.database import Database
from spark_keeper.models import AttemptStatus, BatchMode, BatchResult, BatchStatus, TargetResult
from spark_keeper.mutex import WindowsTaskMutex
from spark_keeper.notifications import notify_batch
from spark_keeper.paths import AppPaths
from spark_keeper.worker import main


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

    assert await worker_module.run_scheduled() == 0
    assert any(event["category"] == "schedule_disabled" for event in database.list_events(limit=20))


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
        FriendCandidate(stable_key="offline-friend", display_name="离线好友"), "离线好友"
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
