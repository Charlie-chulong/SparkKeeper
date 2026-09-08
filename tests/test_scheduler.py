from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from datetime import date, datetime
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QCheckBox, QLineEdit, QMainWindow

from spark_keeper import scheduler as scheduler_module
from spark_keeper.database import Database
from spark_keeper.models import BatchMode, FriendCandidate, MessageKind, Plan
from spark_keeper.scheduler import (
    TASK_XML_NAMESPACE,
    SchedulerError,
    build_task_xml,
    detect_missed_schedule,
)
from spark_keeper.ui.app import SparkKeeperApp


def test_task_xml_uses_interactive_user_and_wake_without_catchup(tmp_path) -> None:
    python = tmp_path / "python.exe"
    root = tmp_path / "project"
    xml = build_task_xml(
        send_time="08:35",
        python_executable=python,
        working_directory=root,
        user_sid="S-1-5-21-1234",
        start_date=date(2026, 9, 4),
    )
    document = ET.fromstring(xml)
    namespace = {"t": TASK_XML_NAMESPACE}
    assert document.findtext(".//t:LogonType", namespaces=namespace) == "InteractiveToken"
    assert document.findtext(".//t:RunLevel", namespaces=namespace) == "LeastPrivilege"
    assert document.findtext(".//t:WakeToRun", namespaces=namespace) == "true"
    assert document.findtext(".//t:StartWhenAvailable", namespaces=namespace) == "false"
    assert document.findtext(".//t:MultipleInstancesPolicy", namespaces=namespace) == "IgnoreNew"
    assert document.findtext(".//t:ExecutionTimeLimit", namespaces=namespace) == "PT0S"
    assert document.findtext(".//t:Command", namespaces=namespace) == str(python.resolve())
    assert document.findtext(".//t:WorkingDirectory", namespaces=namespace) == str(root.resolve())


def configured_database(tmp_path, kind: MessageKind = MessageKind.TEXT) -> Database:
    database = Database(tmp_path / "state.sqlite3")
    database.save_account("account-key", "测试账号")
    database.add_target(
        FriendCandidate(
            "friend-key", "测试好友", profile_url="https://www.douyin.com/user/friend-key"
        ),
        "测试好友",
    )
    database.save_plan(
        enabled=True,
        send_time="09:00",
        message_text="测试消息",
        confirmed=True,
        message_kind=kind,
    )
    with database.connect(immediate=True) as connection:
        connection.execute(
            "UPDATE plan SET confirmed_at = ? WHERE singleton = 1",
            ("2026-09-01T08:00:00+08:00",),
        )
    return database


@pytest.mark.parametrize("kind", list(MessageKind))
def test_missed_schedule_creates_one_deduplicated_action(tmp_path, kind) -> None:
    database = configured_database(tmp_path, kind)
    timezone = datetime.now().astimezone().tzinfo
    now = datetime(2026, 9, 4, 10, 0, tzinfo=timezone)
    first = detect_missed_schedule(database, now=now)
    second = detect_missed_schedule(database, now=now)
    assert first is not None
    assert second is not None
    assert first.action_id == second.action_id
    action = database.list_pending_actions("missed_schedule")[0]
    assert action["payload"]["target_count"] == 1
    assert action["payload"]["message_text"] == ("测试消息" if kind is MessageKind.TEXT else "")
    assert action["payload"]["message_kind"] == kind.value
    assert action["payload"]["delay_min_seconds"] == 3
    assert action["payload"]["delay_max_seconds"] == 8
    other_kind = MessageKind.SPARK_STICKER if kind is MessageKind.TEXT else MessageKind.TEXT
    database.save_plan(
        enabled=True,
        send_time="09:00",
        message_text="变更后的文本",
        confirmed=True,
        message_kind=other_kind,
        delay_min_seconds=4,
        delay_max_seconds=9,
    )
    with database.connect(immediate=True) as connection:
        connection.execute("UPDATE plan SET confirmed_at = ?", ("2026-09-01T08:00:00+08:00",))
    assert detect_missed_schedule(database, now=now).action_id == first.action_id
    assert database.list_pending_actions("missed_schedule")[0]["payload"] == action["payload"]


def test_existing_scheduled_batch_is_not_missed(tmp_path) -> None:
    database = configured_database(tmp_path)
    timezone = datetime.now().astimezone().tzinfo
    now = datetime(2026, 9, 4, 10, 0, tzinfo=timezone)
    database.start_batch(
        BatchMode.SCHEDULED,
        target_count=1,
        scheduled_for="2026-09-04T09:00:00+08:00",
    )
    assert detect_missed_schedule(database, now=now) is None


@pytest.mark.parametrize("event_store_unavailable", [False, True])
def test_plan_scheduler_rollback_failure_preserves_original_diagnostics(
    qapp, event_store_unavailable
) -> None:
    previous = Plan(False, "09:00", "旧的私密正文", None, "2026-09-04T08:00:00+08:00")
    saved = Plan(True, "10:00", "新的私密正文", "2026-09-04T09:00:00+08:00", "now")
    database = Mock()
    database.get_plan.return_value = previous
    database.save_plan.side_effect = [saved, previous]
    if event_store_unavailable:
        database.record_event.side_effect = RuntimeError("事件存储不可写")
    scheduler = Mock()
    original_error = SchedulerError("包含敏感系统详情")
    scheduler.sync.side_effect = [
        original_error,
        SchedulerError("仍包含敏感系统详情"),
    ]
    app = SparkKeeperApp.__new__(SparkKeeperApp)
    QMainWindow.__init__(app)
    app.database = database
    app.scheduler = scheduler
    app.schedule_enabled = QCheckBox(app)
    app.schedule_enabled.setChecked(True)
    app.schedule_time_input = QLineEdit("10:00", app)
    app._current_message = Mock(return_value="新的私密正文")
    app._current_message_kind = Mock(return_value=MessageKind.TEXT)
    app.delay_min_input = QLineEdit("4", app)
    app.delay_max_input = QLineEdit("9", app)

    try:
        with pytest.raises(SchedulerError, match="包含敏感系统详情") as error:
            app._persist_plan(app._snapshot_plan(), require_confirmation=True)
        assert error.value is original_error
        if event_store_unavailable:
            assert "事件存储不可写" in "\n".join(error.value.__notes__)
    finally:
        app.deleteLater()
        qapp.processEvents()

    assert database.save_plan.call_count == 2
    event = database.record_event.call_args.args
    assert event[:2] == ("ERROR", "schedule_rollback_failed")
    assert "SchedulerError: 包含敏感系统详情" in event[2]
    assert "SchedulerError: 仍包含敏感系统详情" in event[2]
    assert "Traceback" in event[2]
    assert "私密正文" not in event[2]


@pytest.mark.parametrize("command", ["whoami", "schtasks"])
@pytest.mark.parametrize("failure", ["timeout", "oserror"])
def test_scheduler_commands_are_bounded_and_preserve_diagnostics(monkeypatch, command, failure):
    monkeypatch.setattr(scheduler_module.shutil, "which", lambda name: name)
    cause = (
        subprocess.TimeoutExpired(command, 30, output="系统诊断")
        if failure == "timeout"
        else OSError("系统命令启动失败")
    )
    run = Mock(side_effect=cause)
    monkeypatch.setattr(scheduler_module.subprocess, "run", run)
    with pytest.raises(SchedulerError) as error:
        if command == "whoami":
            scheduler_module.current_user_sid()
        else:
            scheduler_module.TaskScheduler._run(["/Query", "/TN", "fixture"])
    assert error.value.__cause__ is cause
    assert "30 秒" in str(error.value)
    assert run.call_args.kwargs["timeout"] == 30
