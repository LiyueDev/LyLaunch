"""设置页新分页共用的极小工具集。

只为「不要在三个文件里各写一遍 _size_text / os.startfile」而存在，
不引入任何业务逻辑，也不依赖 settings_window（避免循环 import）。
"""
from __future__ import annotations

import os
import subprocess

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSizePolicy

from ..plugins import theme as _t

# 重置按钮的统一文案：按钮窄（84px），长标题会撑破行右侧
RESET_BTN_TEXT = "重置…"


class ElidedLabel(QLabel):
    """单行省略：说明文字太长时按宽度截断加「…」，不换行也不挤右侧控件。

    设置页的插件行 / 快捷键行都用它 —— 插件说明动辄三行，放开换行会把整张卡
    撑高，一页只能看两三个插件。
    """

    def __init__(self, text: str = "", parent=None, size: int = 11) -> None:
        super().__init__(parent)
        self._full = text or ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setStyleSheet(f"font-size:{size}px; color:#7C8391;")
        self.setToolTip(self._full)
        self._apply()

    def setFullText(self, text: str) -> None:
        self._full = text or ""
        self.setToolTip(self._full)
        self._apply()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply()

    def _apply(self) -> None:
        fm = self.fontMetrics()
        super().setText(fm.elidedText(self._full, Qt.TextElideMode.ElideRight,
                                      max(40, self.width())))


def size_text(n: int) -> str:
    """字节 → 人读的大小（1 位小数，GB/MB/KB 自动选档）。"""
    n = max(0, int(n))
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def mb_text(mb: int) -> str:
    return size_text(int(mb) * 1024 * 1024)


def open_dir(target) -> bool:
    """在资源管理器里打开目录；os.startfile 不灵时退回 explorer.exe。"""
    try:
        os.startfile(str(target))  # type: ignore[attr-defined]
        return True
    except Exception:
        pass
    try:
        subprocess.Popen(f'explorer.exe "{target}"', shell=True)
        return True
    except Exception:
        return False


def open_url(url: str) -> bool:
    """用系统默认浏览器打开链接（失败返回 False，由调用方显示原因）。"""
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        return bool(QDesktopServices.openUrl(QUrl(url)))
    except Exception:
        return False


def value_label(text: str = "") -> "QLabel":
    """行右侧的只读数值（比按钮轻，不抢视线）。"""
    from PySide6.QtWidgets import QLabel
    lab = QLabel(text)
    lab.setStyleSheet(f"font-size:13px; font-weight:600; color:{_t.TEXT_PRIMARY};")
    return lab


def muted_label(text: str = "", size: int = 11) -> "QLabel":
    from PySide6.QtWidgets import QLabel
    lab = QLabel(text)
    lab.setStyleSheet(f"font-size:{size}px; color:#7C8391;")
    lab.setWordWrap(True)
    return lab
