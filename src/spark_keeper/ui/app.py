from __future__ import annotations

import asyncio
import os
import queue
import sqlite3
import sys
import threading
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from PySide6.QtCore import QEvent, QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QColor, QPalette, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, __version__
from ..automation.errors import AutomationError
from ..automation.service import BatchService
from ..database import Database, today_iso
from ..dpapi import DpapiJsonStore
from ..logging_safe import format_error
from ..maintenance import MaintenanceLease, assert_maintenance_clear
from ..models import (
    BatchMode,
    BatchResult,
    ErrorCode,
    FriendCandidate,
    MessageKind,
    Plan,
    SparkScanResult,
    SparkScanStatus,
    Target,
)
from ..paths import AppPaths
from ..scheduler import SchedulerError, TaskScheduler, detect_missed_schedule
from . import theme
from .single_instance import GuiSingleInstance, activate_window
from .spark_preview import SparkImportPreview


@dataclass(frozen=True, slots=True)
class _PlanInput:
    enabled: bool
    send_time: str
    message_text: str
    message_kind: MessageKind
    delay_min_seconds: int
    delay_max_seconds: int


class _ProgramDirectoryLabel(QLabel):
    """Keep the footer path on one line without using its text as a layout constraint."""

    def __init__(self, directory: Path) -> None:
        self._full_text = f"程序目录：{directory}"
        super().__init__(self._full_text)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(False)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setToolTip(str(directory))
        self.setMaximumWidth(440)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(self.fontMetrics().horizontalAdvance("程序目录：…"))
        self._update_elision()

    def _update_elision(self) -> None:
        metrics = self.fontMetrics()
        text = metrics.elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, self.contentsRect().width()
        )
        if text != self.text():
            self.setText(text)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self._update_elision()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            self.setMinimumWidth(self.fontMetrics().horizontalAdvance("程序目录：…"))
            self._update_elision()


class SparkKeeperApp(QMainWindow):
    """Native Qt pages; workers communicate only through the main-thread queue."""

    NAV_ITEMS = (
        ("home", "任务台"),
        ("friends", "好友管理"),
        ("history", "发送历史"),
        ("logs", "运行日志"),
    )

    def __init__(self, *, smoke_mode: bool = False) -> None:
        super().__init__()
        self.smoke_mode = smoke_mode
        self.paths = AppPaths.discover()
        self.paths.ensure_runtime_dirs()
        self.database = Database(self.paths.database)
        recovered = self.database.recover_inflight_if_idle()
        if recovered:
            self.database.record_event(
                "WARNING", "send_unknown", f"启动时恢复 {recovered} 条未完成尝试为结果不确定"
            )
        self.state_store = DpapiJsonStore(self.paths.auth_state)
        self.service = BatchService(self.database, self.state_store)
        self.scheduler = TaskScheduler(self.paths)
        self.messages: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self._closed = False
        self._checking_pending_actions = False
        self._page_epoch = 0
        self._page_versions = dict.fromkeys(("friends", "history", "logs"), 0)
        self._page_reads: dict[str, tuple[int, int]] = {}
        self._scheduler_version = 0
        self._scheduler_read: int | None = None
        self._login_running = False
        self.busy = False
        self._task_buttons_enabled = True
        self._target_enabled: dict[str, bool] = {}
        self.cancel_event = threading.Event()
        self.capture_requested_event = threading.Event()
        self.capture_browser_ready_event = threading.Event()
        self.candidates: dict[str, FriendCandidate] = {}
        self.history_messages: dict[str, str] = {}
        self.log_messages: dict[str, str] = {}
        self._spark_preview: SparkImportPreview | None = None
        self._spark_scan_running = False
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1120, 780)
        self.setMinimumSize(940, 650)
        self._configure_style()
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(80)
        self._poll_timer.timeout.connect(self._poll_messages)
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(1000)
        self._preview_timer.timeout.connect(self._watch_spark_preview)
        self._capture_timer = QTimer(self)
        self._capture_timer.setInterval(100)
        self._capture_timer.timeout.connect(self._poll_capture_browser_ready)
        self._pending_timer = QTimer(self)
        self._pending_timer.setSingleShot(True)
        self._pending_timer.timeout.connect(self._check_pending_actions)
        self._build_ui()
        self.refresh_all()
        self._poll_timer.start()
        if not smoke_mode:
            self._pending_timer.start(500)

    def _configure_style(self) -> None:
        theme.apply_theme(QApplication.instance())

    def _button(self, layout, text, callback=None, *, name="", variant="", task=False):
        button = QPushButton(text)
        button.setObjectName(name)
        if variant:
            button.setProperty("variant", variant)
        if callback is not None:
            button.clicked.connect(callback)
        layout.addWidget(button)
        if task:
            self._register_task_button(button)
        return button

    @staticmethod
    def _label(layout, text, *, muted=False, stretch=0):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        if muted:
            label.setProperty("role", "muted")
        layout.addWidget(label, stretch)
        return label

    @staticmethod
    def _row(layout):
        row = QHBoxLayout()
        row.setSpacing(8)
        layout.addLayout(row)
        return row

    @staticmethod
    def _input(layout, text="", *, name="", width=None):
        field = QLineEdit(text)
        field.setObjectName(name)
        if width is not None:
            field.setFixedWidth(width)
        layout.addWidget(field)
        return field

    def _build_ui(self) -> None:
        self._current_page: str | None = None
        self._task_buttons: list[QPushButton] = []
        shell = QWidget(self)
        self.setCentralWidget(shell)
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        self._build_sidebar(shell_layout)
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        shell_layout.addLayout(right, 1)
        self.page_stack = QStackedWidget()
        right.addWidget(self.page_stack, 1)
        self.pages: dict[str, QWidget] = {}
        for key, _label in self.NAV_ITEMS:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(20, 12, 20, 12)
            layout.setSpacing(10)
            self.pages[key] = page
            self.page_stack.addWidget(page)
        self._build_home_page(self.pages["home"])
        self._build_friends_page(self.pages["friends"])
        self._build_history_page(self.pages["history"])
        self._build_logs_page(self.pages["logs"])
        self._build_status_bar(right)
        self._show_page("home")

    def _build_sidebar(self, shell_layout) -> None:
        rail = QWidget()
        rail.setObjectName("sidebar")
        rail.setFixedWidth(196)
        shell_layout.addWidget(rail)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(10, 18, 10, 16)
        brand = self._label(layout, "续火花助手")
        brand.setProperty("role", "brand")
        self.version_label = self._label(layout, f"本地自用版 · {__version__}", muted=True)
        layout.addSpacing(16)
        self._nav_buttons: dict[str, QPushButton] = {}
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        for key, label in self.NAV_ITEMS:
            self._build_nav_item(layout, key, label)
        layout.addStretch()
        self.mode_chip = theme.make_chip(rail, "本地自用")
        layout.addWidget(self.mode_chip)
        self._label(layout, "不绕过验证码与平台风控；网络仅用于正常访问抖音网页。", muted=True)

    def _build_nav_item(self, layout, key: str, label_text: str) -> None:
        button = self._button(layout, label_text, lambda: self._show_page(key), name="nav-button")
        button.setCheckable(True)
        button.setProperty("page", key)
        self._nav_group.addButton(button)
        self._nav_buttons[key] = button

    def _show_page(self, key: str) -> None:
        if key == self._current_page or self._closed:
            return
        previous = self.pages.get(self._current_page)
        focused = QApplication.focusWidget()
        self.page_stack.setCurrentWidget(self.pages[key])
        if previous and focused and (previous.isAncestorOf(focused) or focused is previous):
            self._nav_buttons[key].setFocus()
        self._current_page = key
        self._page_epoch += 1
        self._nav_buttons[key].setChecked(True)
        self._request_page_snapshot(key)

    def _build_status_bar(self, right) -> None:
        bar = QWidget()
        right.addWidget(bar)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 8, 14, 8)
        self.status_label = self._label(layout, "就绪", stretch=1)
        program_directory = (
            Path(sys.executable).resolve().parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[3]
        )
        self.program_path_label = _ProgramDirectoryLabel(program_directory)
        layout.addWidget(self.program_path_label, 1)
        self.pending_label = self._label(layout, "")
        self.pending_label.setProperty("role", "warning")
        self.status_progress = QProgressBar()
        self.status_progress.setRange(0, 0)
        self.status_progress.setTextVisible(False)
        self.status_progress.setFixedWidth(100)
        layout.addWidget(self.status_progress)
        self.status_progress.hide()
        self.cancel_button = self._button(
            layout, "停止后续目标", self._cancel_current, name="btn-cancel", variant="danger"
        )
        self.cancel_button.setEnabled(False)

    def _register_task_button(self, button: QPushButton) -> QPushButton:
        self._task_buttons.append(button)
        return button

    def _set_task_buttons_state(self, enabled: bool) -> None:
        self._task_buttons_enabled = enabled
        for button in self._task_buttons:
            button.setEnabled(enabled)
        self._update_target_selection()

    def _build_home_page(self, page: QWidget) -> None:
        layout = page.layout()
        layout.addWidget(
            theme.make_page_header(
                page, "任务台", "登录账号、选择发送内容、配置每日计划，并执行验证或整批手动发送。"
            )
        )
        account = theme.make_card(
            page,
            title="账号",
            description="登录状态由 Windows DPAPI 加密保存，仅支持一个抖音账号。",
        )
        layout.addWidget(account)
        row = self._row(account.layout())
        self.account_label = self._label(row, "未登录", stretch=1)
        self.logout_button = self._button(
            row, "清除登录", self._logout, name="btn-logout", variant="danger", task=True
        )
        self.login_button = self._button(
            row, "扫码登录", self._login, name="btn-login", variant="primary", task=True
        )
        actions = self._row(layout)
        self.validate_button = self._button(
            actions, "验证配置（不发送）", self._validate, name="btn-validate", task=True
        )
        self.send_button = self._button(
            actions,
            "预览并手动发送",
            self._manual_send,
            name="btn-manual-send",
            variant="primary",
            task=True,
        )
        actions.addStretch()
        self._button(actions, "刷新", self.refresh_all, name="btn-refresh-home", task=True)
        body = QHBoxLayout()
        layout.addLayout(body, 1)
        message = theme.make_card(
            page,
            title="发送内容",
            description="所有启用好友共用一种内容；原生表情与文本互斥发送。",
        )
        body.addWidget(message, 5)
        modes = self._row(message.layout())
        self.message_kind_group = QButtonGroup(self)
        self.text_mode_button = QRadioButton("文本")
        self.text_mode_button.setObjectName("radio-message-text")
        self.sticker_mode_button = QRadioButton("续火花（原生表情）")
        self.sticker_mode_button.setObjectName("radio-message-spark-sticker")
        for button in (self.text_mode_button, self.sticker_mode_button):
            self.message_kind_group.addButton(button)
            modes.addWidget(button)
        self.text_mode_button.setChecked(True)
        self.message_text = QPlainTextEdit()
        self.message_text.setObjectName("text-message")
        message.layout().addWidget(self.message_text, 1)
        self.sticker_preview = QWidget()
        sticker_layout = QVBoxLayout(self.sticker_preview)
        self.sticker_preview_image = QLabel()
        self.sticker_preview_image.setObjectName("image-spark-sticker")
        self.sticker_preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sticker_preview_image.setMinimumSize(100, 100)
        preview_path = Path(__file__).resolve().parents[1] / "assets" / "spark-sticker.png"
        pixmap = QPixmap(str(preview_path))
        if pixmap.isNull():
            self.sticker_preview_image.setText("表情预览资源无法加载；请检查安装文件。")
        else:
            self.sticker_preview_image.setPixmap(
                pixmap.scaled(
                    100,
                    100,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        sticker_layout.addWidget(self.sticker_preview_image, 1)
        self._label(
            sticker_layout,
            "续火花（原生表情）\n从抖音表情面板发送大表情，不会发送同名文本。\n此处仅为预览，不表示已经发送。",
            muted=True,
        )
        message.layout().addWidget(self.sticker_preview, 1)
        self.sticker_mode_button.toggled.connect(self._update_message_mode)
        self._update_message_mode()
        right = QVBoxLayout()
        body.addLayout(right, 7)
        schedule = theme.make_card(
            page,
            title="每日计划与发送节奏",
            description="到点自动执行；手动与定时发送均在好友间随机等待。等待不能保证避免平台限制。",
        )
        right.addWidget(schedule)
        self.scheduler_chip = theme.make_chip(schedule, "系统任务未创建")
        schedule.layout().addWidget(self.scheduler_chip, 0, Qt.AlignmentFlag.AlignRight)
        row = self._row(schedule.layout())
        self.schedule_enabled = QCheckBox("启用")
        self.schedule_enabled.setObjectName("check-schedule-enabled")
        row.addWidget(self.schedule_enabled)
        self._label(row, "每天")
        self.schedule_time_input = self._input(row, "09:00", name="entry-schedule-time", width=68)
        row.addStretch()
        self._button(
            row, "保存计划", self._save_plan, name="btn-save-plan", variant="primary", task=True
        )
        row = self._row(schedule.layout())
        self._label(row, "好友间随机等待")
        delay_width = max(42, self.fontMetrics().horizontalAdvance("120") + 24)
        self.delay_min_input = self._input(row, "3", name="entry-delay-min", width=delay_width)
        self._label(row, "至")
        self.delay_max_input = self._input(row, "8", name="entry-delay-max", width=delay_width)
        self._label(row, "秒（0–0 可关闭）", muted=True)
        row.addStretch()
        progress = theme.make_card(page, title="本次进度")
        right.addWidget(progress, 1)
        self.progress_tree = theme.make_tree(progress, ("目标", "状态"), bordered=False)
        self.progress_tree.setObjectName("tree-progress")
        progress.layout().addWidget(self.progress_tree, 1)
        self.progress_hint = self._label(
            progress.layout(), "尚未执行任务；验证配置或手动发送后显示实时状态。", muted=True
        )

    def _build_friends_page(self, page: QWidget) -> None:
        layout = page.layout()
        layout.addWidget(
            theme.make_page_header(
                page, "好友管理", "搜索并确认已授权好友；同名候选必须人工核对，不会自动选择。"
            )
        )
        search = theme.make_card(
            page,
            title="添加好友",
            description="搜索已有会话；也可在可见浏览器中手动打开正确聊天后只读捕获。",
        )
        layout.addWidget(search)
        row = self._row(search.layout())
        self.search_input = self._input(row, name="entry-search")
        self.search_button = self._button(
            row, "搜索已有会话", self._search, name="btn-search", variant="primary", task=True
        )
        self.capture_open_button = self._button(
            row,
            "打开浏览器选择",
            self._capture_friend_in_browser,
            name="btn-open-capture",
            task=True,
        )
        self.capture_read_button = self._button(
            row, "读取当前聊天", self._request_current_chat_capture, name="btn-read-chat"
        )
        self.capture_read_button.setEnabled(False)
        row = self._row(search.layout())
        self.scan_sparks_button = self._button(
            row,
            "扫描火花好友（不发送）",
            self._scan_spark_contacts,
            name="btn-scan-sparks",
            task=True,
        )
        self._label(row, "先预览再导入，新好友默认停用。", muted=True)
        row.addStretch()
        body = QHBoxLayout()
        layout.addLayout(body, 1)
        candidates = theme.make_card(
            page, title="候选结果", description="同名候选需人工核对身份依据，不会自动选择。"
        )
        body.addWidget(candidates, 11)
        self.candidate_tree = theme.make_tree(candidates, ("昵称", "抖音号", "依据", "标识摘要"))
        self.candidate_tree.setObjectName("tree-candidates")
        candidates.layout().addWidget(self.candidate_tree, 1)
        self.candidate_hint = self._label(
            candidates.layout(), "尚无候选；搜索已有会话或读取当前聊天。", muted=True
        )
        self._button(
            candidates.layout(),
            "确认并保存所选好友",
            self._save_selected_candidate,
            name="btn-save-candidate",
            variant="primary",
            task=True,
        )
        saved = theme.make_card(
            page, title="已保存好友", description="每次发送前会重新确认当前聊天对象。"
        )
        body.addWidget(saved, 10)
        self.target_tree = theme.make_tree(saved, ("启用", "昵称", "抖音号", "搜索词", "确认时间"))
        self.target_tree.setObjectName("tree-targets")
        self.target_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # QTreeView otherwise sends HasFocus to only the current cell's delegate.
        self.target_tree.setAllColumnsShowFocus(True)
        saved.layout().addWidget(self.target_tree, 1)
        self.target_hint = self._label(
            saved.layout(), "尚未保存好友；先搜索并确认候选。", muted=True
        )
        row = self._row(saved.layout())
        self.target_selection_label = self._label(row, "", muted=True)
        self.target_selection_label.setToolTip("Ctrl 多选，Shift 连选")
        row.addStretch()
        self.target_selection_button = self._button(
            row,
            "全选",
            self._toggle_target_selection,
            name="btn-toggle-target-selection",
            task=True,
        )
        self.target_actions_button = self._button(
            row, "操作", name="btn-target-actions", task=True
        )
        self.target_actions_menu = QMenu(self.target_actions_button)
        self.target_enable_action = self.target_actions_menu.addAction("启用所选")
        self.target_enable_action.triggered.connect(
            lambda: self._set_selected_targets_enabled(True)
        )
        self.target_disable_action = self.target_actions_menu.addAction("停用所选")
        self.target_disable_action.triggered.connect(
            lambda: self._set_selected_targets_enabled(False)
        )
        self.target_actions_menu.addSeparator()
        self.target_delete_action = self.target_actions_menu.addAction("删除所选…")
        self.target_delete_action.triggered.connect(self._delete_targets)
        self.target_clear_selection_action = self.target_actions_menu.addAction("取消选择")
        self.target_clear_selection_action.triggered.connect(self._clear_target_selection)
        self.target_actions_menu.aboutToShow.connect(self._update_target_selection)
        self.target_actions_button.setMenu(self.target_actions_menu)
        self.target_tree.itemSelectionChanged.connect(self._update_target_selection)
        self.saved_count_label = self._label(
            saved.layout(), "已启用 0 位 · 共保存 0 位", muted=True
        )

    def _build_history_page(self, page: QWidget) -> None:
        layout = page.layout()
        layout.addWidget(
            theme.make_page_header(
                page, "发送历史", "每次发送尝试的终态记录；选中记录可在下方查看内容类型与完整文本。"
            )
        )
        row = self._row(layout)
        self._button(row, "刷新", self._refresh_history, name="btn-refresh-history")
        self._label(
            row, "清理仅删除今天以前的记录；今天的防重复依据会保留。", muted=True, stretch=1
        )
        self._button(
            row,
            "清理今天以前的历史",
            self._clear_history,
            name="btn-clear-history",
            variant="danger",
            task=True,
        )
        card = theme.make_card(
            page,
            title="尝试记录",
            description="包含终态、批次模式、内容类型与错误分类；记录保留直到主动清理。",
        )
        layout.addWidget(card, 1)
        self.history_tree = theme.make_tree(
            card, ("时间", "好友", "结果", "模式", "人工覆盖", "错误分类", "内容类型")
        )
        self.history_tree.setObjectName("tree-history")
        card.layout().addWidget(self.history_tree, 1)
        self.history_hint = self._label(
            card.layout(), "暂无发送历史；手动、定时或补发尝试都会记录。", muted=True
        )
        self.history_tree.itemSelectionChanged.connect(self._show_history_message)
        message = theme.make_card(page, title="所选记录的内容")
        layout.addWidget(message)
        self.history_message = QPlainTextEdit()
        self.history_message.setObjectName("text-history-message")
        self.history_message.setReadOnly(True)
        self.history_message.setMaximumHeight(110)
        message.layout().addWidget(self.history_message)

    def _build_logs_page(self, page: QWidget) -> None:
        layout = page.layout()
        layout.addWidget(
            theme.make_page_header(page, "运行日志", "任务事件按批次与真实目标记录，便于诊断。")
        )
        row = self._row(layout)
        self._label(
            row, "事件保留原始诊断；不会主动读取登录凭据或聊天正文。", muted=True, stretch=1
        )
        self._button(row, "刷新", self._refresh_logs, name="btn-refresh-logs")
        card = theme.make_card(
            page, title="事件列表", description="选中事件查看完整原文；失败截图与 Trace 默认关闭。"
        )
        layout.addWidget(card, 1)
        self.logs_tree = theme.make_tree(card, ("时间", "级别", "目标", "分类", "内容"))
        self.logs_tree.setObjectName("tree-logs")
        card.layout().addWidget(self.logs_tree, 1)
        self.logs_tree.itemSelectionChanged.connect(self._show_log_message)
        self.logs_hint = self._label(
            card.layout(), "暂无日志；任务事件与错误详情会记录于此。", muted=True
        )
        detail = theme.make_card(page, title="所选事件的完整原文")
        layout.addWidget(detail)
        self.logs_message = QPlainTextEdit()
        self.logs_message.setReadOnly(True)
        self.logs_message.setObjectName("text-log-detail")
        detail.layout().addWidget(self.logs_message)

    def _set_status(self, text: str, tone: str = "ready") -> None:
        self.status_label.setText(text)
        color = {"ready": theme.SUCCESS, "busy": theme.ACCENT, "warn": theme.WARNING}.get(
            tone, theme.TEXT_MUTED
        )
        palette = self.status_label.palette()
        palette.setColor(QPalette.ColorRole.WindowText, QColor(color))
        self.status_label.setPalette(palette)

    @staticmethod
    def _sync_empty_hint(tree: QTreeWidget, hint: QLabel) -> None:
        hint.setVisible(tree.topLevelItemCount() == 0)

    @staticmethod
    def _selected_keys(tree: QTreeWidget) -> list[str]:
        return [str(item.data(0, theme.ROLE_ID)) for item in tree.selectedItems()]

    @staticmethod
    def _update_tree(
        tree: QTreeWidget, rows: list[tuple[str, tuple[Any, ...], tuple[str, ...]]]
    ) -> None:
        """Keep native items, selection and both scrollbars when applying row differences."""
        existing = {
            str(tree.topLevelItem(i).data(0, theme.ROLE_ID)): tree.topLevelItem(i)
            for i in range(tree.topLevelItemCount())
        }
        desired = {key for key, _values, _tags in rows}
        selected = set(SparkKeeperApp._selected_keys(tree))
        current = tree.currentItem()
        current_key = str(current.data(0, theme.ROLE_ID)) if current else None
        x, y = tree.horizontalScrollBar().value(), tree.verticalScrollBar().value()
        blocker = QSignalBlocker(tree)
        changed = False
        for key in existing.keys() - desired:
            tree.takeTopLevelItem(tree.indexOfTopLevelItem(existing[key]))
            changed = True
        for index, (key, values, tags) in enumerate(rows):
            item = existing.get(key)
            if item is None:
                item = QTreeWidgetItem()
                item.setData(0, theme.ROLE_ID, key)
                tree.insertTopLevelItem(index, item)
                changed = True
            elif tree.indexOfTopLevelItem(item) != index:
                tree.takeTopLevelItem(tree.indexOfTopLevelItem(item))
                tree.insertTopLevelItem(index, item)
                changed = True
            for column, value in enumerate(values):
                if item.text(column) != str(value):
                    item.setText(column, str(value))
                    changed = True
            tone = (
                {
                    "success": "success",
                    "disabled": "muted",
                    "strong": "success",
                    "weak": "warning",
                    "failed": "danger",
                    "unknown": "warning",
                    "duplicate": "muted",
                    "cancelled": "muted",
                    "WARNING": "warning",
                    "ERROR": "danger",
                    "INFO": "normal",
                    "DEBUG": "normal",
                    "CRITICAL": "danger",
                }.get(tags[0], "normal")
                if tags
                else "normal"
            )
            if item.data(0, theme.ROLE_ID + 1) != tone:
                theme.set_tree_row_tone(item, tone)
                item.setData(0, theme.ROLE_ID + 1, tone)
                changed = True
        if changed:
            if current_key in desired:
                for index in range(tree.topLevelItemCount()):
                    item = tree.topLevelItem(index)
                    if str(item.data(0, theme.ROLE_ID)) == current_key:
                        tree.setCurrentItem(item)
                        break
            for index in range(tree.topLevelItemCount()):
                item = tree.topLevelItem(index)
                item.setSelected(str(item.data(0, theme.ROLE_ID)) in selected)
            tree.horizontalScrollBar().setValue(x)
            tree.verticalScrollBar().setValue(y)
        del blocker

    def _refresh_account(self) -> None:
        account = self.database.get_account()
        preview = self._spark_preview
        if preview and (
            account is None
            or account.platform_user_id != preview.scan.account_key
            or account.logged_in_at != preview.scan.account_logged_in_at
        ):
            self._close_spark_preview()
        logged_in = bool(account and self.state_store.exists())
        self.account_label.setText(
            f"已登录 · {account.display_name} · 状态已由 Windows DPAPI 保护"
            if logged_in
            else "未登录"
        )
        self.login_button.setVisible(not logged_in)
        self.logout_button.setVisible(logged_in)

    def _refresh_plan(self) -> None:
        plan = self.database.get_plan()
        self.schedule_enabled.setChecked(plan.enabled)
        self.schedule_time_input.setText(plan.send_time)
        if plan.message_kind == MessageKind.TEXT:
            self.message_text.setPlainText(plan.message_text)
        self.sticker_mode_button.setChecked(plan.message_kind == MessageKind.SPARK_STICKER)
        self.text_mode_button.setChecked(plan.message_kind == MessageKind.TEXT)
        self.delay_min_input.setText(str(plan.delay_min_seconds))
        self.delay_max_input.setText(str(plan.delay_max_seconds))
        self._scheduler_version += 1
        self._request_scheduler_snapshot()

    def _request_scheduler_snapshot(self) -> None:
        if self._closed or self._scheduler_read is not None:
            return
        token = self._scheduler_version
        self._scheduler_read = token
        scheduler, messages = self.scheduler, self.messages

        def read_snapshot() -> None:
            try:
                exists = scheduler.exists()
            except Exception as exc:  # noqa: BLE001 - return read diagnostics to Qt
                messages.put(("scheduler_snapshot", token, None, format_error(exc)))
            else:
                messages.put(("scheduler_snapshot", token, exists, None))

        threading.Thread(target=read_snapshot, name="spark-keeper-scheduler", daemon=True).start()

    def _accept_scheduler_snapshot(self, token: int, exists: bool | None, error: str | None) -> None:
        if self._scheduler_read != token:
            return
        self._scheduler_read = None
        if token != self._scheduler_version:
            self._request_scheduler_snapshot()
            return
        if error is not None:
            theme.set_chip(self.scheduler_chip, "系统任务状态读取失败", "warning")
            self.logs_message.setPlainText(self._record_ui_error("schedule_read_failed", error))
            return
        theme.set_chip(
            self.scheduler_chip,
            "系统任务已创建" if exists else "系统任务未创建",
            "success" if exists else "neutral",
        )

    def _close_spark_preview(self) -> None:
        self._preview_timer.stop()
        preview, self._spark_preview = self._spark_preview, None
        if preview is not None:
            preview.close()
            preview.deleteLater()

    def _watch_spark_preview(self) -> None:
        if self._closed or self._spark_preview is None:
            self._preview_timer.stop()
            return
        self._validate_spark_preview()

    def _show_candidates(self, candidates: list[FriendCandidate]) -> None:
        self.candidates = {candidate.stable_key: candidate for candidate in candidates}
        self._update_tree(
            self.candidate_tree,
            [
                (
                    candidate.stable_key,
                    (
                        candidate.display_name,
                        candidate.douyin_id or "—",
                        "强" if candidate.evidence.get("identity_strength") == "strong" else "弱",
                        candidate.stable_key[:16],
                    ),
                    (
                        "strong"
                        if candidate.evidence.get("identity_strength") == "strong"
                        else "weak",
                    ),
                )
                for candidate in candidates
            ],
        )
        self._sync_empty_hint(self.candidate_tree, self.candidate_hint)

    def _poll_capture_browser_ready(self) -> None:
        if self._closed or not self.busy:
            self._capture_timer.stop()
            self.capture_read_button.setEnabled(False)
        elif self.capture_browser_ready_event.is_set():
            self._capture_timer.stop()
            self.capture_read_button.setEnabled(True)

    def _clear_progress(self) -> None:
        self.progress_tree.clear()
        self._sync_empty_hint(self.progress_tree, self.progress_hint)

    def _show_history_message(self) -> None:
        selected = self._selected_keys(self.history_tree)
        message = self.history_messages.get(selected[0], "") if selected else ""
        if self.history_message.toPlainText() != message:
            self.history_message.setPlainText(message)

    def _show_log_message(self) -> None:
        selected = self._selected_keys(self.logs_tree)
        message = self.log_messages.get(selected[0], "") if selected else ""
        if self.logs_message.toPlainText() != message:
            self.logs_message.setPlainText(message)

    def _poll_messages(self) -> None:
        if self._closed:
            return
        try:
            while not self._closed:
                item = self.messages.get_nowait()
                kind = item[0]
                try:
                    if kind in {"error", "complete"}:
                        self._accept_task_result(item)
                        continue
                    if kind == "page_snapshot":
                        self._accept_page_snapshot(*item[1:])
                    elif kind == "scheduler_snapshot":
                        self._accept_scheduler_snapshot(*item[1:])
                    elif kind == "status":
                        self._set_status(str(item[1]), "busy")
                    elif kind == "progress":
                        alias, state = str(item[1]), str(item[2])
                        rows = [
                            (
                                str(self.progress_tree.topLevelItem(i).data(0, theme.ROLE_ID)),
                                tuple(self.progress_tree.topLevelItem(i).text(c) for c in range(2)),
                                (),
                            )
                            for i in range(self.progress_tree.topLevelItemCount())
                        ]
                        for index, (key, _values, _tags) in enumerate(rows):
                            if key == alias:
                                rows[index] = (alias, (alias, state), ())
                                break
                        else:
                            rows.append((alias, (alias, state), ()))
                        self._update_tree(self.progress_tree, rows)
                        self._sync_empty_hint(self.progress_tree, self.progress_hint)
                except Exception as exc:  # noqa: BLE001 - final UI callback boundary
                    self._show_error(exc)
        except queue.Empty:
            pass

    def _accept_task_result(self, item: tuple[Any, ...]) -> None:
        self._finish_busy()
        error = item[2] if item[0] == "error" else None
        if error is None:
            try:
                item[2](item[3])
            except Exception as exc:  # noqa: BLE001 - invoke completion exactly once
                error = exc
        if isinstance(error, AutomationError) and error.code == ErrorCode.CANCELLED:
            self._set_status("登录已取消" if item[1] == "扫码登录" else "任务已取消", "warn")
        elif error is not None:
            try:
                self._show_error(error)
            except Exception as presentation_error:  # noqa: BLE001 - never recursively show errors
                error.add_note("错误提示未完成：\n" + format_error(presentation_error))
                self.logs_message.setPlainText(format_error(error))
        self._refresh_task_views(self._refresh_pending_indicator)
        if item[1] == "扫码登录" and error is not None:
            self._refresh_task_views(self._refresh_account, self._refresh_plan)

    def _refresh_task_views(self, *refreshers: Callable[[], None]) -> None:
        """Secondary reads never replace a completed task's result or open another dialog."""
        for refresh in refreshers:
            try:
                refresh()
            except Exception as exc:  # noqa: BLE001 - independent best-effort display refresh
                detail = self._record_ui_error("task_refresh_failed", format_error(exc))
                self.logs_message.setPlainText(detail)

    def _on_close(self) -> None:
        self.close()

    def activate_existing_window(self) -> None:
        if not self._closed:
            activate_window(self)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.busy:
            prompt = (
                "登录仍在进行。取消登录并等待浏览器安全关闭？"
                if self._login_running
                else "任务仍在运行。停止后续目标并等待当前步骤结束？"
            )
            if theme.confirm(self, APP_NAME, prompt):
                self._cancel_current()
            event.ignore()
            return
        self._closed = True
        for timer in (
            self._poll_timer,
            self._preview_timer,
            self._capture_timer,
            self._pending_timer,
        ):
            timer.stop()
        self._close_spark_preview()
        event.accept()

    def _request_page_snapshot(self, key: str) -> None:
        if self._closed or key not in self._page_versions or key in self._page_reads:
            return
        token = (self._page_versions[key], self._page_epoch)
        self._page_reads[key] = token
        reader = {
            "friends": self.database.list_targets,
            "history": self.database.list_history,
            "logs": self.database.list_events,
        }[key]
        messages = self.messages

        def read_snapshot() -> None:
            try:
                rows = reader()
            except Exception as exc:  # noqa: BLE001 - return worker failures to the UI
                messages.put(("page_snapshot", key, token, None, format_error(exc)))
            else:
                messages.put(("page_snapshot", key, token, rows, None))

        threading.Thread(target=read_snapshot, name=f"spark-keeper-page-{key}", daemon=True).start()

    def _accept_page_snapshot(
        self, key: str, token: tuple[int, int], rows: Any, error: str | None
    ) -> None:
        if self._page_reads.get(key) != token:
            return
        del self._page_reads[key]
        if self._closed or key != self._current_page:
            return
        if token != (self._page_versions[key], self._page_epoch):
            self._request_page_snapshot(key)
            return
        if error is not None:
            error = self._record_ui_error("page_read_failed", error)
            self.logs_message.setPlainText(error)
            self._set_status("页面读取失败；完整诊断见运行日志", "warn")
            return
        {
            "friends": self._display_targets,
            "history": self._display_history,
            "logs": self._display_logs,
        }[key](rows)

    def refresh_all(self) -> None:
        self._refresh_account()
        self._refresh_plan()
        self._refresh_targets()
        self._refresh_history()
        self._refresh_logs()
        self._refresh_pending_indicator()

    def _refresh_targets(self) -> None:
        self._page_versions["friends"] += 1
        self._display_targets(self.database.list_targets())

    def _display_targets(self, targets: list[Target]) -> None:
        self._target_enabled = {str(target.id): target.enabled for target in targets}
        self._update_tree(
            self.target_tree,
            [
                (
                    str(target.id),
                    (
                        "是" if target.enabled else "否",
                        target.display_name,
                        target.douyin_id or "—",
                        target.search_query,
                        target.confirmed_at,
                    ),
                    () if target.enabled else ("disabled",),
                )
                for target in targets
            ],
        )
        self._sync_empty_hint(self.target_tree, self.target_hint)
        enabled_count = sum(1 for target in targets if target.enabled)
        count_text = f"已启用 {enabled_count} 位 · 共保存 {len(targets)} 位"
        if self.saved_count_label.text() != count_text:
            self.saved_count_label.setText(count_text)
        self.saved_count_label.setVisible(bool(targets))
        self._update_target_selection()

    def _refresh_history(self) -> None:
        self._page_versions["history"] += 1
        self._display_history(self.database.list_history())

    def _display_history(self, rows: list[dict[str, Any]]) -> None:
        self.history_messages = {
            str(row["id"]): (
                "内容类型：续火花（原生表情）\n此记录为原生表情尝试，不是已发送的同名文本；是否成功请以结果列为准。"
                if row.get("message_kind", "text") == MessageKind.SPARK_STICKER
                else str(row["message_text"])
            )
            for row in rows
        }
        self._update_tree(
            self.history_tree,
            [
                (
                    str(row["id"]),
                    (
                        row["started_at"],
                        row["target_name"],
                        str(row["status"]),
                        row["mode"],
                        "是" if row["manual_override"] else "否",
                        row["error_code"] or "—",
                        "续火花（原生表情）"
                        if row.get("message_kind", "text") == MessageKind.SPARK_STICKER
                        else "文本",
                    ),
                    (str(row["status"]),),
                )
                for row in rows
            ],
        )
        self._sync_empty_hint(self.history_tree, self.history_hint)
        self._show_history_message()

    def _refresh_logs(self) -> None:
        self._page_versions["logs"] += 1
        self._display_logs(self.database.list_events())

    def _display_logs(self, rows: list[dict[str, Any]]) -> None:
        self.log_messages = {str(row["id"]): str(row["message"]) for row in rows}
        self._update_tree(
            self.logs_tree,
            [
                (
                    str(row["id"]),
                    (
                        row["created_at"],
                        str(row["level"]),
                        row["target_alias"] or "—",
                        row["category"],
                        " ".join(str(row["message"]).splitlines()),
                    ),
                    (str(row["level"]),),
                )
                for row in rows
            ],
        )
        self._sync_empty_hint(self.logs_tree, self.logs_hint)
        self._show_log_message()

    def _refresh_pending_indicator(self) -> None:
        count = len(self.database.list_pending_actions())
        self.pending_label.setText(f"待处理事项：{count} 项" if count else "")

    def _login(self) -> None:
        if self.busy or self._closed:
            return
        service, database, scheduler = self.service, self.database, self.scheduler
        messages, cancel = self.messages, self.cancel_event

        async def operation() -> None:
            await service.login(status=lambda value: messages.put(("status", value)), cancel=cancel)
            for action in database.list_pending_actions("login_required"):
                database.resolve_pending_action(str(action["id"]), "completed")
            scheduler.sync(database.get_plan())

        def complete(_: None) -> None:
            theme.show_message(self, APP_NAME, "登录状态已使用 Windows DPAPI 安全保存。")
            self._refresh_task_views(self.refresh_all)

        self._login_running = True
        self._start_async("扫码登录", operation, complete)
        self.cancel_button.setText("取消登录")

    def _logout(self) -> None:
        if self.busy or self._closed:
            return
        if not theme.confirm(self, APP_NAME, "清除本机登录状态？好友、计划和历史不会删除。"):
            return
        self.service.logout()
        self.refresh_all()

    def _validate_spark_preview(self) -> bool:
        preview = self._spark_preview
        if preview is None:
            return False
        try:
            account = self.database.get_account()
            if (
                account is None
                or account.platform_user_id != preview.scan.account_key
                or account.logged_in_at != preview.scan.account_logged_in_at
            ):
                raise ValueError("登录账号或登录会话已变化，请重新扫描火花好友。")
        except (ValueError, sqlite3.Error) as exc:
            self._close_spark_preview()
            self._show_error(exc)
            return False
        return True

    def _scan_spark_contacts(self) -> None:
        if self.busy:
            theme.show_message(self, APP_NAME, "已有任务正在运行。", icon=QMessageBox.Icon.Warning)
            return
        self._close_spark_preview()
        self._spark_scan_running = True

        async def operation() -> SparkScanResult:
            return await self.service.scan_spark_contacts(
                progress=lambda value: self.messages.put(("status", value)),
                cancel=self.cancel_event,
            )

        self._start_async("扫描火花好友（不发送）", operation, self._show_spark_preview)
        self.cancel_button.setText("取消扫描")

    def _show_spark_preview(self, scan: SparkScanResult) -> None:
        self._close_spark_preview()
        try:
            account = self.database.get_account()
            if (
                account is None
                or account.platform_user_id != scan.account_key
                or account.logged_in_at != scan.account_logged_in_at
            ):
                raise ValueError("登录账号或登录会话已变化，扫描结果已失效，请重新扫描。")
            targets = self.database.list_targets()
            saved_keys = {target.stable_key for target in targets}
            saved_profiles = {target.profile_url for target in targets if target.profile_url}
            saved_ids = {target.douyin_id for target in targets if target.douyin_id}
            saved_keys.update(
                contact.candidate.stable_key
                for contact in scan.contacts
                if contact.candidate.profile_url in saved_profiles
                or contact.candidate.douyin_id in saved_ids
            )
        except (ValueError, sqlite3.Error) as exc:
            self._show_error(exc)
            return
        self._spark_preview = SparkImportPreview(
            self,
            scan,
            saved_keys,
            validate=self._validate_spark_preview,
            import_selected=self._import_spark_contacts,
            close=self._close_spark_preview,
        )
        self._spark_preview.show()
        self._preview_timer.start()
        if scan.status != SparkScanStatus.COMPLETE:
            self._set_status("火花扫描未完整结束；请查看部分结果和识别限制", "warn")
        else:
            self._set_status("火花扫描已结束；仅预览，尚未导入或发送")

    def _import_spark_contacts(self) -> None:
        if self.busy or not self._validate_spark_preview():
            return
        preview = self._spark_preview
        if preview is None:
            return
        selected_keys = tuple(sorted(preview.selected_keys))
        if not selected_keys:
            theme.show_message(
                preview, APP_NAME, "请勾选至少一位好友。", icon=QMessageBox.Icon.Warning
            )
            return
        if set(selected_keys) - preview.eligible_keys:
            theme.show_message(
                preview,
                APP_NAME,
                "所选项包含群聊或尚未确认的好友身份，本次不会导入任何记录。\n请取消这些选择，或先清空选择，再筛选“可导入”后选择。",
                icon=QMessageBox.Icon.Warning,
            )
            return
        if not theme.confirm(
            preview,
            APP_NAME,
            f"确认导入所选 {len(selected_keys)} 位好友？\n\n仅保存，不发送消息。新好友默认停用；已保存好友的启用状态和资料均保持不变。\n请在核对后，只启用已获授权的好友。",
        ):
            return
        if self._spark_preview is not preview or not self._validate_spark_preview():
            return

        scan = preview.scan

        async def operation() -> tuple[int, int]:
            return await asyncio.to_thread(self.service.import_spark_contacts, scan, selected_keys)

        def complete(counts: tuple[int, int]) -> None:
            self._close_spark_preview()
            self._refresh_targets()
            added, existing = counts
            theme.show_message(
                self,
                APP_NAME,
                f"导入完成：新增 {added} 位，已存在 {existing} 位。\n未发送任何消息。新增好友均已停用，既有好友保持原状态。\n请核对并只启用获授权好友。",
            )

        self._start_async("导入火花好友（不发送）", operation, complete)
        self.cancel_button.setEnabled(False)

    def _capture_friend_in_browser(self) -> None:
        expected_name = self.search_input.text().strip()
        if not expected_name:
            theme.show_message(
                self,
                APP_NAME,
                "请填写右侧聊天顶部显示的准确名称；设置过备注时通常是备注名。",
                icon=QMessageBox.Icon.Warning,
            )
            return
        if self.busy:
            theme.show_message(self, APP_NAME, "已有任务正在运行。", icon=QMessageBox.Icon.Warning)
            return
        theme.show_message(
            self,
            APP_NAME,
            "接下来会打开应用当前账号的可见浏览器。\n\n请按备注名或昵称搜索并手动打开正确好友的私信聊天；输入框中应填写右侧聊天顶部实际显示的名称（设置过备注时通常是备注名）。不要输入或发送消息。打开后返回本软件，点击“读取当前聊天”。",
        )
        self.capture_requested_event.clear()
        self.capture_browser_ready_event.clear()

        async def operation() -> FriendCandidate:
            return await self.service.capture_current_chat_friend(
                expected_name,
                capture_requested=self.capture_requested_event,
                browser_ready=self.capture_browser_ready_event,
                cancel=self.cancel_event,
                status=lambda value: self.messages.put(("status", value)),
            )

        def complete(candidate: FriendCandidate) -> None:
            self._show_candidates([candidate])
            theme.show_message(
                self,
                APP_NAME,
                "已只读提取当前聊天候选，没有输入或发送消息。请核对候选后再确认保存。",
            )

        self._start_async("可见浏览器选择好友", operation, complete)
        self._capture_timer.start()

    def _request_current_chat_capture(self) -> None:
        if not self.capture_browser_ready_event.is_set():
            theme.show_message(
                self, APP_NAME, "好友选择浏览器尚未就绪。", icon=QMessageBox.Icon.Warning
            )
            return
        self.capture_requested_event.set()
        self.capture_read_button.setEnabled(False)
        self._set_status("正在只读核对当前聊天身份…", "busy")

    def _search(self) -> None:
        query = self.search_input.text().strip()
        if not query:
            theme.show_message(
                self, APP_NAME, "请输入好友昵称或抖音号。", icon=QMessageBox.Icon.Warning
            )
            return

        async def operation() -> list[FriendCandidate]:
            return await self.service.search_friends(query)

        def complete(candidates: list[FriendCandidate]) -> None:
            self._show_candidates(candidates)
            if not candidates:
                theme.show_message(
                    self, APP_NAME, "没有提取到可确认的好友候选。", icon=QMessageBox.Icon.Warning
                )

        self._start_async("搜索好友", operation, complete)

    def _save_selected_candidate(self) -> None:
        if self.busy or self._closed:
            return
        selected = self._selected_keys(self.candidate_tree)
        if not selected:
            theme.show_message(
                self, APP_NAME, "请选择一条候选结果。", icon=QMessageBox.Icon.Warning
            )
            return
        candidate = self.candidates[selected[0]]
        strength = candidate.evidence.get("identity_strength")
        detail = f"昵称：{candidate.display_name}\n抖音号：{candidate.douyin_id or '页面未显示'}\n身份依据：{('稳定标识' if strength == 'strong' else '昵称与头像组合（较弱）')}\n\n确认这是您同意接收测试消息的好友吗？"
        if not theme.confirm(self, APP_NAME, detail):
            return
        try:
            self.service.save_friend(candidate, self.search_input.text())
        except (ValueError, sqlite3.Error) as exc:
            self._show_error(exc)
            return
        self._refresh_targets()

    def _update_target_selection(self) -> None:
        selected = self.target_tree.selectedItems()
        count = len(selected)
        total = self.target_tree.topLevelItemCount()
        all_selected = count == total and total > 0
        available = self._task_buttons_enabled and not self.busy and not self._closed
        self.target_selection_label.setText(
            f"已选 {count}/{total} 位" if count else f"共 {total} 位"
        )
        self.target_selection_label.setVisible(total > 0)
        self.target_selection_button.setText("取消选择" if all_selected else "全选")
        self.target_selection_button.setVisible(total > 0)
        self.target_selection_button.setEnabled(available and total > 0)
        self.target_actions_button.setVisible(count > 0)
        self.target_actions_button.setEnabled(available and count > 0)
        enabled_count = sum(
            self._target_enabled[str(item.data(0, theme.ROLE_ID))] for item in selected
        )
        mixed = 0 < enabled_count < count
        self.target_enable_action.setText("全部启用" if mixed else "启用所选")
        self.target_disable_action.setText("全部停用" if mixed else "停用所选")
        for action, applicable in (
            (self.target_enable_action, enabled_count < count),
            (self.target_disable_action, enabled_count > 0),
            (self.target_delete_action, count > 0),
            (self.target_clear_selection_action, 0 < count < total),
        ):
            action.setVisible(applicable)
            action.setEnabled(available and applicable)
        if not available or not count:
            self.target_actions_menu.hide()

    def _toggle_target_selection(self) -> None:
        if len(self.target_tree.selectedItems()) == self.target_tree.topLevelItemCount():
            self._clear_target_selection()
        else:
            self._select_all_targets()

    def _select_all_targets(self) -> None:
        if not self.busy and not self._closed:
            self.target_tree.selectAll()

    def _clear_target_selection(self) -> None:
        if not self.busy and not self._closed:
            self.target_tree.clearSelection()

    def _set_selected_targets_enabled(self, enabled: bool) -> None:
        if self.busy or self._closed:
            return
        target_ids = [int(key) for key in self._selected_keys(self.target_tree)]
        if not target_ids:
            theme.show_message(
                self, APP_NAME, "请选择至少一位已保存好友。", icon=QMessageBox.Icon.Warning
            )
            return
        try:
            self.database.set_targets_enabled(target_ids, enabled)
            self._refresh_targets()
        except (KeyError, ValueError, sqlite3.Error) as exc:
            self._show_error(exc)

    def _delete_targets(self) -> None:
        if self.busy or self._closed:
            return
        selected = self.target_tree.selectedItems()
        if not selected:
            theme.show_message(
                self, APP_NAME, "请选择至少一位已保存好友。", icon=QMessageBox.Icon.Warning
            )
            return
        target_ids = [int(item.data(0, theme.ROLE_ID)) for item in selected]
        names = "\n".join(
            f"• {item.text(1)}（ID: {item.data(0, theme.ROLE_ID)}）" for item in selected
        )
        detail = (
            f"删除所选 {len(target_ids)} 位好友？\n\n{names}\n\n"
            "已有发送历史的好友将保留并停用；没有发送历史的好友将删除。"
        )
        if not theme.confirm(self, APP_NAME, detail):
            return
        if self.busy or self._closed:
            return
        try:
            self.database.delete_targets(target_ids)
            self._refresh_targets()
        except (KeyError, ValueError, sqlite3.Error) as exc:
            self._show_error(exc)

    def _current_message_kind(self) -> MessageKind:
        return (
            MessageKind.SPARK_STICKER
            if self.sticker_mode_button.isChecked()
            else MessageKind.TEXT
        )

    def _update_message_mode(self) -> None:
        native = self._current_message_kind() == MessageKind.SPARK_STICKER
        self.message_text.setVisible(not native)
        self.sticker_preview.setVisible(native)

    @staticmethod
    def _message_preview(message_kind: MessageKind, message_text: str) -> str:
        if message_kind == MessageKind.SPARK_STICKER:
            return "内容：续火花（原生表情）\n将从抖音表情面板发送，不发送同名文本。"
        return f"文本：\n{message_text}"

    def _current_message(self) -> str:
        return (
            self.message_text.toPlainText()
            if self._current_message_kind() == MessageKind.TEXT
            else ""
        )

    def _current_delay_range(self) -> tuple[int, int]:
        try:
            minimum = int(self.delay_min_input.text().strip())
            maximum = int(self.delay_max_input.text().strip())
        except ValueError as exc:
            raise ValueError("好友间等待时间必须为整数秒") from exc
        self.database.validate_delay_range(minimum, maximum)
        return (minimum, maximum)

    def _snapshot_plan(self) -> _PlanInput:
        minimum, maximum = self._current_delay_range()
        return _PlanInput(
            self.schedule_enabled.isChecked(),
            self.schedule_time_input.text().strip(),
            self._current_message(),
            self._current_message_kind(),
            minimum,
            maximum,
        )

    def _persist_plan(self, inputs: _PlanInput, *, require_confirmation: bool) -> Plan:
        """Worker-only persistence; inputs were captured on the Qt thread before dispatch."""
        previous = self.database.get_plan()
        saved = self.database.save_plan(
            enabled=inputs.enabled,
            send_time=inputs.send_time,
            message_text=inputs.message_text,
            message_kind=inputs.message_kind,
            delay_min_seconds=inputs.delay_min_seconds,
            delay_max_seconds=inputs.delay_max_seconds,
            confirmed=require_confirmation or not inputs.enabled,
        )
        try:
            self.scheduler.sync(saved)
        except (OSError, SchedulerError) as original_error:
            try:
                restored = self.database.save_plan(
                    enabled=previous.enabled,
                    send_time=previous.send_time,
                    message_text=previous.message_text,
                    message_kind=previous.message_kind,
                    delay_min_seconds=previous.delay_min_seconds,
                    delay_max_seconds=previous.delay_max_seconds,
                    confirmed=bool(previous.confirmed_at),
                )
                self.scheduler.sync(restored)
            except Exception as exc:  # noqa: BLE001 - preserve the original plan-save error
                try:
                    self.database.record_event(
                        "ERROR",
                        "schedule_rollback_failed",
                        format_error(exc),
                    )
                except Exception as log_error:  # noqa: BLE001 - preserve the original failure
                    original_error.add_note(
                        "回滚错误无法写入事件记录：\n" + format_error(log_error)
                    )
            raise
        return saved

    def _save_plan(self) -> None:
        if self.busy or self._closed:
            return
        targets = self.database.list_targets(enabled_only=True)
        try:
            inputs = self._snapshot_plan()
            message = inputs.message_text
            delay_min_seconds = inputs.delay_min_seconds
            delay_max_seconds = inputs.delay_max_seconds
        except ValueError as exc:
            self._show_error(exc)
            return
        if inputs.enabled:
            content = self._message_preview(inputs.message_kind, message)
            preview = f"每天 {inputs.send_time} 自动发送\n目标：{len(targets)} 位启用好友\n好友间随机等待：{delay_min_seconds}–{delay_max_seconds} 秒\n{content}\n\n保存后到点无需再次确认，是否继续？"
            if not targets:
                theme.show_message(
                    self, APP_NAME, "启用计划前至少需要一位好友。", icon=QMessageBox.Icon.Warning
                )
                return
            if not theme.confirm(self, APP_NAME, preview):
                return
        if self.busy or self._closed:
            return

        async def operation() -> Plan:
            return self._persist_plan(inputs, require_confirmation=True)

        def complete(_: Plan) -> None:
            theme.show_message(self, APP_NAME, "计划已保存并同步到 Windows 计划任务。")
            self._refresh_task_views(self.refresh_all)

        self._start_async("保存计划", operation, complete)

    def _validate(self) -> None:
        if self.busy or self._closed:
            return
        message_kind = self._current_message_kind()
        message = self._current_message()
        self._clear_progress()

        async def operation() -> BatchResult:
            return await self.service.validate_config(
                message_kind_override=message_kind,
                message_text_override=message,
                progress=self._progress_callback,
                cancel=self.cancel_event,
            )

        self._start_async("验证配置", operation, self._batch_complete)

    def _manual_send(self) -> None:
        if self.busy or self._closed:
            return
        targets = self.database.list_targets(enabled_only=True)
        try:
            inputs = self._snapshot_plan()
            message, message_kind = inputs.message_text, inputs.message_kind
            delay_min_seconds = inputs.delay_min_seconds
            delay_max_seconds = inputs.delay_max_seconds
        except ValueError as exc:
            self._show_error(exc)
            return
        if not targets:
            theme.show_message(
                self, APP_NAME, "至少需要启用一位好友。", icon=QMessageBox.Icon.Warning
            )
            return
        if message_kind == MessageKind.TEXT and not message.strip():
            theme.show_message(self, APP_NAME, "消息文本不能为空。", icon=QMessageBox.Icon.Warning)
            return
        preview = "\n".join(f"• {target.display_name}" for target in targets)
        content = self._message_preview(message_kind, message)
        if not theme.confirm(
            self,
            APP_NAME,
            f"账号：当前已登录账号\n目标：\n{preview}\n\n{content}\n\n好友间随机等待：{delay_min_seconds}–{delay_max_seconds} 秒\n\n确认执行整批发送？",
        ):
            return
        if self.busy or self._closed:
            return
        account = self.database.get_account()
        if account is None:
            theme.show_message(self, APP_NAME, "请先扫码登录。", icon=QMessageBox.Icon.Warning)
            return
        duplicates = {
            target.id
            for target in targets
            if self.database.has_daily_guard(account.platform_user_id, target.id, today_iso())
        }
        overrides: set[int] = set()
        if duplicates:
            duplicate_names = "\n".join(
                f"• {target.display_name}" for target in targets if target.id in duplicates
            )
            if not theme.confirm(
                self,
                APP_NAME,
                f"以下好友今天已有成功或结果不确定的记录：\n{duplicate_names}\n\n再次发送可能产生重复消息。确定人工覆盖？",
            ):
                return
            overrides = duplicates
        if self.busy or self._closed:
            return
        self._clear_progress()
        target_ids = tuple(target.id for target in targets)
        override_ids = frozenset(overrides)

        async def operation() -> BatchResult:
            self._persist_plan(inputs, require_confirmation=True)
            if self.cancel_event.is_set():
                raise AutomationError(ErrorCode.CANCELLED, "手动发送已取消")
            return await self.service.run_batch(
                BatchMode.MANUAL,
                target_ids=target_ids,
                message_kind_override=message_kind,
                message_text_override=message,
                inter_target_delay_override=(delay_min_seconds, delay_max_seconds),
                manual_override_target_ids=override_ids,
                progress=self._progress_callback,
                cancel=self.cancel_event,
            )

        self._start_async("手动发送", operation, self._batch_complete)

    def _batch_complete(self, result: BatchResult) -> None:
        counts = result.counts
        theme.show_message(
            self,
            APP_NAME,
            f"批次状态：{result.status.value}\n成功：{counts['success']}\n失败：{counts['failed']}\n结果不确定：{counts['unknown']}\n重复跳过：{counts['duplicate']}",
        )
        self._refresh_task_views(self._refresh_history, self._refresh_logs)

    def _progress_callback(self, alias: str, state: str) -> None:
        self.messages.put(("progress", alias, state))

    def _clear_history(self) -> None:
        if self.busy or self._closed:
            return
        if not theme.confirm(self, APP_NAME, "清理今天以前的历史和日志？今天的防重复依据会保留。"):
            return
        counts = self.database.clear_history_preserving_today()
        self._refresh_history()
        self._refresh_logs()
        theme.show_message(
            self,
            APP_NAME,
            f"已清理：发送记录 {counts['attempts']}，批次 {counts['batches']}，日志 {counts['events']}。",
        )

    def _start_async(
        self, label: str, operation: Callable[[], Awaitable[Any]], complete: Callable[[Any], None]
    ) -> None:
        if self._closed:
            return
        if self.busy:
            theme.show_message(self, APP_NAME, "已有任务正在运行。", icon=QMessageBox.Icon.Warning)
            return
        self.busy = True
        self.cancel_event.clear()
        self._set_status(f"{label}进行中…", "busy")
        self.cancel_button.setEnabled(True)
        self._set_task_buttons_state(False)
        if self._spark_preview is not None:
            self._spark_preview.set_busy(True)
        self.status_progress.show()
        self.status_progress.setRange(0, 0)

        def runner() -> None:
            try:
                result = asyncio.run(operation())
            except BaseException as exc:  # noqa: BLE001 - worker's final failure boundary
                self.messages.put(("error", label, exc))
            else:
                self.messages.put(("complete", label, complete, result))

        threading.Thread(target=runner, name=f"spark-keeper-{label}", daemon=True).start()

    def _finish_busy(self) -> None:
        self.busy = False
        self._spark_scan_running = False
        self._login_running = False
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("停止后续目标")
        if self._spark_preview is not None:
            self._spark_preview.set_busy(False)
        self._set_task_buttons_state(True)
        self._capture_timer.stop()
        self.status_progress.hide()
        self.capture_read_button.setEnabled(False)
        self.capture_browser_ready_event.clear()
        self.capture_requested_event.clear()
        self._set_status("就绪", "ready")

    def _record_ui_error(self, category: str, detail: str) -> str:
        try:
            self.database.record_event("ERROR", category, detail)
            self._refresh_logs()
        except Exception as log_error:  # noqa: BLE001 - never recursively log a logging error
            return detail + "\n\n事件记录失败（原始错误保留如上）：\n" + format_error(log_error)
        return detail

    def _show_error(self, exc: BaseException) -> None:
        if self._closed:
            return
        detail = format_error(exc)
        category = exc.code.value if isinstance(exc, AutomationError) else "ui_error"
        detail = self._record_ui_error(category, detail)
        theme.show_message(self, APP_NAME, detail, icon=QMessageBox.Icon.Critical)

    def _cancel_current(self) -> None:
        self.cancel_event.set()
        if self._login_running:
            self._set_status("已请求取消登录；请等待浏览器安全关闭", "warn")
        elif self._spark_scan_running:
            self._set_status("已请求取消扫描；不会发送消息，请等待当前只读步骤结束", "warn")
        else:
            self._set_status("已请求停止；当前已触发的发送会先完成确认", "warn")

    def _check_pending_actions(self) -> None:
        if self._closed or self._checking_pending_actions:
            return
        if self.busy or QApplication.activeModalWidget() is not None:
            self._pending_timer.start(1000)
            return
        self._checking_pending_actions = True
        self._pending_timer.stop()
        next_check_ms = 5000
        try:
            detect_missed_schedule(self.database)
            self._refresh_pending_indicator()
            actions = self.database.list_pending_actions()
            if not actions:
                return
            next_check_ms = 400
            action = actions[0]
            kind = str(action["kind"])
            action_id = str(action["id"])
            payload = action.get("payload", {})
            if kind == "login_required":
                if theme.confirm(
                    self, APP_NAME, "上次任务检测到登录失效或人工验证。现在打开扫码登录？"
                ):
                    self._login()
                else:
                    self.database.resolve_pending_action(action_id, "skipped")
            elif kind == "unknown_send":
                unknown_actions = [item for item in actions if item["kind"] == "unknown_send"]
                theme.show_message(
                    self,
                    APP_NAME,
                    f"有 {len(unknown_actions)} 条发送结果无法确认，已合并提醒。\n"
                    "请在“发送历史”查看记录，并在抖音聊天中人工核对；定时任务当天不会自动重发。\n\n"
                    "关闭此提示仅表示已知晓，不代表发送成功，也不会解除当天防重复保护。"
                    "这些记录不会重复提醒；新的不确定结果仍会另行提醒。",
                    icon=QMessageBox.Icon.Warning,
                )
                if self._closed:
                    return
                for unknown_action in unknown_actions:
                    self.database.resolve_pending_action(str(unknown_action["id"]), "completed")
                self._set_status(
                    f"已知晓 {len(unknown_actions)} 条不确定结果；请人工核对，发送状态和当天防重复保护均未改变",
                    "warn",
                )
            elif kind == "missed_schedule":
                scheduled_for = str(payload.get("scheduled_for", ""))
                target_count = int(payload.get("target_count", 0))
                message = str(payload.get("message_text", ""))
                message_kind = MessageKind(payload.get("message_kind", "text"))
                content = self._message_preview(message_kind, message)
                delay_min_seconds, delay_max_seconds = self._missed_delay_range(payload)
                should_send = theme.confirm(
                    self,
                    APP_NAME,
                    f"检测到错过的每日任务：{scheduled_for}\n目标：{target_count} 位\n好友间随机等待：{delay_min_seconds}–{delay_max_seconds} 秒\n{content}\n\n现在补发？当天防重复仍会生效。",
                )
                if should_send:
                    self._run_missed_action(action_id, payload)
                    return
                self.database.resolve_pending_action(action_id, "skipped")
            self._refresh_pending_indicator()
        finally:
            self._checking_pending_actions = False
            if not self._closed:
                self._pending_timer.start(next_check_ms)

    def _missed_delay_range(self, payload: dict[str, Any]) -> tuple[int, int]:
        plan = self.database.get_plan()
        minimum = int(payload.get("delay_min_seconds", plan.delay_min_seconds))
        maximum = int(payload.get("delay_max_seconds", plan.delay_max_seconds))
        self.database.validate_delay_range(minimum, maximum)
        return (minimum, maximum)

    def _run_missed_action(self, action_id: str, payload: dict[str, Any]) -> None:
        if self.busy or self._closed:
            return
        target_ids = [int(value) for value in payload.get("target_ids", [])]
        message_text = str(payload.get("message_text", ""))
        message_kind = MessageKind(payload.get("message_kind", "text"))
        if message_kind == MessageKind.SPARK_STICKER:
            message_text = ""
        scheduled_for = str(payload.get("scheduled_for", ""))
        delay_range = self._missed_delay_range(payload)
        self._clear_progress()

        async def operation() -> BatchResult:
            return await self.service.run_batch(
                BatchMode.MISSED,
                target_ids=target_ids,
                scheduled_for=scheduled_for,
                message_text_override=message_text,
                message_kind_override=message_kind,
                inter_target_delay_override=delay_range,
                progress=self._progress_callback,
                cancel=self.cancel_event,
            )

        def complete(result: BatchResult) -> None:
            self.database.resolve_pending_action(action_id, "completed")
            self._batch_complete(result)
            self._pending_timer.start(400)

        self._start_async("补发错过任务", operation, complete)


def run(*, smoke_mode: bool = False) -> int:
    application = QApplication.instance() or QApplication([])
    theme.apply_theme(application)
    guard = None
    temporary_root = None
    previous_root = os.environ.get("SPARK_KEEPER_ROOT")
    try:
        # Smoke always opens this build, never activates another GUI or touches user data.
        if smoke_mode:
            temporary_root = TemporaryDirectory(prefix="spark-keeper-gui-smoke-")
            os.environ["SPARK_KEEPER_ROOT"] = temporary_root.name
        else:
            guard = GuiSingleInstance()
            if not guard.start_or_activate():
                return 0
        # Smoke uses only its temporary root, independently of production maintenance.
        with nullcontext() if smoke_mode else MaintenanceLease():
            assert_maintenance_clear()
            window = SparkKeeperApp(smoke_mode=smoke_mode)
        window.show()
        if guard is not None:
            guard.set_activate_callback(window.activate_existing_window)
        if smoke_mode:
            QTimer.singleShot(700, window.close)
            QTimer.singleShot(750, application.quit)
        return application.exec()
    finally:
        if guard is not None:
            guard.close()
        if temporary_root is not None:
            if previous_root is None:
                os.environ.pop("SPARK_KEEPER_ROOT", None)
            else:
                os.environ["SPARK_KEEPER_ROOT"] = previous_root
            temporary_root.cleanup()
