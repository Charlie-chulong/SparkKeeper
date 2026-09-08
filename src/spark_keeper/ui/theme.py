"""Shared semantic Qt palettes and native widget constructors."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt
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

from spark_keeper.models import ThemeMode

FONT_FAMILY = "Microsoft YaHei UI"


@dataclass(frozen=True, slots=True)
class ThemeColors:
    app_bg: str
    card_bg: str
    card_border: str
    sidebar_bg: str
    sidebar_hover: str
    text: str
    text_muted: str
    accent: str
    accent_active: str
    accent_pressed: str
    accent_disabled: str
    accent_soft: str
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    danger: str
    danger_soft: str
    danger_border: str
    neutral_soft: str
    row_alt: str
    heading_bg: str
    btn_active_border: str
    btn_pressed_bg: str
    nav_selected_bg: str
    nav_selected_hover: str
    primary_bg: str
    primary_hover: str
    primary_pressed: str
    primary_text: str
    checkbox_bg: str
    checkbox_mark: str


_LIGHT = ThemeColors(
    app_bg="#eef2f9",
    card_bg="#ffffff",
    card_border="#dbe3f0",
    sidebar_bg="#e3e9f4",
    sidebar_hover="#d7e0f0",
    text="#1f2937",
    text_muted="#6b7280",
    accent="#2563eb",
    accent_active="#1e40af",
    accent_pressed="#1a3690",
    accent_disabled="#9db4e8",
    accent_soft="#e2ebfd",
    success="#15803d",
    success_soft="#e6f4ea",
    warning="#b45309",
    warning_soft="#fbf0dd",
    danger="#b91c1c",
    danger_soft="#fbeaea",
    danger_border="#e3b7b3",
    neutral_soft="#e8edf5",
    row_alt="#f4f7fc",
    heading_bg="#ecf1f8",
    btn_active_border="#c4cfe3",
    btn_pressed_bg="#e5ebf5",
    nav_selected_bg="#bdd2f5",
    nav_selected_hover="#adc7f0",
    primary_bg="#2563eb",
    primary_hover="#1e40af",
    primary_pressed="#1a3690",
    primary_text="#ffffff",
    checkbox_bg="#ffffff",
    checkbox_mark="#000000",
)
_DARK = ThemeColors(
    app_bg="#141b27",
    card_bg="#1c2635",
    card_border="#3c4c63",
    sidebar_bg="#182232",
    sidebar_hover="#25364f",
    text="#e5edf8",
    text_muted="#a4b2c7",
    accent="#7bafff",
    accent_active="#b4d2ff",
    accent_pressed="#c9dfff",
    accent_disabled="#405a82",
    accent_soft="#2b4263",
    success="#7cdda2",
    success_soft="#203d32",
    warning="#f4c477",
    warning_soft="#443822",
    danger="#ffa3a3",
    danger_soft="#482c35",
    danger_border="#825262",
    neutral_soft="#2b3648",
    row_alt="#202c3e",
    heading_bg="#273449",
    btn_active_border="#6380a8",
    btn_pressed_bg="#31445f",
    nav_selected_bg="#2c466b",
    nav_selected_hover="#365580",
    primary_bg="#2563eb",
    primary_hover="#2456c5",
    primary_pressed="#1e479f",
    primary_text="#ffffff",
    checkbox_bg="#1c2635",
    checkbox_mark="#e5edf8",
)
NAV_WIDTH = 176
NAV_HEIGHT = 40
ROLE_ID = int(Qt.ItemDataRole.UserRole)

_CHIP_TONES = ("success", "warning", "danger", "accent", "neutral")
_ROW_TONES = {
    "normal": "text",
    "neutral": "text_muted",
    "muted": "text_muted",
    "success": "success",
    "warning": "warning",
    "danger": "danger",
    "accent": "accent",
}


def colors_for(mode: ThemeMode) -> ThemeColors:
    """Return immutable colors for an effective (not system-following) mode."""
    if mode == ThemeMode.LIGHT:
        return _LIGHT
    if mode == ThemeMode.DARK:
        return _DARK
    raise ValueError(f"Not an effective theme mode: {mode!r}")


def current_colors() -> ThemeColors:
    app = QApplication.instance()
    mode = app.property("spark-theme-mode") if app is not None else None
    return colors_for(ThemeMode(mode) if mode is not None else ThemeMode.LIGHT)


def resolve_mode(preference: ThemeMode, system_scheme: Qt.ColorScheme) -> ThemeMode:
    if preference == ThemeMode.SYSTEM:
        return ThemeMode.DARK if system_scheme == Qt.ColorScheme.Dark else ThemeMode.LIGHT
    colors_for(preference)
    return preference


def _stylesheet(c: ThemeColors) -> str:
    stylesheet = f"""
QWidget {{ color: {c.text}; }}
QWidget:disabled {{ color: {c.text_muted}; }}
QMainWindow, QDialog, QWidget#page {{ background: {c.app_bg}; }}
QWidget#sidebar {{ background: {c.sidebar_bg}; }}
QScrollArea {{ border: none; background: transparent; }}
QFrame#card {{
    background: {c.card_bg}; border: 1px solid {c.card_border}; border-radius: 14px;
}}
QLabel {{ background: transparent; border: none; }}
QLabel#page-title {{ font-size: 16pt; font-weight: bold; }}
QLabel#card-title, QLabel[role="section"] {{ font-size: 10pt; font-weight: bold; }}
QLabel[role="brand"] {{ font-size: 13pt; font-weight: bold; }}
QLabel#page-description, QLabel#card-description, QLabel[role="muted"] {{
    color: {c.text_muted};
}}
QLabel[role="warning"] {{ color: {c.warning}; }}
QLabel#chip {{ border-radius: 9px; padding: 3px 10px; font-size: 8pt; font-weight: bold; }}
QPushButton {{
    background: {c.card_bg}; border: 1px solid {c.card_border}; border-radius: 8px;
    padding: 7px 14px; min-height: 18px;
}}
QPushButton:hover {{ background: {c.row_alt}; border-color: {c.btn_active_border}; }}
QPushButton:pressed {{ background: {c.btn_pressed_bg}; }}
QPushButton:focus {{ border-color: {c.accent}; }}
QPushButton:disabled {{ color: {c.text_muted}; background: {c.card_bg}; }}
QPushButton[variant="primary"] {{ background: {c.primary_bg}; color: {c.primary_text}; border-color: {c.primary_bg}; }}
QPushButton[variant="primary"]:hover {{ background: {c.primary_hover}; border-color: {c.primary_hover}; }}
QPushButton[variant="primary"]:pressed {{ background: {c.primary_pressed}; }}
QPushButton[variant="primary"]:disabled {{ background: {c.accent_disabled}; border-color: {c.accent_disabled}; }}
QPushButton[variant="danger"] {{ color: {c.danger}; border-color: {c.danger_border}; }}
QPushButton[variant="danger"]:hover {{ background: {c.danger_soft}; }}
QPushButton[variant="danger"]:disabled {{ color: {c.text_muted}; border-color: {c.card_border}; }}
QPushButton#nav-button {{
    background: {c.sidebar_bg}; color: {c.text}; font-size: 10pt; text-align: left;
    border: 1px solid transparent; border-left: 3px solid transparent;
    border-radius: 9px; padding: 8px 12px 8px 17px; min-height: 22px;
}}
QPushButton#nav-button:hover {{ background: {c.sidebar_hover}; }}
QPushButton#nav-button:checked {{
    background: {c.nav_selected_bg}; color: {c.accent_active}; border-left-color: {c.accent};
}}
QPushButton#nav-button:checked:hover {{ background: {c.nav_selected_hover}; }}
QPushButton#nav-button:focus {{ border-top-color: {c.accent}; border-right-color: {c.accent}; border-bottom-color: {c.accent}; }}
QPushButton#nav-button:disabled {{ background: {c.sidebar_bg}; color: {c.text_muted}; }}
QPushButton#nav-button:checked:disabled {{ background: {c.nav_selected_bg}; color: {c.accent_active}; }}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTimeEdit, QDateEdit {{
    background: {c.card_bg}; color: {c.text}; border: 1px solid {c.card_border};
    border-radius: 7px; padding: 6px 8px; selection-background-color: {c.accent_soft};
    selection-color: {c.text};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QComboBox:focus, QTimeEdit:focus, QDateEdit:focus {{ border-color: {c.accent}; }}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled, QSpinBox:disabled,
QDoubleSpinBox:disabled, QComboBox:disabled, QTimeEdit:disabled, QDateEdit:disabled {{
    color: {c.text_muted}; background: {c.row_alt};
}}
QPlainTextEdit[role="mono"] {{ font-family: Consolas; font-size: 10pt; }}
QComboBox QAbstractItemView {{
    background: {c.card_bg}; color: {c.text};
    selection-background-color: {c.accent_soft}; selection-color: {c.text};
}}
QCheckBox {{ background: transparent; spacing: 8px; min-height: 24px; padding: 4px 0; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {c.text_muted}; }}
QCheckBox::indicator {{
    width: 10px; height: 10px; border: 1px solid {c.checkbox_mark}; border-radius: 6px;
    background: {c.checkbox_bg};
}}
QCheckBox::indicator:checked, QCheckBox::indicator:indeterminate {{ background: {c.checkbox_mark}; }}
QCheckBox::indicator:focus {{ border-color: {c.accent}; }}
QCheckBox::indicator:disabled {{ border-color: {c.text_muted}; }}
QCheckBox::indicator:checked:disabled, QCheckBox::indicator:indeterminate:disabled {{ background: {c.text_muted}; }}
QTreeWidget {{
    background: {c.card_bg}; alternate-background-color: {c.row_alt}; color: {c.text};
    border: 1px solid {c.card_border}; border-radius: 7px;
    selection-background-color: {c.accent_soft}; selection-color: {c.text};
}}
QTreeWidget[bordered="false"] {{ border: none; border-radius: 0; }}
QTreeWidget::item {{ min-height: 28px; padding: 2px 6px; }}
QTreeWidget::item:selected {{ background: {c.accent_soft}; color: {c.text}; }}
QHeaderView {{ background: {c.heading_bg}; }}
QHeaderView::section {{
    background: {c.heading_bg}; color: {c.text}; font-weight: bold;
    border: none; border-bottom: 1px solid {c.card_border}; padding: 7px 8px;
}}
QMenu {{ background: {c.card_bg}; color: {c.text}; border: 1px solid {c.card_border}; }}
QMenu::item:selected {{ background: {c.accent_soft}; color: {c.text}; }}
QMenu::item:disabled {{ color: {c.text_muted}; }}
QMenu::separator {{ background: {c.card_border}; height: 1px; }}
QProgressBar {{
    background: {c.neutral_soft}; border: none; border-radius: 4px;
    min-height: 8px; max-height: 8px; text-align: center;
}}
QProgressBar::chunk {{ background: {c.accent}; border-radius: 4px; }}
QToolTip {{ background: {c.card_bg}; color: {c.text}; border: 1px solid {c.card_border}; padding: 5px; }}
""" + "\n".join(
        f'QLabel#chip[tone="{tone}"] {{ color: '
        f"{c.text_muted if tone == 'neutral' else getattr(c, tone)}; "
        f"background: {getattr(c, tone + '_soft')}; }}"
        for tone in _CHIP_TONES
    )
    if c is _DARK:
        # Fusion derives the native radio outline from Window.darker(150).
        # Override only dark indicators, keeping the light native rendering intact.
        stylesheet += f"""
QRadioButton::indicator {{
    width: 12px; height: 12px; border: 1px solid {c.text_muted}; border-radius: 7px;
    background: {c.card_bg};
}}
QRadioButton::indicator:checked {{
    background: qradialgradient(cx: 0.5, cy: 0.5, radius: 0.5, fx: 0.5, fy: 0.5,
        stop: 0 {c.accent}, stop: 0.45 {c.accent}, stop: 0.46 {c.card_bg}, stop: 1 {c.card_bg});
}}
QRadioButton::indicator:hover {{ border-color: {c.accent_active}; }}
QRadioButton::indicator:focus {{ border-color: {c.accent}; }}
QRadioButton::indicator:disabled {{ border-color: {c.text_muted}; }}
QRadioButton::indicator:checked:disabled {{
    background: qradialgradient(cx: 0.5, cy: 0.5, radius: 0.5, fx: 0.5, fy: 0.5,
        stop: 0 {c.text_muted}, stop: 0.45 {c.text_muted}, stop: 0.46 {c.card_bg}, stop: 1 {c.card_bg});
}}
"""
    return stylesheet


def initialize_theme(app: QApplication) -> None:
    """Initialize native style, font and decoded application icon only once."""
    if app.property("spark-theme-initialized"):
        return
    # PNG decoding is built into QtGui; portable builds need no icon/image plugins.
    pixmap = QPixmap(str(Path(__file__).resolve().parents[1] / "assets" / "app-icon.png"))
    if pixmap.isNull():
        raise RuntimeError("软件图标资源无法加载；请检查安装文件。")
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(pixmap))
    app.setFont(QFont(FONT_FAMILY, 9))
    app.setProperty("spark-theme-initialized", True)


def apply_theme(app: QApplication, mode: ThemeMode) -> None:
    """Apply an effective palette without resetting fonts, icons or widget state."""
    c = colors_for(mode)
    initialize_theme(app)
    palette = QPalette()
    roles = (
        (QPalette.ColorRole.Window, c.app_bg),
        (QPalette.ColorRole.WindowText, c.text),
        (QPalette.ColorRole.Base, c.card_bg),
        (QPalette.ColorRole.AlternateBase, c.row_alt),
        (QPalette.ColorRole.Text, c.text),
        (QPalette.ColorRole.Button, c.card_bg),
        (QPalette.ColorRole.ButtonText, c.text),
        (QPalette.ColorRole.Highlight, c.accent_soft),
        (QPalette.ColorRole.HighlightedText, c.text),
        (QPalette.ColorRole.PlaceholderText, c.text_muted),
        (QPalette.ColorRole.ToolTipBase, c.card_bg),
        (QPalette.ColorRole.ToolTipText, c.text),
        (QPalette.ColorRole.Light, c.btn_active_border),
        (QPalette.ColorRole.Midlight, c.heading_bg),
        (QPalette.ColorRole.Mid, c.card_border),
        (QPalette.ColorRole.Dark, c.sidebar_bg),
        (QPalette.ColorRole.Shadow, c.app_bg),
        (QPalette.ColorRole.BrightText, c.primary_text),
        (QPalette.ColorRole.Link, c.accent),
        (QPalette.ColorRole.LinkVisited, c.accent_active),
        (QPalette.ColorRole.Accent, c.accent),
    )
    for group in (
        QPalette.ColorGroup.Active,
        QPalette.ColorGroup.Inactive,
        QPalette.ColorGroup.Disabled,
    ):
        for role, color in roles:
            palette.setColor(group, role, QColor(color))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.HighlightedText,
        QPalette.ColorRole.ToolTipText,
        QPalette.ColorRole.Link,
        QPalette.ColorRole.LinkVisited,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(c.text_muted))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, QColor(c.row_alt))
    app.setProperty("spark-theme-mode", mode.value)
    app.setPalette(palette)
    app.setStyleSheet(_stylesheet(c))


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
    """Store semantic text color without overriding native selection or fills."""
    brush = QBrush(QColor(getattr(current_colors(), _ROW_TONES[tone])))
    item.setData(0, ROLE_ID + 1, tone)
    for column in range(item.columnCount()):
        item.setForeground(column, brush)


def refresh_tree_tones(tree: QTreeWidget) -> None:
    """Recolor existing rows recursively without emitting business item changes."""

    def refresh(parent: QTreeWidgetItem) -> None:
        for index in range(parent.childCount()):
            item = parent.child(index)
            tone = item.data(0, ROLE_ID + 1)
            if tone in _ROW_TONES:
                set_tree_row_tone(item, tone)
            refresh(item)

    with QSignalBlocker(tree):
        refresh(tree.invisibleRootItem())
