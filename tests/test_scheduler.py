from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QCheckBox, QLineEdit, QMainWindow

from spark_keeper import scheduler as scheduler_module
from spark_keeper.clock import BEIJING_TZ
from spark_keeper.database import Database
from spark_keeper.models import BatchMode, FriendCandidate, MessageKind, Plan
from spark_keeper.scheduler import (
    TASK_XML_NAMESPACE,
    SchedulerError,
    build_task_xml,
    detect_missed_schedule,
    scheduled_datetime,
)
from spark_keeper.ui.app import SparkKeeperApp

PACIFIC_TZ = timezone(timedelta(hours=-7))


@pytest.fixture
def pacific_local_clock(monkeypatch):
    class PacificLocalDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = cls(2026, 9, 4, 21, 30, tzinfo=PACIFIC_TZ)
            return moment.replace(tzinfo=None) if tz is None else moment.astimezone(tz)

        def astimezone(self, tz=None):
            moment = self.replace(tzinfo=PACIFIC_TZ) if self.tzinfo is None else self
            return datetime.astimezone(moment, tz if tz is not None else PACIFIC_TZ)

    monkeypatch.setattr(scheduler_module, "datetime", PacificLocalDatetime)
    beijing_now = PacificLocalDatetime.now(BEIJING_TZ)
    monkeypatch.setattr(scheduler_module, "now_beijing", lambda: beijing_now)
    return PacificLocalDatetime



def test_task_xml_uses_interactive_user_and_wake_without_catchup(tmp_path) -> None:
    python = tmp_path / "python.exe"
    root = tmp_path / "project"
    xml = build_task_xml(
        send_time="12:00",
        python_executable=python,
        working_directory=root,
        user_sid="S-1-5-21-1234",
        start_date=date(2026, 9, 4),
    )
    document = ET.fromstring(xml)
    namespace = {"t": TASK_XML_NAMESPACE}
    assert document.findtext(".//t:StartBoundary", namespaces=namespace) == (
        "2026-09-04T12:00:00+08:00"
    )
    assert document.findtext(".//t:LogonType", namespaces=namespace) == "InteractiveToken"
    assert document.findtext(".//t:RunLevel", namespaces=namespace) == "LeastPrivilege"
    assert document.findtext(".//t:WakeToRun", namespaces=namespace) == "true"
    assert document.findtext(".//t:StartWhenAvailable", namespaces=namespace) == "false"
    assert document.findtext(".//t:MultipleInstancesPolicy", namespaces=namespace) == "IgnoreNew"
    assert document.findtext(".//t:ExecutionTimeLimit", namespaces=namespace) == "PT0S"
    assert document.findtext(".//t:Command", namespaces=namespace) == str(python.resolve())
    assert document.findtext(".//t:WorkingDirectory", namespaces=namespace) == str(root.resolve())


def test_task_xml_default_date_uses_beijing_calendar(tmp_path, pacific_local_clock) -> None:
    assert pacific_local_clock.now().date() == date(2026, 9, 4)
    xml = build_task_xml(
        send_time="12:00",
        python_executable=tmp_path / "python.exe",
        working_directory=tmp_path,
        user_sid="S-1-5-21-1234",
    )
    document = ET.fromstring(xml)
    assert document.findtext(
        ".//t:StartBoundary", namespaces={"t": TASK_XML_NAMESPACE}
    ) == "2026-09-05T12:00:00+08:00"


def test_scheduled_datetime_is_beijing_aware_under_pacific_local_clock(pacific_local_clock) -> None:
    plan = Plan(True, "12:00", "测试消息", "2026-09-01T08:00:00+08:00", "now")
    scheduled = scheduled_datetime(plan, date(2026, 9, 5))
    assert scheduled.isoformat(timespec="seconds") == "2026-09-05T12:00:00+08:00"
    assert scheduled.tzinfo == BEIJING_TZ


@pytest.fixture
def registered_daily_task(tmp_path, monkeypatch):
    document = ET.fromstring(
        build_task_xml(
            send_time="12:00",
            python_executable=tmp_path / "python.exe",
            working_directory=tmp_path,
            user_sid="S-1-5-21-1234",
            start_date=date(2026, 9, 4),
        )
    )
    query = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    scheduler = scheduler_module.TaskScheduler(Mock())
    monkeypatch.setattr(scheduler, "_run", query)
    return scheduler, document, query


@pytest.mark.parametrize(
    ("boundary", "verified"),
    [
        ("2026-09-04T12:00:00+08:00", True),
        ("2026-09-03T21:00:00-07:00", True),
        ("2026-11-02T20:00:00-08:00", True),
        ("2026-09-04T12:00:00", False),
        ("2026-09-04T12:00:00-07:00", False),
        ("2026-09-04T11:00:00+08:00", False),
        ("invalid", False),
    ],
)
def test_registered_daily_task_requires_matching_beijing_boundary(
    registered_daily_task, boundary, verified
) -> None:
    scheduler, document, query = registered_daily_task
    start = document.find(f".//{{{TASK_XML_NAMESPACE}}}StartBoundary")
    assert start is not None
    start.text = boundary
    query.return_value.stdout = ET.tostring(document, encoding="unicode")

    assert scheduler.has_beijing_daily_trigger("12:00") is verified

    query.assert_called_once_with(["/Query", "/TN", scheduler_module.TASK_NAME, "/XML"])


@pytest.mark.parametrize("registration", ["weekly", "extra-trigger", "catchup", "missing", "invalid"])
def test_registered_daily_task_rejects_unverified_registration(
    registered_daily_task, registration
) -> None:
    scheduler, document, query = registered_daily_task
    namespace = {"t": TASK_XML_NAMESPACE}
    if registration == "weekly":
        daily = document.find(".//t:ScheduleByDay", namespace)
        assert daily is not None
        daily.tag = f"{{{TASK_XML_NAMESPACE}}}ScheduleByWeek"
    elif registration == "extra-trigger":
        triggers = document.find("t:Triggers", namespace)
        assert triggers is not None
        ET.SubElement(triggers, f"{{{TASK_XML_NAMESPACE}}}LogonTrigger")
    elif registration == "catchup":
        catchup = document.find(".//t:StartWhenAvailable", namespace)
        assert catchup is not None
        catchup.text = "true"
    elif registration == "missing":
        query.return_value.returncode = 1
    query.return_value.stdout = (
        "not xml" if registration == "invalid" else ET.tostring(document, encoding="unicode")
    )

    assert not scheduler.has_beijing_daily_trigger("12:00")


def test_registered_daily_task_accepts_exported_utf16_declaration(registered_daily_task) -> None:
    scheduler, document, query = registered_daily_task
    query.return_value.stdout = ET.tostring(
        document, encoding="utf-16", xml_declaration=True
    ).decode("utf-16")

    assert scheduler.has_beijing_daily_trigger("12:00")


def configured_database(
    tmp_path, kind: MessageKind = MessageKind.TEXT, *, send_time: str = "09:00"
) -> Database:
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
        send_time=send_time,
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
    now = datetime(2026, 9, 4, 10, 0, tzinfo=BEIJING_TZ)
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


@pytest.mark.parametrize("naive_local", [False, True], ids=["aware-pacific", "naive-local"])
@pytest.mark.parametrize(
    ("local_time", "candidate_date"),
    [
        ("2026-09-04T20:30:00-07:00", "2026-09-04"),
        ("2026-09-04T21:05:00-07:00", "2026-09-04"),
        ("2026-09-04T21:05:01-07:00", "2026-09-05"),
        ("2026-09-04T21:30:00-07:00", "2026-09-05"),
    ],
)
def test_missed_schedule_uses_beijing_calendar_and_grace_boundary(
    tmp_path, pacific_local_clock, local_time, candidate_date, naive_local
) -> None:
    database = configured_database(tmp_path, send_time="12:00")
    now = pacific_local_clock.fromisoformat(local_time)
    if naive_local:
        now = now.replace(tzinfo=None)
    assert now.date() == date(2026, 9, 4)

    missed = detect_missed_schedule(database, now=now)

    assert missed is not None
    expected = f"{candidate_date}T12:00:00+08:00"
    assert missed.scheduled_for == expected
    action = database.list_pending_actions("missed_schedule")[0]
    assert action["payload"]["scheduled_for"] == expected
    assert database.list_batches() == []


def test_missed_schedule_default_clock_uses_beijing_date(tmp_path, pacific_local_clock) -> None:
    database = configured_database(tmp_path, send_time="12:00")

    missed = detect_missed_schedule(database)

    assert missed is not None
    assert missed.scheduled_for == "2026-09-05T12:00:00+08:00"


@pytest.mark.parametrize(
    ("confirmed_at", "missed_expected"),
    [
        ("2026-09-04T20:59:59-07:00", True),
        ("2026-09-04T21:00:00-07:00", True),
        ("2026-09-04T21:00:01-07:00", False),
    ],
)
def test_missed_schedule_compares_confirmation_by_instant(
    tmp_path, confirmed_at, missed_expected
) -> None:
    database = configured_database(tmp_path, send_time="12:00")
    with database.connect(immediate=True) as connection:
        connection.execute("UPDATE plan SET confirmed_at = ?", (confirmed_at,))

    missed = detect_missed_schedule(
        database, now=datetime(2026, 9, 5, 12, 30, tzinfo=BEIJING_TZ)
    )

    assert (missed is not None) is missed_expected
    if missed is not None:
        assert missed.scheduled_for == "2026-09-05T12:00:00+08:00"


def test_existing_scheduled_batch_is_not_missed(tmp_path) -> None:
    database = configured_database(tmp_path)
    now = datetime(2026, 9, 4, 10, 0, tzinfo=BEIJING_TZ)
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
