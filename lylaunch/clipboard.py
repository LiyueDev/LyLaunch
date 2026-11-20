"""剪贴板历史轮询（对应 C# 的 ClipTick）。

后台定时器每 700ms 读一次剪贴板文本；内容变化就记入历史（上限 200 条）。
忽略"我们自己写入"的变更（用 _just_set 标记），避免自循环。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication


class ClipboardWatcher(QObject):
    changed = Signal(str)  # 新增一条剪贴板文本时触发

    def __init__(self, parent: QObject | None = None, interval_ms: int = 700, max_items: int = 200) -> None:
        super().__init__(parent)
        self.history: list[str] = []
        self.max_items = max_items
        self._last: str | None = None
        self._just_set = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._timer.start(700)

    def _tick(self) -> None:
        try:
            text = QApplication.clipboard().text()
        except Exception:
            return
        if not text or not text.strip():
            return
        if self._just_set:           # 这次变更是我们自己写的
            self._just_set = False
            self._last = text
            return
        if text == self._last:
            return
        self._last = text
        self.history.insert(0, text)
        while len(self.history) > self.max_items:
            self.history.pop()
        self.changed.emit(text)

    def write(self, text: str) -> None:
        """把文本写回剪贴板（点击剪贴板项时调用）。"""
        self._just_set = True
        try:
            QApplication.clipboard().setText(text)
        except Exception:
            self._just_set = False
