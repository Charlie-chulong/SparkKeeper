from __future__ import annotations

import re
from copy import deepcopy
from types import MappingProxyType

import pytest

from spark_keeper.display_labels import (
    category_label,
    error_label,
    event_summary,
    level_label,
    mode_label,
    progress_failure_label,
    status_label,
    target_label,
)
from spark_keeper.models import AttemptStatus, BatchMode, BatchStatus, ErrorCode


@pytest.mark.parametrize("value", list(AttemptStatus) + list(BatchStatus))
def test_every_attempt_and_batch_status_has_a_chinese_label(value) -> None:
    label = status_label(value.value)
    assert label != "未知状态"
    assert not re.search(r"[A-Za-z]", label)
    assert status_label(value) == label


@pytest.mark.parametrize("value", list(BatchMode))
def test_every_mode_has_a_chinese_label(value) -> None:
    label = mode_label(value.value)
    assert label != "未知模式"
    assert not re.search(r"[A-Za-z]", label)
    assert mode_label(value) == label


@pytest.mark.parametrize("value", list(ErrorCode))
def test_every_error_is_translated_in_all_display_surfaces(value) -> None:
    label = error_label(value.value)
    assert label != "未知错误，请查看详情"
    assert not re.search(r"[A-Za-z]", label)
    assert error_label(value) == label
    assert category_label(value.value) == label
    assert event_summary({"category": value.value, "message": "Traceback: RuntimeError"}) == label
    assert (
        progress_failure_label(BatchMode.MANUAL, AttemptStatus.FAILED, value)
        == f"发送失败：{label}"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("DEBUG", "调试"),
        ("INFO", "信息"),
        ("WARNING", "警告"),
        ("WARN", "警告"),
        ("ERROR", "错误"),
        ("CRITICAL", "严重错误"),
        ("FATAL", "严重错误"),
        ("NOTSET", "未设置"),
    ],
)
def test_logging_levels(value, expected) -> None:
    assert level_label(value) == expected
    assert level_label(value.lower()) == expected


@pytest.mark.parametrize(
    "category",
    [
        "login",
        "logout",
        "friend_search",
        "friend_captured",
        "friend_saved",
        "spark_scan",
        "spark_contacts_imported",
        "batch_started",
        "batch_finished",
        "validation_success",
        "send_success",
        "schedule_disabled",
        "notification_failed",
        "schedule_read_failed",
        "schedule_rollback_failed",
        "task_refresh_failed",
        "page_read_failed",
        "ui_error",
    ],
)
def test_every_current_non_error_category_has_safe_chinese_summary(category) -> None:
    assert category_label(category) != "其他事件"
    label = event_summary(
        {
            "category": category,
            "message": "Traceback (most recent call last):\nRuntimeError: secret",
        }
    )
    assert label != "其他事件，请查看详情"
    assert not re.search(r"[A-Za-z]", label)


@pytest.mark.parametrize("value", ["", "future_code", "Traceback: secret"])
def test_unknown_machine_values_never_echo_input(value) -> None:
    assert status_label(value) == "未知状态"
    assert mode_label(value) == "未知模式"
    assert level_label(value) == "未知级别"
    assert category_label(value) == "其他事件"
    assert error_label(value) == ("未知错误，请查看详情" if value else "—")
    assert (
        event_summary({"category": value, "message": "private diagnostic"})
        == "其他事件，请查看详情"
    )


def test_absent_error_is_dash() -> None:
    assert error_label(None) == "—"


@pytest.mark.parametrize("mode", ["manual", "手动发送"])
def test_started_batch_preserves_count_and_translates_legacy_mode(mode) -> None:
    assert (
        event_summary(
            {
                "category": "batch_started",
                "message": f"批次开始，模式 {mode}，目标数量 12",
            }
        )
        == "批次开始，模式 手动发送，目标数量 12"
    )


@pytest.mark.parametrize("status", ["partial", "部分成功"])
def test_finished_batch_preserves_all_counts_and_translates_legacy_status(status) -> None:
    assert (
        event_summary(
            {
                "category": "batch_finished",
                "message": f"批次结束，状态 {status}，成功 12，失败 3，结果不确定 4，重复跳过 5",
            }
        )
        == "批次结束，状态 部分成功，成功 12，失败 3，结果不确定 4，重复跳过 5"
    )
    assert status_label(BatchStatus.PARTIAL) == "部分成功"


@pytest.mark.parametrize(
    ("category", "message", "expected"),
    [
        ("friend_search", "好友搜索完成，候选数量 9", "好友搜索完成，候选数量 9"),
        (
            "spark_scan",
            "火花只读扫描结束，状态 partial，候选数量 8",
            "火花只读扫描结束，状态 部分完成，候选数量 8",
        ),
        (
            "spark_scan",
            "火花只读扫描结束，状态 complete，候选数量 0",
            "火花只读扫描结束，状态 完整，候选数量 0",
        ),
        (
            "spark_contacts_imported",
            "火花好友导入完成：新增 3 位（停用），已有 2 位保持不变",
            "火花好友导入完成：新增 3 位（停用），已有 2 位保持不变",
        ),
        (
            "batch_started",
            "批次开始，模式 future_mode，目标数量 6",
            "批次开始，模式 未知模式，目标数量 6",
        ),
    ],
)
def test_known_local_summary_templates(category, message, expected) -> None:
    assert event_summary({"category": category, "message": message}) == expected


@pytest.mark.parametrize(
    "message",
    [
        "Traceback (most recent call last):\nRuntimeError: manual partial secret",
        "批次结束，状态 partial，成功 1，失败 2，结果不确定 3，重复跳过 4\nTraceback: secret",
        "批次结束，状态 partial，成功 private，失败 2，结果不确定 3，重复跳过 4",
    ],
)
def test_unrecognised_diagnostic_never_becomes_a_truncated_summary(message) -> None:
    event = {"category": "batch_finished", "message": message, "target_alias": "English昵称"}
    before = deepcopy(event)
    assert event_summary(MappingProxyType(event)) == "批次已结束，请查看详情"
    assert event == before


def test_known_summary_does_not_mutate_original_event() -> None:
    event = {
        "category": "batch_started",
        "message": "批次开始，模式 manual，目标数量 2",
        "target_alias": "English昵称（ID: 42）",
        "metadata": {"raw": "Traceback"},
    }
    before = deepcopy(event)
    assert "手动发送" in event_summary(event)
    assert event == before


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (AttemptStatus.UNKNOWN, "结果不确定，请人工核对聊天"),
        (AttemptStatus.DUPLICATE, "防重复已拦截，请核对发送记录"),
        (AttemptStatus.CANCELLED, "已取消"),
    ],
)
@pytest.mark.parametrize("mode", list(BatchMode))
@pytest.mark.parametrize("code", list(ErrorCode))
def test_normalised_outcome_precedes_original_error(mode, status, code, expected) -> None:
    assert progress_failure_label(mode, status, code) == expected


def test_failure_action_uses_batch_mode() -> None:
    assert (
        progress_failure_label(
            BatchMode.VALIDATION,
            AttemptStatus.FAILED,
            ErrorCode.TARGET_NOT_FOUND,
        )
        == "验证失败：未找到好友"
    )
    assert (
        progress_failure_label(
            BatchMode.MANUAL,
            AttemptStatus.FAILED,
            ErrorCode.INTERNAL_ERROR,
        )
        == "发送失败：运行异常，请查看详情"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("English昵称（ID: 42）", "English昵称（编号：42）"),
        ("English昵称(ID: 7)", "English昵称（编号：7）"),
        ("English昵称", "English昵称"),
        ("昵称（ID: words）", "昵称（ID: words）"),
        ("昵称（ID: 7）后缀", "昵称（ID: 7）后缀"),
        ("昵称(ID: 7）", "昵称(ID: 7）"),
        ("昵称（ID: 7）\n", "昵称（ID: 7）\n"),
    ],
)
def test_target_label_only_changes_generated_terminal_suffix(value, expected) -> None:
    assert target_label(value) == expected
