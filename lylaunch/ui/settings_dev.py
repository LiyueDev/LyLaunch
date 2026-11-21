"""设置「开发者功能」页：排障与重置这类"非日常"操作集中到这里。

为什么单独成页：
- 「导出诊断包」写出来的是带本机路径、用户名的 zip，「重置本机数据」会删掉
  全部配置 + 插件 + 模型 —— 这两种东西都不该混在日常设置里，误点代价太高。
- 关于页本来只讲版本与署名，塞一组诊断进去后它变成了半张工具页；
  数据页里"占用 / 清理"是日常的，"删除一切"不是。

所以：诊断（日志、导出诊断包）从「关于」搬来，危险操作（重置）从「数据与存储」
搬来，本页统一收口。分组装配复用 settings_diag.build_diag_group，避免逻辑两份。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from ..plugins import theme as _t
from .settings_common import RESET_BTN_TEXT
from .settings_diag import build_diag_group
from .settings_window import _DANGER_QSS


def _caption(lay, text: str) -> None:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet(f"font-size:12px; color:{_t.TEXT_SECONDARY}; padding:2px 2px 0 2px;")
    lay.addWidget(lab)


def build_dev_page(host, lay) -> None:
    """由 SettingsWindow._page("dev") 调用。"""
    _caption(lay, "调试与排障用。这里的操作会往桌面写文件、或删掉本机数据，"
                 "日常使用不需要动。")

    # 诊断：日志入口 + 导出诊断包（只读不写配置）
    build_diag_group(host, lay)

    # 危险操作：重置本机数据（全量删除，删完直接退出程序）
    cv = host._group(lay, "危险操作")
    btn = QPushButton(RESET_BTN_TEXT)
    btn.setFixedSize(84, 30)
    btn.setStyleSheet(_DANGER_QSS)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.clicked.connect(lambda _=False: host._on_reset_all())
    col = host._row(
        cv, "重置本机数据",
        "删除全部配置、插件与已下载模型，程序随后退出",
        btn, wide_hint=True)
    hint = host._hint_label(col, wide=True)
    hint.setText(host._reset_usage_text())

    # SettingsWindow._on_reset_all / _reset_usage_text 靠这两个属性改文案与禁用按钮
    host.reset_btn = btn
    host.reset_hint = hint
