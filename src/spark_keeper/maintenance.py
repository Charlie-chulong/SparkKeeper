from __future__ import annotations

import json
import os
from pathlib import Path

from .mutex import AlreadyRunningError, WindowsTaskMutex
from .windows_identity import current_user_sid


class MaintenanceRequired(RuntimeError):
    """An installer transaction must be repaired before any business data is opened."""


class MaintenanceBusy(RuntimeError):
    """Another worker or installer owns the user-wide maintenance gate."""


def maintenance_mutex_name() -> str:
    return rf"Global\SparkKeeper.Maintenance.{current_user_sid()}"


class MaintenanceLease(WindowsTaskMutex):
    """Separate from the automation mutex, so existing inner locks never re-enter it."""

    def __init__(self) -> None:
        super().__init__(maintenance_mutex_name())

    def acquire(self) -> None:
        try:
            super().acquire()
        except AlreadyRunningError as exc:
            raise MaintenanceBusy(
                "安装维护或定时任务正在运行，本次未打开数据库、未执行发送；"
                "请等待其完成后重试，不要强制结束任务。"
            ) from exc


def maintenance_journal_path() -> Path:
    configured = os.environ.get("SPARK_KEEPER_ROOT")
    if configured:
        return Path(configured).resolve() / "maintenance" / "pending.json"
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local_app_data / "SparkKeeper" / "maintenance" / "pending.json"


_PHASE_DESCRIPTIONS = {
    "prepared": "已记录安装维护事务",
    "paused": "计划已暂停",
    "mutating": "程序文件更新未完成",
    "verified": "程序文件已校验，计划恢复未完成",
    "restoring": "计划恢复未完成",
    "uninstall-mutating": "程序卸载未完成",
    "uninstall-verified": "程序卸载清理未完成",
    "uninstall-purging": "卸载用户数据清理未完成",
}
_REPAIR_MESSAGE = (
    "安装维护尚未完成，计划仍保持暂停；本次未打开数据库、未执行发送。"
    "请重新运行安装包并选择原安装目录完成修复；不要删除维护记录或手动启用计划。"
)


def assert_maintenance_clear() -> None:
    """Fail closed on any pending record; only the installer may resolve it."""
    path = maintenance_journal_path()
    try:
        # Opening directly distinguishes a missing record from inaccessible or corrupt data.
        with path.open("r", encoding="utf-8-sig") as stream:
            text = stream.read(65537)
    except FileNotFoundError:
        # A dangling link is still a pending record, not permission to open the database.
        if not path.is_symlink():
            return
        raise MaintenanceRequired(f"{_REPAIR_MESSAGE}（维护记录不可读取）") from None
    except OSError as exc:
        raise MaintenanceRequired(f"{_REPAIR_MESSAGE}（维护记录不可读取）") from exc
    except UnicodeError as exc:
        raise MaintenanceRequired(f"{_REPAIR_MESSAGE}（维护记录损坏）") from exc

    description = "维护记录损坏或格式未知"
    if len(text) <= 65536:
        try:
            record = json.loads(text)
            if (
                isinstance(record, dict)
                and type(record.get("format")) is int
                and record["format"] == 1
                and record.get("product") == "SparkKeeper"
                and isinstance(record.get("phase"), str)
            ):
                description = _PHASE_DESCRIPTIONS.get(record["phase"], "未知维护阶段")
        except (ValueError, RecursionError):
            pass
    raise MaintenanceRequired(f"{_REPAIR_MESSAGE}（{description}）")
