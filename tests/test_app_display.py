from __future__ import annotations

import asyncio
import sqlite3
import threading
from dataclasses import replace
from time import monotonic
from unittest.mock import AsyncMock, Mock

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFrame,
    QLineEdit,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
)

from spark_keeper.automation.errors import AutomationError
from spark_keeper.logging_safe import format_error
from spark_keeper.models import (
    AttemptStatus,
    BatchMode,
    ErrorCode,
    FriendCandidate,
    MessageKind,
    Target,
)
from spark_keeper.scheduler import SchedulerError
from spark_keeper.ui import app as app_module
from spark_keeper.ui import theme
from spark_keeper.ui.app import SparkKeeperApp


@pytest.fixture
def app(tmp_path, monkeypatch, qapp):
    monkeypatch.setenv("SPARK_KEEPER_ROOT", str(tmp_path))
    threads = []
    start_thread = threading.Thread

    def track_thread(*args, **kwargs):
        thread = start_thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(app_module.threading, "Thread", track_thread)
    monkeypatch.setattr(app_module.TaskScheduler, "exists", lambda self: False)
    instance = SparkKeeperApp(smoke_mode=True)
    instance._poll_timer.stop()
    message = instance.messages.get(timeout=10)
    assert message[0] == "scheduler_snapshot"
    instance.messages.put(message)
    instance._poll_messages()
    yield instance
    instance.busy = False
    instance.close()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    instance.deleteLater()
    qapp.processEvents()


def wait_until(predicate):
    deadline = monotonic() + 10
    while not predicate():
        QApplication.processEvents()
        if monotonic() >= deadline:
            pytest.fail("Qt did not deliver the expected window event")


def show_app(app):
    app.show()
    wait_until(lambda: app.isVisible() and app.page_stack.width() > 1)
    QApplication.processEvents()


def keys(tree):
    return tuple(
        str(tree.topLevelItem(i).data(0, theme.ROLE_ID)) for i in range(tree.topLevelItemCount())
    )


def item(tree, key):
    return next(
        tree.topLevelItem(i)
        for i in range(tree.topLevelItemCount())
        if str(tree.topLevelItem(i).data(0, theme.ROLE_ID)) == key
    )


def test_login_actions_are_mutually_exclusive_and_remain_disabled_while_busy(app):
    assert not app.login_button.isHidden()
    assert app.logout_button.isHidden()
    app.database.save_account("fixture-account", "测试账号")
    app.state_store = Mock()
    app.state_store.exists.return_value = True
    app._refresh_account()
    assert app.login_button.isHidden()
    assert not app.logout_button.isHidden()
    app._set_task_buttons_state(False)
    assert not app.login_button.isEnabled()
    assert not app.logout_button.isEnabled()
    app.database.clear_account()
    app._refresh_account()
    assert not app.login_button.isHidden()
    assert app.logout_button.isHidden()
    assert not app.login_button.isEnabled()


def test_navigation_shows_one_native_page_and_preserves_unsaved_message(app):
    app.message_text.setPlainText("未保存的编辑")
    show_app(app)
    for _ in range(3):
        for key, button in app._nav_buttons.items():
            assert isinstance(button, QPushButton)
            button.click()
            QApplication.processEvents()
            assert app._current_page == key
            assert app.page_stack.currentWidget() is app.pages[key]
            assert {name for name, page in app.pages.items() if page.isVisible()} == {key}
            assert {name for name, nav in app._nav_buttons.items() if nav.isChecked()} == {key}
    assert app.message_text.toPlainText() == "未保存的编辑"


def test_only_progress_table_omits_inner_border(app):
    assert app.progress_tree.property("bordered") is False
    assert app.progress_tree.frameShape() == QFrame.Shape.NoFrame
    for tree in (app.candidate_tree, app.target_tree, app.history_tree, app.logs_tree):
        assert tree.property("bordered") is True
        assert tree.frameShape() == QFrame.Shape.StyledPanel


@pytest.fixture
def blocked_reader():
    releases = []
    workers = []

    def create(rows, *, error=None):
        started = threading.Event()
        release = threading.Event()
        releases.append(release)
        calls = []

        def reader():
            assert threading.current_thread() is not threading.main_thread()
            calls.append(threading.get_ident())
            workers.append(threading.current_thread())
            started.set()
            assert release.wait(10), "Test did not release the page read"
            if error is not None:
                raise error
            return rows

        return reader, started, release, calls

    yield create
    for release in releases:
        release.set()
    for worker in workers:
        worker.join(10)
        assert not worker.is_alive()


def accept_next_snapshot(app):
    message = app.messages.get(timeout=10)
    assert message[0] == "page_snapshot"
    app.messages.put(message)
    app._poll_messages()


def target(identifier, name=None, *, enabled=True):
    return Target(
        id=identifier,
        stable_key=f"fixture:{identifier}",
        display_name=name or f"测试好友 {identifier}",
        douyin_id="",
        profile_url="",
        avatar_url="",
        search_query="测试",
        evidence={},
        enabled=enabled,
        confirmed_at="2026-01-01",
    )


def test_page_read_does_not_block_navigation_or_qt_events(app, monkeypatch, blocked_reader):
    reader, started, release, calls = blocked_reader([target(1)])
    monkeypatch.setattr(app.database, "list_targets", reader)
    app.message_text.setPlainText("未保存正文")
    app._show_page("friends")
    assert started.wait(10)
    assert not release.is_set()
    assert len(calls) == 1
    event_ran = []
    QTimer.singleShot(0, lambda: event_ran.append(True))
    wait_until(lambda: bool(event_ran))
    app._show_page("home")
    assert app.page_stack.currentWidget() is app.pages["home"]
    release.set()
    accept_next_snapshot(app)
    assert keys(app.target_tree) == ()
    assert app.message_text.toPlainText() == "未保存正文"


def test_same_page_has_no_layout_or_read_work(app, monkeypatch):
    changed = Mock()
    app.page_stack.currentChanged.connect(changed)
    request = Mock(wraps=app._request_page_snapshot)
    monkeypatch.setattr(app, "_request_page_snapshot", request)
    app._show_page("home")
    changed.assert_not_called()
    request.assert_not_called()


def test_navigation_displays_destination_before_background_read(app, monkeypatch):
    show_app(app)
    observations = []

    def request(key):
        observations.append((key, app.page_stack.currentWidget(), app.pages[key].isVisible()))

    monkeypatch.setattr(app, "_request_page_snapshot", request)
    app._show_page("friends")
    assert observations == [("friends", app.pages["friends"], True)]
    assert not app.pages["home"].isVisible()
    assert not app._nav_buttons["home"].isChecked()
    assert all(
        button.focusPolicy() != Qt.FocusPolicy.NoFocus for button in app._nav_buttons.values()
    )


def test_quick_switches_coalesce_and_discard_old_visit(app, monkeypatch, blocked_reader):
    old, started, release, calls = blocked_reader([target(1, "旧快照")])
    new, new_started, new_release, new_calls = blocked_reader([target(2, "新快照")])
    monkeypatch.setattr(app.database, "list_targets", old)
    app._show_page("friends")
    assert started.wait(10)
    for _ in range(12):
        app._show_page("home")
        app._show_page("friends")
    assert len(calls) == len(app._page_reads) == 1
    monkeypatch.setattr(app.database, "list_targets", new)
    release.set()
    accept_next_snapshot(app)
    assert keys(app.target_tree) == ()
    assert new_started.wait(10)
    assert len(new_calls) == 1
    new_release.set()
    accept_next_snapshot(app)
    assert keys(app.target_tree) == ("2",)


def test_local_refresh_invalidates_inflight_snapshot(app, monkeypatch, blocked_reader):
    reader, started, release, _ = blocked_reader([target(1, "旧名称")])
    monkeypatch.setattr(app.database, "list_targets", reader)
    app._show_page("friends")
    assert started.wait(10)
    monkeypatch.setattr(app.database, "list_targets", lambda: [target(1, "已保存名称")])
    app._refresh_targets()
    assert item(app.target_tree, "1").text(1) == "已保存名称"
    replacement, replacement_started, replacement_release, _ = blocked_reader(
        [target(1, "已保存名称")]
    )
    monkeypatch.setattr(app.database, "list_targets", replacement)
    release.set()
    accept_next_snapshot(app)
    assert item(app.target_tree, "1").text(1) == "已保存名称"
    assert replacement_started.wait(10)
    replacement_release.set()
    accept_next_snapshot(app)
    assert item(app.target_tree, "1").text(1) == "已保存名称"


@pytest.mark.parametrize("leave_page", [False, True])
def test_page_read_error_preserves_details_and_is_never_modal(
    app, monkeypatch, blocked_reader, leave_page
):
    reader, started, release, _ = blocked_reader(
        [], error=sqlite3.OperationalError("private-cookie-and-path")
    )
    monkeypatch.setattr(app.database, "list_targets", reader)
    popup = Mock()
    monkeypatch.setattr(theme, "show_message", popup)
    app._show_page("friends")
    assert started.wait(10)
    if leave_page:
        app._show_page("home")
    previous_status = app.status_label.text()
    release.set()
    accept_next_snapshot(app)
    popup.assert_not_called()
    assert app.status_label.text() == (
        previous_status if leave_page else "页面读取失败；完整诊断见运行日志"
    )
    events = app.database.list_events()
    if leave_page:
        assert not events
    else:
        assert events[0]["category"] == "page_read_failed"
        assert "OperationalError: private-cookie-and-path" in events[0]["message"]
        assert "Traceback" in events[0]["message"]
        assert app.logs_message.toPlainText() == events[0]["message"]


def history_row():
    return {
        "id": "attempt-1",
        "message_text": "完整正文",
        "started_at": "2026-01-01",
        "target_name": "测试好友",
        "status": "success",
        "mode": "manual",
        "manual_override": False,
        "error_code": None,
    }


@pytest.mark.parametrize("page", ["friends", "history", "logs"])
def test_identical_rows_do_not_rebuild_or_mutate_tree(app, page):
    if page == "friends":
        rows = [target(i) for i in range(1, 5)]
        display, tree = app._display_targets, app.target_tree
    elif page == "history":
        rows = [history_row()]
        display, tree = app._display_history, app.history_tree
    else:
        rows = [
            {
                "id": 1,
                "created_at": "2026-01-01",
                "level": "INFO",
                "target_alias": "",
                "category": "fixture",
                "message": "测试日志",
            }
        ]
        display, tree = app._display_logs, app.logs_tree
    display(rows)
    before = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    changes = Mock()
    for signal in (
        tree.model().dataChanged,
        tree.model().rowsInserted,
        tree.model().rowsRemoved,
        tree.model().rowsMoved,
    ):
        signal.connect(changes)
    display(rows)
    changes.assert_not_called()
    assert before == [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]


def test_difference_updates_preserve_selection_focus_and_scroll(app, monkeypatch):
    monkeypatch.setattr(app, "_request_page_snapshot", Mock())
    app._show_page("friends")
    show_app(app)
    rows = [target(i) for i in range(1, 101)]
    app._display_targets(rows)
    QApplication.processEvents()
    selected = item(app.target_tree, "30")
    app.target_tree.setCurrentItem(selected)
    second_selected = item(app.target_tree, "32")
    second_selected.setSelected(True)
    app.target_tree.verticalScrollBar().setValue(app.target_tree.verticalScrollBar().maximum() // 4)
    before = app.target_tree.verticalScrollBar().value()
    assert before > 0
    rows[29] = replace(rows[29], display_name="更新的名称", enabled=False)
    app._display_targets(rows)
    assert app.target_tree.currentItem() is selected
    assert app._selected_keys(app.target_tree) == ["30", "32"]
    assert app.target_tree.verticalScrollBar().value() == before
    assert selected.text(1) == "更新的名称"
    assert selected.text(0) == "否"
    assert selected.foreground(0).color().name() == theme.TEXT_MUTED
    app._display_targets([target(101)] + rows[:-1])
    assert keys(app.target_tree)[0] == "101"
    assert "100" not in keys(app.target_tree)
    assert app.target_tree.currentItem() is selected
    assert app._selected_keys(app.target_tree) == ["30", "32"]
    assert app.target_tree.verticalScrollBar().value() == before


def test_history_removal_clears_body_and_unchanged_data_preserves_body(app):
    row = history_row()
    app._display_history([row])
    app.history_tree.setCurrentItem(item(app.history_tree, "attempt-1"))
    app._show_history_message()
    assert app.history_message.toPlainText() == "完整正文"
    changed = Mock()
    app.history_message.textChanged.connect(changed)
    app._display_history([row])
    changed.assert_not_called()
    app._display_history([])
    assert app.history_messages == {}
    assert app.history_message.toPlainText() == ""


def test_close_during_read_has_no_worker_qt_calls(app, monkeypatch, blocked_reader):
    reader, started, release, _ = blocked_reader([target(1)])
    monkeypatch.setattr(app.database, "list_targets", reader)
    app._show_page("friends")
    assert started.wait(10)
    app._on_close()
    assert app._closed
    display = Mock(side_effect=AssertionError("Tree used after close"))
    monkeypatch.setattr(app, "_display_targets", display)
    release.set()
    message = app.messages.get(timeout=10)
    assert message[0] == "page_snapshot"
    app.messages.put(message)
    app._poll_messages()
    display.assert_not_called()
    assert not any(
        timer.isActive()
        for timer in (app._poll_timer, app._preview_timer, app._capture_timer, app._pending_timer)
    )


def test_independent_page_reads_never_paint_the_wrong_page(app, monkeypatch, blocked_reader):
    friends, friends_started, friends_release, friends_calls = blocked_reader([target(1)])
    logs, logs_started, logs_release, logs_calls = blocked_reader(
        [
            {
                "id": 7,
                "created_at": "2026-01-01",
                "level": "INFO",
                "target_alias": "",
                "category": "fixture",
                "message": "当前页日志",
            }
        ]
    )
    monkeypatch.setattr(app.database, "list_targets", friends)
    monkeypatch.setattr(app.database, "list_events", logs)
    app._show_page("friends")
    assert friends_started.wait(10)
    app._show_page("logs")
    assert logs_started.wait(10)
    assert len(friends_calls) == len(logs_calls) == 1
    friends_release.set()
    accept_next_snapshot(app)
    assert app._current_page == "logs"
    assert keys(app.target_tree) == keys(app.logs_tree) == ()
    logs_release.set()
    accept_next_snapshot(app)
    assert keys(app.logs_tree) == ("7",)
    assert keys(app.target_tree) == ()


def test_native_stack_resizes_and_initial_fast_navigation_keeps_latest_page(app, monkeypatch):
    monkeypatch.setattr(app, "_request_page_snapshot", Mock())
    app._show_page("history")
    app._show_page("friends")
    show_app(app)
    app.resize(1240, 850)
    QApplication.processEvents()
    assert app.page_stack.currentWidget() is app.pages["friends"]
    assert app.pages["friends"].size() == app.page_stack.contentsRect().size()
    assert {key for key, page in app.pages.items() if page.isVisible()} == {"friends"}


def test_inactive_pages_are_excluded_from_keyboard_traversal(app, monkeypatch):
    monkeypatch.setattr(app, "_request_page_snapshot", Mock())
    show_app(app)
    app.message_text.setFocus()
    app._show_page("friends")
    for _ in range(40):
        app.focusNextChild()
        focused = QApplication.focusWidget()
        if focused is not None:
            assert not app.pages["home"].isAncestorOf(focused)
            assert not app.pages["history"].isAncestorOf(focused)
            assert not app.pages["logs"].isAncestorOf(focused)
    app._show_page("home")
    assert app.message_text.focusPolicy() != Qt.FocusPolicy.NoFocus


def test_closing_before_first_show_stops_all_timers_and_ignores_late_results(app):
    callback = Mock()
    app.close()
    app.messages.put(("complete", "late", callback, None))
    app.messages.put(("status", "late private state"))
    app._poll_messages()
    QApplication.processEvents()
    callback.assert_not_called()
    assert app._closed
    assert app.status_label.text() == "就绪"
    assert not any(
        timer.isActive()
        for timer in (app._poll_timer, app._preview_timer, app._capture_timer, app._pending_timer)
    )


def test_busy_close_requests_cancellation_but_keeps_current_step_alive(app, monkeypatch):
    monkeypatch.setattr(theme, "confirm", Mock(return_value=True))
    app.busy = True
    app.close()
    assert app.cancel_event.is_set()
    assert not app._closed
    app.busy = False
    app.close()
    assert app._closed


def test_closed_window_is_not_resurrected_by_activation(app, monkeypatch):
    activate = Mock()
    monkeypatch.setattr(app_module, "activate_window", activate)
    app.close()
    app.activate_existing_window()
    activate.assert_not_called()
    assert not app.isVisible()


@pytest.mark.parametrize("confirm_stop", [True, False])
def test_busy_rejected_close_keeps_gui_lease_and_activation(app, monkeypatch, confirm_stop):
    import uuid

    from spark_keeper.ui.single_instance import GuiSingleInstance, InstanceNames

    identity = f"SparkKeeper.Gui.CloseTest.{uuid.uuid4().hex}"
    names = InstanceNames(f"Local\\{identity}", identity)
    owner = GuiSingleInstance(names)
    contender = GuiSingleInstance(names)
    activate = Mock()
    monkeypatch.setattr(app_module, "activate_window", activate)
    monkeypatch.setattr(theme, "confirm", Mock(return_value=confirm_stop))
    try:
        assert owner.start_or_activate()
        owner.set_activate_callback(app.activate_existing_window)
        app.busy = True
        app.close()
        assert not app._closed
        assert owner._server.isListening()
        with pytest.raises(RuntimeError, match="不会另开窗口"):
            contender.start_or_activate(timeout_ms=0)
        app.activate_existing_window()
        activate.assert_called_once_with(app)
    finally:
        app.busy = False
        app.close()
        contender.close()
        owner.close()


def test_visible_version_and_program_directory_do_not_follow_data_override(app):
    from pathlib import Path

    from spark_keeper import __version__

    assert __version__ in app.windowTitle()
    assert __version__ in app.version_label.text()
    assert app.program_path_label.toolTip() == str(Path(app_module.__file__).resolve().parents[3])
    assert str(app.paths.root) not in app.program_path_label.text()


@pytest.fixture
def app_with_long_program_directory(tmp_path, monkeypatch, request):
    directory = tmp_path / ("很长的中文 程序安装目录 " * 6) / "续火花助手"
    monkeypatch.setattr(app_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_module.sys, "executable", str(directory / "SparkKeeper.exe"))
    return request.getfixturevalue("app"), directory


@pytest.mark.parametrize("working", [False, True])
def test_program_directory_stays_on_one_line_through_window_resizes(
    app_with_long_program_directory, working
):
    app, directory = app_with_long_program_directory
    label = app.program_path_label
    full_text = f"程序目录：{directory}"
    app.status_label.setText("正在执行测试任务" if working else "就绪")
    app.pending_label.setText("等待确认" if working else "")
    app.status_progress.setVisible(working)
    app.cancel_button.setEnabled(working)
    show_app(app)
    displayed = []
    for size in ((940, 650), (1120, 780), None, (940, 650)):
        if size is None:
            app.showMaximized()
        else:
            app.showNormal()
            app.resize(*size)
        QApplication.processEvents()
        if size is not None:
            assert (app.width(), app.height()) == size
        assert (app.minimumWidth(), app.minimumHeight()) == (940, 650)
        assert not label.wordWrap()
        assert not label.hasHeightForWidth()
        assert label.textFormat() == Qt.TextFormat.PlainText
        assert label.toolTip() == str(directory)
        assert label.text().startswith("程序目录：")
        assert label.text().endswith("…")
        assert label.text() == label.fontMetrics().elidedText(
            full_text, Qt.TextElideMode.ElideRight, label.contentsRect().width()
        )
        assert label.fontMetrics().horizontalAdvance(label.text()) <= label.contentsRect().width()
        assert label.sizeHint().height() <= label.height()
        for other in (app.status_label, app.pending_label, app.status_progress, app.cancel_button):
            if other.isVisible():
                assert not label.geometry().intersects(other.geometry())
                assert other.parentWidget().rect().contains(other.geometry())
        assert app.cancel_button.width() >= app.cancel_button.sizeHint().width()
        assert app.status_label.width() >= app.status_label.sizeHint().width()
        assert app.pending_label.width() >= app.pending_label.sizeHint().width()
        displayed.append(label.text())
    assert displayed[0] == displayed[-1]
    assert len(displayed[1]) > len(displayed[0])


def test_program_directory_reappears_in_full_when_space_becomes_available(qapp, tmp_path):
    directory = app_module.Path(tmp_path.anchor) / "中文 空格目录"
    label = app_module._ProgramDirectoryLabel(directory)
    try:
        label.resize(label.minimumWidth(), 40)
        label.show()
        qapp.processEvents()
        assert label.text().endswith("…")
        label.resize(440, 40)
        qapp.processEvents()
        assert label.text() == f"程序目录：{directory}"
        font = label.font()
        font.setPointSize(font.pointSize() + 2)
        label.setFont(font)
        qapp.processEvents()
        assert label.minimumWidth() == label.fontMetrics().horizontalAdvance("程序目录：…")
        assert label.text() == f"程序目录：{directory}"
        assert label.toolTip() == str(directory)
    finally:
        label.close()
        label.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize(
    ("callback", "args"),
    [
        ("_logout", ()),
        ("_save_selected_candidate", ()),
        ("_set_selected_targets_enabled", (True,)),
        ("_set_selected_targets_enabled", (False,)),
        ("_delete_targets", ()),
        ("_select_all_targets", ()),
        ("_clear_target_selection", ()),
        ("_save_plan", ()),
        ("_validate", ()),
        ("_manual_send", ()),
        ("_clear_history", ()),
    ],
)
@pytest.mark.parametrize("guard", ["busy", "_closed"])
def test_guarded_mutations_do_not_change_business_state(app, monkeypatch, callback, args, guard):
    setattr(app, guard, True)
    database = Mock(side_effect=AssertionError("Busy callback accessed database"))
    service = Mock(side_effect=AssertionError("Busy callback accessed backend"))
    monkeypatch.setattr(app, "database", database)
    monkeypatch.setattr(app, "service", service)
    getattr(app, callback)(*args)
    assert not database.mock_calls
    assert not service.mock_calls
    setattr(app, guard, False)


def test_progress_queue_updates_existing_native_row(app):
    app.messages.put(("progress", "target-1", "开始"))
    app._poll_messages()
    first = item(app.progress_tree, "target-1")
    app.messages.put(("progress", "target-1", "完成"))
    app._poll_messages()
    assert item(app.progress_tree, "target-1") is first
    assert first.text(1) == "完成"
    assert app.progress_hint.isHidden()
    app._clear_progress()
    assert keys(app.progress_tree) == ()


def test_dynamic_labels_preserve_literal_markup(app):
    text = "<b>好友 &amp; 正文</b>\n第二行"
    label = app._label(app.centralWidget().layout(), text)
    assert label.text() == text
    assert label.textFormat() == Qt.TextFormat.PlainText


@pytest.mark.parametrize("action", ["manual", "plan", "missed", "candidate", "duplicate"])
def test_confirmation_previews_preserve_markup_and_decline_prevents_actions(
    app, monkeypatch, action
):
    message = "<b>正文 &amp; 保留</b>\n<img src='private-file'>"
    friend_name = "<i>好友 &amp; 昵称</i>"
    candidate = FriendCandidate(
        "fixture:html",
        friend_name,
        douyin_id="<u>literal-id</u>",
        evidence={"identity_strength": "strong"},
    )
    database = Mock()
    database.list_targets.return_value = [target(1, friend_name)]
    database.get_plan.return_value = app.database.get_plan()
    database.has_daily_guard.return_value = True
    database.list_pending_actions.return_value = [
        {
            "id": "missed-1",
            "kind": "missed_schedule",
            "payload": {"target_count": 1, "message_text": message},
        }
    ]
    monkeypatch.setattr(app, "database", database)
    monkeypatch.setattr(app, "service", Mock())
    monkeypatch.setattr(app, "_persist_plan", Mock())
    monkeypatch.setattr(app, "_start_async", Mock())
    monkeypatch.setattr(app, "_run_missed_action", Mock())
    monkeypatch.setattr(app, "_refresh_pending_indicator", Mock())
    monkeypatch.setattr(app_module, "detect_missed_schedule", Mock())
    monkeypatch.setattr(theme, "confirm", Mock(return_value=False))
    monkeypatch.setattr(theme, "show_message", Mock())
    app.message_text.setPlainText(message)
    if action == "plan":
        app.schedule_enabled.setChecked(True)
        app._save_plan()
    elif action == "missed":
        app._check_pending_actions()
        database.resolve_pending_action.assert_called_once_with("missed-1", "skipped")
    elif action == "candidate":
        app._show_candidates([candidate])
        app.candidate_tree.topLevelItem(0).setSelected(True)
        app._save_selected_candidate()
    else:
        if action == "duplicate":
            theme.confirm.side_effect = [True, False]
        app._manual_send()
    preview = theme.confirm.call_args.args[2]
    if action in {"candidate", "duplicate"}:
        assert friend_name in preview
    else:
        assert message in preview
    if action == "manual":
        assert friend_name in preview
    if action == "candidate":
        assert candidate.douyin_id in preview
    if action == "duplicate":
        assert theme.confirm.call_count == 2
        assert message in theme.confirm.call_args_list[0].args[2]
        app._persist_plan.assert_not_called()
    else:
        theme.confirm.assert_called_once()
        app._persist_plan.assert_not_called()
    theme.show_message.assert_not_called()
    app.service.save_friend.assert_not_called()
    app.service.run_batch.assert_not_called()
    app._start_async.assert_not_called()
    app._run_missed_action.assert_not_called()


def save_targets(app, count=3):
    targets = [
        app.database.add_target(
            FriendCandidate(f"friend:{i}", f"好友 <{i}>", douyin_id=f"friend-{i}"), "好友"
        )
        for i in range(count)
    ]
    app._refresh_targets()
    return targets


def test_saved_targets_support_multiselect_select_all_and_clear_only(app):
    targets = save_targets(app)
    assert app.target_tree.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection
    for tree in (app.candidate_tree, app.history_tree, app.logs_tree, app.progress_tree):
        assert tree.selectionMode() == QAbstractItemView.SelectionMode.SingleSelection
    item(app.target_tree, str(targets[0].id)).setSelected(True)
    item(app.target_tree, str(targets[2].id)).setSelected(True)
    assert set(app._selected_keys(app.target_tree)) == {str(targets[0].id), str(targets[2].id)}
    assert app.target_selection_label.text() == "已选 2/3 位"
    app.target_selection_button.click()
    assert len(app.target_tree.selectedItems()) == 3
    assert app.target_selection_label.text() == "已选 3/3 位"
    assert app.target_selection_button.text() == "取消选择"
    app.target_selection_button.click()
    assert not app.target_tree.selectedItems()
    assert app.target_selection_label.text() == "共 3 位"
    assert app.database.list_targets() == targets


def visible_target_actions(app):
    return [
        None if action.isSeparator() else action.text()
        for action in app.target_actions_menu.actions()
        if action.isVisible()
    ]


@pytest.mark.parametrize(
    ("enabled", "selected_count", "label", "selection_text", "actions"),
    [
        ([], 0, None, None, []),
        ([True, True, True], 0, "共 3 位", "全选", []),
        ([True, True, True], 2, "已选 2/3 位", "全选",
         ["停用所选", None, "删除所选…", "取消选择"]),
        ([False, False, True], 2, "已选 2/3 位", "全选",
         ["启用所选", None, "删除所选…", "取消选择"]),
        ([True, False, True], 2, "已选 2/3 位", "全选",
         ["全部启用", "全部停用", None, "删除所选…", "取消选择"]),
        ([True, True, True], 3, "已选 3/3 位", "取消选择",
         ["停用所选", None, "删除所选…"]),
        ([False, False, False], 3, "已选 3/3 位", "取消选择",
         ["启用所选", None, "删除所选…"]),
        ([True, False, True], 3, "已选 3/3 位", "取消选择",
         ["全部启用", "全部停用", None, "删除所选…"]),
    ],
)
def test_target_selection_context_matrix(
    app, enabled, selected_count, label, selection_text, actions
):
    app._display_targets([target(i + 1, enabled=value) for i, value in enumerate(enabled)])
    for index in range(selected_count):
        app.target_tree.topLevelItem(index).setSelected(True)
    assert app.target_hint.isHidden() == bool(enabled)
    assert app.saved_count_label.isHidden() == (not enabled)
    assert app.target_selection_label.isHidden() == (label is None)
    assert app.target_selection_button.isHidden() == (selection_text is None)
    if label is not None:
        assert app.target_selection_label.text() == label
        assert app.target_selection_button.text() == selection_text
    assert app.target_selection_button.isEnabled() == bool(enabled)
    assert app.target_actions_button.isHidden() == (not selected_count)
    assert app.target_actions_button.isEnabled() == bool(selected_count)
    if actions:
        assert visible_target_actions(app) == actions
    assert all(
        action.isEnabled() == (action.isVisible() and selected_count > 0)
        for action in app.target_actions_menu.actions()
        if not action.isSeparator()
    )


def test_partial_selection_menu_clear_does_not_change_saved_targets(app):
    targets = save_targets(app)
    item(app.target_tree, str(targets[1].id)).setSelected(True)
    app.target_clear_selection_action.trigger()
    assert not app.target_tree.selectedItems()
    assert app.target_selection_label.text() == "共 3 位"
    assert app.target_actions_button.isHidden()
    assert app.database.list_targets() == targets


@pytest.mark.parametrize("enabled", [True, False])
def test_mixed_selection_menu_normalizes_only_selected_targets(app, enabled):
    targets = save_targets(app)
    app.database.set_targets_enabled([targets[1].id], False)
    app._refresh_targets()
    for saved in targets[:2]:
        item(app.target_tree, str(saved.id)).setSelected(True)
    action = app.target_enable_action if enabled else app.target_disable_action
    assert action.text() == ("全部启用" if enabled else "全部停用")
    action.trigger()
    assert [saved.enabled for saved in app.database.list_targets()] == [enabled, enabled, True]
    assert set(app._selected_keys(app.target_tree)) == {str(saved.id) for saved in targets[:2]}
    assert visible_target_actions(app)[0] == ("停用所选" if enabled else "启用所选")


def test_target_context_refreshes_without_selection_change_and_after_empty_delete(app, monkeypatch):
    targets = save_targets(app)
    app.target_selection_button.click()
    app.database.set_targets_enabled([saved.id for saved in targets], False)
    app._refresh_targets()
    assert visible_target_actions(app) == ["启用所选", None, "删除所选…"]
    monkeypatch.setattr(theme, "confirm", Mock(return_value=True))
    app.target_delete_action.trigger()
    assert app.database.list_targets() == []
    assert not app.target_hint.isHidden()
    assert all(
        widget.isHidden()
        for widget in (
            app.target_selection_label,
            app.target_selection_button,
            app.target_actions_button,
            app.saved_count_label,
        )
    )


@pytest.mark.parametrize("final_selection", [0, 1, 3])
def test_async_busy_selection_changes_cannot_reenable_target_actions(
    app, final_selection, blocked_reader
):
    targets = save_targets(app)
    reader, started, release, _ = blocked_reader(None)

    async def operation():
        return reader()

    app._start_async("本地状态检查", operation, lambda result: None)
    assert started.wait(10)
    for index in range(final_selection):
        app.target_tree.topLevelItem(index).setSelected(True)
    assert not app.target_selection_button.isEnabled()
    assert not app.target_actions_button.isEnabled()
    for action in app.target_actions_menu.actions():
        if not action.isSeparator():
            assert not action.isEnabled()
            action.trigger()
    app.target_selection_button.click()
    assert len(app.target_tree.selectedItems()) == final_selection
    assert app.database.list_targets() == targets
    release.set()
    message = app.messages.get(timeout=10)
    assert message[0] == "complete"
    app.messages.put(message)
    app._poll_messages()
    assert not app.busy
    assert app.target_selection_button.isEnabled()
    assert app.target_actions_button.isEnabled() == bool(final_selection)
    assert app.target_actions_button.isHidden() == (not final_selection)
    assert app.target_selection_button.text() == (
        "取消选择" if final_selection == len(targets) else "全选"
    )


@pytest.mark.parametrize("guard", ["busy", "_closed"])
def test_target_context_keeps_actions_disabled_across_selection_signals(app, guard):
    save_targets(app)
    setattr(app, guard, True)
    try:
        app.target_tree.selectAll()
        app._set_task_buttons_state(True)
        assert not app.target_selection_button.isEnabled()
        assert not app.target_actions_button.isEnabled()
        assert all(
            not action.isEnabled()
            for action in app.target_actions_menu.actions()
            if not action.isSeparator()
        )
    finally:
        setattr(app, guard, False)


def test_target_columns_use_native_row_selection_and_keyboard_focus(app, monkeypatch):
    monkeypatch.setattr(app, "_request_page_snapshot", Mock())
    app._show_page("friends")
    app._display_targets([target(i) for i in range(1, 5)])
    show_app(app)
    app.activateWindow()
    tree = app.target_tree
    assert tree.allColumnsShowFocus()
    assert tree.focusPolicy() != Qt.FocusPolicy.NoFocus
    for other in (app.candidate_tree, app.history_tree, app.logs_tree, app.progress_tree):
        assert not other.allColumnsShowFocus()

    painted_focus = []

    class FocusRecordingDelegate(QStyledItemDelegate):
        def paint(self, painter, option, index):
            painted_focus.append(bool(option.state & QStyle.StateFlag.State_HasFocus))
            super().paint(painter, option, index)

    delegate = FocusRecordingDelegate(tree)
    tree.setItemDelegate(delegate)

    def click_cell(row, column, modifiers=Qt.KeyboardModifier.NoModifier):
        index = tree.model().index(row, column)
        tree.scrollTo(index)
        QApplication.processEvents()
        QTest.mouseClick(
            tree.viewport(), Qt.MouseButton.LeftButton, modifiers, tree.visualRect(index).center()
        )
        QApplication.processEvents()

    for column in range(tree.columnCount()):
        tree.clearSelection()
        click_cell(1, column)
        assert app._selected_keys(tree) == ["2"]
        assert {index.column() for index in tree.selectionModel().selectedIndexes()} == set(
            range(tree.columnCount())
        )
        assert tree.currentColumn() == column
        assert tree.hasFocus()
        painted_focus.clear()
        tree.viewport().repaint()
        assert painted_focus and not any(painted_focus)
    click_cell(0, 1)
    click_cell(2, 3, Qt.KeyboardModifier.ControlModifier)
    assert set(app._selected_keys(tree)) == {"1", "3"}
    click_cell(0, 1)
    click_cell(2, 4, Qt.KeyboardModifier.ShiftModifier)
    assert set(app._selected_keys(tree)) == {"1", "2", "3"}
    QTest.keyClick(tree, Qt.Key.Key_Down)
    assert app._selected_keys(tree) == ["4"]
    QTest.keyClick(tree, Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)
    assert set(app._selected_keys(tree)) == {"3", "4"}
    QTest.keyClick(tree, Qt.Key.Key_F2)
    assert not tree.findChildren(QLineEdit)
    assert tree.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers


def test_enable_disable_selected_updates_entire_selection_and_preserves_it(app):
    targets = save_targets(app)
    selected_ids = {str(targets[0].id), str(targets[2].id)}
    for key in selected_ids:
        item(app.target_tree, key).setSelected(True)
    app.target_disable_action.trigger()
    assert [target.enabled for target in app.database.list_targets()] == [False, True, False]
    assert set(app._selected_keys(app.target_tree)) == selected_ids
    assert visible_target_actions(app) == ["启用所选", None, "删除所选…", "取消选择"]
    app.target_enable_action.trigger()
    assert [target.enabled for target in app.database.list_targets()] == [True, True, True]
    assert set(app._selected_keys(app.target_tree)) == selected_ids
    assert app.target_selection_label.text() == "已选 2/3 位"
    assert visible_target_actions(app) == ["停用所选", None, "删除所选…", "取消选择"]


@pytest.mark.parametrize("confirmed", [False, True])
def test_delete_selected_confirms_names_and_count_and_keeps_history(app, monkeypatch, confirmed):
    targets = save_targets(app)
    account = app.database.save_account("account", "测试账号")
    batch = app.database.start_batch(BatchMode.MANUAL, target_count=1)
    attempt = app.database.record_terminal_attempt(
        batch_id=batch,
        account_key=account.platform_user_id,
        target_id=targets[0].id,
        run_date="2026-09-08",
        message_text="测试文本",
        status=AttemptStatus.FAILED,
    )
    for target in targets[:2]:
        item(app.target_tree, str(target.id)).setSelected(True)
    confirm = Mock(return_value=confirmed)
    monkeypatch.setattr(theme, "confirm", confirm)
    app.target_delete_action.trigger()
    detail = confirm.call_args.args[2]
    assert "所选 2 位" in detail
    assert all(target.display_name in detail for target in targets[:2])
    assert targets[2].display_name not in detail
    assert app.database.get_attempt(attempt) is not None
    if not confirmed:
        assert app.database.list_targets() == targets
        assert len(app.target_tree.selectedItems()) == 2
    else:
        assert app.database.get_target(targets[0].id).enabled is False
        assert app.database.get_target(targets[1].id) is None
        assert app.database.get_target(targets[2].id) == targets[2]
        assert app._selected_keys(app.target_tree) == [str(targets[0].id)]
        assert app.target_selection_label.text() == "已选 1/2 位"
        assert visible_target_actions(app) == ["启用所选", None, "删除所选…", "取消选择"]


def test_bulk_action_error_preserves_selection_and_displays_error(app, monkeypatch):
    targets = save_targets(app)
    app._select_all_targets()
    error = sqlite3.OperationalError("数据库被锁定：完整原因")
    monkeypatch.setattr(app.database, "set_targets_enabled", Mock(side_effect=error))
    popup = Mock()
    monkeypatch.setattr(theme, "show_message", popup)
    app._set_selected_targets_enabled(False)
    assert app.database.list_targets() == targets
    assert len(app.target_tree.selectedItems()) == len(targets)
    assert "数据库被锁定：完整原因" in popup.call_args.args[2]


def test_raw_error_chain_is_displayed_and_persisted_as_plain_text(app, monkeypatch):
    popup = Mock()
    monkeypatch.setattr(theme, "show_message", popup)
    try:
        try:
            raise ValueError("<b>真实好友</b> token=原始诊断\n" + "详情" * 1500)
        except ValueError as cause:
            error = RuntimeError("操作失败：原始路径 C:/诊断")
            error.add_note("查找条件：昵称与唯一ID")
            raise error from cause
    except RuntimeError as error:
        detail = format_error(error)
        app._show_error(error)
    assert popup.call_args.args[2] == detail
    event = app.database.list_events()[0]
    assert event["message"] == detail
    app.logs_tree.setCurrentItem(item(app.logs_tree, str(event["id"])))
    assert app.logs_message.isReadOnly()
    assert app.logs_message.toPlainText() == detail
    app._refresh_logs()
    assert app.logs_message.toPlainText() == detail


def test_event_write_failure_never_recurses_or_hides_primary_error(app, monkeypatch):
    record = Mock(side_effect=sqlite3.OperationalError("日志不可写"))
    monkeypatch.setattr(app.database, "record_event", record)
    popup = Mock()
    monkeypatch.setattr(theme, "show_message", popup)
    error = RuntimeError("首要错误完整详情")
    app._show_error(error)
    record.assert_called_once_with("ERROR", "ui_error", format_error(error))
    assert popup.call_args.args[2].startswith(format_error(error))
    assert "日志不可写" in popup.call_args.args[2]


def test_bulk_disable_invalidates_late_snapshot_without_losing_selection(
    app, monkeypatch, blocked_reader
):
    targets = save_targets(app)
    reader, started, release, _ = blocked_reader(targets)
    original_reader = app.database.list_targets
    monkeypatch.setattr(app.database, "list_targets", reader)
    app._show_page("friends")
    assert started.wait(10)
    app._select_all_targets()
    monkeypatch.setattr(app.database, "list_targets", original_reader)
    app._set_selected_targets_enabled(False)
    assert all(not target.enabled for target in original_reader())
    release.set()
    accept_next_snapshot(app)
    assert all(app.target_tree.topLevelItem(i).text(0) == "否" for i in range(len(targets)))
    assert set(app._selected_keys(app.target_tree)) == {str(target.id) for target in targets}
    accept_next_snapshot(app)
    assert len(app.target_tree.selectedItems()) == len(targets)


@pytest.mark.parametrize("guard", ["busy", "_closed"])
def test_guarded_selection_buttons_preserve_existing_selection(app, guard):
    targets = save_targets(app)
    key = str(targets[1].id)
    item(app.target_tree, key).setSelected(True)
    setattr(app, guard, True)
    try:
        app._select_all_targets()
        assert app._selected_keys(app.target_tree) == [key]
        app._clear_target_selection()
        assert app._selected_keys(app.target_tree) == [key]
    finally:
        setattr(app, guard, False)


@pytest.mark.parametrize("guard", ["busy", "_closed"])
def test_delete_rechecks_guard_after_confirmation(app, monkeypatch, guard):
    targets = save_targets(app)
    app._select_all_targets()

    def confirm(*args):
        setattr(app, guard, True)
        return True

    monkeypatch.setattr(theme, "confirm", confirm)
    try:
        app._delete_targets()
        assert app.database.list_targets() == targets
    finally:
        setattr(app, guard, False)


def test_page_error_remains_readable_when_event_store_is_unavailable(app, monkeypatch):
    error = format_error(sqlite3.OperationalError("原始读取失败 <详情>"))
    app._current_page = "logs"
    token = (app._page_versions["logs"], app._page_epoch)
    app._page_reads["logs"] = token
    record = Mock(side_effect=sqlite3.OperationalError("事件数据库不可写"))
    monkeypatch.setattr(app.database, "record_event", record)
    popup = Mock()
    monkeypatch.setattr(theme, "show_message", popup)
    app._accept_page_snapshot("logs", token, None, error)
    record.assert_called_once_with("ERROR", "page_read_failed", error)
    popup.assert_not_called()
    assert app.logs_message.toPlainText().startswith(error)
    assert "事件数据库不可写" in app.logs_message.toPlainText()


def test_log_detail_clears_when_selected_event_disappears(app):
    app.database.record_event("ERROR", "fixture", "<b>完整事件</b>\n第二行")
    app._refresh_logs()
    app.logs_tree.setCurrentItem(app.logs_tree.topLevelItem(0))
    assert "\n" not in app.logs_tree.topLevelItem(0).text(4)
    assert app.logs_message.toPlainText() == "<b>完整事件</b>\n第二行"
    app._display_logs([])
    assert app.logs_message.toPlainText() == ""


def test_message_modes_are_exclusive_and_keep_text_draft_with_real_preview(app):
    draft = "未保存的文本草稿\n第二行"
    app.message_text.setPlainText(draft)
    assert app._current_message_kind() == MessageKind.TEXT
    assert not app.message_text.isHidden()
    assert app.sticker_preview.isHidden()

    app.sticker_mode_button.setChecked(True)
    assert not app.text_mode_button.isChecked()
    assert app.message_text.isHidden()
    assert not app.sticker_preview.isHidden()
    assert app._current_message() == ""
    assert not app.sticker_preview_image.pixmap().isNull()

    app.text_mode_button.setChecked(True)
    assert not app.sticker_mode_button.isChecked()
    assert app._current_message() == draft
    assert not app.message_text.isHidden()
    assert app.sticker_preview.isHidden()


@pytest.mark.parametrize("size", [(940, 650), (1120, 780)])
def test_native_preview_fits_without_clipping_at_supported_window_sizes(app, size):
    app.resize(*size)
    app.sticker_mode_button.setChecked(True)
    show_app(app)
    preview = app.sticker_preview_image
    image_size = preview.pixmap().deviceIndependentSize()
    assert not preview.pixmap().isNull()
    assert preview.contentsRect().width() >= image_size.width()
    assert preview.contentsRect().height() >= image_size.height()
    assert app.width() == size[0]
    assert app.height() == size[1]


def test_native_plan_save_and_reload_preserve_hidden_text_draft(app, monkeypatch):
    monkeypatch.setattr(app.scheduler, "sync", Mock())
    app.message_text.setPlainText("保留草稿")
    app.sticker_mode_button.setChecked(True)
    saved = app._persist_plan(app._snapshot_plan(), require_confirmation=True)
    assert saved.message_kind == MessageKind.SPARK_STICKER
    assert saved.message_text == ""
    app._refresh_plan()
    assert app.sticker_mode_button.isChecked()
    app.text_mode_button.setChecked(True)
    assert app.message_text.toPlainText() == "保留草稿"

    app.database.save_plan(
        enabled=False, send_time="09:00", message_text="旧文本", confirmed=True
    )
    app._refresh_plan()
    assert app._current_message_kind() == MessageKind.TEXT
    assert app._current_message() == "旧文本"


@pytest.mark.parametrize("previous_kind", list(MessageKind))
def test_plan_scheduler_failure_restores_previous_message_kind(app, monkeypatch, previous_kind):
    previous = app.database.save_plan(
        enabled=False,
        send_time="08:00",
        message_text="之前的文本",
        message_kind=previous_kind,
        confirmed=True,
    )
    app.sticker_mode_button.setChecked(previous_kind == MessageKind.TEXT)
    app.text_mode_button.setChecked(previous_kind == MessageKind.SPARK_STICKER)
    app.message_text.setPlainText("新的文本")
    monkeypatch.setattr(
        app.scheduler, "sync", Mock(side_effect=[SchedulerError("同步失败"), None])
    )
    with pytest.raises(SchedulerError, match="同步失败"):
        app._persist_plan(app._snapshot_plan(), require_confirmation=True)
    restored = app.database.get_plan()
    assert restored.message_kind == previous.message_kind
    assert restored.message_text == previous.message_text
    assert restored.send_time == previous.send_time
    app._refresh_plan()
    assert app._current_message_kind() == previous_kind


@pytest.mark.parametrize("action", ["manual", "plan", "missed", "duplicate"])
def test_native_confirmation_decline_never_sends_or_previews_text(app, monkeypatch, action):
    save_targets(app, count=1)
    app.message_text.setPlainText("绝不能作为表情发出的草稿")
    app.sticker_mode_button.setChecked(True)
    monkeypatch.setattr(theme, "confirm", Mock(return_value=False))
    monkeypatch.setattr(theme, "show_message", Mock())
    monkeypatch.setattr(app, "_start_async", Mock())
    monkeypatch.setattr(app, "_persist_plan", Mock())
    monkeypatch.setattr(app, "_run_missed_action", Mock())
    monkeypatch.setattr(app.service, "run_batch", AsyncMock())
    if action == "plan":
        app.schedule_enabled.setChecked(True)
        app._save_plan()
    elif action == "missed":
        monkeypatch.setattr(app_module, "detect_missed_schedule", Mock())
        monkeypatch.setattr(app.database, "list_pending_actions", Mock(return_value=[{
            "id": "native-missed",
            "kind": "missed_schedule",
            "payload": {
                "target_count": 1,
                "message_kind": "spark_sticker",
                "message_text": "绝不能作为表情发出的草稿",
            },
        }]))
        monkeypatch.setattr(app.database, "resolve_pending_action", Mock())
        app._check_pending_actions()
        app.database.resolve_pending_action.assert_called_once_with("native-missed", "skipped")
    else:
        if action == "duplicate":
            monkeypatch.setattr(app.database, "get_account", Mock(return_value=Mock()))
            monkeypatch.setattr(app.database, "has_daily_guard", Mock(return_value=True))
            theme.confirm.side_effect = [True, False]
        app._manual_send()
    preview = theme.confirm.call_args_list[0].args[2]
    assert "续火花（原生表情）" in preview
    assert "不发送同名文本" in preview
    assert "绝不能作为表情发出的草稿" not in preview
    app._start_async.assert_not_called()
    app._run_missed_action.assert_not_called()
    app.service.run_batch.assert_not_called()
    app._persist_plan.assert_not_called()
    theme.show_message.assert_not_called()


@pytest.mark.parametrize("message_kind", list(MessageKind))
async def test_manual_send_uses_confirmed_message_snapshot(app, monkeypatch, message_kind):
    targets = save_targets(app, count=1)
    app.message_text.setPlainText("确认过的文本")
    app.sticker_mode_button.setChecked(message_kind == MessageKind.SPARK_STICKER)
    monkeypatch.setattr(theme, "confirm", Mock(return_value=True))
    monkeypatch.setattr(app, "_persist_plan", Mock())
    monkeypatch.setattr(app.database, "get_account", Mock(return_value=Mock()))
    monkeypatch.setattr(app.database, "has_daily_guard", Mock(return_value=False))
    monkeypatch.setattr(app, "_start_async", Mock())
    monkeypatch.setattr(app.service, "run_batch", AsyncMock())
    app._manual_send()
    operation = app._start_async.call_args.args[1]
    app.text_mode_button.setChecked(True)
    app.message_text.setPlainText("确认之后的修改")
    await operation()
    kwargs = app.service.run_batch.call_args.kwargs
    assert kwargs["message_kind_override"] == message_kind
    assert kwargs["message_text_override"] == (
        "" if message_kind == MessageKind.SPARK_STICKER else "确认过的文本"
    )
    assert kwargs["target_ids"] == (targets[0].id,)
    assert app._persist_plan.call_args.args[0].message_text == kwargs["message_text_override"]


@pytest.mark.parametrize("message_kind", list(MessageKind))
async def test_validate_uses_current_unsaved_message_without_saving(app, monkeypatch, message_kind):
    app.message_text.setPlainText("未保存验证文本")
    app.sticker_mode_button.setChecked(message_kind == MessageKind.SPARK_STICKER)
    monkeypatch.setattr(app, "_start_async", Mock())
    monkeypatch.setattr(app, "_persist_plan", Mock())
    monkeypatch.setattr(app.service, "validate_config", AsyncMock())
    app._validate()
    operation = app._start_async.call_args.args[1]
    app.text_mode_button.setChecked(True)
    app.message_text.setPlainText("后续修改")
    await operation()
    kwargs = app.service.validate_config.call_args.kwargs
    assert kwargs["message_kind_override"] == message_kind
    assert kwargs["message_text_override"] == (
        "" if message_kind == MessageKind.SPARK_STICKER else "未保存验证文本"
    )
    app._persist_plan.assert_not_called()


@pytest.mark.parametrize("payload_kind", [None, "text", "spark_sticker"])
async def test_missed_action_uses_payload_kind_not_current_mode(app, monkeypatch, payload_kind):
    app.sticker_mode_button.setChecked(payload_kind != "spark_sticker")
    payload = {
        "target_ids": [1],
        "message_text": "错过任务的文本",
        "scheduled_for": "2026-01-01T09:00:00",
        "delay_min_seconds": 3,
        "delay_max_seconds": 8,
    }
    if payload_kind is not None:
        payload["message_kind"] = payload_kind
    monkeypatch.setattr(app, "_start_async", Mock())
    monkeypatch.setattr(app.service, "run_batch", AsyncMock())
    app._run_missed_action("missed", payload)
    await app._start_async.call_args.args[1]()
    kwargs = app.service.run_batch.call_args.kwargs
    assert kwargs["message_kind_override"] == MessageKind(payload_kind or "text")
    assert kwargs["message_text_override"] == (
        "" if payload_kind == "spark_sticker" else "错过任务的文本"
    )


def test_native_history_labels_attempt_without_claiming_sent_text(app):
    row = history_row()
    row["message_kind"] = "spark_sticker"
    row["status"] = "unknown"
    row["message_text"] = "不应展示的文本"
    app._display_history([row])
    selected = item(app.history_tree, row["id"])
    selected.setSelected(True)
    assert selected.text(6) == "续火花（原生表情）"
    assert selected.text(2) == "unknown"
    detail = app.history_message.toPlainText()
    assert "续火花（原生表情）" in detail
    assert "不是已发送的同名文本" in detail
    assert "不应展示的文本" not in detail
    del row["message_kind"]
    app._display_history([row])
    assert selected.text(6) == "文本"
    assert app.history_message.toPlainText() == "不应展示的文本"


def test_unknown_reminders_aggregate_and_acknowledge_without_changing_send_state(app, monkeypatch):
    monkeypatch.setattr("spark_keeper.database.today_iso", lambda: "2026-09-04")
    monkeypatch.setattr(app_module, "detect_missed_schedule", Mock())
    show = Mock()
    monkeypatch.setattr(theme, "show_message", show)
    account = app.database.save_account("pending-fixture", "测试账号")
    saved_targets = save_targets(app, count=3)
    batch_id = app.database.start_batch(BatchMode.SCHEDULED, target_count=3)
    attempt_ids = []
    for saved in saved_targets:
        reservation = app.database.reserve_attempt(
            batch_id=batch_id,
            account_key=account.platform_user_id,
            target_id=saved.id,
            run_date="2026-09-04",
            message_text="仅本地记录，不发送",
            manual_override=False,
        )
        app.database.mark_attempt_triggered(reservation.attempt_id)
        app.database.finish_attempt(reservation.attempt_id, AttemptStatus.UNKNOWN)
        attempt_ids.append(reservation.attempt_id)
        app.database.create_pending_action(
            "unknown_send",
            f"unknown:{reservation.attempt_id}",
            {"attempt_id": reservation.attempt_id, "batch_id": batch_id},
        )
    other_id = app.database.create_pending_action("login_required", "login:fixture", {})
    attempts_before = [app.database.get_attempt(attempt_id) for attempt_id in attempt_ids]

    app._check_pending_actions()

    show.assert_called_once()
    assert "3 条" in show.call_args.args[2]
    assert "人工核对" in show.call_args.args[2]
    assert "仅表示已知晓" in show.call_args.args[2]
    assert not app.database.list_pending_actions("unknown_send")
    assert [row["id"] for row in app.database.list_pending_actions()] == [other_id]
    assert app.pending_label.text() == "待处理事项：1 项"
    assert "已知晓 3 条" in app.status_label.text()
    assert [app.database.get_attempt(attempt_id) for attempt_id in attempt_ids] == attempts_before
    assert all(
        app.database.has_daily_guard(account.platform_user_id, saved.id, "2026-09-04")
        for saved in saved_targets
    )
    with app.database.connect() as connection:
        rows = connection.execute(
            "SELECT status, resolved_at FROM pending_actions WHERE kind = 'unknown_send'"
        ).fetchall()
    assert len(rows) == 3
    assert all(row["status"] == "completed" and row["resolved_at"] for row in rows)


def test_unknown_modal_reentry_and_new_event_acknowledge_only_displayed_snapshot(app, monkeypatch):
    monkeypatch.setattr(app_module, "detect_missed_schedule", Mock())
    first_id = app.database.create_pending_action("unknown_send", "unknown:first", {})
    seen = []

    def show(_parent, _title, text, **_kwargs):
        seen.append(text)
        if len(seen) == 1:
            assert [row["id"] for row in app.database.list_pending_actions()] == [first_id]
            app.database.create_pending_action("unknown_send", "unknown:during-dialog", {})
            for _ in range(4):
                app._check_pending_actions()
            assert len(app.database.list_pending_actions()) == 2

    monkeypatch.setattr(theme, "show_message", show)
    app._check_pending_actions()
    assert len(seen) == 1
    remaining = app.database.list_pending_actions()
    assert len(remaining) == 1
    assert remaining[0]["dedupe_key"] == "unknown:during-dialog"
    assert app.pending_label.text() == "待处理事项：1 项"

    app._check_pending_actions()
    assert len(seen) == 2
    assert all("1 条" in text for text in seen)
    for _ in range(5):
        app._check_pending_actions()
    assert len(seen) == 2
    assert not app.database.list_pending_actions()
    assert app.pending_label.text() == ""


def test_idle_pending_poll_discovers_new_unknown_without_restart(app, monkeypatch):
    monkeypatch.setattr(app_module, "detect_missed_schedule", Mock())
    show = Mock()
    monkeypatch.setattr(theme, "show_message", show)
    app._check_pending_actions()
    assert app._pending_timer.isActive()
    assert app._pending_timer.interval() == 5000
    show.assert_not_called()
    app.database.create_pending_action("unknown_send", "unknown:after-idle", {})
    app._pending_timer.timeout.emit()
    show.assert_called_once()
    assert not app.database.list_pending_actions()
    app._pending_timer.timeout.emit()
    show.assert_called_once()


@pytest.mark.parametrize("acknowledged", [False, True])
def test_unknown_pending_acknowledgement_survives_window_restart(app, monkeypatch, acknowledged):
    monkeypatch.setattr(app_module, "detect_missed_schedule", Mock())
    show = Mock()
    monkeypatch.setattr(theme, "show_message", show)
    app.database.create_pending_action("unknown_send", "unknown:restart", {})
    if acknowledged:
        app._check_pending_actions()
        show.assert_called_once()
    app.close()
    show.reset_mock()
    restarted = SparkKeeperApp(smoke_mode=True)
    restarted._poll_timer.stop()
    try:
        restarted._check_pending_actions()
        assert show.call_count == (0 if acknowledged else 1)
        assert not restarted.database.list_pending_actions()
        restarted.database.create_pending_action("unknown_send", "unknown:restart", {})
        restarted._check_pending_actions()
        assert show.call_count == (0 if acknowledged else 1)
        restarted.database.create_pending_action("unknown_send", "unknown:new-after-restart", {})
        restarted._check_pending_actions()
        assert show.call_count == (1 if acknowledged else 2)
    finally:
        restarted.close()
        restarted.deleteLater()
        QApplication.processEvents()


@pytest.mark.parametrize("blocked_by", ["busy", "other-modal"])
def test_pending_reminders_wait_for_running_task_or_other_modal(app, monkeypatch, blocked_by):
    monkeypatch.setattr(app_module, "detect_missed_schedule", Mock())
    show = Mock()
    monkeypatch.setattr(theme, "show_message", show)
    app.database.create_pending_action("unknown_send", "unknown:deferred", {})
    dialog = QDialog(app)
    try:
        if blocked_by == "busy":
            app.busy = True
        else:
            dialog.setModal(True)
            dialog.show()
            QApplication.processEvents()
        app._check_pending_actions()
        show.assert_not_called()
        assert len(app.database.list_pending_actions()) == 1
        assert app._pending_timer.isActive()
        assert app._pending_timer.interval() == 1000
    finally:
        app.busy = False
        dialog.close()
        dialog.deleteLater()
        QApplication.processEvents()
    app._check_pending_actions()
    show.assert_called_once()
    assert not app.database.list_pending_actions()


def test_failed_unknown_presentation_keeps_pending_and_releases_reentry_guard(app, monkeypatch):
    monkeypatch.setattr(app_module, "detect_missed_schedule", Mock())
    app.database.create_pending_action("unknown_send", "unknown:interrupted", {})
    show = Mock(side_effect=RuntimeError("提示未完成"))
    monkeypatch.setattr(theme, "show_message", show)
    with pytest.raises(RuntimeError, match="提示未完成"):
        app._check_pending_actions()
    assert len(app.database.list_pending_actions()) == 1
    show.side_effect = None
    app._check_pending_actions()
    assert show.call_count == 2
    assert not app.database.list_pending_actions()


@pytest.mark.parametrize("result_kind", ["complete", "error", "callback_error"])
def test_task_result_survives_failed_pending_refresh(app, monkeypatch, result_kind):
    original = RuntimeError("原始任务失败")
    callback = Mock(side_effect=original if result_kind == "callback_error" else None)
    show = Mock()
    monkeypatch.setattr(theme, "show_message", show)
    monkeypatch.setattr(
        app.database, "list_pending_actions", Mock(side_effect=sqlite3.OperationalError("收尾读取失败"))
    )
    app.busy = True
    if result_kind == "error":
        app.messages.put(("error", "任务", original))
    else:
        app.messages.put(("complete", "任务", callback, "原始返回值"))
    app._poll_messages()
    app._poll_messages()
    assert not app.busy
    if result_kind == "error":
        callback.assert_not_called()
    else:
        callback.assert_called_once_with("原始返回值")
    if result_kind == "complete":
        show.assert_not_called()
    else:
        show.assert_called_once()
        assert "原始任务失败" in show.call_args.args[2]
        assert "收尾读取失败" not in show.call_args.args[2]
    assert "收尾读取失败" in app.logs_message.toPlainText()


@pytest.mark.parametrize("action", ["save", "manual", "login"])
def test_slow_scheduler_keeps_qt_responsive_and_operation_exclusive(
    app, monkeypatch, blocked_reader, action
):
    reader, started, release, calls = blocked_reader(None)
    monkeypatch.setattr(app.scheduler, "sync", lambda plan: reader())
    monkeypatch.setattr(theme, "confirm", Mock(return_value=True))
    show = Mock()
    monkeypatch.setattr(theme, "show_message", show)
    monkeypatch.setattr(app.service, "login", AsyncMock())
    monkeypatch.setattr(app.service, "run_batch", AsyncMock())
    completed = Mock()
    monkeypatch.setattr(app, "_batch_complete", completed)
    app.database.save_account("fixture-account", "测试账号")
    save_targets(app, count=1)
    app.message_text.setPlainText("已确认的消息")
    getattr(app, {"save": "_save_plan", "manual": "_manual_send", "login": "_login"}[action])()
    assert started.wait(10)
    assert app.busy
    assert not app.login_button.isEnabled()
    assert all(not button.isEnabled() for button in app._task_buttons)
    event_ran = []
    QTimer.singleShot(0, lambda: event_ran.append(True))
    wait_until(lambda: bool(event_ran))
    app.message_text.setPlainText("主线程继续编辑")
    app._manual_send()
    app._save_plan()
    app._login()
    assert len(calls) == 1
    app.service.run_batch.assert_not_called()
    release.set()
    wait_until(lambda: (app._poll_messages(), not app.busy)[1])
    if action == "manual":
        app.service.run_batch.assert_awaited_once()
        assert app.service.run_batch.call_args.kwargs["message_text_override"] == "已确认的消息"
        assert app.database.get_plan().message_text == "已确认的消息"
        completed.assert_called_once()
    elif action == "save":
        assert app.database.get_plan().message_text == "已确认的消息"
        show.assert_called_once()
    else:
        assert app.service.login.call_args.kwargs["cancel"] is app.cancel_event
        show.assert_called_once()


def test_scheduler_snapshot_is_async_and_stale_query_cannot_replace_newer_status(
    app, monkeypatch, blocked_reader
):
    reader, started, release, calls = blocked_reader(False)
    monkeypatch.setattr(app.scheduler, "exists", reader)
    app._refresh_plan()
    assert started.wait(10)
    event_ran = []
    QTimer.singleShot(0, lambda: event_ran.append(True))
    wait_until(lambda: bool(event_ran))
    app._refresh_plan()
    assert len(calls) == 1
    monkeypatch.setattr(app.scheduler, "exists", lambda: True)
    app.message_text.setPlainText("未保存文本不被后台结果覆盖")
    release.set()
    wait_until(lambda: (app._poll_messages(), app._scheduler_read is None)[1])
    assert app.scheduler_chip.text() == "系统任务已创建"
    assert app.message_text.toPlainText() == "未保存文本不被后台结果覆盖"


@pytest.mark.parametrize("cancel_by", ["button", "close"])
def test_login_cancel_reaches_worker_and_is_not_success_or_error(app, monkeypatch, cancel_by):
    entered, exited = threading.Event(), threading.Event()
    show = Mock()
    monkeypatch.setattr(theme, "show_message", show)
    confirm = Mock(return_value=True)
    monkeypatch.setattr(theme, "confirm", confirm)
    sync = Mock()
    monkeypatch.setattr(app.scheduler, "sync", sync)

    async def login(status=None, *, cancel=None):
        assert threading.current_thread() is not threading.main_thread()
        assert cancel is app.cancel_event
        entered.set()
        try:
            while not cancel.is_set():
                await asyncio.sleep(0.01)
            raise AutomationError(ErrorCode.CANCELLED, "登录已取消")
        finally:
            exited.set()

    monkeypatch.setattr(app.service, "login", login)
    app._login()
    assert entered.wait(10)
    assert app.cancel_button.text() == "取消登录"
    if cancel_by == "button":
        app.cancel_button.click()
    else:
        app.close()
        assert not app._closed
        assert "取消登录" in confirm.call_args.args[2]
    assert app.cancel_event.is_set()
    assert "取消登录" in app.status_label.text()
    wait_until(lambda: (app._poll_messages(), not app.busy)[1])
    assert exited.is_set()
    assert app.status_label.text() == "登录已取消"
    show.assert_not_called()
    sync.assert_not_called()


def test_manual_cancel_during_scheduler_never_starts_batch(app, monkeypatch, blocked_reader):
    reader, started, release, _ = blocked_reader(None)
    monkeypatch.setattr(app.scheduler, "sync", lambda plan: reader())
    monkeypatch.setattr(theme, "confirm", Mock(return_value=True))
    monkeypatch.setattr(theme, "show_message", Mock())
    monkeypatch.setattr(app.service, "run_batch", AsyncMock())
    app.database.save_account("fixture-account", "测试账号")
    save_targets(app, count=1)
    app.message_text.setPlainText("不会发送")
    app._manual_send()
    assert started.wait(10)
    app.cancel_button.click()
    assert app.busy
    release.set()
    wait_until(lambda: (app._poll_messages(), not app.busy)[1])
    app.service.run_batch.assert_not_called()
    theme.show_message.assert_not_called()
    assert app.status_label.text() == "任务已取消"


def test_task_error_presentation_failure_preserves_original_without_recursive_dialog(app, monkeypatch):
    original = RuntimeError("原始任务失败")
    show = Mock(side_effect=RuntimeError("对话框失败"))
    monkeypatch.setattr(theme, "show_message", show)
    app.messages.put(("error", "任务", original))
    app._poll_messages()
    show.assert_called_once()
    assert "原始任务失败" in app.logs_message.toPlainText()
    assert "对话框失败" in app.logs_message.toPlainText()


def test_manual_scheduler_rollback_holds_busy_and_never_starts_send(
    app, monkeypatch, blocked_reader
):
    reader, started, release, _ = blocked_reader(None)
    original = SchedulerError("计划同步失败")
    calls = []

    def sync(plan):
        assert threading.current_thread() is not threading.main_thread()
        calls.append(plan)
        if len(calls) == 1:
            raise original
        return reader()

    monkeypatch.setattr(app.scheduler, "sync", sync)
    monkeypatch.setattr(theme, "confirm", Mock(return_value=True))
    show = Mock()
    monkeypatch.setattr(theme, "show_message", show)
    monkeypatch.setattr(app.service, "run_batch", AsyncMock())
    app.database.save_account("fixture-account", "测试账号")
    save_targets(app, count=1)
    previous = app.database.get_plan()
    app.message_text.setPlainText("新的计划内容")
    app._manual_send()
    assert started.wait(10)
    assert app.busy
    assert all(not button.isEnabled() for button in app._task_buttons)
    event_ran = []
    QTimer.singleShot(0, lambda: event_ran.append(True))
    wait_until(lambda: bool(event_ran))
    assert app.database.get_plan().message_text == previous.message_text
    release.set()
    wait_until(lambda: (app._poll_messages(), not app.busy)[1])
    app.service.run_batch.assert_not_called()
    show.assert_called_once()
    assert "计划同步失败" in show.call_args.args[2]
