from __future__ import annotations

import sqlite3
import threading
from time import monotonic
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from spark_keeper.database import Database
from spark_keeper.models import (
    FriendCandidate,
    SparkContact,
    SparkScanResult,
    SparkScanStatus,
    SparkState,
    Target,
    ThemeMode,
)
from spark_keeper.paths import AppPaths
from spark_keeper.ui import app as app_module
from spark_keeper.ui import theme
from spark_keeper.ui.app import SparkKeeperApp
from spark_keeper.ui.spark_preview import SparkImportPreview


def wait_until(predicate):
    deadline = monotonic() + 10
    while not predicate():
        QApplication.processEvents()
        if monotonic() >= deadline:
            pytest.fail("Qt did not deliver the theme transition")
        QTest.qWait(1)


@pytest.fixture
def windows(tmp_path, monkeypatch, qapp):
    monkeypatch.setenv("SPARK_KEEPER_ROOT", str(tmp_path))
    monkeypatch.setattr(app_module.TaskScheduler, "exists", lambda self: False)
    monkeypatch.setattr(theme, "show_message", Mock())
    threads = []
    created = []
    start_thread = threading.Thread

    def track_thread(*args, **kwargs):
        thread = start_thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(app_module.threading, "Thread", track_thread)

    def create(stored=None):
        if stored is not None:
            Database(AppPaths.discover().database).set_meta("theme_mode", stored)
        window = SparkKeeperApp(smoke_mode=True)
        created.append(window)
        return window

    yield create
    for window in created:
        window._theme_writer.wait()
        window._poll_messages()
        window.busy = False
        window.close()
        window.deleteLater()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    qapp.processEvents()
    qapp.styleHints().unsetColorScheme()
    theme.apply_theme(qapp, ThemeMode.LIGHT)
    qapp.processEvents()


def choose(window, mode):
    window.show()
    QApplication.processEvents()
    combo = window.theme_combo
    combo.showPopup()
    QApplication.processEvents()
    index = combo.model().index(combo.findData(mode.value), 0)
    combo.view().scrollTo(index)
    QTest.mouseClick(
        combo.view().viewport(),
        Qt.MouseButton.LeftButton,
        pos=combo.view().visualRect(index).center(),
    )
    assert window.theme_preference is mode


def saved(window):
    wait_until(lambda: window._theme_completed_revision == window._theme_revision)


@pytest.mark.parametrize("stored", [None, "obsolete", "", "SYSTEM"])
def test_missing_or_unknown_preference_defaults_to_light_before_show(windows, stored):
    window = windows(stored)
    assert not window.isVisible()
    assert window.theme_preference is ThemeMode.LIGHT
    assert window.effective_theme is ThemeMode.LIGHT
    assert window.theme_combo.currentText() == "浅色模式"
    assert theme.current_colors() == theme.colors_for(ThemeMode.LIGHT)


@pytest.mark.parametrize("mode", list(ThemeMode))
def test_native_choice_persists_and_restores_before_first_show(windows, mode):
    window = windows()
    # Light is initially selected; visit Dark first so Light also exercises a real write.
    choose(window, ThemeMode.DARK if mode is ThemeMode.LIGHT else ThemeMode.LIGHT)
    choose(window, mode)
    saved(window)
    assert window.database.get_meta("theme_mode") == mode.value
    window.close()
    restarted = windows()
    assert not restarted.isVisible()
    assert restarted.theme_preference is mode
    assert restarted.effective_theme is theme.resolve_mode(
        mode, restarted._theme_hints.colorScheme()
    )
    assert theme.current_colors() == theme.colors_for(restarted.effective_theme)


def test_real_qt_notifications_follow_only_system_and_coalesce(windows, monkeypatch):
    window = windows()
    choose(window, ThemeMode.SYSTEM)
    saved(window)
    write = Mock(wraps=window.database.set_meta)
    monkeypatch.setattr(window.database, "set_meta", write)
    apply = Mock(wraps=theme.apply_theme)
    monkeypatch.setattr(theme, "apply_theme", apply)
    hints = window._theme_hints
    hints.colorSchemeChanged.emit(Qt.ColorScheme.Light)
    hints.colorSchemeChanged.emit(Qt.ColorScheme.Dark)
    hints.colorSchemeChanged.emit(Qt.ColorScheme.Dark)
    wait_until(lambda: window.effective_theme is ThemeMode.DARK)
    assert apply.call_count <= 1
    write.assert_not_called()
    hints.colorSchemeChanged.emit(Qt.ColorScheme.Unknown)
    wait_until(lambda: window.effective_theme is ThemeMode.LIGHT)
    choose(window, ThemeMode.DARK)
    saved(window)
    hints.colorSchemeChanged.emit(Qt.ColorScheme.Light)
    QApplication.processEvents()
    assert window.effective_theme is ThemeMode.DARK
    choose(window, ThemeMode.SYSTEM)
    assert window.effective_theme is theme.resolve_mode(ThemeMode.SYSTEM, hints.colorScheme())


def test_busy_native_choice_is_responsive_and_preserves_draft_and_page(windows, monkeypatch):
    window = windows()
    started, release = threading.Event(), threading.Event()
    real_write = window.database.set_meta

    def blocked_write(key, value):
        started.set()
        assert release.wait(10)
        real_write(key, value)

    monkeypatch.setattr(window.database, "set_meta", blocked_write)
    window.message_text.setPlainText("合成未保存草稿")
    window._show_page("friends")
    window.busy = True
    window._set_task_buttons_state(False)
    try:
        choose(window, ThemeMode.DARK)
        assert started.wait(10)
        fired = []
        QTimer.singleShot(0, lambda: fired.append(True))
        wait_until(lambda: bool(fired))
        assert window.theme_combo.isEnabled()
        assert window.busy
        assert not window.login_button.isEnabled()
        assert window.page_stack.currentWidget() is window.pages["friends"]
        assert window.message_text.toPlainText() == "合成未保存草稿"
        assert window.effective_theme is ThemeMode.DARK
    finally:
        release.set()
        window.busy = False
    saved(window)


@pytest.mark.parametrize("newer", [False, True])
def test_failed_save_restores_saved_choice_without_overwriting_newer_selection(
    windows, monkeypatch, newer
):
    window = windows()
    started, release = threading.Event(), threading.Event()
    real_write = window.database.set_meta

    def fail_first(key, value):
        if value == "dark":
            started.set()
            assert release.wait(10)
            raise sqlite3.OperationalError("合成数据库锁定错误")
        real_write(key, value)

    monkeypatch.setattr(window.database, "set_meta", fail_first)
    try:
        choose(window, ThemeMode.DARK)
        assert started.wait(10)
        if newer:
            choose(window, ThemeMode.SYSTEM)
            choose(window, ThemeMode.LIGHT)
            choose(window, ThemeMode.SYSTEM)
    finally:
        release.set()
    saved(window)
    expected = ThemeMode.SYSTEM if newer else ThemeMode.LIGHT
    assert window.theme_preference is expected
    assert window.theme_combo.currentData() == expected.value
    assert window.database.get_meta("theme_mode", "light") == expected.value
    theme.show_message.assert_called_once()
    assert "合成数据库锁定错误" in theme.show_message.call_args.args[2]


def test_close_waits_without_blocking_and_finishes_original_close(windows, monkeypatch):
    window = windows()
    started, release = threading.Event(), threading.Event()
    real_write = window.database.set_meta

    def blocked_write(key, value):
        started.set()
        assert release.wait(10)
        real_write(key, value)

    monkeypatch.setattr(window.database, "set_meta", blocked_write)
    try:
        choose(window, ThemeMode.DARK)
        assert started.wait(10)
        choose(window, ThemeMode.SYSTEM)
        window.close()
        assert not window._closed
        assert window.isVisible()
        assert window._theme_close_pending
        fired = []
        QTimer.singleShot(0, lambda: fired.append(True))
        wait_until(lambda: bool(fired))
    finally:
        release.set()
    wait_until(lambda: window._closed)
    window._theme_writer.wait()
    assert window.database.get_meta("theme_mode") == "system"


def test_failed_save_cancels_deferred_close_and_shows_restored_option(windows, monkeypatch):
    window = windows()
    window._poll_timer.stop()
    monkeypatch.setattr(window.database, "set_meta", Mock(side_effect=OSError("保存被拒绝")))
    choose(window, ThemeMode.DARK)
    window.close()
    window._theme_writer.wait()
    window._poll_messages()
    assert not window._closed
    assert window.isVisible()
    assert not window._theme_close_pending
    assert window.theme_preference is ThemeMode.LIGHT
    theme.show_message.assert_called_once()


def test_busy_rejected_close_keeps_signal_and_accepted_close_disconnects(windows, monkeypatch):
    window = windows("system")
    monkeypatch.setattr(theme, "confirm", Mock(return_value=False))
    window.busy = True
    window.close()
    window._theme_hints.colorSchemeChanged.emit(Qt.ColorScheme.Dark)
    wait_until(lambda: window.effective_theme is ThemeMode.DARK)
    window.busy = False
    window.close()
    assert window._closed
    restarted = windows("light")
    restarted._theme_hints.colorSchemeChanged.emit(Qt.ColorScheme.Light)
    QApplication.processEvents()
    assert not window._theme_timer.isActive()
    assert window.effective_theme is ThemeMode.DARK
    assert restarted.effective_theme is ThemeMode.LIGHT


def test_open_preview_and_existing_rows_keep_selection_filter_and_semantic_colors(
    windows, monkeypatch
):
    window = windows()
    monkeypatch.setattr(window, "_request_page_snapshot", Mock())
    targets = [
        Target(
            id=i,
            stable_key=f"uid:{i}",
            display_name=f"合成好友 {i}",
            profile_url=f"https://www.douyin.com/user/test{i}",
            avatar_url="",
            search_query=f"合成好友 {i}",
            evidence={},
            enabled=i % 2 == 0,
            confirmed_at="2026-09-01",
        )
        for i in range(1, 81)
    ]
    window._display_targets(targets)
    window._show_page("friends")
    window.show()
    QApplication.processEvents()
    tree = window.target_tree
    tree.topLevelItem(3).setSelected(True)
    tree.scrollToItem(tree.topLevelItem(50))
    QApplication.processEvents()
    scroll = tree.verticalScrollBar().value()
    rows = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    contacts = tuple(
        SparkContact(
            FriendCandidate(
                f"uid:{i}",
                f"合成预览 {i}",
                profile_url=f"https://www.douyin.com/user/test{i}",
                evidence={
                    "identity_strength": "strong",
                    "user_id": str(i),
                    "chat_type": "single",
                },
            ),
            state,
            "合成识别依据",
        )
        for i, state in enumerate((SparkState.ACTIVE, SparkState.RECOVER), 1)
    )
    scan = SparkScanResult("synthetic", "now", "now", SparkScanStatus.COMPLETE, contacts, 2)
    preview = SparkImportPreview(
        window,
        scan,
        set(),
        validate=lambda: True,
        import_selected=lambda: None,
        close=lambda: None,
    )
    preview.show()
    preview.filter_box.setCurrentText("待恢复")
    before = set(preview.selected_keys)
    preview_rows = list(preview._items)
    window._set_status("合成警告", "warn")
    choose(window, ThemeMode.DARK)
    assert [tree.topLevelItem(i) for i in range(len(rows))] == rows
    assert window._selected_keys(tree) == ["4"]
    assert tree.verticalScrollBar().value() == scroll
    assert preview._items == preview_rows
    assert preview.selected_keys == before
    assert preview.filter_box.currentText() == "待恢复"
    assert preview_rows[0].isHidden()
    assert preview_rows[0].checkState(0) is Qt.CheckState.Checked
    colors = theme.current_colors()
    assert rows[0].foreground(0).color() == QColor(colors.text_muted)
    assert preview_rows[0].foreground(0).color() == QColor(colors.text)
    assert window.status_label.palette().color(QPalette.ColorRole.WindowText) == QColor(
        colors.warning
    )
    preview.close()
