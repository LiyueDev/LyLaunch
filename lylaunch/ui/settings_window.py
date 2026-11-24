"""设置窗口（对应 C# 的 SettingsWindow.xaml.cs）。

功能：开机自动启动（HKCU\\Run）、全局唤起热键捕获（实时生效）、插件管理（启用/禁用/安装/卸载）。
"""

from __future__ import annotations

import os

from ctypes.wintypes import MSG
from PySide6.QtCore import (
    Qt, QAbstractNativeEventFilter, QCoreApplication, QRect, QSize, Signal,
    QPropertyAnimation, QEasingCurve, QPointF, QRectF, QThread, QTimer,
)
from PySide6.QtGui import QKeyEvent, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QStackedWidget, QButtonGroup, QWidget, QFrame,
    QApplication, QGraphicsOpacityEffect, QSlider,
)

from .. import autostart, build_info, config, hotkey, reset
from ..config import MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN
from ..hotkey import format_hotkey
from ..integrations import dsh as _dsh
from ..plugins import theme as _t
from . import backdrop as _backdrop
from .settings_common import RESET_BTN_TEXT, open_url
from .switch import SwitchButton


# ── 设置页定稿样式（两栏：左导航 + 右分页）──
# 主色仍取自 lylaunch/plugins/theme.py（= 原型 air 色板），
# 但整体改为中性灰底 + 白卡，读起来更像「系统设置」而不是工具面板。
_SIDE_BG = "#EEF2F8"      # 左导航底色（比内容区略沉一档）
_PANE_BG = "#F6F8FB"      # 右内容区底色（白卡浮在上面才分得清层级）
_LINE = "#E3E9F1"         # 卡片描边 / 行分割线
_MUTED = "#7C8391"        # 行说明文字 / 次要信息的灰
_TITLE_QSS = f"font-size:20px; font-weight:700; color:{_t.TEXT_PRIMARY};"
_CARD_QSS = (f"QWidget#setCard {{background:#FFFFFF; border:1px solid {_LINE};"
             f" border-radius:12px;}}")
_NAV_QSS = (f"QPushButton{{background:transparent; border:1px solid transparent;"
            f" border-radius:8px; text-align:left; padding-left:9px;"
            f" font-size:13px; color:{_t.TEXT_SECONDARY};}}"
            f" QPushButton:hover{{background:#E9EEF7;}}"
            f" QPushButton:checked{{background:#FFFFFF; border-color:{_LINE};"
            f" color:{_t.ACCENT}; font-weight:600;}}")
_LINK_QSS = (f"QPushButton{{background:transparent; border:none; padding:0 2px;"
             f" font-size:12px; color:{_t.ACCENT};}}"
             f" QPushButton:hover{{color:{_t.ACCENT_HOVER};}}")
_PILL_QSS = (f"background:{_t.BG_SUBTLE}; border:1px solid {_t.BORDER};"
             f" border-radius:8px; padding:6px 12px; font-weight:600;"
             f" font-size:13px; color:{_t.TEXT_PRIMARY};")
_PRIMARY_QSS = (f"QPushButton{{background:{_t.ACCENT}; color:#FFFFFF; border:none;"
                f" border-radius:8px; font-size:12px;}}"
                f" QPushButton:hover{{background:{_t.ACCENT_HOVER};}}"
                f" QPushButton:disabled{{background:{_t.ACCENT_DISABLED};"
                f" color:#FFFFFF;}}")
_GHOST_QSS = (f"QPushButton{{background:#FFFFFF; border:1px solid {_t.BORDER_STRONG};"
              f" border-radius:8px; font-size:12px; color:{_t.TEXT_PRIMARY};}}"
              f" QPushButton:hover{{background:{_t.BG_SUBTLE};}}"
              f" QPushButton:disabled{{color:{_t.TEXT_PLACEHOLDER};"
              f" background:#F7F9FC; border-color:{_LINE};}}")
_BODY_QSS = f"font-size:12px; color:{_t.TEXT_SECONDARY};"
_SET_BTN_TEXT = "修改"
# 危险按钮（开发工具里的「重置」）：浅红底 + 红字，和普通主按钮区分开
_DANGER_QSS = (f"QPushButton{{background:{_t.DANGER_SOFT}; color:{_t.DANGER};"
               f" border:1px solid {_t.DANGER_BORDER}; border-radius:8px;"
               f" font-size:12px; padding:0 14px;}}"
               f" QPushButton:hover{{background:#FBDDD9;}}"
               f" QPushButton:disabled{{color:{_t.TEXT_SECONDARY};"
               f" background:{_t.BG_SUBTLE}; border-color:{_t.BORDER};}}")
# 上下文滑块：细轨 + 蓝色已填充段 + 圆点滑块，和开关/胶囊同一套观感。
# 轨道高度必须与 sub-page 一致，否则 groove 与 sub-page 对不齐（QSS 的老毛病）。
_SLIDER_QSS = (
    f"QSlider::groove:horizontal{{height:4px; background:{_t.BORDER};"
    f" border-radius:2px;}}"
    f" QSlider::sub-page:horizontal{{height:4px; background:{_t.ACCENT};"
    f" border-radius:2px;}}"
    f" QSlider::handle:horizontal{{width:14px; height:14px; margin:-5px 0;"
    f" background:#FFFFFF; border:1px solid {_t.ACCENT}; border-radius:7px;}}"
    f" QSlider::handle:horizontal:hover{{background:{_t.BG_SUBTLE};}}"
    f" QSlider::handle:horizontal:disabled{{border-color:{_t.TEXT_PLACEHOLDER};}}")


def _ctx_text(n: int) -> str:
    """上下文读数：8192 → 8K（内部一律按字节数算，见 config.ENGINE_CONTEXT_STEPS）。"""
    return f"{n // 1024}K"


def _size_text(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    return f"{max(0, n) / 1024:.0f} KB"


def _hint(color: str | None = None) -> str:
    """底部小字：默认次要灰，状态色（成功/警告/危险）可覆盖。"""
    return f"font-size:11px; color:{color or _t.TEXT_SECONDARY};"


def _tray_available() -> bool:
    """系统托盘是否可用（远程会话 / 精简系统里可能没有）。"""
    try:
        from PySide6.QtWidgets import QSystemTrayIcon
        return bool(QSystemTrayIcon.isSystemTrayAvailable())
    except Exception:
        return False


def _nav_icon(kind: str, color: str) -> QIcon:
    """导航项前面的 16px 线性图标（不依赖图标文件，直接画出来）。

    统一 1.4px 线宽、圆头收尾、无填充块，与设置页里那套细描边保持一致；
    按 2 倍分辨率绘制（32px 位图 + dpr 2），125% 缩放下不会糊。
    """
    pm = QPixmap(32, 32)
    pm.fill(Qt.GlobalColor.transparent)
    # 不用 setDevicePixelRatio：QPainter 在高 DPI 位图上会自己再乘一次缩放，
    # 与下面的 scale(2) 叠起来会把字形放大 4 倍再裁掉右下半（实测踩过）。
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.scale(2.0, 2.0)                       # 之后都用 16×16 逻辑坐标
    pen = QPen(QColor(color))
    pen.setWidthF(1.4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    solid = QColor(color)

    if kind == "general":                   # 三条滑块
        for y, x in ((4.5, 6.0), (8.0, 10.5), (11.5, 4.0)):
            p.drawLine(QPointF(2.5, y), QPointF(13.5, y))
            p.setBrush(solid)
            p.drawEllipse(QPointF(x, y), 1.5, 1.5)
            p.setBrush(Qt.BrushStyle.NoBrush)
    elif kind == "hotkeys":                 # 键盘
        p.drawRoundedRect(QRectF(1.6, 4.4, 12.8, 7.6), 2.0, 2.0)
        p.setBrush(solid)
        for i in range(4):
            p.drawEllipse(QPointF(4.0 + i * 2.6, 7.4), 0.65, 0.65)
        p.drawEllipse(QPointF(8.0, 9.9), 0.65, 0.65)
        p.setBrush(Qt.BrushStyle.NoBrush)
    elif kind == "plugins":                 # 四格
        for gx, gy in ((2.2, 2.2), (8.6, 2.2), (2.2, 8.6), (8.6, 8.6)):
            p.drawRoundedRect(QRectF(gx, gy, 5.2, 5.2), 1.4, 1.4)
    elif kind == "models":                  # 芯片：方框 + 四边引脚
        p.drawRoundedRect(QRectF(4.6, 4.6, 6.8, 6.8), 1.6, 1.6)
        p.drawRoundedRect(QRectF(6.9, 6.9, 2.2, 2.2), 0.8, 0.8)
        for t in (6.0, 8.0, 10.0):
            p.drawLine(QPointF(t, 2.4), QPointF(t, 4.6))
            p.drawLine(QPointF(t, 11.4), QPointF(t, 13.6))
            p.drawLine(QPointF(2.4, t), QPointF(4.6, t))
            p.drawLine(QPointF(11.4, t), QPointF(13.6, t))
    elif kind == "integrations":            # 双环相扣（对接）
        for cx in (5.4, 10.6):              # 两个圆角环横向相扣，读作"链接/对接"
            p.drawRoundedRect(QRectF(cx - 3.6, 5.2, 7.2, 5.6), 2.8, 2.8)
    elif kind == "search":                  # 放大镜
        p.drawEllipse(QPointF(7.0, 7.0), 4.6, 4.6)
        p.drawLine(QPointF(10.4, 10.4), QPointF(13.6, 13.6))
    elif kind == "data":                    # 圆柱（数据库）
        p.drawEllipse(QRectF(2.6, 2.2, 10.8, 3.6))
        p.drawLine(QPointF(2.6, 4.0), QPointF(2.6, 12.0))
        p.drawLine(QPointF(13.4, 4.0), QPointF(13.4, 12.0))
        p.drawArc(QRectF(2.6, 10.2, 10.8, 3.6), 180 * 16, 180 * 16)
        p.drawArc(QRectF(2.6, 6.2, 10.8, 3.6), 180 * 16, 180 * 16)
    elif kind == "about":                   # 圆环 + i
        p.drawEllipse(QPointF(8.0, 8.0), 6.0, 6.0)
        p.setBrush(solid)
        p.drawEllipse(QPointF(8.0, 5.1), 0.75, 0.75)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(8.0, 7.4), QPointF(8.0, 11.2))
    elif kind == "dev":                     # 尖括号 </>（代码）
        p.drawLine(QPointF(6.0, 4.6), QPointF(2.4, 8.0))
        p.drawLine(QPointF(2.4, 8.0), QPointF(6.0, 11.4))
        p.drawLine(QPointF(10.0, 4.6), QPointF(13.6, 8.0))
        p.drawLine(QPointF(13.6, 8.0), QPointF(10.0, 11.4))
        p.drawLine(QPointF(9.2, 3.4), QPointF(6.8, 12.6))
    p.end()
    return QIcon(pm)


class _HintLabel(QLabel):
    """卡片里的小字提示：没有内容时不占高度。

    QLabel 空文本也会留一行字高，设置页里 6 张卡各留一行空白，整页就虚胖；
    这里让空状态的高度归零，setText 后再 updateGeometry 把高度要回来。
    """

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setStyleSheet(_hint())
        # 默认是垂直居中，高度差几像素时上下两行都会被裁；改成左上对齐
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt 命名)
        super().setText(text)
        # 提示是动态写进来的（自启状态、插件数量…），空→有内容时卡片要跟着长高，
        # 否则文字会顶到卡片边框上。逐级通知父级重排最稳。
        self._sync_min_height()
        self.updateGeometry()
        w = self.parentWidget()
        while w is not None:
            w.updateGeometry()
            w = w.parentWidget()

    def _empty(self) -> bool:
        return not self.text().strip()

    def _need_height(self, width: int) -> int:
        """按当前宽度算这块字真正需要的高度。

        QLabel 自己的行高在 125% 缩放下会少算（11px 字实际每行 ~18px），
        这里按实测换行行数 × (行距 + 余量) 给足，宁可多留 4px 也不裁字。
        """
        if self._empty():
            return 0
        fm = self.fontMetrics()
        rect = fm.boundingRect(QRect(0, 0, max(1, width), 100000),
                              int(Qt.TextFlag.TextWordWrap), self.text())
        lines = max(1, round(rect.height() / max(1, fm.height())))
        # 关键点：125% 缩放时 QLabel 实际行高是「字体行距 × 屏幕缩放」，按逻辑行距算会少一截，
        # 末行会被自己裁掉；这里按 DPR 补偿，再留 4px 余量。
        dpr = self.devicePixelRatio() or 1.0
        per_line = fm.lineSpacing() * dpr + 6
        return int(round(lines * per_line + 3))

    def sizeHint(self):  # noqa: N802
        if self._empty():
            return QSize(0, 0)
        s = super().sizeHint()
        return QSize(s.width(), max(s.height(), self._need_height(self.width() or 520)))

    def heightForWidth(self, w: int) -> int:  # noqa: N802
        if self._empty():
            return 0
        return max(super().heightForWidth(w), self._need_height(w))

    def minimumSizeHint(self):  # noqa: N802
        if self._empty():
            return QSize(0, 0)
        return QSize(super().minimumSizeHint().width(),
                     self._need_height(self.width() or 520))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_min_height()

    def _sync_min_height(self) -> None:
        """宽度/文字变了行数就变：把高度下限锁到实测值，避免布局把末行挤掉。"""
        need = self._need_height(self.width() or 520)
        if self.minimumHeight() != need:
            self.setMinimumHeight(need)

    def set_color(self, color: str | None = None) -> None:
        self.setStyleSheet(_hint(color))


class _DshProbe(QThread):
    """后台读一次 dsh 配置状态：设置页要建完就走，不能卡在磁盘 IO 上。"""

    done = Signal(object)

    def run(self) -> None:  # noqa: D401 - QThread 入口
        try:
            self.done.emit(_dsh.probe())
        except Exception:  # 读不到就当没装，别让线程异常带崩界面
            self.done.emit(None)


class _EngineWarm(QThread):
    """后台跑一次引擎动作（起 / 重启，首次启动约 1~2 秒，别卡住设置页）。"""

    done = Signal(bool)

    def __init__(self, target, parent=None) -> None:
        super().__init__(parent)
        self._target = target

    def run(self) -> None:  # noqa: D401 - QThread 入口
        ok = False
        try:
            ok = bool(self._target())
        except Exception:      # 起不来不抛：状态由提示文案交代
            ok = False
        self.done.emit(ok)


class _StateProbe(QThread):
    """后台问一次两套引擎在不在线（18090 / 18091）。

    为什么必须后台：本机回环上连"没监听的端口"不会立刻 RST，而是耗满超时
    （实测 127.0.0.1 任意空端口 connect 都要 1000 ms），`running()` 默认
    0.6 s × 2 = 1.2 s。放在构造期就是打开设置先卡一秒多；丢到线程里，
    首帧立刻出来，"检测中…" 随后被真实状态替换。
    """

    done = Signal(object)

    def __init__(self, host, parent=None) -> None:
        super().__init__(parent)
        self._host = host

    def run(self) -> None:  # noqa: D401 - QThread 入口
        result: dict = {"ext": None, "keep": None}
        try:
            ext = self._host._ext()
            if ext is not None:
                result["ext"] = ext.state(probe=True, timeout=0.35)
        except Exception:
            pass
        try:
            from ..engine.manager import EngineManager
            result["keep"] = EngineManager.instance().running(timeout=0.35)
        except Exception:
            pass
        self.done.emit(result)


class _Segmented(QWidget):
    """分段选择器：一排互斥小按钮，选中=蓝色实心，未选中=浅灰描边。

    设置页只有 3 个档位的选项用它（下拉框在浅色卡片里不好看，且默认箭头会被
    DIALOG_QSS 吃掉，ocr 插件那边就得自己补箭头）。
    """

    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str]], current: str,
                 parent=None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._buttons: dict[str, QPushButton] = {}
        self._value = current
        for value, label in options:
            btn = QPushButton(label)
            btn.setFixedHeight(30)
            btn.setMinimumWidth(64)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, v=value: self._pick(v))
            self._buttons[value] = btn
            lay.addWidget(btn)
        self._repaint()

    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        if value in self._buttons:
            self._value = value
            self._repaint()

    def _pick(self, value: str) -> None:
        if value == self._value:
            return
        self._value = value
        self._repaint()
        self.changed.emit(value)

    def _repaint(self) -> None:
        for value, btn in self._buttons.items():
            btn.setStyleSheet(_PRIMARY_QSS if value == self._value else _GHOST_QSS)


class _NativeHotkeyCapture(QAbstractNativeEventFilter):
    """在**原生消息层**截获按键，专治 Qt 层收不到/收到错键码的组合。

    两个真实问题：
    1. **Alt+空格 在 Qt 层根本到不了 keyPressEvent** —— Windows 把它当作"打开窗口
       系统菜单"(SC_KEYMENU) 在 DefWindowProc 里吃掉，所以之前设置 Alt+空格时界面
       毫无反应。这里读 WM_SYSKEYDOWN 就能拿到（此时 wParam=0x20、lParam bit29=Alt）。
    2. **Qt 的 key 值不等于 Win32 虚拟键码** —— F1 在 Qt 里是 0x01000030，直接当 VK
       注册会注册到一个不存在的键。这里直接取 MSG.wParam，天生就是真 VK。

    只在设置页处于"捕获中"时生效；返回 True 即吃掉该消息（系统菜单不会弹出）。
    """

    def __init__(self, owner: "SettingsWindow") -> None:
        super().__init__()
        self._owner = owner

    def nativeEventFilter(self, eventType, message):  # noqa: N802,ANN001
        owner = self._owner
        if owner is None or owner.capturing_target() is None:
            return False, 0
        try:
            et = bytes(eventType)
        except Exception:
            return False, 0
        if et not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return False, 0
        try:
            msg = MSG.from_address(int(message))
        except Exception:
            return False, 0
        if msg.message not in (hotkey.WM_KEYDOWN, hotkey.WM_SYSKEYDOWN):
            return False, 0
        vk = int(msg.wParam) & 0xFFFF
        alt_ctx = bool((int(msg.lParam) >> 29) & 1)   # 上下文代码位：Alt 组合为真
        return (owner.handle_native_key(vk, alt_ctx), 0)


class SettingsWindow(QDialog):
    plugins_changed = Signal()   # 插件安装/卸载/启用禁用后触发，通知主窗口刷新首页
    _CAPTURE_TIP = ("同时按住 Ctrl / Alt / Shift / Win 中至少一个再按一个键；"
                    "支持 Alt+空格、F1-F12、符号键（Esc 取消）")

    def __init__(self, main) -> None:
        # Qt 父对象传 None：让设置成为真正独立的顶层窗口，
        # 不会被主窗口隐藏时级联隐藏，也不会嵌在主窗口里。
        # 主窗口引用通过 self._main 单独保存（用于改热键），与 Qt 父子关系解耦。
        super().__init__(None)
        self._main = main
        self._capturing_for = None  # None / "show" / "screenshot"，当前正在捕获哪个热键
        self.setWindowTitle("LyLaunch 设置")
        # 两栏版式：宽度固定够放下「标题 + 说明 + 控件」，高度按屏幕可用高度收
        _scr = QApplication.primaryScreen()
        _avail = _scr.availableGeometry().height() if _scr else 0
        _h = 620 if not _avail else max(560, min(620, _avail - 100))
        self.setFixedSize(900, _h)
        # 不要 WindowStaysOnTopHint：设置页是 ApplicationModal，只要它开着就没人动得了
        # 主窗口，再叠一层"永远置顶"只会让它死死压在其他应用（浏览器、dsh 本体）上面，
        # 用户切过去看一眼 dsh 的输出都做不到。主窗口在打开设置时已 hide()，不会盖回来。
        self.setWindowFlags(
            Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowSystemMenuHint
        )
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setStyleSheet(_t.DIALOG_QSS)
        _t.apply_light_titlebar(self)   # 0.36：浅色内容配浅色系统标题栏
        # 关掉 DWM 淡出过渡：截图前要把本窗 hide() 掉再抓屏，否则过渡的半透明帧
        # 会被截进图里（实测不关要 ~180ms 才从屏幕消失，关了 40ms 内就干净）。
        _t.disable_window_transitions(self)
        self._setup_ui()
        # 原生按键过滤器：Alt+空格 / F1-F12 / 符号键只有在这一层才拿得到（见 _NativeHotkeyCapture）
        self._native_filter = _NativeHotkeyCapture(self)
        QCoreApplication.instance().installNativeEventFilter(self._native_filter)
        self._load_autostart()
        self._refresh_models_max_hint()
        self._refresh_about()
        self._center_on_screen()

    def done(self, result: int) -> None:  # noqa: N802
        """窗口关闭时摘掉原生过滤器，避免它常驻在 QApplication 上（会一直持有本对象）。"""
        # 关闭时若还停在捕获状态，必须先恢复热键注册：否则用户关掉设置页之后，
        # 全局热键会一直是"已注销"，整个程序看起来彻底失灵。
        self._capturing_for = None
        self._resume_main_hotkeys()
        self._remove_native_filter()
        # 引擎状态探测还在跑时关窗：断信号再等它退，避免回调打到已析构的控件上
        probe = getattr(self, "_state_probe", None)
        if probe is not None:
            self._state_probe = None
            try:
                probe.done.disconnect()
            except Exception:
                pass
            try:
                probe.wait(800)
            except Exception:
                pass
        # 后台线程收尾：下载/卸载/目录统计还在跑时直接销毁窗口，信号会打到
        # 已经析构的控件上。下载会被取消（.part 保留，下次续传）。
        for attr in ("models_section", "data_section", "plugin_keys_section"):
            sec = getattr(self, attr, None)
            if sec is not None:
                try:
                    sec.stop()
                except Exception:
                    pass
        super().done(result)

    def _remove_native_filter(self) -> None:
        f = getattr(self, "_native_filter", None)
        if f is None:
            return
        self._native_filter = None
        try:
            QCoreApplication.instance().removeNativeEventFilter(f)
        except Exception:
            pass

    def _center_on_screen(self) -> None:
        screen = self.screen().availableGeometry()
        x = (screen.width() - self.width()) // 2 + screen.x()
        y = (screen.height() - self.height()) // 2 + screen.y()
        self.move(x, y)

    # ---------- UI ----------
    def _setup_ui(self) -> None:
        """两栏版式：左栏导航 + 右侧分页内容（行式卡片，参考系统设置的样子）。"""
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 左栏：导航 ──
        side = QWidget()
        side.setObjectName("setSide")
        side.setFixedWidth(208)
        side.setStyleSheet(
            f"QWidget#setSide{{background:{_SIDE_BG}; border-right:1px solid {_LINE};}}")
        sv = QVBoxLayout(side)
        sv.setContentsMargins(12, 18, 12, 12)
        sv.setSpacing(2)

        side_title = QLabel("设置")
        side_title.setStyleSheet(
            f"font-size:18px; font-weight:700; color:{_t.TEXT_PRIMARY}; padding:0 8px;")
        sv.addWidget(side_title)
        sv.addSpacing(10)

        self._stack = QStackedWidget()
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._pages: list[str] = []
        self._nav_btns: list[QPushButton] = []
        self._nav_icons: list[tuple[QIcon, QIcon]] = []   # (未选中灰, 选中蓝)

        for key, text in self._nav_items():
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setFixedHeight(36)
            btn.setStyleSheet(_NAV_QSS)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setIconSize(QSize(16, 16))
            gray, accent = _nav_icon(key, _MUTED), _nav_icon(key, _t.ACCENT)
            btn.setIcon(gray)
            idx = len(self._pages)
            self._nav_group.addButton(btn, idx)
            btn.clicked.connect(lambda _=False, i=idx: self._go(i))
            sv.addWidget(btn)
            self._nav_btns.append(btn)
            self._nav_icons.append((gray, accent))
            self._pages.append(key)
            if idx == 0:
                btn.setChecked(True)
                btn.setIcon(accent)

        sv.addStretch(1)
        ver = QLabel(f"LyLaunch {config.VERSION}")
        ver.setStyleSheet(f"font-size:11px; color:{_MUTED}; padding:0 8px;")
        sv.addWidget(ver)
        root.addWidget(side)

        # ── 右栏：内容 ──
        pane = QWidget()
        pane.setObjectName("setPane")
        pane.setStyleSheet(f"QWidget#setPane{{background:{_PANE_BG};}}")
        pv = QVBoxLayout(pane)
        pv.setContentsMargins(26, 20, 26, 18)
        pv.setSpacing(0)
        pv.addWidget(self._stack, stretch=1)
        root.addWidget(pane, stretch=1)

        # 各分页按导航顺序装配。
        # 每页套一层透明滚动区：模型管理 / 数据统计的卡片会超过 620 的高度，
        # 没滚动条就只能靠窗口长高，而窗口高度要按屏幕可用高度收。
        for key in self._pages:
            page = QWidget()
            page.setStyleSheet("background:transparent;")
            pv_page = QVBoxLayout(page)
            pv_page.setContentsMargins(0, 0, 6, 0)
            pv_page.setSpacing(14)
            self._page(key, pv_page)
            pv_page.addStretch(1)

            sa = QScrollArea()
            sa.setWidget(page)
            sa.setWidgetResizable(True)
            sa.setFrameShape(QFrame.Shape.NoFrame)
            sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            sa.setStyleSheet(
                "QScrollArea{background:transparent; border:none;}"
                " QScrollArea > QWidget > QWidget{background:transparent;}")
            sa.viewport().setAutoFillBackground(False)
            self._stack.addWidget(sa)

    def _nav_items(self) -> list[tuple[str, str]]:
        """导航项顺序即分页顺序。"""
        return [("general", "通用"), ("hotkeys", "快捷键"), ("plugins", "插件"),
                ("models", "本地模型"), ("integrations", "外部集成"),
                ("search", "搜索"), ("data", "数据与存储"),
                ("about", "关于"), ("dev", "开发者")]

    # ---------- 切页 ----------
    def _go(self, idx: int) -> None:
        """切到第 idx 页：同步图标高亮，再做一次短淡入。"""
        w = self._stack.widget(idx)
        if w is None:
            return
        changed = idx != self._stack.currentIndex()
        self._stack.setCurrentIndex(idx)
        for i, btn in enumerate(self._nav_btns):
            btn.setIcon(self._nav_icons[i][1 if i == idx else 0])
        if changed:
            self._fade_in(w)

    def _fade_in(self, w: QWidget) -> None:
        """120ms 透明度淡入；切页太快时先收掉上一次的动画，别让页面停在半透明。"""
        prev = getattr(self, "_fade_anim", None)
        if prev is not None:
            try:
                prev.stop()
            except Exception:
                pass
            old = getattr(self, "_fade_widget", None)
            if old is not None:
                try:
                    old.setGraphicsEffect(None)
                except Exception:
                    pass
        eff = QGraphicsOpacityEffect(w)
        w.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(120)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda: w.setGraphicsEffect(None))
        self._fade_anim = anim
        self._fade_widget = w
        anim.start()

    def _page(self, key: str, lay: QVBoxLayout) -> None:
        """按 key 装一页内容：页标题 + 若干「分组标签 + 行式卡片」。"""
        if key == "general":
            self._page_title(lay, "通用", "启动与基础偏好")
            cv = self._group(lay, "启动")
            self.autostart = SwitchButton()   # 滑动开关：灰=关 / 蓝=开
            self.autostart.toggled.connect(self._on_autostart)
            col = self._row(cv, "开机自动启动", "登录系统后自动运行 LyLaunch", self.autostart,
                            wide_hint=True)
            self.autostart_hint = self._hint_label(col, wide=True)

            cv2 = self._group(lay, "窗口")
            self.close_seg = _Segmented(
                [(v, config.CLOSE_ACTION_LABELS[v]) for v in config.CLOSE_ACTIONS],
                config.load_close_action())
            self.close_seg.changed.connect(self._on_close_action)
            col = self._row(cv2, "关闭窗口",
                            "点标题栏的 X 或按 Alt+F4 时做什么",
                            self.close_seg, wide_hint=True)
            self.close_hint = self._hint_label(col, wide=True)
            self._refresh_close_hint()

            # 皮肤：原版（白→淡蓝渐变）与毛玻璃（跟随壁纸做 Acrylic 模糊）
            cv3 = self._group(lay, "外观")
            self.skin_seg = _Segmented(
                [(v, config.WIN_SKIN_LABELS[v]) for v in config.WIN_SKINS],
                config.load_win_skin())
            self.skin_seg.changed.connect(self._on_skin)
            col = self._row(cv3, "窗口皮肤",
                            "原版是不透明的白蓝渐变；毛玻璃在原版渐变上做半透明 + 壁纸模糊",
                            self.skin_seg, wide_hint=True)
            self.skin_hint = self._hint_label(col, wide=True)
            self._refresh_skin_hint()

        elif key == "integrations":
            self._page_title(lay, "外部集成",
                             "把本地模型开放给本机的其他工具（目前支持 DeepSeek Harness）")
            self._build_integrations_page(lay)

        elif key == "hotkeys":
            self._page_title(lay, "快捷键", "全局唤起与截图组合键")
            cv = self._group(lay, "全局快捷键")
            self.hotkey_display = QLabel(config.DEFAULT_HOTKEY_DISPLAY)
            self.hotkey_display.setStyleSheet(_PILL_QSS)
            self.capture_btn = QPushButton(_SET_BTN_TEXT)
            self.capture_btn.setFixedSize(84, 30)
            self.capture_btn.setStyleSheet(_GHOST_QSS)
            self.capture_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.capture_btn.clicked.connect(lambda: self._on_capture("show"))
            col = self._row(cv, "唤起 LyLaunch",
                            f"用哪个组合键唤出主窗口（默认 {config.DEFAULT_HOTKEY_DISPLAY}）",
                            self._controls(self.hotkey_display, self.capture_btn))
            self.hotkey_hint = self._hint_label(col)

            col = self._row(cv, "截图",
                            f"按下组合键直接进入截图（默认 {getattr(self._main, '_shot_display', 'Alt+A')}）",
                            self._controls(
                                self._pill(getattr(self._main, "_shot_display", "Alt+A")),
                                self._ghost_button(_SET_BTN_TEXT, lambda: self._on_capture("screenshot"))),
                            divider=True)
            self.shot_hotkey_display = self._last_pill
            self.shot_capture_btn = self._last_btn
            self.shot_hotkey_hint = self._hint_label(col)

            # 插件（如「桌面净化」）声明的全局快捷键也放这一页：
            # 快捷键是"全局输入"，跟唤起 / 截图摆一起才找得到，插件列表里只留只读展示。
            from .settings_hotkeys import build_plugin_keys
            cv3 = self._group(lay, "插件快捷键")
            self.plugin_keys_section = build_plugin_keys(self, cv3)

        elif key == "plugins":
            self._page_title(lay, "插件", "安装、启用与目录管理")
            # 列表 / 包库 / 目录由独立模块装配，见 settings_plugins.py
            # （原来是独立小窗口，一屏一卡；现在并进设置，改用分组行式白卡）
            from .settings_plugins import build_plugins_page
            build_plugins_page(self, lay)

        elif key == "models":
            self._page_title(lay, "本地模型", "并发设置与模型文件管理")
            cv = self._group(lay, "运行")
            self.models_max_seg = _Segmented(
                [("auto", "自动"), ("1", "1 个"), ("2", "2 个")],
                config.load_models_max())
            self.models_max_seg.changed.connect(self._on_models_max)
            col = self._row(cv, "同时运行模型数",
                            "翻译、文档识别、本地 AI 共用一套引擎，这里选同时开几个",
                            self.models_max_seg, wide_hint=True)
            self.models_max_hint = self._hint_label(col, wide=True)
            # 模型文件管理（汇总 / 已装 / 可下载）由独立模块装配，见 settings_models.py
            from .settings_models import build_models_page
            build_models_page(self, lay)

        elif key == "search":
            self._page_title(lay, "搜索", "索引范围与重建")
            cv = self._group(lay, "索引")
            col = self._row(cv, "索引范围",
                            "桌面与开始菜单里的快捷方式，外加一组系统自带工具",
                            None, wide_hint=True)
            self.index_scope_hint = self._hint_label(col, wide=True)
            self.index_scope_hint.setText(
                "桌面 · 公共桌面 · 开始菜单 · 公共开始菜单 · 系统工具")
            col = self._row(cv, "重建索引",
                            "新装的软件搜不到？重建一次就好，不影响正在用的窗口",
                            self._ghost_button("重建", self._on_rebuild_index),
                            divider=True, wide_hint=True)
            self.index_rebuild_btn = self._last_btn
            self.index_hint = self._hint_label(col, wide=True)
            self._refresh_index_hint()

        elif key == "data":
            self._page_title(lay, "数据与存储", "本机数据占用与清理")
            from .settings_data import build_data_page
            build_data_page(self, lay)

        elif key == "about":
            self._page_title(lay, "关于", "版本与署名")
            cv = self._group(lay, "版本信息")
            self.about = QLabel("")
            self.about.setStyleSheet(f"font-size:12px; color:{_t.TEXT_SECONDARY};")
            self.about.setWordWrap(True)
            # 关于页只有一段文字，四周给足内边距，别贴着卡片边
            cv.setContentsMargins(16, 14, 16, 14)
            cv.addWidget(self.about)

            # 链接：仓库与作者主页（点一下用默认浏览器打开，地址本身也留在行里方便复制）
            cv3 = self._group(lay, "链接")

            def _open(url: str, hint) -> None:
                if open_url(url):
                    hint.setText(f"已在浏览器打开 · {url}")
                    hint.set_color(_t.SUCCESS)
                else:
                    hint.setText(f"没能调起浏览器，可以手动访问：{url}")
                    hint.set_color(_t.DANGER)

            # 提示标签要在行之后才能建（要 _row 的父布局），用 dict 先占个位给按钮闭包取用
            box: dict = {}

            def _open_repo() -> None:
                _open(build_info.REPO_URL, box.get("repo"))

            def _open_home() -> None:
                _open(build_info.HOMEPAGE_URL, box.get("home"))

            col = self._row(cv3, "GitHub 仓库", "源码、版本记录与问题反馈",
                            self._link_button("打开仓库", _open_repo), wide_hint=True)
            box["repo"] = self._hint_label(col, wide=True)
            box["repo"].setText(build_info.REPO_URL)

            col = self._row(cv3, "开发者主页", "LiyueDev 的主页，其它项目也在这里",
                            self._link_button("打开主页", _open_home),
                            divider=True, wide_hint=True)
            box["home"] = self._hint_label(col, wide=True)
            box["home"].setText(build_info.HOMEPAGE_URL)

            # 诊断（日志 / 导出诊断包）已搬到「开发者」页，关于页只讲版本与署名

        elif key == "dev":
            self._page_title(lay, "开发者", "排障、诊断包与重置")
            from .settings_dev import build_dev_page
            build_dev_page(self, lay)

    # ---------- 版式零件 ----------
    def _page_title(self, lay: QVBoxLayout, text: str, sub: str = "") -> None:
        head = QVBoxLayout()
        head.setSpacing(3)
        title = QLabel(text)
        title.setStyleSheet(f"font-size:17px; font-weight:700; color:{_t.TEXT_PRIMARY};")
        head.addWidget(title)
        if sub:
            d = QLabel(sub)
            d.setStyleSheet(f"font-size:12px; color:{_MUTED};")
            head.addWidget(d)
        lay.addLayout(head)

    def _group(self, lay: QVBoxLayout, label: str) -> QVBoxLayout:
        """一组设置：小灰标题 + 白卡片，卡片里每行之间有细分割线。"""
        wrap = QVBoxLayout()
        wrap.setSpacing(7)
        lab = QLabel(label)
        lab.setStyleSheet(f"font-size:12px; color:#6E7683; padding-left:2px;")
        wrap.addWidget(lab)
        card = self._card()
        wrap.addWidget(card)
        lay.addLayout(wrap)
        cv = self._card_body(card)
        return cv

    def _row(self, cv: QVBoxLayout, title: str, desc: str = "",
             control: QWidget | None = None, divider: bool = False,
             wide_hint: bool = False):
        """卡片里的一行：左侧「标题 + 说明」，右侧控件。

        返回挂提示用的布局：默认挂在左列（跟着行文走），
        wide_hint=True 时挂在卡片整行下方（长路径 / 长说明不挤控件）。
        """
        if divider:
            line = QWidget()
            line.setFixedHeight(1)
            line.setStyleSheet(f"background:{_LINE};")
            cv.addWidget(line)
        row = QWidget()
        h = QHBoxLayout(row)
        # 下面紧跟整行提示时，缩小行底内边距，别让提示飘得太远
        h.setContentsMargins(2, 11, 2, 6 if wide_hint else 11)
        h.setSpacing(14)
        col = QVBoxLayout()
        col.setSpacing(2)
        t = QLabel(title)
        t.setStyleSheet(f"font-size:13px; font-weight:600; color:{_t.TEXT_PRIMARY};")
        col.addWidget(t)
        if desc:
            d = QLabel(desc)
            d.setStyleSheet(f"font-size:11px; color:{_MUTED};")
            col.addWidget(d)
        h.addLayout(col, 1)
        if control is not None:
            h.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        cv.addWidget(row)
        return cv if wide_hint else col

    def _pill(self, text: str) -> QLabel:
        """键位胶囊（只读展示用）。"""
        l = QLabel(text)
        l.setStyleSheet(_PILL_QSS)
        self._last_pill = l
        return l

    def _ghost_button(self, text: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setFixedSize(84, 30)
        b.setStyleSheet(_GHOST_QSS)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda _=False: slot())
        self._last_btn = b
        return b

    def _link_button(self, text: str, slot) -> QPushButton:
        """行尾的文字按钮：蓝字 + 右箭头，像系统设置里的「›」入口。"""
        b = QPushButton(f"{text}  ›")
        b.setFixedHeight(28)
        b.setStyleSheet(_LINK_QSS)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda _=False: slot())
        self._last_btn = b
        return b

    def _card_body(self, card: QWidget) -> QVBoxLayout:
        """卡片内文：左右 16px 内边距，行与行之间由 _row(divider=True) 画线。"""
        cv = QVBoxLayout(card)
        cv.setContentsMargins(16, 2, 16, 2)
        cv.setSpacing(0)
        return cv

    def _card(self) -> QWidget:
        w = QWidget()
        w.setObjectName("setCard")
        w.setStyleSheet(_CARD_QSS)
        w.setContentsMargins(0, 0, 0, 0)
        return w

    def _label(self, text: str, bold: bool = False, size: int = 14,
               color: str = _t.TEXT_PRIMARY) -> QLabel:
        l = QLabel(text)
        l.setStyleSheet(f"font-size:{size}px; color:{color};" + ("font-weight:bold;" if bold else ""))
        l.setWordWrap(True)
        return l

    def _hint_label(self, parent_layout: QVBoxLayout, text: str = "",
                    wide: bool = False) -> _HintLabel:
        l = _HintLabel(text)
        if wide:
            # 挂在卡片整行下方：用一层容器给底部内边距，
            # 这样调用方后续改文字颜色（setStyleSheet）不会把边距一起冲掉。
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(2, 0, 0, 4)
            bl.setSpacing(0)
            bl.addWidget(l)
            parent_layout.addWidget(box)
        else:
            parent_layout.addWidget(l)
        return l

    def _controls(self, *widgets: QWidget) -> QWidget:
        """把几个控件并成一列（键位胶囊 + 按钮这种组合）。"""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        for item in widgets:
            h.addWidget(item, 0, Qt.AlignmentFlag.AlignVCenter)
        return w

    def _refresh_about(self) -> None:
        """「关于」页：版本号 + 定位 + 开发者署名。

        不展示源码路径（__file__）等调试信息；署名与版本号统一取自
        build_info / config，避免多处不一致。
        """
        self.about.setText("\n".join(build_info.about_lines(config.VERSION)))

    # ---------- 开发工具：重置
    def _reset_usage_text(self) -> str:
        """卡片底部小字：当前占用 + 数据目录，点之前就能看出要删多少。"""
        try:
            n = reset.total_size()
        except Exception:
            n = 0
        return f"当前占用 {_size_text(n)} · {reset.data_dir()}"

    def _on_reset_all(self) -> None:
        """删除全部本地数据（配置 + 插件 + 模型），随后立即退出程序。

        为什么删完必须退：配置是「改一次写一次」的，进程继续跑就会把刚删掉的
        配置又写回去；插件也可能在退出时落盘。所以走 os._exit，跳过所有收尾。
        引擎在这之前已经 shutdown（模型文件被 mmap 着，不停根本删不掉）。
        """
        items = reset.plan()
        if not items:
            _t.show_warning(self, "没有可重置的数据",
                            f"{reset.data_dir()} 已经是空的。")
            return
        total = reset.total_size()
        top = items[:5]
        # 大小放前面：数字宽度相近，视觉上成一列，比「名称 + 大小」参差好看
        lines = [f"· {_size_text(it['size'])}　{it['label']}" for it in top]
        if len(items) > len(top):
            lines.append(f"· 另有 {len(items) - len(top)} 项配置 / 数据")
        text = (f"将删除 {reset.data_dir()}\n下的全部数据，共 {_size_text(total)}：\n\n"
                + "\n".join(lines)
                + "\n\n模型删掉后要在插件里重新下载；程序会立即退出，"
                  "下次启动等于全新安装。\n\n"
                  "若某个插件正在下载 / 运行，请先关掉它，否则那几个文件会删不掉。\n\n"
                  "此操作不可恢复，确定继续吗？")
        if not _t.ask_confirm(self, "重置所有配置和模型", text,
                              yes_text="确定重置", no_text="取消", default_yes=False):
            return

        self.reset_btn.setEnabled(False)
        self.reset_btn.setText("正在删除…")
        self.reset_hint.setText("正在停止本地推理服务并删除文件…")
        QApplication.processEvents()

        ok, failed = reset.reset_all()
        if ok:
            _t.show_warning(self, "重置完成",
                            "全部配置与模型已删除。\n\n"
                            "程序现在退出，下次启动等于全新安装。")
            os._exit(0)          # 不做任何收尾：收尾会写回配置
        self.reset_btn.setEnabled(True)
        self.reset_btn.setText(RESET_BTN_TEXT)
        self.reset_hint.setText(self._reset_usage_text())
        _t.show_warning(self, "部分内容没删掉",
                        "以下条目被占用或权限不足，关掉相关程序后可再点一次：\n\n"
                        + "\n".join(f"· {f}" for f in failed))

    # ---------- 开机自启 ----------
    # ---------- 本地模型驻留 ----------
    def _refresh_models_max_hint(self) -> None:
        """把「自动判成几个 / 检测到多少显存 / 这样选的代价」说清楚。"""
        try:
            from ..engine.manager import EngineManager
            from ..engine.runtime import vram_total_mb
            mgr = EngineManager.instance()
            setting = mgr.models_max_setting()
            eff = mgr.effective_models_max()
            vram = vram_total_mb()
        except Exception:
            self.models_max_hint.setText("读取引擎设置失败")
            return

        if setting == "auto":
            head = f"按你的显卡自动选了 {eff} 个" if vram else f"没识别到显卡，先按稳的来：{eff} 个"
        else:
            head = f"你选的：同时开 {eff} 个"

        if eff >= 2:
            tail = "来回切模型不用等（两个大模型一起开可能有点卡）"
        else:
            tail = "更省显存，切模型要重新加载一下"
        self.models_max_hint.setStyleSheet(_hint())
        self.models_max_hint.setText(f"{head}，{tail}")

    def _on_models_max(self, value: str) -> None:
        from ..engine.manager import EngineManager
        try:
            mgr = EngineManager.instance()
        except Exception:
            return
        before = mgr.effective_models_max()
        after = EngineManager.resolve_models_max(value)
        running = False
        try:
            from ..engine.runtime import port_health
            running = port_health(mgr.port, timeout=0.6)
        except Exception:
            running = False

        if after != before and running:
            if not _t.ask_confirm(
                self, "需要重启本地引擎",
                "修改后会重启本地推理引擎，当前已加载的模型会被卸载。\n\n"
                "下次打开翻译 / 文档识别 / 本地 AI 时会自动重新加载，不用手动处理。\n\n确定修改吗？",
                yes_text="修改并重启", no_text="算了",
            ):
                self.models_max_seg.set_value(config.load_models_max())
                return

        try:
            res = mgr.set_models_max(value)
        except Exception as e:
            _t.show_warning(self, "设置失败", f"写入引擎设置失败：{e}")
            self.models_max_seg.set_value(config.load_models_max())
            return

        self._refresh_models_max_hint()
        if res.get("restarted"):
            self.models_max_hint.setText(
                self.models_max_hint.text() + "\n引擎已重启，设置已生效。")

    # ---------- 关闭窗口行为 ----------
    def _refresh_close_hint(self) -> None:
        if config.load_close_action() == "tray":
            if _tray_available():
                self.close_hint.setText(
                    "关掉面板只是隐藏，进程与热键继续常驻；"
                    "托盘右键「退出」才真正结束")
                self.close_hint.set_color(None)
            else:
                self.close_hint.setText("当前系统没有托盘区，会按「退出程序」处理")
                self.close_hint.set_color(_t.WARNING)
        else:
            self.close_hint.setText("退出后全局热键随之失效；想一直后台待命就选「最小化到托盘」")
            self.close_hint.set_color(None)

    def _on_close_action(self, value: str) -> None:
        if value == "tray" and not _tray_available():
            _t.show_warning(self, "系统托盘不可用",
                            "当前环境没有系统托盘区，关到托盘后窗口就再也叫不回来了。\n\n"
                            "已保持「退出程序」。")
            self.close_seg.set_value(config.load_close_action())
            return
        config.save_close_action(value)
        self._refresh_close_hint()

    # ---------- 皮肤 ----------
    def _refresh_skin_hint(self) -> None:
        skin = config.load_win_skin()
        if skin != "glass":
            self.skin_hint.setText("当前：原版（默认）。任何壁纸下都最稳")
            self.skin_hint.set_color(None)
            return
        if not _backdrop.supported():
            self.skin_hint.setText(
                "毛玻璃需要在 Windows「设置 → 个性化 → 颜色」里打开「透明效果」；"
                "现在没开，面板会退回原版渐变")
            self.skin_hint.set_color(_t.WARNING)
            return
        self.skin_hint.setText(
            "当前：毛玻璃。原版渐变 + 半透明，投影和边缘与原版一致；关掉设置即可看到")
        self.skin_hint.set_color(None)

    def _on_skin(self, value: str) -> None:
        config.save_win_skin(value)
        self.skin_seg.set_value(value)
        main = self._main
        if main is not None and hasattr(main, "apply_win_skin"):
            main.apply_win_skin(value)
        self._refresh_skin_hint()

    # ---------- 外部集成（独立页） ----------
    def _build_integrations_page(self, lay: QVBoxLayout) -> None:
        """DeepSeek Harness 接入 + 独立的外部引擎。

        外部集成跑在自己的一套 Router（18091）上：preset 是 Router 启动时读进内存的，
        和插件共用一套就意味着「改外部集成的上下文要打断插件正在用的模型」，
        所以拆两套、各用各的 preset 与端口。开关默认关闭，打开时插件那套的模型被卸载。
        """
        cv = self._group(lay, "DeepSeek Harness")
        self.dsh_apply_btn = self._ghost_button("接入", self._on_dsh_apply)
        self.dsh_revert_btn = self._ghost_button("撤销", self._on_dsh_revert)
        col = self._row(cv, "接入到 dsh",
                        "把本地模型注册成 dsh 的自定义模型提供商",
                        self._controls(self.dsh_apply_btn, self.dsh_revert_btn),
                        wide_hint=True)
        self.dsh_hint = self._hint_label(col, wide=True)
        self._refresh_dsh_hint()

        cv2 = self._group(lay, "外部引擎")
        self.ext_engine = SwitchButton()
        self.ext_engine.toggled.connect(self._on_ext_engine)
        col = self._row(cv2, "启用外部引擎",
                        "给 dsh 单开一套 Router（端口 18091）；开启后插件暂时用不了本地模型，"
                        "显存整块让给它",
                        self.ext_engine, wide_hint=True)
        self.ext_hint = self._hint_label(col, wide=True)

        self.ctx_value = self._pill(_ctx_text(config.load_engine_context()))
        self.ctx_slider = QSlider(Qt.Orientation.Horizontal)
        self.ctx_slider.setFixedWidth(188)
        self.ctx_slider.setRange(0, len(config.ENGINE_CONTEXT_STEPS) - 1)
        self.ctx_slider.setSingleStep(1)
        self.ctx_slider.setPageStep(1)
        self.ctx_slider.setStyleSheet(_SLIDER_QSS)
        self.ctx_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ctx_slider.valueChanged.connect(self._on_ctx_slide)
        self.ctx_slider.sliderReleased.connect(self._commit_engine_ctx)
        # 拖完才落盘并重启：拖动过程每挪一档就重启一次引擎，机器会卡成幻灯片
        self._ctx_timer = QTimer(self)
        self._ctx_timer.setSingleShot(True)
        self._ctx_timer.setInterval(400)
        self._ctx_timer.timeout.connect(self._commit_engine_ctx)
        col = self._row(cv2, "模型上下文",
                        "只作用于外部引擎；dsh 的系统提示和工具描述就要约 9.4K tokens",
                        self._controls(self.ctx_slider, self.ctx_value),
                        divider=True, wide_hint=True)
        self.ctx_hint = self._hint_label(col, wide=True)
        self._load_engine_ctx()

        self.ext_restart_btn = self._ghost_button("重启", lambda: self._on_ext_restart())
        self.ext_stop_btn = self._ghost_button("结束", lambda: self._on_ext_stop())
        col = self._row(cv2, "引擎进程",
                        "改完上下文要重启才生效；dsh 报 Connection error 时先确认这里还在跑",
                        self._controls(self.ext_restart_btn, self.ext_stop_btn),
                        divider=True, wide_hint=True)
        self.ext_proc_hint = self._hint_label(col, wide=True)

        # 插件那套引擎的常驻开关：和外部引擎同一张卡片，省一屏高度；两者互斥关系写在提示里
        self.keep_engine = SwitchButton()
        self.keep_engine.toggled.connect(self._on_keep_engine)
        col = self._row(cv2, "保持插件引擎运行",
                        "翻译 / 文档识别 / 本地 AI 共用 18090；开着就随程序启动，平时不加载模型",
                        self.keep_engine, divider=True, wide_hint=True)
        self.keep_engine_hint = self._hint_label(col, wide=True)
        self._load_keep_engine()

        self._load_ext_engine()
        self._start_dsh_probe()
        self._start_state_probe()      # 两套引擎在不在线，后台问，首帧不等它

    def _load_engine_ctx(self) -> None:
        """滑块对齐到已保存的档位（不触发 valueChanged，避免误判成用户改动）。"""
        idx = config.engine_context_index(config.load_engine_context())
        self.ctx_slider.blockSignals(True)
        self.ctx_slider.setValue(idx)
        self.ctx_slider.blockSignals(False)
        self._refresh_ctx_hint()

    def _on_ctx_slide(self, idx: int) -> None:
        """拖动过程中只更新读数，停手 400ms 后再落盘（见 _ctx_timer）。"""
        self.ctx_value.setText(_ctx_text(config.engine_context_value(idx)))
        self._ctx_timer.start()

    def _commit_engine_ctx(self) -> None:
        """把滑块当前档位写进设置：外部 preset 的 ctx-size 与 dsh 的 contextWindow 一起改。

        只重启外部那套 Router（18091）：插件那套的 preset 不吃这个值，也不该被打断。
        """
        self._ctx_timer.stop()
        value = config.engine_context_value(self.ctx_slider.value())
        if value == config.load_engine_context():
            self._refresh_ctx_hint()
            return

        from ..engine.external import ExternalEngine
        try:
            ext = ExternalEngine.instance()
            running = ext.running()
        except Exception as e:
            _t.show_warning(self, "设置失败", f"读取外部引擎状态失败：{e}")
            self._load_engine_ctx()
            return

        if running:
            if not _t.ask_confirm(
                self, "需要重启外部引擎",
                f"上下文改成 {_ctx_text(value)} 之后要重启外部引擎（端口 18091），"
                "dsh 里已加载的模型会被卸载。\n\n"
                "大上下文更吃显存：9B 模型 16K 约 7.5GB，再翻倍就是 KV cache 也翻倍。\n\n"
                "插件那套引擎不受影响。dsh 下次发消息时会按新值重新加载。\n\n"
                "确定修改吗？",
                yes_text="修改并重启", no_text="算了",
            ):
                self._load_engine_ctx()
                return

        try:
            res = ext.set_context(value)
        except Exception as e:
            _t.show_warning(self, "设置失败", f"写入引擎设置失败：{e}")
            self._load_engine_ctx()
            return

        # dsh 那边的 contextWindow 也得跟着改，否则它按旧值算预算照样 400
        self._resync_dsh_context()
        self._refresh_ctx_hint()
        self._refresh_ext_hint()
        if res.get("restarted"):
            self.ctx_hint.setText(self.ctx_hint.text() + "\n外部引擎已按新值重启。")

    def _resync_dsh_context(self) -> None:
        """上下文变了就把已接入的 dsh provider 段重写一遍（没接入就什么都不做）。"""
        st = getattr(self, "_dsh_status", None)
        if st is None or not st.applied:
            return
        try:
            self._dsh_status = _dsh.apply(_dsh.chat_models())
        except Exception:   # noqa: BLE001 —— 接入状态原本就正常，同步失败不该打断设置流程
            pass
        self._refresh_dsh_hint()

    def _refresh_ctx_hint(self) -> None:
        """说清楚这个值意味着什么：够不够跑 dsh、显存上要不要谨慎。"""
        value = config.load_engine_context()
        self.ctx_value.setText(_ctx_text(value))
        if value < _dsh.MIN_USABLE_CONTEXT:
            # dsh 的提示就要 ~9.4K，再小的档配上输出预算只剩几百 token 余量
            tail = f"跑 dsh 会爆窗，至少给到 {_ctx_text(_dsh.MIN_USABLE_CONTEXT)}"
            self.ctx_hint.set_color(_t.WARNING)
        elif value == _dsh.MIN_USABLE_CONTEXT:
            tail = "跑 dsh 够用（提示占 9.4K，剩下约 7K 给对话）"
            self.ctx_hint.set_color(None)
        elif value >= config.ENGINE_CONTEXT_MAX:
            # 顶档就是按这台机器的显存账单算出来的天花板，再高会直接 OOM
            tail = "这台机器的实测上限（12GB 显存刚好装下，再高要 OOM）"
            self.ctx_hint.set_color(_t.WARNING)
        else:
            tail = "更长对话也装得下；显存吃紧就往下调一档"
            self.ctx_hint.set_color(None)
        self.ctx_hint.setText(f"当前 {_ctx_text(value)} · {tail}")

    # ---------- 外部引擎（第二套 Router） ----------
    def _ext(self):
        """外部引擎单例；拿不到返回 None（引擎模块缺失时设置页仍要能建起来）。"""
        try:
            from ..engine.external import ExternalEngine
            return ExternalEngine.instance()
        except Exception:
            return None

    def _load_ext_engine(self) -> None:
        """开关对齐到已保存的状态；在线状态留给后台探测，构造期不碰网络。"""
        on = config.load_ext_engine()
        self.ext_engine.blockSignals(True)
        self.ext_engine.setChecked(on)
        self.ext_engine.blockSignals(False)
        self._refresh_ext_hint(probe=False)

    def _refresh_ext_hint(self, probe: bool = True) -> None:
        """外部引擎现在到底跑没跑、pid 多少、已加载模型的实际 n_ctx。

        ``probe=False`` 只渲染「检测中…」（0 ms，构造期用）；``probe=True``
        同步问一次端口，超时压到 0.35 s（本机回环空端口要耗满超时，
        默认 0.6 s 会让按钮点完顿半秒）。
        """
        from ..engine import EXTERNAL_PORT
        if not probe:
            self._render_ext_hint({
                "running": None, "pid": 0, "port": EXTERNAL_PORT, "n_ctx": 0,
                "enabled": config.load_ext_engine(),
            })
            return
        state = {"running": False, "pid": 0, "port": EXTERNAL_PORT,
                 "n_ctx": 0, "enabled": config.load_ext_engine()}
        ext = self._ext()
        if ext is not None:
            try:
                state = ext.state(probe=True, timeout=0.35)
            except Exception:
                pass
        self._render_ext_hint(state)

    def _render_ext_hint(self, state: dict) -> None:
        """把上面那份状态渲染成提示文案（同步/异步两条路径共用）。"""
        want = config.load_engine_context()
        running = state.get("running")
        if running is None:
            self.ext_hint.setText("正在检测外部引擎状态…")
            self.ext_hint.set_color(None)
        elif running:
            ctx = state.get("n_ctx") or 0
            if ctx and ctx != want:
                self.ext_hint.setText(
                    f"运行中 · pid {state['pid']} · 已加载 {_ctx_text(ctx)}"
                    f"（设置是 {_ctx_text(want)}），点「重启」让它按新值加载")
                self.ext_hint.set_color(_t.WARNING)
            else:
                tail = f" · n_ctx {_ctx_text(ctx)}" if ctx else ""
                self.ext_hint.setText(
                    f"运行中 · pid {state['pid']} · 端口 {state['port']}{tail} · 插件暂停使用本地模型")
                self.ext_hint.set_color(_t.SUCCESS)
        elif state.get("enabled"):
            self.ext_hint.setText(
                "已开启，但 18091 还没应答（首次启动要一两秒；一直不行就点「重启」）")
            self.ext_hint.set_color(_t.WARNING)
        else:
            self.ext_hint.setText("已关闭 · 插件可以正常用本地模型；dsh 现在连不上（Connection error）")
            self.ext_hint.set_color(None)
        self.ext_proc_hint.setText("「重启」按当前上下文重新加载；「结束」只收掉外部引擎，不影响主程序")

    def _on_ext_engine(self, checked: bool) -> None:
        if not checked:
            ext = self._ext()
            if ext is not None:
                try:
                    ext.disable()
                except Exception:
                    pass
            self._load_ext_engine()
            self._start_dsh_probe()
            self._start_state_probe()
            return
        if not _t.ask_confirm(
            self, "启用外部引擎",
            "会单独起一套推理引擎（端口 18091），专给 dsh 用：\n\n"
            "· 插件那套已加载的模型会先被卸载，之后翻译 / 文档识别 / 本地 AI 暂时用不了本地模型\n"
            f"· 9B 模型按当前上下文（{_ctx_text(config.load_engine_context())}）加载，"
            "显存占用比插件那套高\n\n"
            "确定启用吗？",
            yes_text="启用",
        ):
            self._load_ext_engine()      # 回弹：用户点了「算了」
            return
        self._warm_external()

    def _warm_external(self) -> None:
        self.ext_hint.setText("正在启动外部引擎…")
        self.ext_hint.set_color(None)
        ext = self._ext()
        if ext is None:
            self.ext_hint.setText("外部引擎模块不可用，看看安装是否完整")
            self.ext_hint.set_color(_t.DANGER)
            return
        warm = _EngineWarm(ext.enable, self)
        warm.done.connect(self._on_ext_warm)
        warm.finished.connect(warm.deleteLater)
        self._ext_warm = warm            # 保住引用：线程跑完前被回收会崩进程
        warm.start()

    def _on_ext_warm(self, ok: bool) -> None:
        self._load_ext_engine()
        if not ok:
            self.ext_hint.setText(
                "外部引擎没起来（端口被占或运行库缺失），看「开发者」页的 engine-ext.log")
            self.ext_hint.set_color(_t.DANGER)
        else:
            self._start_state_probe()    # 起来了：后台补一次 pid / n_ctx 的真实状态
        self._start_dsh_probe()          # 引擎状态变了，接入提示里"没在跑"该消掉

    def _on_ext_restart(self) -> None:
        ext = self._ext()
        if ext is None:
            return
        if not _t.ask_confirm(
            self, "重启外部引擎",
            "会卸载 dsh 里已加载的模型，按当前上下文重新加载。\n\n确定吗？",
            yes_text="重启",
        ):
            return
        self.ext_hint.setText("正在重启外部引擎…")
        self.ext_hint.set_color(None)
        warm = _EngineWarm(ext.restart, self)
        warm.done.connect(self._on_ext_warm)
        warm.finished.connect(warm.deleteLater)
        self._ext_warm = warm
        warm.start()

    def _on_ext_stop(self) -> None:
        ext = self._ext()
        if ext is None:
            return
        if not _t.ask_confirm(
            self, "结束外部引擎",
            "18091 会立刻断开，dsh 那边会报 Connection error。\n\n"
            "插件那套不受影响，但「启用外部引擎」开关也会一起关掉。确定吗？",
            yes_text="结束",
        ):
            return
        try:
            ext.disable()
        except Exception as e:
            _t.show_warning(self, "结束失败", str(e))
        self._load_ext_engine()
        self._start_dsh_probe()
        self._start_state_probe()

    # ---------- DeepSeek Harness ----------
    def _start_state_probe(self) -> None:
        """后台问一次两套引擎在不在线；回来再替换「检测中…」文案。

        幂等：上一个还没跑完就不重复起（连点开关时会连调这里的）。
        """
        prev = getattr(self, "_state_probe", None)
        if prev is not None and prev.isRunning():
            return
        probe = _StateProbe(self, self)
        probe.done.connect(self._on_state_probed)
        probe.finished.connect(probe.deleteLater)
        self._state_probe = probe        # 保住引用：线程还没跑完就被回收会崩进程
        probe.start()

    def _on_state_probed(self, result) -> None:
        if not isinstance(result, dict):
            return
        if result.get("ext") is not None:
            try:
                self._render_ext_hint(result["ext"])
            except Exception:
                pass
        keep = result.get("keep")
        if keep is not None:
            try:
                self._render_keep_engine_hint(bool(keep))
            except Exception:
                pass

    def _start_dsh_probe(self) -> None:
        probe = _DshProbe(self)
        probe.done.connect(self._on_dsh_probed)
        probe.finished.connect(probe.deleteLater)
        self._dsh_probe = probe     # 保住引用：线程还没跑完就被回收会崩进程
        probe.start()

    def _on_dsh_probed(self, status) -> None:
        self._dsh_status = status
        self._refresh_dsh_hint()

    def _refresh_dsh_hint(self) -> None:
        st = getattr(self, "_dsh_status", None)
        btn = getattr(self, "dsh_revert_btn", None)
        if btn is not None:
            btn.setEnabled(bool(st is not None and st.applied))
        if st is None:
            self.dsh_hint.setText("正在检查本机 dsh…")
            self.dsh_hint.set_color(None)
            return
        if st.applied:
            ids = "、".join(st.models[:3])
            if len(st.models) > 3:
                ids += f" 等 {len(st.models)} 个"
            # dsh 的 agent 提示约 9.4k tokens：上下文不到 16K 必然爆窗
            too_small = any(m.context < _dsh.MIN_USABLE_CONTEXT
                            for m in _dsh.chat_models() if m.id in st.models)
            tail = ""
            if st.port_mismatch:
                # 从"共用 18090"改成"独立 18091"之后，旧文件还指着插件那套端口
                tail += (f"；写进去的地址还是旧端口 {st.base_port}，"
                         "点一次「接入」切到外部引擎")
            if st.ctx_mismatch:
                # 滑块档位改过、但 dsh 那份还按老窗口算预算：dsh 会显示 /16.4K
                tail += (f"；写进去的窗口还是 {_ctx_text(st.base_ctx)}，"
                         f"点一次「接入」更新到 {_ctx_text(config.load_engine_context())}")
            if too_small:
                tail += (f"；上下文只有 {_ctx_text(config.load_engine_context())}，"
                         "跑 dsh 会爆窗，把上面「模型上下文」调大")
            if not st.engine_online:
                tail += "；外部引擎现在没在跑，dsh 会报 Connection error，把「启用外部引擎」打开"
            wrong = st.port_mismatch or st.ctx_mismatch or too_small or not st.engine_online
            self.dsh_hint.setText(
                f"已接入：{ids}。到 dsh 的模型选择器里选「{_dsh.PROVIDER_NAME}」{tail}")
            self.dsh_hint.set_color(_t.WARNING if wrong else None)
            return
        if not st.installed:
            self.dsh_hint.setText(f"没找到 dsh（{st.home}）。装好并跑过一次之后再来接入")
            self.dsh_hint.set_color(_t.WARNING)
            return
        if not _dsh.chat_models():
            self.dsh_hint.setText("未接入 · 本机还没有对话模型，先到「本地模型」页下载一个")
            self.dsh_hint.set_color(_t.WARNING)
            return
        self.dsh_hint.setText(f"未接入 · 目标文件 {st.target}")
        self.dsh_hint.set_color(None)

    def _on_dsh_apply(self) -> None:
        models = _dsh.chat_models()
        if not models:
            _t.show_warning(self, "没有可接入的模型",
                            "本地还没有下载好的对话模型（Qwen3.5-9B、Qwen3-4B 这类）。\n\n"
                            "先到「本地模型」页下载一个，再回来接入。")
            return
        st = _dsh.probe()
        names = "、".join(m.id for m in models[:5])
        if len(models) > 5:
            names += f" 等 {len(models)} 个"
        where = ("dsh 的 patch 层（桌面版实际读这一层）"
                 if st.mode == "patch" else "dsh 的 settings.yaml")
        text = (f"写入{where}：\n{st.target}\n\n"
                f"注册模型：{names}\n\n"
                "只改 llm-pi-ai.providers 下 LyLaunch 自己那一段，你的其它配置原样保留；"
                f"首次改动前会把原文件备份成 {st.target.name}.lylaunch-bak。")
        if not _t.ask_confirm(self, "接入 DeepSeek Harness", text, yes_text="接入"):
            return
        try:
            self._dsh_status = _dsh.apply(models)
        except _dsh.DshError as exc:
            _t.show_warning(self, "接入失败", str(exc))
            return
        except OSError as exc:
            _t.show_warning(self, "接入失败", f"写文件出错：{exc}")
            return
        self._refresh_dsh_hint()
        # 接入完就该能用：dsh 连的是外部引擎（18091），没开就顺手打开
        if not config.load_ext_engine():
            self.ext_engine.setChecked(True)      # 触发 _on_ext_engine：确认 + 预热
        elif not self._dsh_status.engine_online:
            self._warm_external()

    def _on_dsh_revert(self) -> None:
        st = _dsh.probe()
        if not st.applied:
            _t.show_warning(self, "没有可撤销的配置", "当前 dsh 配置里没有 LyLaunch 写入的内容。")
            return
        text = (f"从 {st.target} 里摘掉 LyLaunch 写的那一段，\n"
                f"并删掉 .env 里的占位密钥 {_dsh.API_KEY_ENV}。\n\n"
                "你自己的 provider 和其它设置不受影响。")
        if not _t.ask_confirm(self, "撤销 dsh 接入", text, yes_text="撤销"):
            return
        try:
            self._dsh_status = _dsh.revert()
        except OSError as exc:
            _t.show_warning(self, "撤销失败", f"写文件出错：{exc}")
            return
        self._refresh_dsh_hint()

    # ---------- 插件引擎常驻 ----------
    def _load_keep_engine(self) -> None:
        """初始化开关显示：blockSignals 避免 setChecked 触发 toggled 重复写配置。

        在线状态同样留给后台探测（构造期只读配置，不探端口）。
        """
        on = config.load_keep_engine()
        self.keep_engine.blockSignals(True)
        self.keep_engine.setChecked(on)
        self.keep_engine.blockSignals(False)
        self._refresh_keep_engine_hint(probe=False)

    def _refresh_keep_engine_hint(self, probe: bool = True) -> None:
        if config.load_ext_engine():
            self.keep_engine_hint.setText(
                "外部引擎开着时不预热这一套（显存要留给 dsh）；关掉外部引擎后按需启动")
            self.keep_engine_hint.set_color(_t.WARNING)
            return
        online = None
        if probe:
            try:
                from ..engine.manager import EngineManager
                online = EngineManager.instance().running(timeout=0.35)
            except Exception:
                online = False
        self._render_keep_engine_hint(online)

    def _render_keep_engine_hint(self, online: bool | None) -> None:
        """``online=None`` 表示后台还没问回来，先显示「检测中」。"""
        if config.load_ext_engine():
            self.keep_engine_hint.setText(
                "外部引擎开着时不预热这一套（显存要留给 dsh）；关掉外部引擎后按需启动")
            self.keep_engine_hint.set_color(_t.WARNING)
            return
        if not config.load_keep_engine():
            self.keep_engine_hint.setText(
                "未开启 · 插件用到模型时临时启动（每次多等一两秒）")
            self.keep_engine_hint.set_color(None)
            return
        if online is None:
            self.keep_engine_hint.setText("正在检测插件引擎状态…")
            self.keep_engine_hint.set_color(None)
            return
        if online:
            self.keep_engine_hint.setText(
                "已开启 · Router 常驻，模型仍按需加载（不加载就不占显存）")
            self.keep_engine_hint.set_color(_t.SUCCESS)
            return
        self.keep_engine_hint.setText("已开启 · Router 还没起来（首次启动需要一两秒）")
        self.keep_engine_hint.set_color(_t.WARNING)

    def _on_keep_engine(self, checked: bool) -> None:
        config.save_keep_engine(checked)
        # 不在这里同步探端口（本机空端口要耗满 0.35s）：先显示「检测中」，后台补真实状态
        self._refresh_keep_engine_hint(probe=False)
        self._start_state_probe()
        # 关掉只是不再自动拉起；已在跑的 Router 留给本次运行用（不硬杀，免得打断插件）
        if checked and not config.load_ext_engine():
            self._warm_engine()

    def _warm_engine(self) -> None:
        from ..engine.manager import EngineManager
        self.keep_engine_hint.setText("正在启动本地引擎…")
        self.keep_engine_hint.set_color(None)
        warm = _EngineWarm(lambda: EngineManager.instance().start() or True, self)
        warm.done.connect(self._on_engine_warm)
        warm.finished.connect(warm.deleteLater)
        self._engine_warm = warm      # 保住引用：线程跑完前被回收会崩进程
        warm.start()

    def _on_engine_warm(self, ok: bool) -> None:
        if ok:
            self._render_keep_engine_hint(True)   # 刚起成功了，不用再问一次端口
        else:
            self._refresh_keep_engine_hint(probe=False)
            self.keep_engine_hint.setText(
                "引擎没起来（端口被占或运行库缺失），看「开发者」页的日志")
            self.keep_engine_hint.set_color(_t.DANGER)

    # ---------- 搜索索引 ----------
    def _index_obj(self):
        return getattr(self._main, "search_index", None)

    def _refresh_index_hint(self) -> None:
        idx = self._index_obj()
        if idx is None:
            self.index_hint.setText("当前环境拿不到索引（预览模式）")
            self.index_hint.set_color(_t.WARNING)
            btn = getattr(self, "index_rebuild_btn", None)
            if btn is not None:
                btn.setEnabled(False)
            return
        n = int(getattr(idx, "last_count", 0) or 0)
        s = int(getattr(idx, "last_system_count", 0) or 0)
        ms = int(getattr(idx, "last_ms", 0) or 0)
        if n or s:
            self.index_hint.setText(
                f"本次运行已索引 {n} 个应用 + {s} 个系统工具 · 构建耗时 {ms} ms")
        else:
            self.index_hint.setText("尚未构建")
        self.index_hint.set_color(None)

    def _on_rebuild_index(self) -> None:
        idx = self._index_obj()
        if idx is None:
            return
        btn = self.index_rebuild_btn
        btn.setEnabled(False)
        btn.setText("重建中…")
        self.index_hint.setText("正在扫描桌面与开始菜单…")
        self.index_hint.set_color(None)
        QApplication.processEvents()
        try:
            idx.build()
        except Exception as exc:
            btn.setEnabled(True)
            btn.setText("重建")
            self.index_hint.setText(f"重建失败：{exc}")
            self.index_hint.set_color(_t.DANGER)
            return
        try:
            self._main.refresh_list()   # 首页瓦片跟着换一批（不重建就还是旧列表）
        except Exception:
            pass
        btn.setEnabled(True)
        btn.setText("重建")
        self._refresh_index_hint()
        self.index_hint.setText("已重建 · " + self.index_hint.text())
        self.index_hint.set_color(_t.SUCCESS)

    # ---------- 开机自启 ----------
    def _load_autostart(self) -> None:
        # blockSignals：仅初始化开关显示，避免 setChecked 触发 toggled 又去重写注册表
        enabled = autostart.is_enabled()
        self.autostart.blockSignals(True)
        self.autostart.setChecked(enabled)
        self.autostart.blockSignals(False)
        if not enabled:
            self.autostart_hint.setText("未开启")
            self.autostart_hint.setStyleSheet(_hint())
            return
        stored = autostart.stored_command() or ""
        if not autostart.command_is_current():
            # 重装/换目录后旧值还指向已经不存在的 exe：开关是开的，开机却没反应
            self.autostart_hint.setText(
                f"已开启，但启动项还指向旧路径：{stored}\n关闭再打开这个开关即可更新为新路径")
            self.autostart_hint.setStyleSheet(_hint(_t.WARNING))
            return
        self.autostart_hint.setText(f"已开启：{stored}")
        self.autostart_hint.setStyleSheet(_hint(_t.SUCCESS))

    def _on_autostart(self, checked: bool) -> None:
        try:
            if checked:
                autostart.enable()
                if not autostart.is_enabled():
                    raise RuntimeError("注册表里没读到刚写入的启动项")
                self.autostart_hint.setText(f"已开启：{autostart.stored_command()}")
                self.autostart_hint.setStyleSheet(_hint(_t.SUCCESS))
            else:
                autostart.disable()
                self.autostart_hint.setText("已关闭")
                self.autostart_hint.setStyleSheet(_hint())
        except Exception as e:
            # 失败回滚开关状态，避免勾选与实际注册表不一致。
            # 必须 blockSignals：否则 setChecked 会再次发 toggled → 递归进来重试 → 死循环。
            self.autostart.blockSignals(True)
            self.autostart.setChecked(not checked)
            self.autostart.blockSignals(False)
            self.autostart_hint.setText(f"设置开机自启失败：{e}")
            self.autostart_hint.setStyleSheet(_hint(_t.DANGER))

    # ---------- 插件管理（列表本体在 settings_plugins.py）
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 每次打开都重读一次插件状态：插件可能在别处被启停 / 装掉过。
        # 排到首帧之后：插件列表要扫目录 + 读图标（实测 26 ms），
        # 先让窗口画出来再做刷新，打开的那一下才不顿。
        QTimer.singleShot(0, self._refresh_sections)

    def _refresh_sections(self) -> None:
        for attr in ("plugins_section", "plugin_keys_section"):
            sec = getattr(self, attr, None)
            if sec is not None:
                try:
                    sec.refresh()
                except Exception:
                    pass

    # ---------- 热键捕获 ----------
    def capturing_target(self) -> str | None:
        """当前正在捕获哪个热键（供原生消息过滤器查询）：None / "show" / "screenshot"。"""
        return self._capturing_for

    def handle_native_key(self, vk: int, alt_ctx: bool) -> bool:
        """原生键盘消息入口：返回 True 表示这条消息已被吃掉。

        全程 try/except：这里跑在 Qt 的原生消息分发里，异常往上抛会穿到 C++ 层，
        可能直接崩进程。再怎么出问题也只该是"这一次按键没生效"。
        """
        if self._capturing_for is None:
            return False
        try:
            # 纯修饰键：按住 Ctrl/Alt 本身不算组合键，吃掉消息但不动作
            if hotkey.is_modifier_vk(vk):
                return True
            if vk == hotkey.VK_ESCAPE:
                self._cancel_capture()
                return True
            self._apply_capture(hotkey.pressed_modifiers(alt_ctx), vk)
        except Exception:
            pass
        return True

    def _on_capture(self, which: str) -> None:
        """进入/取消快捷键捕获。which: "show"(全局唤起) / "screenshot"(截图)。"""
        # 插件快捷键那边正在捕获时先收掉：两种捕获不能同时抢键盘
        keys = getattr(self, "plugin_keys_section", None)
        if keys is not None:
            try:
                keys.cancel()
            except Exception:
                pass
        if self._capturing_for == which:          # 再点一次 = 取消
            self._cancel_capture()
            return
        if self._capturing_for is not None:       # 正在捕获另一个，先取消
            self._cancel_capture()
        self._capturing_for = which
        # 捕获期间必须把两把全局热键临时注销：否则 Windows 会把这两个组合从键盘
        # 消息流里抽走改投 WM_HOTKEY —— 按键既到不了捕获逻辑，还会真的触发
        # "启动截图/唤出主窗口"（见 MainWindow.suspend_hotkeys 的说明）。
        self._suspend_main_hotkeys()
        if which == "show":
            self.capture_btn.setText("请按下快捷键…")
            self.hotkey_hint.setText(self._CAPTURE_TIP)
            self.hotkey_hint.setStyleSheet(_hint())
        else:
            self.shot_capture_btn.setText("请按下快捷键…")
            self.shot_hotkey_hint.setText(self._CAPTURE_TIP)
            self.shot_hotkey_hint.setStyleSheet(_hint())
        self.activateWindow()

    def _suspend_main_hotkeys(self) -> None:
        """让主窗口临时注销全局热键（捕获按键期间）。失败也不能挡住捕获流程。"""
        try:
            self._main.suspend_hotkeys()
        except Exception:
            pass

    def _resume_main_hotkeys(self) -> None:
        """恢复主窗口的全局热键注册（捕获结束/取消时）。"""
        try:
            self._main.resume_hotkeys()
        except Exception:
            pass

    def _cancel_capture(self) -> None:
        self._capturing_for = None
        self._restore_capture_ui()
        self._resume_main_hotkeys()   # 取消后原热键立刻恢复可用

    def _restore_capture_ui(self) -> None:
        """把两个捕获按钮/提示恢复到未捕获状态（不动热键注册）。"""
        self.capture_btn.setText(_SET_BTN_TEXT)
        self.shot_capture_btn.setText(_SET_BTN_TEXT)
        self.hotkey_hint.setText("")
        self.shot_hotkey_hint.setText("")
        self.hotkey_hint.setStyleSheet(_hint())
        self.shot_hotkey_hint.setStyleSheet(_hint())

    def _set_hint(self, label: QLabel, text: str, error: bool = False) -> None:
        label.setText(text)
        label.setStyleSheet(
            _hint(_t.DANGER) if error else _hint(_t.SUCCESS))

    def _apply_capture(self, mods: int, vk: int) -> None:
        """拿到一组 (修饰键, 虚拟键码)：校验 → 交给主窗口注册 → 反馈。

        失败时**不退出捕获状态**：用户直接再按一个组合即可（不必重新点按钮），
        并且原来的热键仍然可用（回滚在 MainWindow.set_hotkey 里保证）。
        """
        which = self._capturing_for
        if which is None:
            return
        hint = self.hotkey_hint if which == "show" else self.shot_hotkey_hint
        if vk == 0:
            self._set_hint(hint, "这个按键不支持，请换成字母 / 数字 / F1-F12 / 空格 / 符号键", True)
            return
        if mods == 0:
            self._set_hint(hint, "需要至少一个修饰键：请同时按住 Ctrl / Alt / Shift / Win 再按键", True)
            return
        combo = format_hotkey(mods, vk)
        if which == "show":
            if (mods, vk) == (self._main._shot_mods, self._main._shot_vk):
                self._set_hint(hint, f"{combo} 已经是「截图快捷键」了，请换一个组合", True)
                return
            ok, err = self._main.set_hotkey(mods, vk)
            if ok:
                self._capturing_for = None
                self.capture_btn.setText(_SET_BTN_TEXT)
                self.hotkey_display.setText(self._main._hotkey_display)
                self._set_hint(hint, f"已保存：按 {self._main._hotkey_display} 即可唤出（无需重启）")
                # 新组合已由 set_hotkey 注册好，这里只需把另一把（截图）补回来
                self._resume_main_hotkeys()
            else:
                self._set_hint(hint, err or "设置失败，请重试", True)
        else:
            if (mods, vk) == (self._main._hotkey_mods, self._main._hotkey_vk):
                self._set_hint(hint, f"{combo} 已经是「全局唤起热键」了，请换一个组合", True)
                return
            ok, err = self._main.set_screenshot_hotkey(mods, vk)
            if ok:
                self._capturing_for = None
                self.shot_capture_btn.setText(_SET_BTN_TEXT)
                self.shot_hotkey_display.setText(self._main._shot_display)
                self._set_hint(hint, f"已保存：按 {self._main._shot_display} 直接截图（无需重启）")
                self._resume_main_hotkeys()
            else:
                self._set_hint(hint, err or "设置失败，请重试", True)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        """Qt 层兜底路径（正常情况按键已被原生过滤器吃掉，走不到这里）。"""
        if self._capturing_for is not None:
            event.accept()
            key = event.key()
            # 纯修饰键先忽略
            if key in (Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Shift,
                       Qt.Key.Key_Meta, Qt.Key.Key_AltGr):
                return
            if key == Qt.Key.Key_Escape:
                self._cancel_capture()
                return
            mods = event.modifiers()
            if mods == Qt.KeyboardModifier.NoModifier:
                hint = self.hotkey_hint if self._capturing_for == "show" else self.shot_hotkey_hint
                self._set_hint(hint, "需要至少一个修饰键：请同时按住 Ctrl / Alt / Shift / Win 再按键", True)
                return
            fmods = 0
            if mods & Qt.KeyboardModifier.ControlModifier:
                fmods |= MOD_CONTROL
            if mods & Qt.KeyboardModifier.AltModifier:
                fmods |= MOD_ALT
            if mods & Qt.KeyboardModifier.ShiftModifier:
                fmods |= MOD_SHIFT
            if mods & Qt.KeyboardModifier.MetaModifier:
                fmods |= MOD_WIN
            # Qt 的 key 值 ≠ Win32 虚拟键码（F1 是 0x01000030），必须换算
            self._apply_capture(fmods, hotkey.vk_from_qt_key(key.value))
            return
        if event.key() == Qt.Key.Key_Escape:
            self.accept()
            return
        super().keyPressEvent(event)
