from __future__ import annotations

import asyncio
import queue
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMainWindow, QMessageBox, QPushButton, QWidget

from spark_keeper.models import (
    Account,
    FriendCandidate,
    SparkContact,
    SparkScanResult,
    SparkScanStatus,
    SparkState,
)
from spark_keeper.ui import theme
from spark_keeper.ui.app import SparkKeeperApp
from spark_keeper.ui.spark_preview import SparkImportPreview


def contact(
    key: str, state: SparkState = SparkState.ACTIVE, *, strong: bool = True, group: bool = False
) -> SparkContact:
    return SparkContact(
        candidate=FriendCandidate(
            stable_key=f"uid:{key}",
            display_name=f"好友 {key}",
            profile_url=f"https://www.douyin.com/user/{key}",
            evidence={
                "identity_strength": "strong" if strong else "weak",
                "user_id": key,
                "chat_type": "group" if group else "single",
            },
        ),
        spark_state=state,
        reason="测试页面识别依据",
        is_group=group,
    )


@pytest.fixture
def scan() -> SparkScanResult:
    return SparkScanResult(
        account_key="test-account",
        account_logged_in_at="2026-09-01T08:00:00",
        scanned_at="2026-09-01T09:00:00",
        status=SparkScanStatus.COMPLETE,
        contacts=(
            contact("new"),
            contact("saved"),
            contact("unknown", SparkState.UNKNOWN),
            contact("inactive", SparkState.INACTIVE),
            contact("weak", strong=False),
            contact("group", group=True),
            contact("recover", SparkState.RECOVER),
        ),
        scanned_count=7,
    )


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, scan: SparkScanResult, qapp):
    instance = SparkKeeperApp.__new__(SparkKeeperApp)
    QMainWindow.__init__(instance)
    instance._closed = False
    instance.busy = False
    instance._spark_preview = None
    instance._spark_scan_running = False
    instance._login_running = False
    instance.cancel_event = threading.Event()
    instance.cancel_button = QPushButton(instance)
    instance.messages = queue.Queue()
    instance._preview_timer = QTimer(instance)
    instance._poll_timer = QTimer(instance)
    instance.database = Mock()
    instance.database.get_account.return_value = Account(
        scan.account_key, "测试账号", scan.account_logged_in_at
    )
    instance.database.list_targets.return_value = [
        SimpleNamespace(stable_key="uid:saved", profile_url="", douyin_id="")
    ]
    instance.service = Mock()
    instance._set_status = Mock()
    instance._refresh_targets = Mock()
    instance._refresh_logs = Mock()
    instance._refresh_pending_indicator = Mock()
    monkeypatch.setattr(theme, "confirm", Mock(return_value=True))
    monkeypatch.setattr(theme, "show_message", Mock())
    try:
        yield instance
    finally:
        instance._close_spark_preview()
        instance._closed = True
        instance._poll_timer.stop()
        instance.deleteLater()
        instance._preview_timer.stop()
        qapp.processEvents()


@pytest.fixture
def preview(scan, qapp):
    parent = QWidget()
    dialog = SparkImportPreview(
        parent,
        scan,
        {"uid:saved"},
        validate=Mock(return_value=True),
        import_selected=Mock(),
        close=Mock(),
    )
    dialog.show()
    qapp.processEvents()
    try:
        yield dialog
    finally:
        dialog.close()
        parent.deleteLater()
        qapp.processEvents()


def row(preview, index):
    return preview.tree.topLevelItem(index)


def visible_keys(preview):
    return tuple(
        item.data(0, theme.ROLE_ID)
        for index in range(preview.tree.topLevelItemCount())
        if not (item := preview.tree.topLevelItem(index)).isHidden()
    )


def toggle(preview, index):
    item = row(preview, index)
    item.setCheckState(
        0,
        Qt.CheckState.Unchecked
        if item.checkState(0) == Qt.CheckState.Checked
        else Qt.CheckState.Checked,
    )


def test_default_selection_and_free_choice_across_all_states(preview, scan) -> None:
    defaults = {"uid:new", "uid:recover"}
    eligible = {"uid:new", "uid:saved", "uid:unknown", "uid:inactive", "uid:recover"}
    assert preview.selected_keys == defaults
    assert preview.eligible_keys == eligible
    assert row(preview, 1).text(4) == "是"
    for index, contact_item in enumerate(scan.contacts):
        was_selected = contact_item.candidate.stable_key in preview.selected_keys
        toggle(preview, index)
        assert (row(preview, index).checkState(0) == Qt.CheckState.Checked) != was_selected
        assert (contact_item.candidate.stable_key in preview.selected_keys) != was_selected
        toggle(preview, index)
    assert preview.selected_keys == defaults

    preview.filter_box.setCurrentText("只看新增")
    assert visible_keys(preview) == (
        "uid:new",
        "uid:unknown",
        "uid:inactive",
        "uid:weak",
        "uid:group",
        "uid:recover",
    )
    preview.select_all_button.click()
    assert "uid:saved" not in preview.selected_keys
    assert len(preview.selected_keys) == 6
    assert "含隐藏项" in preview.selection_label.text()
    preview.filter_box.setCurrentText("身份待确认/群聊")
    assert visible_keys(preview) == ("uid:weak", "uid:group")
    assert "当前显示 2 项" in preview.selection_label.text()
    preview.clear_button.click()
    assert preview.selected_keys == set()
    preview.filter_box.setCurrentText("可导入")
    assert visible_keys(preview) == (
        "uid:new",
        "uid:saved",
        "uid:unknown",
        "uid:inactive",
        "uid:recover",
    )
    preview.select_all_button.click()
    assert preview.selected_keys == eligible
    assert preview.eligible_keys == eligible


def test_recover_is_default_selected_and_keeps_separate_filter(preview) -> None:
    assert row(preview, 6).text(2) == "待恢复火花"
    assert "uid:recover" in preview.selected_keys
    assert "待恢复 1 项" in preview.status_label.text()
    preview.filter_box.setCurrentText("待恢复")
    assert visible_keys(preview) == ("uid:recover",)
    toggle(preview, 6)
    assert row(preview, 6).checkState(0) == Qt.CheckState.Unchecked
    preview.select_all()
    assert "uid:recover" in preview.selected_keys


def test_space_toggles_even_unverified_rows_without_hiding_selection(preview, qapp) -> None:
    preview.tree.setCurrentItem(row(preview, 4), 0)
    preview.tree.setFocus()
    QTest.keyClick(preview.tree, Qt.Key.Key_Space)
    qapp.processEvents()
    assert "uid:weak" in preview.selected_keys
    assert row(preview, 4).checkState(0) == Qt.CheckState.Checked
    preview.filter_box.setCurrentText("可导入")
    assert "uid:weak" in preview.selected_keys
    assert "已勾选 3 项（含隐藏项）" in preview.selection_label.text()


def test_busy_blocks_selection_filters_and_import_but_allows_close(preview) -> None:
    selected = preview.selected_keys.copy()
    preview.set_busy(True)
    for control in (
        preview.tree,
        preview.filter_box,
        preview.select_all_button,
        preview.clear_button,
        preview.import_button,
    ):
        assert not control.isEnabled()
    preview.select_all()
    preview.clear_selection()
    toggle(preview, 4)
    preview.filter_box.setCurrentText("可导入")
    preview.import_button.click()
    assert preview.selected_keys == selected
    assert row(preview, 4).checkState(0) == Qt.CheckState.Unchecked
    assert preview.filter_box.currentText() == "全部"
    preview._import_selected.assert_not_called()
    preview.set_busy(False)
    preview.import_button.click()
    preview._import_selected.assert_called_once_with()
    preview.set_busy(True)
    preview.close_button.click()
    assert not preview.isVisible()
    preview._close_callback.assert_called_once_with()


def test_close_callback_and_parent_validation_do_not_reenter(preview) -> None:
    selected = preview.selected_keys.copy()
    callback = Mock(side_effect=preview.close)
    preview._close_callback = callback

    def invalidate():
        preview.close()
        return False

    preview.validate = Mock(side_effect=invalidate)
    toggle(preview, 4)
    assert preview.selected_keys == selected
    assert not preview.isVisible()
    callback.assert_called_once_with()
    preview.select_all()
    preview.clear_selection()
    preview._request_import()
    preview.close()
    assert preview.selected_keys == selected
    preview.validate.assert_called_once_with()
    preview._import_selected.assert_not_called()
    callback.assert_called_once_with()


def test_empty_eligible_result_explains_identity_limits(scan, qapp) -> None:
    scan = replace(scan, contacts=(contact("weak", strong=False), contact("group", group=True)))
    parent = QWidget()
    preview = SparkImportPreview(
        parent, scan, set(), validate=lambda: True, import_selected=Mock(), close=Mock()
    )
    try:
        assert not preview.eligible_keys
        assert not preview.selected_keys
        text = preview.limits_label.text()
        for phrase in (
            "没有满足导入身份条件",
            "明确单聊",
            "可靠稳定身份",
            "单人搜索",
            "浏览器捕获",
        ):
            assert phrase in text
        preview.select_all()
        assert preview.selected_keys == {"uid:weak", "uid:group"}
    finally:
        preview.close()
        parent.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize(
    "changed_account",
    [None, Account("other", "其他账号", "time"), Account("test-account", "原账号", "new-login")],
)
def test_changed_account_invalidates_preview_before_selection(app, scan, changed_account) -> None:
    app._show_spark_preview(scan)
    preview = app._spark_preview
    app.database.get_account.return_value = changed_account
    preview.select_all()
    assert app._spark_preview is None
    assert not preview.isVisible()
    app.service.import_spark_contacts.assert_not_called()
    assert "重新扫描" in theme.show_message.call_args.args[2]
    assert theme.show_message.call_args.kwargs["icon"] == QMessageBox.Icon.Critical


def test_stale_scan_never_opens_preview(app, scan) -> None:
    app.database.get_account.return_value = Account("other", "其他账号", "time")
    app._show_spark_preview(scan)
    assert app._spark_preview is None
    assert "已失效" in theme.show_message.call_args.args[2]
    assert theme.show_message.call_args.kwargs["icon"] == QMessageBox.Icon.Critical


def test_database_error_closes_preview_and_preserves_details(app, scan) -> None:
    app._show_spark_preview(scan)
    app.database.get_account.side_effect = sqlite3.OperationalError("private-cookie-value")
    app._refresh_logs.side_effect = sqlite3.OperationalError("private-path")
    app._import_spark_contacts()
    assert app._spark_preview is None
    detail = theme.show_message.call_args.args[2]
    assert "OperationalError: private-cookie-value" in detail
    assert "Traceback" in detail
    recorded = app.database.record_event.call_args.args
    assert recorded[:2] == ("ERROR", "ui_error")
    assert "OperationalError: private-cookie-value" in recorded[2]
    assert detail.startswith(recorded[2])
    assert "OperationalError: private-path" in detail
    assert app.database.record_event.call_count == 1
    assert theme.show_message.call_args.kwargs["icon"] == QMessageBox.Icon.Critical
    app.service.import_spark_contacts.assert_not_called()
    theme.confirm.assert_not_called()


@pytest.mark.parametrize(
    "status, expected",
    [(SparkScanStatus.PARTIAL, "部分结果"), (SparkScanStatus.CANCELLED, "已取消")],
)
def test_partial_and_cancelled_are_not_presented_as_complete(app, scan, status, expected) -> None:
    app._show_spark_preview(replace(scan, status=status, detail="达到读取边界"))
    preview = app._spark_preview
    assert expected in preview.status_label.text()
    assert f"已扫描 {scan.scanned_count} 项" in preview.status_label.text()
    assert "扫描结束" not in preview.status_label.text()
    assert "达到读取边界" in preview.limits_label.text()
    assert app._set_status.call_args.args[1] == "warn"


def test_import_uses_scan_selected_keys_and_reports_counts_without_sending(app, scan) -> None:
    app._show_spark_preview(scan)
    app._spark_preview.filter_box.setCurrentText("可导入")
    app._spark_preview.select_all()
    app._start_async = Mock()
    app.service.import_spark_contacts.return_value = (4, 1)
    app._import_spark_contacts()
    _, operation, complete = app._start_async.call_args.args
    confirmation = theme.confirm.call_args.args[2]
    assert "默认停用" in confirmation and "不发送" in confirmation and "保持不变" in confirmation
    result = asyncio.run(operation())
    app.service.import_spark_contacts.assert_called_once_with(
        scan, ("uid:inactive", "uid:new", "uid:recover", "uid:saved", "uid:unknown")
    )
    complete(result)
    assert app._spark_preview is None
    app._refresh_targets.assert_called_once()
    assert "新增 4 位，已存在 1 位" in theme.show_message.call_args.args[2]
    assert (
        theme.show_message.call_args.kwargs.get("icon", QMessageBox.Icon.Information)
        == QMessageBox.Icon.Information
    )
    app.service.run_batch.assert_not_called()


def test_account_changes_during_confirmation_prevent_import(app, scan) -> None:
    app._show_spark_preview(scan)
    app._start_async = Mock()

    def confirm(*args, **kwargs):
        app.database.get_account.return_value = Account("other", "其他账号", "time")
        return True

    theme.confirm.side_effect = confirm
    app._import_spark_contacts()
    app._start_async.assert_not_called()
    assert app._spark_preview is None
    theme.show_message.assert_called_once()
    assert theme.show_message.call_args.kwargs["icon"] == QMessageBox.Icon.Critical


def test_import_database_failure_uses_async_full_error_path(app, scan) -> None:
    app._show_spark_preview(scan)
    app._start_async = Mock()
    app.service.import_spark_contacts.side_effect = sqlite3.OperationalError("private-cookie-value")
    app._import_spark_contacts()
    _, operation, _ = app._start_async.call_args.args
    with pytest.raises(sqlite3.OperationalError) as error:
        asyncio.run(operation())
    app._finish_busy = Mock()
    app.messages.put(("error", "导入火花好友（不发送）", error.value))
    app._poll_messages()
    app._finish_busy.assert_called_once()
    theme.show_message.assert_called_once()
    detail = theme.show_message.call_args.args[2]
    assert "OperationalError: private-cookie-value" in detail
    assert "Traceback" in detail
    assert app.database.record_event.call_args.args == ("ERROR", "ui_error", detail)
    assert theme.show_message.call_args.kwargs["icon"] == QMessageBox.Icon.Critical


def test_new_scan_discards_preview_and_wires_readonly_progress_and_cancel(app, scan) -> None:
    app._show_spark_preview(scan)
    old_preview = app._spark_preview
    app._start_async = Mock()

    async def scan_contacts(*, progress, cancel):
        assert cancel is app.cancel_event
        progress("只读扫描中")
        return scan

    app.service.scan_spark_contacts = scan_contacts
    app._scan_spark_contacts()
    assert app._spark_preview is None
    assert not old_preview.isVisible()
    _, operation, _ = app._start_async.call_args.args
    assert asyncio.run(operation()) is scan
    assert app.messages.get_nowait() == ("status", "只读扫描中")
    assert app.cancel_button.text() == "取消扫描"
    app._cancel_current()
    assert app.cancel_event.is_set()
    assert "不会发送消息" in app._set_status.call_args.args[0]
    app.service.run_batch.assert_not_called()


@pytest.mark.parametrize("match_by", ["profile_url", "douyin_id"])
def test_existing_equivalent_identity_is_not_default_selected(app, scan, match_by) -> None:
    candidate = replace(scan.contacts[0].candidate, douyin_id="new-account")
    scan = replace(scan, contacts=(replace(scan.contacts[0], candidate=candidate),))
    saved = SimpleNamespace(stable_key="different-key", profile_url="", douyin_id="")
    setattr(saved, match_by, getattr(candidate, match_by))
    app.database.list_targets.return_value = [saved]
    app._show_spark_preview(scan)
    preview = app._spark_preview
    assert row(preview, 0).text(4) == "是"
    assert preview.selected_keys == set()
    preview.filter_box.setCurrentText("只看新增")
    assert visible_keys(preview) == ()
    preview.select_all()
    assert preview.selected_keys == set()


@pytest.mark.parametrize("index", [4, 5])
def test_selected_unverified_identity_is_explicitly_rejected_not_silently_filtered(
    app, scan, index
) -> None:
    app._show_spark_preview(scan)
    toggle(app._spark_preview, index)
    selected = app._spark_preview.selected_keys.copy()
    assert scan.contacts[index].candidate.stable_key in selected
    app._start_async = Mock()
    app._import_spark_contacts()
    app._start_async.assert_not_called()
    theme.confirm.assert_not_called()
    assert "本次不会导入任何记录" in theme.show_message.call_args.args[2]
    assert theme.show_message.call_args.kwargs["icon"] == QMessageBox.Icon.Warning
    assert app._spark_preview.selected_keys == selected
    app.service.run_batch.assert_not_called()


def test_scan_details_are_displayed_as_literal_text(app, scan) -> None:
    detail = "<b>这是扫描原文，不是格式标签</b>\n下一行"
    app._show_spark_preview(replace(scan, detail=detail))
    label = app._spark_preview.limits_label
    assert detail in label.text()
    assert label.textFormat() == Qt.TextFormat.PlainText
