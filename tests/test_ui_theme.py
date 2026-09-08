from __future__ import annotations

from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QStyle,
    QStyleOptionButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from spark_keeper.models import ThemeMode
from spark_keeper.ui import theme


@pytest.fixture
def root(qapp: QApplication) -> Iterator[QWidget]:
    theme.apply_theme(qapp, ThemeMode.LIGHT)
    window = QWidget()
    window.resize(600, 360)
    QVBoxLayout(window)
    window.show()
    qapp.processEvents()
    yield window
    window.close()
    window.deleteLater()
    qapp.processEvents()
    theme.apply_theme(qapp, ThemeMode.LIGHT)


def _button_image(
    button: QPushButton | QCheckBox | QRadioButton,
    *,
    checked: bool,
    hovered: bool = False,
    focused: bool = False,
) -> tuple[QImage, QStyleOptionButton]:
    option = QStyleOptionButton()
    option.initFrom(button)
    option.text = button.text()
    option.state &= ~(
        QStyle.StateFlag.State_On
        | QStyle.StateFlag.State_Off
        | QStyle.StateFlag.State_MouseOver
        | QStyle.StateFlag.State_HasFocus
        | QStyle.StateFlag.State_Sunken
    )
    option.state |= QStyle.StateFlag.State_On if checked else QStyle.StateFlag.State_Off
    if hovered:
        option.state |= QStyle.StateFlag.State_MouseOver
    if focused:
        option.state |= QStyle.StateFlag.State_HasFocus
    image = QImage(button.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(theme.current_colors().app_bg))
    painter = QPainter(image)
    if isinstance(button, QCheckBox):
        control = QStyle.ControlElement.CE_CheckBox
    elif isinstance(button, QRadioButton):
        control = QStyle.ControlElement.CE_RadioButton
    else:
        control = QStyle.ControlElement.CE_PushButton
    button.style().drawControl(control, option, painter, button)
    painter.end()
    return image, option


def test_application_icon_is_inherited_by_windows_and_dialogs(root, qapp) -> None:
    icon = qapp.windowIcon()
    assert not icon.isNull()
    for size in (16, 32, 48, 256):
        assert not icon.pixmap(size, size).isNull()
    dialog = QMessageBox(root)
    try:
        assert root.windowIcon().cacheKey() == icon.cacheKey()
        assert dialog.windowIcon().cacheKey() == icon.cacheKey()
    finally:
        dialog.deleteLater()


def test_theme_reapplication_keeps_existing_widgets_readable(root, qapp) -> None:
    entry = QLineEdit("续火花", root)
    root.layout().addWidget(entry)
    qapp.processEvents()
    font = entry.font()
    palette = entry.palette()
    for _ in range(2):
        theme.apply_theme(qapp, ThemeMode.LIGHT)
        qapp.processEvents()
        assert entry.text() == "续火花"
        assert entry.font() == font
        assert entry.palette().color(QPalette.ColorRole.Text) == QColor(theme.current_colors().text)
        assert entry.palette().color(QPalette.ColorRole.Base) == QColor(
            theme.current_colors().card_bg
        )
        assert entry.palette().color(QPalette.ColorRole.Text) == palette.color(
            QPalette.ColorRole.Text
        )
    assert not entry.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)


def test_nav_selected_hover_and_marker_are_distinct_without_layout_shift(root, qapp) -> None:
    button = QPushButton("任务列表", root)
    button.setObjectName("nav-button")
    button.setCheckable(True)
    button.setFixedSize(theme.NAV_WIDTH, theme.NAV_HEIGHT)
    root.layout().addWidget(button)
    qapp.processEvents()
    font = button.font()
    hint = button.sizeHint()
    content_rects = []
    for checked, hovered, fill in (
        (False, False, theme.current_colors().sidebar_bg),
        (False, True, theme.current_colors().sidebar_hover),
        (True, False, "#bdd2f5"),
        (True, True, "#adc7f0"),
    ):
        button.setChecked(checked)
        image, option = _button_image(button, checked=checked, hovered=hovered)
        assert image.pixelColor(button.width() - 15, button.height() // 2) == QColor(fill)
        marker = image.pixelColor(1, button.height() // 2)
        assert (marker == QColor(theme.current_colors().accent)) is checked
        if checked:
            assert any(
                image.pixelColor(x, y) == QColor("#1e40af")
                for y in range(8, button.height() - 8)
                for x in range(20, 120)
            )
        assert button.font() == font
        assert button.sizeHint() == hint
        content_rects.append(
            button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
        )
    assert all(rect == content_rects[0] for rect in content_rects)
    assert not button.findChildren(QWidget)
    assert not button.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)


def test_nav_uses_native_mouse_keyboard_and_disabled_behavior(root, qapp) -> None:
    button = QPushButton("任务列表", root)
    button.setObjectName("nav-button")
    button.setCheckable(True)
    root.layout().addWidget(button)
    clicked = []
    button.clicked[bool].connect(lambda checked: clicked.append(checked))
    qapp.processEvents()
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert button.isChecked()
    button.setFocus()
    QTest.keyClick(button, Qt.Key.Key_Space)
    assert not button.isChecked()
    assert clicked == [True, False]
    button.setDisabled(True)
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    QTest.keyClick(button, Qt.Key.Key_Space)
    button.click()
    assert clicked == [True, False]


@pytest.mark.parametrize("in_card", [False, True])
def test_enable_circle_is_twelve_logical_pixels_with_full_text_hit_area(
    root, qapp, in_card
) -> None:
    parent = theme.make_card(root) if in_card else root
    if in_card:
        root.layout().addWidget(parent)
    check = QCheckBox("启用每日自动续火花", parent)
    parent.layout().addWidget(check)
    qapp.processEvents()
    empty, option = _button_image(check, checked=False)
    indicator = check.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, check)
    assert indicator.width() == indicator.height() == 12
    assert check.height() >= 28
    assert empty.pixelColor(indicator.center()) == QColor("white")
    selected, _ = _button_image(check, checked=True)
    assert selected.pixelColor(indicator.center()) == QColor("black")
    assert selected.pixelColor(indicator.topLeft()) != QColor("black")

    QTest.mouseClick(check, Qt.MouseButton.LeftButton, pos=indicator.center())
    assert check.isChecked()
    text_rect = check.style().subElementRect(QStyle.SubElement.SE_CheckBoxContents, option, check)
    text_point = QPoint(
        text_rect.left() + check.fontMetrics().horizontalAdvance("启用每日") // 2,
        text_rect.center().y(),
    )
    assert not indicator.contains(text_point)
    QTest.mouseClick(check, Qt.MouseButton.LeftButton, pos=text_point)
    assert not check.isChecked()
    check.setFocus()
    QTest.keyClick(check, Qt.Key.Key_Space)
    assert check.isChecked()
    check.setDisabled(True)
    QTest.mouseClick(check, Qt.MouseButton.LeftButton, pos=indicator.center())
    QTest.keyClick(check, Qt.Key.Key_Space)
    check.click()
    assert check.isChecked()
    assert not check.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)


@pytest.mark.parametrize(
    ("button", "expected"),
    [(QMessageBox.StandardButton.Yes, True), (QMessageBox.StandardButton.No, False)],
)
def test_confirm_displays_literal_text_and_defaults_to_no(root, qapp, button, expected) -> None:
    text = "<b>保持原样</b>\n第二行 & <朋友>"
    observed = []

    def answer() -> None:
        dialog = qapp.activeModalWidget()
        try:
            if isinstance(dialog, QMessageBox):
                observed.append(
                    (
                        dialog.windowTitle(),
                        dialog.text(),
                        dialog.textFormat(),
                        dialog.icon(),
                        dialog.standardButtons(),
                        dialog.standardButton(dialog.defaultButton()),
                    )
                )
                dialog.button(button).click()
        finally:
            if dialog is not None and dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, answer)
    assert theme.confirm(root, "确认发送", text) is expected
    assert observed == [
        (
            "确认发送",
            text,
            Qt.TextFormat.PlainText,
            QMessageBox.Icon.Question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
    ]


@pytest.mark.parametrize("icon", [None, QMessageBox.Icon.Warning])
def test_show_message_displays_literal_text_with_ok(root, qapp, icon) -> None:
    text = "<b>保持原样</b>\n第二行 & <朋友>"
    observed = []

    def answer() -> None:
        dialog = qapp.activeModalWidget()
        try:
            if isinstance(dialog, QMessageBox):
                observed.append(
                    (dialog.text(), dialog.textFormat(), dialog.icon(), dialog.standardButtons())
                )
                dialog.button(QMessageBox.StandardButton.Ok).click()
        finally:
            if dialog is not None and dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, answer)
    kwargs = {} if icon is None else {"icon": icon}
    assert theme.show_message(root, "操作结果", text, **kwargs) is None
    assert observed == [
        (
            text,
            Qt.TextFormat.PlainText,
            QMessageBox.Icon.Information if icon is None else icon,
            QMessageBox.StandardButton.Ok,
        )
    ]


def test_label_helpers_keep_markup_and_newlines_literal(root) -> None:
    text = "<b>保持原样</b>\n第二行 & <朋友>"
    header = theme.make_page_header(root, text, text)
    card = theme.make_card(root, title=text, description=text)
    chip = theme.make_chip(root, text)
    labels = header.findChildren(QLabel) + card.findChildren(QLabel) + [chip]
    assert len(labels) == 5
    for label in labels:
        assert label.text() == text
        assert label.textFormat() == Qt.TextFormat.PlainText
    theme.set_chip(chip, text + "\n更新", "success")
    assert chip.text() == text + "\n更新"
    assert chip.textFormat() == Qt.TextFormat.PlainText


def test_cards_and_headers_wrap_descriptions_with_native_layout(root, qapp) -> None:
    header = theme.make_page_header(root, "任务列表", "查看任务进度和每日发送计划。" * 8)
    card = theme.make_card(root, title="当前进度", description="描述内容需要随卡片宽度换行。" * 12)
    root.layout().addWidget(header)
    root.layout().addWidget(card)
    appended = QLabel("后续内容", card)
    card.layout().addWidget(appended)
    qapp.processEvents()
    assert isinstance(card, QFrame)
    assert card.layout().contentsMargins().left() == 16
    assert card.layout().contentsMargins().top() == 16
    assert card.layout().contentsMargins().right() == 16
    assert card.layout().contentsMargins().bottom() == 16
    assert card.layout().spacing() == 8
    assert card.layout().itemAt(2).widget() is appended
    description = card.findChild(QLabel, "card-description")
    header_description = header.findChild(QLabel, "page-description")
    assert description.wordWrap()
    assert header_description.wordWrap()
    assert description.heightForWidth(250) > description.heightForWidth(500)
    assert header_description.heightForWidth(250) > header_description.heightForWidth(500)
    root.resize(800, 600)
    qapp.processEvents()
    assert description.width() <= card.width() - 32
    assert description.width() > 500
    assert not card.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)


def _wait_for_scrollbars(tree: QTreeWidget, *, visible: bool) -> None:
    # Item layout and scrollbar geometry can post further events after processEvents returns.
    for _ in range(100):
        QTest.qWait(10)
        if (
            tree.horizontalScrollBar().isVisible() == visible
            and tree.verticalScrollBar().isVisible() == visible
        ):
            return
    assert tree.horizontalScrollBar().isVisible() == visible
    assert tree.verticalScrollBar().isVisible() == visible


@pytest.mark.parametrize("bordered", [True, False])
def test_tables_retain_native_scrolling_and_only_requested_frame(root, qapp, bordered) -> None:
    tree = theme.make_tree(root, ["名称", "说明"], bordered=bordered)
    root.layout().addWidget(tree)
    tree.header().setStretchLastSection(False)
    assert tree.header().sectionResizeMode(0) == QHeaderView.ResizeMode.Interactive
    assert tree.header().sectionResizeMode(1) == QHeaderView.ResizeMode.Interactive
    tree.setColumnWidth(0, 100)
    tree.setColumnWidth(1, 100)
    _wait_for_scrollbars(tree, visible=False)
    assert tree.frameWidth() == (1 if bordered else 0)
    assert not tree.rootIsDecorated()
    assert not tree.itemsExpandable()
    assert tree.uniformRowHeights()
    assert tree.alternatingRowColors()
    assert tree.selectionMode() == QAbstractItemView.SelectionMode.SingleSelection
    assert tree.headerItem().text(0) == "名称"
    assert tree.headerItem().text(1) == "说明"
    assert not tree.horizontalScrollBar().isVisible()
    assert not tree.verticalScrollBar().isVisible()
    outer_size = tree.size()
    for _ in range(2):
        tree.setColumnWidth(1, 800)
        for index in range(100):
            item = QTreeWidgetItem([str(index), "内容"])
            item.setData(0, theme.ROLE_ID, f"id-{index}")
            tree.addTopLevelItem(item)
        _wait_for_scrollbars(tree, visible=True)
        assert tree.size() == outer_size
        assert tree.horizontalScrollBar().isVisible()
        assert tree.verticalScrollBar().isVisible()
        last = tree.topLevelItem(99)
        tree.setCurrentItem(last)
        tree.scrollToItem(last, QAbstractItemView.ScrollHint.PositionAtBottom)
        tree.horizontalScrollBar().setValue(tree.horizontalScrollBar().maximum())
        qapp.processEvents()
        assert tree.verticalScrollBar().value() > 0
        assert tree.horizontalScrollBar().value() > 0
        assert tree.viewport().rect().intersects(tree.visualItemRect(last))
        assert tree.currentItem().data(0, theme.ROLE_ID) == "id-99"
        tree.clear()
        tree.setColumnWidth(1, 100)
        _wait_for_scrollbars(tree, visible=False)
        assert not tree.horizontalScrollBar().isVisible()
        assert not tree.verticalScrollBar().isVisible()
        assert tree.size() == outer_size
    assert not tree.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)
    assert not tree.viewport().testAttribute(Qt.WidgetAttribute.WA_NativeWindow)


def test_chip_updates_text_and_color_without_replacing_label(root, qapp) -> None:
    chip = theme.make_chip(root, "系统任务未创建")
    root.layout().addWidget(chip)
    qapp.processEvents()
    assert chip.palette().color(QPalette.ColorRole.WindowText) == QColor(
        theme.current_colors().text_muted
    )
    theme.set_chip(chip, "系统任务已创建", "success")
    qapp.processEvents()
    assert chip.text() == "系统任务已创建"
    assert chip.palette().color(QPalette.ColorRole.WindowText) == QColor(
        theme.current_colors().success
    )
    assert chip.palette().color(QPalette.ColorRole.Window) == QColor(
        theme.current_colors().success_soft
    )
    theme.set_chip(chip, "系统任务状态未知", "warning")
    qapp.processEvents()
    assert chip.text() == "系统任务状态未知"
    assert chip.palette().color(QPalette.ColorRole.WindowText) == QColor(
        theme.current_colors().warning
    )


def test_row_tones_preserve_ids_selection_and_alternating_backgrounds(root, qapp) -> None:
    tree = theme.make_tree(root, ["名称", "状态"])
    root.layout().addWidget(tree)
    item = QTreeWidgetItem(["测试好友", "待验证"])
    item.setData(0, theme.ROLE_ID, "friend-42")
    tree.addTopLevelItem(item)
    tree.setCurrentItem(item)
    for tone, color in (
        ("success", theme.current_colors().success),
        ("warning", theme.current_colors().warning),
        ("normal", theme.current_colors().text),
    ):
        theme.set_tree_row_tone(item, tone)
        qapp.processEvents()
        assert item.data(0, theme.ROLE_ID) == "friend-42"
        assert tree.currentItem() is item
        assert item.isSelected()
        for column in range(2):
            assert item.foreground(column).color() == QColor(color)
            assert item.background(column).style() == Qt.BrushStyle.NoBrush


def _contrast(foreground: str, background: str) -> float:
    def luminance(value: str) -> float:
        color = QColor(value)
        channels = (color.redF(), color.greenF(), color.blueF())
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    values = sorted((luminance(foreground), luminance(background)))
    return (values[1] + 0.05) / (values[0] + 0.05)


@pytest.mark.parametrize("scheme", list(Qt.ColorScheme))
@pytest.mark.parametrize("preference", list(ThemeMode))
def test_resolve_mode_changes_only_system_preference(preference, scheme) -> None:
    expected = (
        (ThemeMode.DARK if scheme == Qt.ColorScheme.Dark else ThemeMode.LIGHT)
        if preference == ThemeMode.SYSTEM
        else preference
    )
    assert theme.resolve_mode(preference, scheme) == expected


def test_colors_are_immutable_and_require_effective_mode(root, qapp) -> None:
    colors = theme.colors_for(ThemeMode.LIGHT)
    with pytest.raises(FrozenInstanceError):
        colors.text = "#ffffff"
    with pytest.raises(ValueError):
        theme.colors_for(ThemeMode.SYSTEM)
    with pytest.raises(ValueError):
        theme.apply_theme(qapp, ThemeMode.SYSTEM)
    assert theme.current_colors() is colors


def test_switching_colors_never_reinitializes_font_style_or_icon(root, qapp, monkeypatch) -> None:
    custom_font = qapp.font()
    custom_font.setPointSize(custom_font.pointSize() + 1)
    original_font = qapp.font()
    qapp.setFont(custom_font)
    icon_key = qapp.windowIcon().cacheKey()
    style = qapp.style()
    decode = Mock(side_effect=AssertionError("Theme switches must not decode the icon"))
    monkeypatch.setattr(theme, "QPixmap", decode)
    try:
        for mode in (ThemeMode.DARK, ThemeMode.DARK, ThemeMode.LIGHT, ThemeMode.DARK):
            theme.apply_theme(qapp, mode)
            qapp.processEvents()
            assert qapp.font() == custom_font
            assert qapp.windowIcon().cacheKey() == icon_key
            assert qapp.style() is style
            assert theme.current_colors() is theme.colors_for(mode)
        decode.assert_not_called()
    finally:
        qapp.setFont(original_font)


def test_dark_palette_covers_native_active_inactive_and_disabled_roles(root, qapp) -> None:
    theme.apply_theme(qapp, ThemeMode.DARK)
    colors = theme.current_colors()
    palette = qapp.palette()
    for group in (
        QPalette.ColorGroup.Active,
        QPalette.ColorGroup.Inactive,
        QPalette.ColorGroup.Disabled,
    ):
        disabled = group == QPalette.ColorGroup.Disabled
        for role, expected in (
            (QPalette.ColorRole.Window, colors.app_bg),
            (QPalette.ColorRole.WindowText, colors.text_muted if disabled else colors.text),
            (QPalette.ColorRole.Base, colors.row_alt if disabled else colors.card_bg),
            (QPalette.ColorRole.Button, colors.card_bg),
            (QPalette.ColorRole.ButtonText, colors.text_muted if disabled else colors.text),
            (QPalette.ColorRole.Text, colors.text_muted if disabled else colors.text),
            (QPalette.ColorRole.Highlight, colors.accent_soft),
            (QPalette.ColorRole.HighlightedText, colors.text_muted if disabled else colors.text),
            (QPalette.ColorRole.PlaceholderText, colors.text_muted),
            (QPalette.ColorRole.ToolTipBase, colors.card_bg),
            (QPalette.ColorRole.ToolTipText, colors.text_muted if disabled else colors.text),
        ):
            assert palette.color(group, role) == QColor(expected)
    for foreground, background in (
        (colors.text, colors.card_bg),
        (colors.text, colors.accent_soft),
        (colors.text_muted, colors.row_alt),
        (colors.text_muted, colors.card_bg),
        (colors.accent_active, colors.nav_selected_hover),
        (colors.success, colors.success_soft),
        (colors.warning, colors.warning_soft),
        (colors.danger, colors.danger_soft),
        (colors.accent, colors.accent_soft),
        (colors.primary_text, colors.primary_bg),
        (colors.primary_text, colors.accent_disabled),
    ):
        assert _contrast(foreground, background) >= 4.5


def test_dark_navigation_selection_hover_and_disabled_are_readable(root, qapp) -> None:
    button = QPushButton("任务列表", root)
    button.setObjectName("nav-button")
    button.setCheckable(True)
    button.setFixedSize(theme.NAV_WIDTH, theme.NAV_HEIGHT)
    root.layout().addWidget(button)
    theme.apply_theme(qapp, ThemeMode.DARK)
    qapp.processEvents()
    colors = theme.current_colors()
    for checked, hovered, fill in (
        (False, False, colors.sidebar_bg),
        (False, True, colors.sidebar_hover),
        (True, False, colors.nav_selected_bg),
        (True, True, colors.nav_selected_hover),
    ):
        button.setChecked(checked)
        image, _ = _button_image(button, checked=checked, hovered=hovered)
        assert image.pixelColor(button.width() - 15, button.height() // 2) == QColor(fill)
        assert (image.pixelColor(1, button.height() // 2) == QColor(colors.accent)) is checked
        foreground = colors.accent_active if checked else colors.text
        assert any(
            image.pixelColor(x, y) == QColor(foreground)
            for y in range(8, button.height() - 8)
            for x in range(20, 120)
        )
    button.setChecked(False)
    button.setDisabled(True)
    qapp.processEvents()
    assert button.palette().color(QPalette.ColorRole.ButtonText) == QColor(colors.text_muted)


def test_existing_inputs_chips_popup_and_dialog_follow_dark_palette(root, qapp) -> None:
    entry = QLineEdit("保留输入和选区", root)
    entry.setSelection(2, 2)
    disabled = QLineEdit("禁用", root)
    disabled.setDisabled(True)
    combo = QComboBox(root)
    combo.addItems(["浅色模式", "暗夜模式", "跟随系统"])
    chips = [
        theme.make_chip(root, tone, tone)
        for tone in ("success", "warning", "danger", "accent", "neutral")
    ]
    for widget in (entry, disabled, combo, *chips):
        root.layout().addWidget(widget)
    menu = QMenu(root)
    menu.addAction("操作")
    menu.addAction("不可用").setEnabled(False)
    dialog = QMessageBox(QMessageBox.Icon.Warning, "提示", "主题切换", parent=root)
    try:
        theme.apply_theme(qapp, ThemeMode.DARK)
        for widget in (menu, dialog, combo.view()):
            widget.ensurePolished()
        qapp.processEvents()
        colors = theme.current_colors()
        assert entry.text() == "保留输入和选区"
        assert entry.selectedText() == "输入"
        assert entry.palette().color(QPalette.ColorRole.Text) == QColor(colors.text)
        assert entry.palette().color(QPalette.ColorRole.Highlight) == QColor(colors.accent_soft)
        assert entry.palette().color(QPalette.ColorRole.HighlightedText) == QColor(colors.text)
        assert disabled.palette().color(QPalette.ColorRole.Text) == QColor(colors.text_muted)
        assert disabled.palette().color(QPalette.ColorRole.Base) == QColor(colors.row_alt)
        assert menu.palette().color(QPalette.ColorRole.Window) == QColor(colors.card_bg)
        assert menu.palette().color(QPalette.ColorRole.WindowText) == QColor(colors.text)
        assert dialog.palette().color(QPalette.ColorRole.Window) == QColor(colors.app_bg)
        assert not dialog.iconPixmap().isNull()
        assert combo.view().palette().color(QPalette.ColorRole.Base) == QColor(colors.card_bg)
        assert combo.view().palette().color(QPalette.ColorRole.Text) == QColor(colors.text)
        for chip in chips:
            tone = chip.property("tone")
            foreground = colors.text_muted if tone == "neutral" else getattr(colors, tone)
            assert chip.palette().color(QPalette.ColorRole.WindowText) == QColor(foreground)
            assert chip.palette().color(QPalette.ColorRole.Window) == QColor(
                getattr(colors, tone + "_soft")
            )
    finally:
        menu.deleteLater()
        dialog.deleteLater()


@pytest.mark.parametrize("widget_type", [QCheckBox, QRadioButton])
def test_dark_native_checks_distinguish_checked_and_disabled_states(
    root, qapp, widget_type
) -> None:
    control = widget_type("启用计划", root)
    root.layout().addWidget(control)
    theme.apply_theme(qapp, ThemeMode.DARK)
    qapp.processEvents()
    colors = theme.current_colors()
    empty, option = _button_image(control, checked=False)
    selected, _ = _button_image(control, checked=True)
    element = (
        QStyle.SubElement.SE_CheckBoxIndicator
        if widget_type is QCheckBox
        else QStyle.SubElement.SE_RadioButtonIndicator
    )
    rect = control.style().subElementRect(element, option, control)
    assert empty.copy(rect) != selected.copy(rect)
    assert (
        _contrast(empty.pixelColor(rect.center()).name(), selected.pixelColor(rect.center()).name())
        >= 3
    )
    if widget_type is QCheckBox:
        assert rect.width() == rect.height() == 12
        assert empty.pixelColor(rect.center()) == QColor(colors.checkbox_bg)
        assert selected.pixelColor(rect.center()) == QColor(colors.checkbox_mark)
    QTest.mouseClick(control, Qt.MouseButton.LeftButton, pos=rect.center())
    assert control.isChecked()
    control.setDisabled(True)
    qapp.processEvents()
    assert control.palette().color(QPalette.ColorRole.WindowText) == QColor(colors.text_muted)
    QTest.mouseClick(control, Qt.MouseButton.LeftButton, pos=rect.center())
    assert control.isChecked()


@pytest.mark.parametrize(
    ("hovered", "focused", "enabled"),
    [(False, False, True), (True, False, True), (False, True, True), (True, True, False)],
)
def test_dark_radio_outline_and_center_remain_visible(
    root, qapp, hovered, focused, enabled
) -> None:
    radio = QRadioButton("续火花", root)
    root.layout().addWidget(radio)
    radio.setEnabled(enabled)
    theme.apply_theme(qapp, ThemeMode.DARK)
    qapp.processEvents()
    colors = theme.current_colors()
    for checked in (False, True):
        image, option = _button_image(radio, checked=checked, hovered=hovered, focused=focused)
        rect = radio.style().subElementRect(
            QStyle.SubElement.SE_RadioButtonIndicator, option, radio
        )
        assert rect.width() == rect.height() == 14
        # Sample the painted outline, not the center: Fusion's dark native
        # outline was nearly black even though its checked center was legible.
        for point in (
            QPoint(rect.left(), rect.center().y()),
            QPoint(rect.right(), rect.center().y()),
            QPoint(rect.center().x(), rect.top()),
            QPoint(rect.center().x(), rect.bottom()),
        ):
            assert _contrast(image.pixelColor(point).name(), colors.card_bg) >= 3
        assert image.pixelColor(rect.left() + 2, rect.center().y()) == QColor(colors.card_bg)
        center = colors.accent if enabled else colors.text_muted
        assert image.pixelColor(rect.center()) == QColor(center if checked else colors.card_bg)


def test_radio_theme_roundtrip_preserves_light_pixels_and_exclusive_keyboard_input(
    root, qapp
) -> None:
    first = QRadioButton("跟随系统", root)
    second = QRadioButton("暗夜", root)
    for radio in (first, second):
        root.layout().addWidget(radio)
    qapp.processEvents()
    light = [_button_image(first, checked=checked)[0] for checked in (False, True)]
    theme.apply_theme(qapp, ThemeMode.DARK)
    qapp.processEvents()
    first.setFocus()
    QTest.keyClick(first, Qt.Key.Key_Space)
    assert first.isChecked() and not second.isChecked()
    second.setFocus()
    QTest.keyClick(second, Qt.Key.Key_Space)
    assert second.isChecked() and not first.isChecked()
    first.setDisabled(True)
    QTest.keyClick(first, Qt.Key.Key_Space)
    assert second.isChecked() and not first.isChecked()
    first.setEnabled(True)
    theme.apply_theme(qapp, ThemeMode.LIGHT)
    qapp.processEvents()
    for checked, expected in zip((False, True), light, strict=True):
        actual, option = _button_image(first, checked=checked)
        rect = first.style().subElementRect(
            QStyle.SubElement.SE_RadioButtonIndicator, option, first
        )
        assert actual.copy(rect) == expected.copy(rect)


def test_refresh_tree_tones_preserves_recursive_items_scroll_checks_and_signals(root, qapp) -> None:
    tree = theme.make_tree(root, ["名称", "状态"])
    root.layout().addWidget(tree)
    for index in range(60):
        item = QTreeWidgetItem(tree, [str(index), "已启用"])
        item.setData(0, theme.ROLE_ID, f"friend-{index}")
        item.setCheckState(0, Qt.CheckState.Checked)
        theme.set_tree_row_tone(item, "success")
    current = tree.topLevelItem(40)
    child = QTreeWidgetItem(current, ["嵌套", "警告"])
    theme.set_tree_row_tone(child, "warning")
    tree.setCurrentItem(current)
    tree.scrollToItem(current, QAbstractItemView.ScrollHint.PositionAtCenter)
    qapp.processEvents()
    scroll = tree.verticalScrollBar().value()
    changed = Mock()
    tree.itemChanged.connect(changed)
    theme.apply_theme(qapp, ThemeMode.DARK)
    theme.refresh_tree_tones(tree)
    qapp.processEvents()
    colors = theme.current_colors()
    assert tree.topLevelItemCount() == 60
    assert tree.topLevelItem(40) is current
    assert current.child(0) is child
    assert current.data(0, theme.ROLE_ID) == "friend-40"
    assert current.data(0, theme.ROLE_ID + 1) == "success"
    assert tree.currentItem() is current
    assert current.isSelected()
    assert current.checkState(0) == Qt.CheckState.Checked
    assert tree.verticalScrollBar().value() == scroll
    assert child.foreground(0).color() == QColor(colors.warning)
    for column in range(2):
        assert current.foreground(column).color() == QColor(colors.success)
        assert current.background(column).style() == Qt.BrushStyle.NoBrush
    changed.assert_not_called()
