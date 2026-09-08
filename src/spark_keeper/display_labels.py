from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .models import AttemptStatus, BatchMode, ErrorCode

_STATUS_LABELS = {
    "running": "进行中",
    "sending": "发送中",
    "success": "成功",
    "partial": "部分成功",
    "failed": "失败",
    "cancelled": "已取消",
    "skipped": "已跳过",
    "unknown": "结果不确定",
    "duplicate": "重复跳过",
}
_MODE_LABELS = {
    "validation": "仅验证",
    "manual": "手动发送",
    "scheduled": "定时发送",
    "missed": "补发",
}
_ERROR_LABELS = {
    "authentication_required": "登录已失效，请重新扫码",
    "human_verification_required": "需要本人完成安全验证",
    "page_not_ready": "聊天页面未就绪",
    "page_structure_changed": "聊天页面结构已变化",
    "target_not_found": "未找到好友",
    "target_ambiguous": "无法唯一确认好友",
    "target_identity_mismatch": "好友身份不一致",
    "composer_unavailable": "聊天输入框不可用",
    "send_failed": "页面提示消息发送失败",
    "send_unknown": "结果不确定，请人工核对聊天",
    "already_running": "已有任务正在运行",
    "duplicate_blocked": "防重复已拦截，请核对发送记录",
    "login_state_unavailable": "登录状态不可用，请重新扫码",
    "configuration_invalid": "配置需要重新确认",
    "cancelled": "已取消",
    "internal_error": "运行异常，请查看详情",
}
_LEVEL_LABELS = {
    "DEBUG": "调试",
    "INFO": "信息",
    "WARNING": "警告",
    "WARN": "警告",
    "ERROR": "错误",
    "CRITICAL": "严重错误",
    "FATAL": "严重错误",
    "NOTSET": "未设置",
}
# Non-error categories emitted by the service, database, worker and UI.
_EVENT_LABELS = {
    "login": ("登录", "扫码登录状态已安全保存"),
    "logout": ("退出登录", "本机登录状态已清除"),
    "friend_search": ("好友搜索", "好友搜索完成"),
    "friend_captured": ("好友提取", "已提取当前聊天好友，尚未保存"),
    "friend_saved": ("好友保存", "已确认并保存好友"),
    "spark_scan": ("火花扫描", "火花只读扫描结束"),
    "spark_contacts_imported": ("火花好友导入", "火花好友导入完成"),
    "batch_started": ("批次开始", "批次已开始"),
    "batch_finished": ("批次结束", "批次已结束，请查看详情"),
    "validation_success": ("验证成功", "好友配置验证成功"),
    "send_success": ("发送成功", "消息已确认发送成功"),
    "schedule_disabled": ("计划未启用", "每日计划未启用，本次任务跳过"),
    "notification_failed": ("通知失败", "系统通知发送失败，请查看详情"),
    "schedule_read_failed": ("计划读取失败", "系统任务状态读取失败，请查看详情"),
    "schedule_rollback_failed": ("计划恢复失败", "系统任务恢复失败，请查看详情"),
    "task_refresh_failed": ("任务刷新失败", "任务状态刷新失败，请查看详情"),
    "page_read_failed": ("页面读取失败", "页面读取失败，请查看详情"),
    "ui_error": ("界面错误", "界面操作失败，请查看详情"),
}
_SCAN_STATUS_LABELS = {"complete": "完整", "partial": "部分完成", "cancelled": "已取消"}

# Parse only complete, locally generated templates, never arbitrary diagnostic text.
_BATCH_STARTED = re.compile(r"批次开始，模式 ([^，\n]+)，目标数量 ([0-9]+)")
_BATCH_FINISHED = re.compile(
    r"批次结束，状态 ([^，\n]+)，成功 ([0-9]+)，失败 ([0-9]+)，"
    r"结果不确定 ([0-9]+)，重复跳过 ([0-9]+)"
)
_FRIEND_SEARCH = re.compile(r"好友搜索完成，候选数量 ([0-9]+)")
_SPARK_SCAN = re.compile(r"火花只读扫描结束，状态 ([^，\n]+)，候选数量 ([0-9]+)")
_SPARK_IMPORTED = re.compile(
    r"火花好友导入完成：新增 ([0-9]+) 位（停用），已有 ([0-9]+) 位保持不变"
)
_TARGET_SUFFIX = re.compile(r"(?:（ID: ([0-9]+)）|\(ID: ([0-9]+)\))\Z")


def status_label(value: str) -> str:
    return _STATUS_LABELS.get(value, "未知状态")


def mode_label(value: str) -> str:
    return _MODE_LABELS.get(value, "未知模式")


def error_label(value: str | None) -> str:
    if not value:
        return "—"
    return _ERROR_LABELS.get(value, "未知错误，请查看详情")


def level_label(value: str) -> str:
    return _LEVEL_LABELS.get(value.upper(), "未知级别")


def category_label(value: str) -> str:
    if value in _ERROR_LABELS:
        return error_label(value)
    labels = _EVENT_LABELS.get(value)
    return labels[0] if labels else "其他事件"


def target_label(value: str) -> str:
    """Translate the generated numeric suffix without altering the user's name."""
    match = _TARGET_SUFFIX.search(value)
    if not match:
        return value
    return f"{value[: match.start()]}（编号：{match.group(1) or match.group(2)}）"


def _stored_label(value: str, labels: Mapping[str, str], fallback: str) -> str:
    # Older events store enum values; newer events may already store their labels.
    if value in labels.values():
        return value
    return labels.get(value, fallback)


def event_summary(event: Mapping[str, Any]) -> str:
    """Return a safe list summary; the original message remains untouched for details."""
    category = event.get("category", "")
    if not isinstance(category, str):
        return "其他事件，请查看详情"
    if category in _ERROR_LABELS:
        return error_label(category)
    labels = _EVENT_LABELS.get(category)
    if not labels:
        return "其他事件，请查看详情"
    message = event.get("message", "")
    if not isinstance(message, str):
        return labels[1]
    if category == "batch_started" and (match := _BATCH_STARTED.fullmatch(message)):
        mode = _stored_label(match[1], _MODE_LABELS, "未知模式")
        return f"批次开始，模式 {mode}，目标数量 {match[2]}"
    if category == "batch_finished" and (match := _BATCH_FINISHED.fullmatch(message)):
        status = _stored_label(match[1], _STATUS_LABELS, "未知状态")
        return (
            f"批次结束，状态 {status}，成功 {match[2]}，失败 {match[3]}，"
            f"结果不确定 {match[4]}，重复跳过 {match[5]}"
        )
    if category == "friend_search" and (match := _FRIEND_SEARCH.fullmatch(message)):
        return f"好友搜索完成，候选数量 {match[1]}"
    if category == "spark_scan" and (match := _SPARK_SCAN.fullmatch(message)):
        status = _stored_label(match[1], _SCAN_STATUS_LABELS, "未知状态")
        return f"火花只读扫描结束，状态 {status}，候选数量 {match[2]}"
    if category == "spark_contacts_imported" and (match := _SPARK_IMPORTED.fullmatch(message)):
        return f"火花好友导入完成：新增 {match[1]} 位（停用），已有 {match[2]} 位保持不变"
    return labels[1]


def progress_failure_label(mode: BatchMode, status: AttemptStatus, code: ErrorCode) -> str:
    # Persisted/normalised outcome takes precedence over the original exception code.
    if status == AttemptStatus.UNKNOWN:
        return error_label(ErrorCode.SEND_UNKNOWN)
    if status == AttemptStatus.DUPLICATE:
        return error_label(ErrorCode.DUPLICATE_BLOCKED)
    if status == AttemptStatus.CANCELLED:
        return status_label(status)
    action = "验证失败" if mode == BatchMode.VALIDATION else "发送失败"
    return f"{action}：{error_label(code)}"
