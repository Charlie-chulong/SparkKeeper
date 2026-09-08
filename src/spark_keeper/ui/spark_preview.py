from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models import SparkContact, SparkScanResult, SparkScanStatus, SparkState
from . import theme


class SparkImportPreview(QDialog):
    """只读扫描结果；勾选引用稳定身份，导入安全校验由父窗口执行。"""

    FILTERS = ("全部", "只看新增", "可导入", "待恢复", "身份待确认/群聊")

    def __init__(
        self,
        parent: QWidget,
        scan: SparkScanResult,
        saved_keys: set[str],
        *,
        validate: Callable[[], bool],
        import_selected: Callable[[], None],
        close: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self.scan = scan
        self.saved_keys = set(saved_keys)
        self.validate = validate
        self.busy = False
        self._import_selected = import_selected
        self._close_callback = close
        self._close_notified = False
        self._filter = self.FILTERS[0]
        self.eligible_keys = {
            contact.candidate.stable_key for contact in scan.contacts if contact.importable
        }
        self.selected_keys = {
            contact.candidate.stable_key
            for contact in scan.contacts
            if contact.importable
            and contact.spark_state in {SparkState.ACTIVE, SparkState.RECOVER}
            and contact.candidate.stable_key not in self.saved_keys
        }
        self.setObjectName("spark-import-preview")
        self.setWindowTitle("火花好友扫描预览（不发送）")
        self.resize(900, 580)
        self.setMinimumSize(720, 460)
        self.finished.connect(self._notify_closed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        status = {
            SparkScanStatus.COMPLETE: "扫描结束（仅本次可识别范围）",
            SparkScanStatus.PARTIAL: "部分结果（扫描未完整结束）",
            SparkScanStatus.CANCELLED: "已取消（仅保留取消前结果）",
        }[scan.status]
        recover_count = sum(contact.spark_state is SparkState.RECOVER for contact in scan.contacts)
        self.status_label = QLabel(
            f"{status} · 已扫描 {scan.scanned_count} 项 · 可导入 {len(self.eligible_keys)} 位"
            f" · 待恢复 {recover_count} 项",
            self,
        )
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)
        self.limits_label = QLabel(
            "默认勾选身份可靠的新有效火花（含灰色）和待恢复好友；其他火花状态可自行勾选。"
            "\n仅保存、不发送；新好友默认停用，既有好友保持原状态。请只启用获授权好友。"
            "\n所有行可选择；选中群聊或身份未确认项时会明确阻止导入，不会悄悄跳过。"
            f"\n扫描时间：{scan.scanned_at}"
            + (f"\n{scan.detail}" if scan.detail else "")
            + (
                "\n本次没有满足导入身份条件的好友：需要明确单聊及可靠稳定身份；"
                "可查看各行识别依据，或使用原有单人搜索 / 浏览器捕获流程核对。"
                if not self.eligible_keys
                else ""
            ),
            self,
        )
        self.limits_label.setObjectName("lbl-spark-limits")
        self.limits_label.setWordWrap(True)
        self.limits_label.setTextFormat(Qt.TextFormat.PlainText)
        self.limits_label.setProperty("role", "muted")
        layout.addWidget(self.limits_label)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("显示：", self))
        self.filter_box = QComboBox(self)
        self.filter_box.addItems(self.FILTERS)
        self.filter_box.currentTextChanged.connect(self._filter_changed)
        toolbar.addWidget(self.filter_box)
        self.select_all_button = QPushButton("全选当前列表", self)
        self.select_all_button.clicked.connect(self.select_all)
        toolbar.addWidget(self.select_all_button)
        self.clear_button = QPushButton("清空选择", self)
        self.clear_button.clicked.connect(self.clear_selection)
        toolbar.addWidget(self.clear_button)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self.tree = theme.make_tree(
            self, ("勾选", "名称", "火花状态", "身份", "已保存", "识别依据 / 限制")
        )
        self.tree.setObjectName("tree-spark-preview")
        for column, width in enumerate((50, 130, 100, 180, 65, 300)):
            self.tree.setColumnWidth(column, width)
        self._items: list[QTreeWidgetItem] = []
        for contact in scan.contacts:
            candidate = contact.candidate
            strength = "强" if candidate.evidence.get("identity_strength") == "strong" else "待确认"
            item = QTreeWidgetItem(
                [
                    "",
                    candidate.display_name,
                    {
                        SparkState.ACTIVE: "有效火花",
                        SparkState.RECOVER: "待恢复火花",
                        SparkState.INACTIVE: "非有效火花",
                        SparkState.UNKNOWN: "待确认",
                    }[contact.spark_state],
                    f"{'群聊' if contact.is_group else strength} · {candidate.stable_key}",
                    "是" if candidate.stable_key in self.saved_keys else "否",
                    contact.reason,
                ]
            )
            item.setData(0, theme.ROLE_ID, candidate.stable_key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setToolTip(5, contact.reason)
            if not contact.importable:
                theme.set_tree_row_tone(item, "muted")
            self.tree.addTopLevelItem(item)
            self._items.append(item)
        self.tree.itemChanged.connect(self._item_changed)
        layout.addWidget(self.tree, 1)

        footer = QHBoxLayout()
        self.selection_label = QLabel(self)
        self.selection_label.setWordWrap(True)
        self.selection_label.setTextFormat(Qt.TextFormat.PlainText)
        footer.addWidget(self.selection_label, 1)
        self.close_button = QPushButton("关闭", self)
        self.close_button.clicked.connect(self.close)
        footer.addWidget(self.close_button)
        self.import_button = QPushButton("确认导入所选（不发送）", self)
        self.import_button.setProperty("variant", "primary")
        self.import_button.clicked.connect(self._request_import)
        footer.addWidget(self.import_button)
        layout.addLayout(footer)
        # Enter must not accidentally import while the user is navigating the table.
        for button in (
            self.select_all_button,
            self.clear_button,
            self.close_button,
            self.import_button,
        ):
            button.setAutoDefault(False)
        self.render()

    def visible_contacts(self) -> list[tuple[int, SparkContact]]:
        return [
            (index, contact)
            for index, contact in enumerate(self.scan.contacts)
            if self._filter == "全部"
            or (self._filter == "只看新增" and contact.candidate.stable_key not in self.saved_keys)
            or (self._filter == "可导入" and contact.importable)
            or (self._filter == "待恢复" and contact.spark_state is SparkState.RECOVER)
            or (self._filter == "身份待确认/群聊" and not contact.importable)
        ]

    def render(self) -> None:
        visible = {index for index, _ in self.visible_contacts()}
        with QSignalBlocker(self.tree):
            for index, item in enumerate(self._items):
                item.setHidden(index not in visible)
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked
                    if item.data(0, theme.ROLE_ID) in self.selected_keys
                    else Qt.CheckState.Unchecked,
                )
        self.selection_label.setText(
            f"已勾选 {len(self.selected_keys)} 项（含隐藏项） · 当前显示 {len(visible)} 项"
        )

    def _can_edit(self) -> bool:
        return not self.busy and not self._close_notified and self.validate()

    def _filter_changed(self, text: str) -> None:
        if self._can_edit():
            self._filter = text
            self.render()
        else:
            with QSignalBlocker(self.filter_box):
                self.filter_box.setCurrentText(self._filter)

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        if self._can_edit():
            key = item.data(0, theme.ROLE_ID)
            if item.checkState(0) == Qt.CheckState.Checked:
                self.selected_keys.add(key)
            else:
                self.selected_keys.discard(key)
        self.render()

    def select_all(self) -> None:
        if self._can_edit():
            self.selected_keys.update(
                contact.candidate.stable_key for _, contact in self.visible_contacts()
            )
            self.render()

    def clear_selection(self) -> None:
        if self._can_edit():
            self.selected_keys.clear()
            self.render()

    def _request_import(self) -> None:
        if self._can_edit():
            self._import_selected()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        for control in (
            self.select_all_button,
            self.clear_button,
            self.import_button,
            self.filter_box,
            self.tree,
        ):
            control.setEnabled(not busy)

    def _notify_closed(self, _result: int) -> None:
        if not self._close_notified:
            self._close_notified = True
            self._close_callback()
