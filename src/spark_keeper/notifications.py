from __future__ import annotations

from dataclasses import dataclass

from windows_toasts import Toast, WindowsToaster

from . import APP_NAME
from .logging_safe import format_error
from .models import BatchResult


@dataclass(frozen=True, slots=True)
class NotificationResult:
    delivered: bool
    error: str = ""


def notify(title: str, message: str) -> NotificationResult:
    """发送不含好友名和消息正文的本地 Windows 通知。"""

    safe_title = " ".join(title.split())[:80] or APP_NAME
    safe_message = " ".join(message.split())[:240]
    try:
        toaster = WindowsToaster(APP_NAME)
        toast = Toast()
        toast.text_fields = [safe_title, safe_message]
        toaster.show_toast(toast)
        return NotificationResult(True)
    except Exception as exc:  # noqa: BLE001
        # Keep the native failure for the event log; the toast itself stays statistical.
        return NotificationResult(False, format_error(exc))


def notify_batch(result: BatchResult) -> NotificationResult:
    counts = result.counts
    message = (
        f"状态：{result.status.value}；成功 {counts['success']}，失败 {counts['failed']}，"
        f"结果不确定 {counts['unknown']}，重复跳过 {counts['duplicate']}"
    )
    return notify("每日发送任务已结束", message)
