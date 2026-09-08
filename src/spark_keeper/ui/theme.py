"""Shared light-blue Qt theme and native widget constructors."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHeaderView,
    QLabel,
    QMessageBox,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

FONT_FAMILY = "Microsoft YaHei UI"
APP_BG = "#eef2f9"
CARD_BG = "#ffffff"
CARD_BORDER = "#dbe3f0"
BORDER = CARD_BORDER
SIDEBAR_BG = "#e3e9f4"
SIDEBAR_HOVER = "#d7e0f0"
TEXT = "#1f2937"
TEXT_MUTED = "#6b7280"
ACCENT = "#2563eb"
ACCENT_ACTIVE = "#1e40af"
ACCENT_PRESSED = "#1a3690"
ACCENT_DISABLED = "#9db4e8"
ACCENT_SOFT = "#e2ebfd"
SUCCESS = "#15803d"
SUCCESS_SOFT = "#e6f4ea"
WARNING = "#b45309"
WARNING_SOFT = "#fbf0dd"
DANGER = "#b91c1c"
DANGER_SOFT = "#fbeaea"
DANGER_BORDER = "#e3b7b3"
NEUTRAL_SOFT = "#e8edf5"
ROW_ALT = "#f4f7fc"
HEADING_BG = "#ecf1f8"
BTN_ACTIVE_BORDER = "#c4cfe3"
BTN_PRESSED_BG = "#e5ebf5"
NAV_SELECTED_BG = "#bdd2f5"
NAV_SELECTED_HOVER = "#adc7f0"
NAV_WIDTH = 176
NAV_HEIGHT = 40
ROLE_ID = int(Qt.ItemDataRole.UserRole)

_CHIP_TONES = {
    "success": (SUCCESS, SUCCESS_SOFT),
    "warning": (WARNING, WARNING_SOFT),
    "danger": (DANGER, DANGER_SOFT),
    "accent": (ACCENT, ACCENT_SOFT),
    "neutral": (TEXT_MUTED, NEUTRAL_SOFT),
}
_ROW_TONES = {
    "normal": TEXT,
    "neutral": TEXT_MUTED,
    "muted": TEXT_MUTED,
    "success": SUCCESS,
    "warning": WARNING,
    "danger": DANGER,
    "accent": ACCENT,
}

_STYLESHEET = f"""
QWidget {{ color: {TEXT}; }}
QMainWindow, QDialog, QWidget#page {{ background: {APP_BG}; }}
QWidget#sidebar {{ background: {SIDEBAR_BG}; }}
QScrollArea {{ border: none; background: transparent; }}
QFrame#card {{
    background: {CARD_BG}; border: 1px solid {CARD_BORDER}; border-radius: 14px;
}}
QLabel {{ background: transparent; border: none; }}
QLabel#page-title {{ font-size: 16pt; font-weight: bold; }}
QLabel#card-title, QLabel[role="section"] {{ font-size: 10pt; font-weight: bold; }}
QLabel[role="brand"] {{ font-size: 13pt; font-weight: bold; }}
QLabel#page-description, QLabel#card-description, QLabel[role="muted"] {{
    color: {TEXT_MUTED};
}}
QLabel[role="warning"] {{ color: {WARNING}; }}
QLabel#chip {{ border-radius: 9px; padding: 3px 10px; font-size: 8pt; font-weight: bold; }}
QPushButton {{
    background: {CARD_BG}; border: 1px solid {CARD_BORDER}; border-radius: 8px;
    padding: 7px 14px; min-height: 18px;
}}
QPushButton:hover {{ background: {ROW_ALT}; border-color: {BTN_ACTIVE_BORDER}; }}
QPushButton:pressed {{ background: {BTN_PRESSED_BG}; }}
QPushButton:focus {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {TEXT_MUTED}; background: {CARD_BG}; }}
QPushButton[variant="primary"] {{ background: {ACCENT}; color: white; border-color: {ACCENT}; }}
QPushButton[variant="primary"]:hover {{ background: {ACCENT_ACTIVE}; border-color: {ACCENT_ACTIVE}; }}
QPushButton[variant="primary"]:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton[variant="primary"]:disabled {{ background: {ACCENT_DISABLED}; border-color: {ACCENT_DISABLED}; }}
QPushButton[variant="danger"] {{ color: {DANGER}; border-color: {DANGER_BORDER}; }}
QPushButton[variant="danger"]:hover {{ background: {DANGER_SOFT}; }}
QPushButton[variant="danger"]:disabled {{ color: {TEXT_MUTED}; border-color: {CARD_BORDER}; }}
QPushButton#nav-button {{
    background: {SIDEBAR_BG}; color: {TEXT}; font-size: 10pt; text-align: left;
    border: 1px solid transparent; border-left: 3px solid transparent;
    border-radius: 9px; padding: 8px 12px 8px 17px; min-height: 22px;
}}
QPushButton#nav-button:hover {{ background: {SIDEBAR_HOVER}; }}
QPushButton#nav-button:checked {{
    background: {NAV_SELECTED_BG}; color: {ACCENT_ACTIVE}; border-left-color: {ACCENT};
}}
QPushButton#nav-button:checked:hover {{ background: {NAV_SELECTED_HOVER}; }}
QPushButton#nav-button:focus {{ border-top-color: {ACCENT}; border-right-color: {ACCENT}; border-bottom-color: {ACCENT}; }}
QPushButton#nav-button:disabled {{ background: {SIDEBAR_BG}; color: {TEXT_MUTED}; }}
QPushButton#nav-button:checked:disabled {{ background: {NAV_SELECTED_BG}; color: {ACCENT_ACTIVE}; }}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTimeEdit, QDateEdit {{
    background: {CARD_BG}; color: {TEXT}; border: 1px solid {CARD_BORDER};
    border-radius: 7px; padding: 6px 8px; selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QComboBox:focus, QTimeEdit:focus, QDateEdit:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled, QSpinBox:disabled,
QDoubleSpinBox:disabled, QComboBox:disabled, QTimeEdit:disabled, QDateEdit:disabled {{
    color: {TEXT_MUTED}; background: {ROW_ALT};
}}
QPlainTextEdit[role="mono"] {{ font-family: Consolas; font-size: 10pt; }}
QComboBox QAbstractItemView {{ background: {CARD_BG}; color: {TEXT}; selection-background-color: {ACCENT_SOFT}; }}
QCheckBox {{ background: transparent; spacing: 8px; min-height: 24px; padding: 4px 0; }}
QCheckBox:disabled {{ color: {TEXT_MUTED}; }}
QCheckBox::indicator {{
    width: 10px; height: 10px; border: 1px solid black; border-radius: 6px;
    background: white;
}}
QCheckBox::indicator:checked, QCheckBox::indicator:indeterminate {{ background: black; }}
QCheckBox::indicator:focus {{ border-color: {ACCENT}; }}
QCheckBox::indicator:disabled {{ border-color: {TEXT_MUTED}; }}
QCheckBox::indicator:checked:disabled, QCheckBox::indicator:indeterminate:disabled {{ background: {TEXT_MUTED}; }}
QTreeWidget {{
    background: {CARD_BG}; alternate-background-color: {ROW_ALT}; color: {TEXT};
    border: 1px solid {CARD_BORDER}; border-radius: 7px;
    selection-background-color: {ACCENT_SOFT}; selection-color: {TEXT};
}}
QTreeWidget[bordered="false"] {{ border: none; border-radius: 0; }}
QTreeWidget::item {{ min-height: 28px; padding: 2px 6px; }}
QTreeWidget::item:selected {{ background: {ACCENT_SOFT}; color: {TEXT}; }}
QHeaderView {{ background: {HEADING_BG}; }}
QHeaderView::section {{
    background: {HEADING_BG}; color: {TEXT}; font-weight: bold;
    border: none; border-bottom: 1px solid {CARD_BORDER}; padding: 7px 8px;
}}
QProgressBar {{
    background: {NEUTRAL_SOFT}; border: none; border-radius: 4px;
    min-height: 8px; max-height: 8px; text-align: center;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QToolTip {{ background: {CARD_BG}; color: {TEXT}; border: 1px solid {CARD_BORDER}; padding: 5px; }}
""" + "\n".join(
    f'QLabel#chip[tone="{tone}"] {{ color: {foreground}; background: {background}; }}'
    for tone, (foreground, background) in _CHIP_TONES.items()
)


def apply_theme(app: QApplication) -> None:
    """Apply a readable light palette independently of the desktop's dark mode."""
    # PNG decoding is built into QtGui; portable builds need no icon/image plugins.
    pixmap = QPixmap(str(Path(__file__).resolve().parents[1] / "assets" / "app-icon.png"))
    if pixmap.isNull():
        raise RuntimeError("软件图标资源无法加载；请检查安装文件。")
    app.setWindowIcon(QIcon(pixmap))
    app.setFont(QFont(FONT_FAMILY, 9))
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, APP_BG),
        (QPalette.ColorRole.WindowText, TEXT),
        (QPalette.ColorRole.Base, CARD_BG),
        (QPalette.ColorRole.AlternateBase, ROW_ALT),
        (QPalette.ColorRole.Text, TEXT),
        (QPalette.ColorRole.Button, CARD_BG),
        (QPalette.ColorRole.ButtonText, TEXT),
        (QPalette.ColorRole.Highlight, ACCENT_SOFT),
        (QPalette.ColorRole.HighlightedText, TEXT),
        (QPalette.ColorRole.PlaceholderText, TEXT_MUTED),
        (QPalette.ColorRole.ToolTipBase, CARD_BG),
        (QPalette.ColorRole.ToolTipText, TEXT),
    ):
        palette.setColor(role, QColor(color))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(TEXT_MUTED))
    app.setPalette(palette)
    app.setStyleSheet(_STYLESHEET)


def confirm(parent: QWidget, title: str, text: str) -> bool:
    """Ask for explicit approval without interpreting user text as rich text."""
    dialog = QMessageBox(parent)
    dialog.setWindowTitle(title)
    dialog.setTextFormat(Qt.TextFormat.PlainText)
    dialog.setText(text)
    dialog.setIcon(QMessageBox.Icon.Question)
    dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    dialog.setDefaultButton(QMessageBox.StandardButton.No)
    return dialog.exec() == QMessageBox.StandardButton.Yes


def show_message(
    parent: QWidget,
    title: str,
    text: str,
    *,
    icon: QMessageBox.Icon = QMessageBox.Icon.Information,
) -> None:
    """Show a message literally, including markup-like text and line breaks."""
    dialog = QMessageBox(parent)
    dialog.setWindowTitle(title)
    dialog.setTextFormat(Qt.TextFormat.PlainText)
    dialog.setText(text)
    dialog.setIcon(icon)
    dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
    dialog.exec()


def make_page_header(parent: QWidget, title: str, description: str) -> QWidget:
    header = QWidget(parent)
    header.setObjectName("page-header")
    layout = QVBoxLayout(header)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    heading = QLabel(title, header)
    heading.setTextFormat(Qt.TextFormat.PlainText)
    heading.setObjectName("page-title")
    layout.addWidget(heading)
    detail = QLabel(description, header)
    detail.setTextFormat(Qt.TextFormat.PlainText)
    detail.setObjectName("page-description")
    detail.setWordWrap(True)
    layout.addWidget(detail)
    return header


def make_card(parent: QWidget | None = None, *, title: str = "", description: str = "") -> QFrame:
    card = QFrame(parent)
    card.setObjectName("card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(8)
    if title:
        heading = QLabel(title, card)
        heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setObjectName("card-title")
        layout.addWidget(heading)
    if description:
        detail = QLabel(description, card)
        detail.setTextFormat(Qt.TextFormat.PlainText)
        detail.setObjectName("card-description")
        detail.setWordWrap(True)
        layout.addWidget(detail)
    return card


def make_tree(parent: QWidget, headers: Sequence[str], *, bordered: bool = True) -> QTreeWidget:
    """A flat table with native, as-needed scrolling and no extra wrapper frame."""
    tree = QTreeWidget(parent)
    tree.setProperty("bordered", bordered)
    tree.setFrameShape(QFrame.Shape.StyledPanel if bordered else QFrame.Shape.NoFrame)
    tree.setColumnCount(len(headers))
    tree.setHeaderLabels(list(headers))
    tree.setRootIsDecorated(False)
    tree.setItemsExpandable(False)
    tree.setUniformRowHeights(True)
    tree.setAlternatingRowColors(True)
    tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    tree.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    tree.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    tree.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    tree.header().setMinimumSectionSize(40)
    tree.header().setStretchLastSection(True)
    return tree


def make_chip(parent: QWidget, text: str, tone: str = "neutral") -> QLabel:
    chip = QLabel(parent)
    chip.setObjectName("chip")
    chip.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    set_chip(chip, text, tone)
    return chip


def set_chip(chip: QLabel, text: str, tone: str) -> None:
    if tone not in _CHIP_TONES:
        raise KeyError(tone)
    chip.setTextFormat(Qt.TextFormat.PlainText)
    chip.setText(text)
    if chip.property("tone") != tone:
        chip.setProperty("tone", tone)
        chip.style().unpolish(chip)
        chip.style().polish(chip)
        chip.update()


def set_tree_row_tone(item: QTreeWidgetItem, tone: str) -> None:
    """Color row text without overriding native selection or alternating fills."""
    brush = QBrush(QColor(_ROW_TONES[tone]))
    for column in range(item.columnCount()):
        item.setForeground(column, brush)
