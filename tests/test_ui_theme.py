from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFrame,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyleOptionButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from spark_keeper.ui import theme


@pytest.fixture
def root(qapp: QApplication) -> Iterator[QWidget]:
    theme.apply_theme(qapp)
    window = QWidget()
    window.resize(600, 360)
    QVBoxLayout(window)
    window.show()
    qapp.processEvents()
    yield window
    window.close()
    window.deleteLater()
    qapp.processEvents()


def _button_image(
    button: QPushButton | QCheckBox, *, checked: bool, hovered: bool = False
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
    image = QImage(button.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(theme.APP_BG))
    painter = QPainter(image)
    control = (
        QStyle.ControlElement.CE_CheckBox
        if isinstance(button, QCheckBox)
        else QStyle.ControlElement.CE_PushButton
    )
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
        theme.apply_theme(qapp)
        qapp.processEvents()
        assert entry.text() == "续火花"
        assert entry.font() == font
        assert entry.palette().color(QPalette.ColorRole.Text) == QColor(theme.TEXT)
        assert entry.palette().color(QPalette.ColorRole.Base) == QColor(theme.CARD_BG)
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
        (False, False, theme.SIDEBAR_BG),
        (False, True, theme.SIDEBAR_HOVER),
        (True, False, "#bdd2f5"),
        (True, True, "#adc7f0"),
    ):
        button.setChecked(checked)
        image, option = _button_image(button, checked=checked, hovered=hovered)
        assert image.pixelColor(button.width() - 15, button.height() // 2) == QColor(fill)
        marker = image.pixelColor(1, button.height() // 2)
        assert (marker == QColor(theme.ACCENT)) is checked
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
    assert chip.palette().color(QPalette.ColorRole.WindowText) == QColor(theme.TEXT_MUTED)
    theme.set_chip(chip, "系统任务已创建", "success")
    qapp.processEvents()
    assert chip.text() == "系统任务已创建"
    assert chip.palette().color(QPalette.ColorRole.WindowText) == QColor(theme.SUCCESS)
    assert chip.palette().color(QPalette.ColorRole.Window) == QColor(theme.SUCCESS_SOFT)
    theme.set_chip(chip, "系统任务状态未知", "warning")
    qapp.processEvents()
    assert chip.text() == "系统任务状态未知"
    assert chip.palette().color(QPalette.ColorRole.WindowText) == QColor(theme.WARNING)


def test_row_tones_preserve_ids_selection_and_alternating_backgrounds(root, qapp) -> None:
    tree = theme.make_tree(root, ["名称", "状态"])
    root.layout().addWidget(tree)
    item = QTreeWidgetItem(["测试好友", "待验证"])
    item.setData(0, theme.ROLE_ID, "friend-42")
    tree.addTopLevelItem(item)
    tree.setCurrentItem(item)
    for tone, color in (
        ("success", theme.SUCCESS),
        ("warning", theme.WARNING),
        ("normal", theme.TEXT),
    ):
        theme.set_tree_row_tone(item, tone)
        qapp.processEvents()
        assert item.data(0, theme.ROLE_ID) == "friend-42"
        assert tree.currentItem() is item
        assert item.isSelected()
        for column in range(2):
            assert item.foreground(column).color() == QColor(color)
            assert item.background(column).style() == Qt.BrushStyle.NoBrush
