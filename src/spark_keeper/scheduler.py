from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .database import Database
from .models import Plan
from .paths import AppPaths

TASK_NAME = "SparkKeeperLocalDaily"
TASK_XML_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
SCHEDULER_COMMAND_TIMEOUT_SECONDS = 30
ET.register_namespace("", TASK_XML_NAMESPACE)


class SchedulerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MissedSchedule:
    action_id: str
    scheduled_for: str


def _tag(name: str) -> str:
    return f"{{{TASK_XML_NAMESPACE}}}{name}"


def _child(parent: ET.Element, name: str, text: str | None = None, **attributes: str) -> ET.Element:
    element = ET.SubElement(parent, _tag(name), attributes)
    if text is not None:
        element.text = text
    return element


def _parse_send_time(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("计划时间必须为 HH:MM") from exc
    if parsed.tzinfo is not None or parsed.isoformat(timespec="minutes") != value:
        raise ValueError("计划时间必须为 HH:MM")
    return parsed


def current_user_sid() -> str:
    if os.name != "nt":
        raise SchedulerError("Windows 计划任务仅支持 Windows")
    executable = shutil.which("whoami")
    if not executable:
        raise SchedulerError("系统缺少 whoami，无法确定当前用户")
    try:
        completed = subprocess.run(
            [executable, "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=SCHEDULER_COMMAND_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SchedulerError("读取 Windows 用户 SID 失败或超时（30 秒）") from exc
    if completed.returncode != 0:
        raise SchedulerError("无法读取当前 Windows 用户 SID")
    rows = list(csv.reader(io.StringIO(completed.stdout.strip())))
    if not rows or len(rows[0]) < 2 or not rows[0][1].startswith("S-"):
        raise SchedulerError("当前 Windows 用户 SID 格式无效")
    return rows[0][1]


def build_task_xml(
    *,
    send_time: str,
    python_executable: Path,
    working_directory: Path,
    user_sid: str,
    start_date: date | None = None,
    arguments: str = "-m spark_keeper.worker --scheduled",
) -> bytes:
    parsed_time = _parse_send_time(send_time)
    start = datetime.combine(
        start_date or datetime.now().astimezone().date(),
        parsed_time,
    )
    root = ET.Element(_tag("Task"), {"version": "1.4"})
    registration = _child(root, "RegistrationInfo")
    _child(registration, "Description", "续火花助手本地每日任务")

    triggers = _child(root, "Triggers")
    calendar = _child(triggers, "CalendarTrigger")
    _child(calendar, "StartBoundary", start.isoformat(timespec="seconds"))
    _child(calendar, "Enabled", "true")
    daily = _child(calendar, "ScheduleByDay")
    _child(daily, "DaysInterval", "1")

    principals = _child(root, "Principals")
    principal = _child(principals, "Principal", id="Author")
    _child(principal, "UserId", user_sid)
    _child(principal, "LogonType", "InteractiveToken")
    _child(principal, "RunLevel", "LeastPrivilege")

    settings = _child(root, "Settings")
    _child(settings, "MultipleInstancesPolicy", "IgnoreNew")
    _child(settings, "DisallowStartIfOnBatteries", "false")
    _child(settings, "StopIfGoingOnBatteries", "false")
    _child(settings, "AllowHardTerminate", "true")
    _child(settings, "StartWhenAvailable", "false")
    _child(settings, "RunOnlyIfNetworkAvailable", "true")
    idle = _child(settings, "IdleSettings")
    _child(idle, "StopOnIdleEnd", "false")
    _child(idle, "RestartOnIdle", "false")
    _child(settings, "AllowStartOnDemand", "true")
    _child(settings, "Enabled", "true")
    _child(settings, "Hidden", "true")
    _child(settings, "RunOnlyIfIdle", "false")
    _child(settings, "WakeToRun", "true")
    _child(settings, "ExecutionTimeLimit", "PT0S")
    _child(settings, "Priority", "7")

    actions = _child(root, "Actions", Context="Author")
    execute = _child(actions, "Exec")
    _child(execute, "Command", str(python_executable.resolve()))
    _child(execute, "Arguments", arguments)
    _child(execute, "WorkingDirectory", str(working_directory.resolve()))
    return ET.tostring(root, encoding="utf-16", xml_declaration=True)


def _scheduled_task_arguments() -> str:
    if getattr(sys, "frozen", False):
        return "--scheduled"
    return "-m spark_keeper.worker --scheduled"


class TaskScheduler:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    def sync(self, plan: Plan) -> None:
        if not plan.enabled:
            self.remove()
            return
        if not plan.confirmed_at:
            raise SchedulerError("计划尚未确认，不能创建系统任务")
        self.install(plan.send_time)

    def install(self, send_time: str) -> None:
        self.paths.ensure_runtime_dirs()
        xml = build_task_xml(
            send_time=send_time,
            python_executable=Path(sys.executable),
            working_directory=self.paths.root,
            user_sid=current_user_sid(),
            arguments=_scheduled_task_arguments(),
        )
        self.paths.scheduler_xml.write_bytes(xml)
        completed = self._run(
            ["/Create", "/TN", TASK_NAME, "/XML", str(self.paths.scheduler_xml), "/F"]
        )
        if completed.returncode != 0:
            raise SchedulerError("Windows 计划任务创建失败")

    def remove(self) -> None:
        if not self.exists():
            return
        completed = self._run(["/Delete", "/TN", TASK_NAME, "/F"])
        if completed.returncode != 0:
            raise SchedulerError("Windows 计划任务删除失败")

    def exists(self) -> bool:
        return self._run(["/Query", "/TN", TASK_NAME]).returncode == 0

    def run_now(self) -> None:
        completed = self._run(["/Run", "/TN", TASK_NAME])
        if completed.returncode != 0:
            raise SchedulerError("Windows 计划任务启动失败")

    @staticmethod
    def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        executable = shutil.which("schtasks")
        if not executable:
            raise SchedulerError("系统缺少 schtasks，无法管理计划任务")
        try:
            return subprocess.run(
                [executable, *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=SCHEDULER_COMMAND_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SchedulerError(
                f"Windows 计划任务命令 {arguments[0]} 失败或超时（30 秒）"
            ) from exc


def scheduled_datetime(plan: Plan, on_date: date) -> datetime:
    parsed = _parse_send_time(plan.send_time)
    return datetime.combine(on_date, parsed).astimezone()


def detect_missed_schedule(
    database: Database,
    *,
    now: datetime | None = None,
    grace: timedelta = timedelta(minutes=5),
) -> MissedSchedule | None:
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    plan = database.get_plan()
    if not plan.enabled or not plan.confirmed_at:
        return None

    today_scheduled = scheduled_datetime(plan, current.date())
    candidate_date = (
        current.date() if current > today_scheduled + grace else current.date() - timedelta(days=1)
    )
    candidate = scheduled_datetime(plan, candidate_date)
    try:
        confirmed_at = datetime.fromisoformat(plan.confirmed_at)
    except ValueError:
        return None
    if confirmed_at.tzinfo is None:
        confirmed_at = confirmed_at.astimezone()
    if candidate < confirmed_at:
        return None
    if database.scheduled_batch_exists(candidate_date.isoformat()):
        return None

    targets = database.list_targets(enabled_only=True)
    if not targets:
        return None
    dedupe_key = f"missed_schedule:{candidate_date.isoformat()}"
    action_id = database.create_pending_action(
        "missed_schedule",
        dedupe_key,
        {
            "scheduled_for": candidate.isoformat(timespec="seconds"),
            "target_ids": [target.id for target in targets],
            "target_count": len(targets),
            "message_text": plan.message_text,
            "message_kind": plan.message_kind.value,
            "delay_min_seconds": plan.delay_min_seconds,
            "delay_max_seconds": plan.delay_max_seconds,
        },
    )
    return MissedSchedule(action_id, candidate.isoformat(timespec="seconds"))
