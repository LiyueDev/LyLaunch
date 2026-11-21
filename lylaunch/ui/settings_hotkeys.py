"""设置「快捷键」页里的「插件快捷键」分组。

为什么搬到这里：快捷键是"全局输入"，和「唤起 LyLaunch」「截图」放一起才好找；
插件列表里每行挂一个「修改」按钮，既挤又容易被当成插件自身的设置。
插件行那边只留只读的一行「快捷键 Alt+D」。

改键流程（和原来一致，只是换了个宿主）：
1. 点「修改」→ 先 `MainWindow.suspend_hotkeys()`。已注册的组合会被 Windows 从
   键盘消息流里抽走改投 WM_HOTKEY，不注销的话捕获逻辑一个按键都收不到。
2. 捕获到组合 → 写回该插件 plugin.json 的 Hotkey + 同步内存 manifest。
3. `resume_hotkeys()` 按新清单重注册（幂等，只补没注册上的）。
"""
from __future__ import annotations

import json
import os

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from ..hotkey import format_hotkey, parse_hotkey, vk_from_qt_key
from ..plugins import theme as _t
from ..plugins.manager import PluginManager
from .settings_common import ElidedLabel
from .settings_window import _GHOST_QSS, _LINE, _PILL_QSS, _SET_BTN_TEXT

_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008


def _qt_mods_to_win(mods) -> int:
    """Qt 键盘修饰符 → Win32 RegisterHotKey 的 MOD_* 位。"""
    value = 0
    if mods & Qt.KeyboardModifier.ControlModifier:
        value |= _MOD_CONTROL
    if mods & Qt.KeyboardModifier.AltModifier:
        value |= _MOD_ALT
    if mods & Qt.KeyboardModifier.ShiftModifier:
        value |= _MOD_SHIFT
    if mods & Qt.KeyboardModifier.MetaModifier:
        value |= _MOD_WIN
    return value


class _PluginKeysSection(QObject):
    """把所有声明了 Hotkey 的插件列成行；捕获逻辑挂在这个 QObject 上。"""

    def __init__(self, host, cv) -> None:
        super().__init__(host)
        self.host = host
        self._mgr = PluginManager.instance()
        self._holder: QVBoxLayout | None = None
        self._rows: list[QWidget] = []
        self._hk: dict | None = None
        self._build(cv)
        self.refresh()

    # ---------------------------------------------------------------- 装配
    def _build(self, cv) -> None:
        holder = QWidget()
        holder.setStyleSheet("background:transparent;")
        self._holder = QVBoxLayout(holder)
        self._holder.setContentsMargins(0, 0, 0, 2)
        self._holder.setSpacing(0)
        cv.addWidget(holder)

    # ---------------------------------------------------------------- 刷新
    def refresh(self) -> None:
        if self._hk is not None:
            self._stop_hk()
        self._clear()
        items = [p for p in self._mgr.all_plugins()
                 if (p.manifest.hotkey or "").strip()]
        for i, info in enumerate(items):
            if i:
                self._divider()
            row, ctx = self._build_row(info)
            self._holder.addWidget(row)
            self._rows.append(row)
        if not items:
            empty = QLabel("（还没有插件声明全局快捷键）")
            empty.setStyleSheet(f"font-size:12px; color:{_t.TEXT_SECONDARY};")
            empty.setContentsMargins(2, 12, 2, 12)
            self._holder.addWidget(empty)
            self._rows.append(empty)

    def _clear(self) -> None:
        if self._holder is None:
            return
        while self._holder.count():
            item = self._holder.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()              # 先离场，别和新行叠一帧
                w.setParent(None)
                w.deleteLater()
        self._rows.clear()

    def _divider(self) -> None:
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{_LINE};")
        self._holder.addWidget(line)

    def _build_row(self, info) -> tuple[QWidget, dict]:
        spec = (info.manifest.hotkey or "").strip()
        wrap = QWidget()
        vl = QVBoxLayout(wrap)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        top = QWidget()
        h = QHBoxLayout(top)
        h.setContentsMargins(2, 11, 2, 6)
        h.setSpacing(14)

        col = QVBoxLayout()
        col.setSpacing(2)
        title = QLabel(info.manifest.name)
        title.setStyleSheet(f"font-size:13px; font-weight:600; color:{_t.TEXT_PRIMARY};")
        col.addWidget(title)
        desc = ElidedLabel(info.manifest.description or "", size=11)
        col.addWidget(desc)
        h.addLayout(col, 1)

        pill = QLabel(spec)
        pill.setStyleSheet(_PILL_QSS)
        btn = QPushButton(_SET_BTN_TEXT)
        btn.setFixedSize(84, 30)
        btn.setStyleSheet(_GHOST_QSS)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip("点一下再按新组合键，立即生效（Esc 取消）")
        h.addWidget(self.host._controls(pill, btn), 0, Qt.AlignmentFlag.AlignVCenter)
        vl.addWidget(top)

        hint = self.host._hint_label(vl, wide=True)
        hint.setText("")                    # 空状态不占高度，只在捕获/保存后出字

        ctx = {"info": info, "pill": pill, "btn": btn, "hint": hint}
        btn.clicked.connect(lambda _=False, c=ctx: self._start_hk(c))
        return wrap, ctx

    # ---------------------------------------------------------------- 捕获
    def _start_hk(self, ctx: dict) -> None:
        if self._hk is not None:
            self._stop_hk()
        # 设置页自己的「唤起 / 截图」捕获正在跑时先收掉，两种捕获不能同时占用键盘
        if getattr(self.host, "_capturing_for", None):
            try:
                self.host._cancel_capture()
            except Exception:
                pass
        self._hk = ctx
        try:
            ctx["btn"].setText("请按下快捷键…")
        except RuntimeError:
            pass
        self.host._suspend_main_hotkeys()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        ctx["hint"].set_color(_t.TEXT_SECONDARY)
        ctx["hint"].setText("同时按住 Ctrl / Alt / Shift / Win 中至少一个再按一个键（Esc 取消）")

    def _stop_hk(self) -> None:
        ctx = self._hk
        self._hk = None
        if ctx is not None:
            try:
                ctx["btn"].setText(_SET_BTN_TEXT)
            except RuntimeError:
                pass
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.host._resume_main_hotkeys()

    def cancel(self) -> None:
        """外部要抢键盘时调用（设置页进入自己的捕获前）。"""
        if self._hk is not None:
            self._stop_hk()

    def _persist(self, ctx: dict, spec: str) -> bool:
        info = ctx["info"]
        path = os.path.join(info.directory, "plugin.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data["Hotkey"] = spec
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            ctx["hint"].setText(f"保存失败：{exc}")
            ctx["hint"].set_color(_t.DANGER)
            return False
        info.manifest.hotkey = spec        # 内存里的清单同步更新
        try:
            ctx["pill"].setText(spec)
        except RuntimeError:
            pass
        ctx["hint"].setText(f"已改为 {spec}，立即生效")
        ctx["hint"].set_color(_t.SUCCESS)
        return True

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if self._hk is None:
            return super().eventFilter(obj, event)
        if event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(obj, event)
        key = event.key()
        if key == Qt.Key.Key_Escape:
            ctx = self._hk
            ctx["hint"].setText("")
            ctx["hint"].set_color(None)
            self._stop_hk()
            return True
        if key in (Qt.Key.Key_Control, Qt.Key.Key_Shift,
                   Qt.Key.Key_Alt, Qt.Key.Key_Meta):
            return True                    # 纯修饰键不算组合
        spec = format_hotkey(_qt_mods_to_win(event.modifiers()), vk_from_qt_key(key))
        if parse_hotkey(spec) is None:
            return True
        ctx = self._hk
        self._persist(ctx, spec)           # 先改 manifest
        self._stop_hk()                    # 再 resume：按新值重注册
        return True

    def stop(self) -> None:
        if self._hk is not None:
            self._stop_hk()


def build_plugin_keys(host, cv) -> _PluginKeysSection:
    """设置「快捷键」页装配入口：挂到「全局快捷键」卡片下面。"""
    return _PluginKeysSection(host, cv)
