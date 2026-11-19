"""设置窗口（对应 C# 的 SettingsWindow.xaml.cs）。

功能：开机自动启动（HKCU\\Run）、全局唤起热键捕获（实时生效）、插件管理（启用/禁用/安装/卸载）。
"""

from __future__ import annotations

import os

from ctypes.wintypes import MSG
from PySide6.QtCore import Qt, QAbstractNativeEventFilter, QCoreApplication, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QWidget,
    QApplication,
)

from .. import autostart, build_info, config, hotkey, reset
from ..config import MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN
from ..hotkey import format_hotkey
from ..plugins.manager import PluginManager
from ..plugins import theme as _t
from .switch import SwitchButton


# ── 设置页定稿样式（原型 .card / .pill / .primary-btn / .hint / .mini-btn）──
# 色值全部来自 liytools/plugins/theme.py（= 原型 data-scheme="air" 色板）
_TITLE_QSS = f"font-size:20px; font-weight:700; color:{_t.TEXT_PRIMARY};"
_CARD_QSS = (f"QWidget#setCard {{background:{_t.BG_SURFACE}; border:1px solid {_t.BORDER};"
             f" border-radius:{_t.RADIUS_CARD}px;}}")
_PILL_QSS = (f"background:{_t.BG_SUBTLE}; border:1px solid {_t.BORDER};"
             f" border-radius:8px; padding:6px 12px; font-weight:600;"
             f" font-size:13px; color:{_t.TEXT_PRIMARY};")
_PRIMARY_QSS = (f"QPushButton{{background:{_t.ACCENT}; color:#FFFFFF; border:none;"
                f" border-radius:8px; font-size:12px;}}"
                f" QPushButton:hover{{background:{_t.ACCENT_HOVER};}}")
_GHOST_QSS = (f"QPushButton{{background:{_t.BG_SUBTLE}; border:1px solid {_t.BORDER};"
              f" border-radius:8px; font-size:12px; color:{_t.TEXT_PRIMARY};}}"
              f" QPushButton:hover{{background:{_t.BG_TILE_HOVER};}}"
              f" QPushButton:disabled{{color:{_t.TEXT_SECONDARY};}}")
_BODY_QSS = f"font-size:12px; color:{_t.TEXT_SECONDARY};"
# 危险按钮（开发工具里的「重置」）：浅红底 + 红字，和普通主按钮区分开
_DANGER_QSS = (f"QPushButton{{background:{_t.DANGER_SOFT}; color:{_t.DANGER};"
               f" border:1px solid {_t.DANGER_BORDER}; border-radius:8px;"
               f" font-size:12px; padding:0 14px;}}"
               f" QPushButton:hover{{background:#FBDDD9;}}"
               f" QPushButton:disabled{{color:{_t.TEXT_SECONDARY};"
               f" background:{_t.BG_SUBTLE}; border-color:{_t.BORDER};}}")


def _size_text(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    return f"{max(0, n) / 1024:.0f} KB"


def _hint(color: str | None = None) -> str:
    """底部小字：默认次要灰，状态色（成功/警告/危险）可覆盖。"""
    return f"font-size:11px; color:{color or _t.TEXT_SECONDARY};"


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
        self.setFixedSize(520, 580)
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTitleHint
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
        self._plugin_mgr = PluginManager.instance()  # 插件管理区读它
        self._refresh_plugin_summary()
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
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        title = QLabel("设置")
        title.setStyleSheet(_TITLE_QSS)
        root.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{border:none;}")
        root.addWidget(scroll, stretch=1)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(12)
        scroll.setWidget(body)

        # 开机自启
        box1 = self._card()
        b1 = QHBoxLayout(box1)
        left = QVBoxLayout()
        left.addWidget(self._label("开机自动启动", bold=True))
        left.addWidget(self._label("登录系统后自动运行 LyLaunch", size=12, color=_t.TEXT_SECONDARY))
        self.autostart_hint = QLabel("")
        self.autostart_hint.setStyleSheet(_hint())
        self.autostart_hint.setWordWrap(True)
        left.addWidget(self.autostart_hint)
        b1.addLayout(left, stretch=1)
        self.autostart = SwitchButton()  # 滑动开关：灰=关 / 蓝=开
        self.autostart.toggled.connect(self._on_autostart)
        b1.addWidget(self.autostart)
        body_layout.addWidget(box1)

        # 全局热键
        box2 = self._card()
        b2 = QVBoxLayout(box2)
        b2.addWidget(self._label("全局唤起热键", bold=True))
        b2.addWidget(self._label(
            f"设置用哪个快捷键唤出 LyLaunch（默认 {config.DEFAULT_HOTKEY_DISPLAY}），更改后立即生效",
            size=12, color=_t.TEXT_SECONDARY))
        hk = QHBoxLayout()
        self.hotkey_display = QLabel(config.DEFAULT_HOTKEY_DISPLAY)
        self.hotkey_display.setStyleSheet(_PILL_QSS)
        self.capture_btn = QPushButton("点击设置")
        self.capture_btn.setFixedSize(88, 32)
        self.capture_btn.setStyleSheet(_PRIMARY_QSS)
        self.capture_btn.clicked.connect(lambda: self._on_capture("show"))
        hk.addWidget(self.hotkey_display)
        hk.addWidget(self.capture_btn)
        hk.addStretch(1)
        b2.addLayout(hk)
        self.hotkey_hint = QLabel("")
        self.hotkey_hint.setStyleSheet(_hint())
        b2.addWidget(self.hotkey_hint)
        body_layout.addWidget(box2)

        # 截图快捷键
        box2b = self._card()
        b2b = QVBoxLayout(box2b)
        b2b.addWidget(self._label("截图快捷键", bold=True))
        b2b.addWidget(self._label("按组合键直接进入截图（默认 Alt+A），更改后立即生效", size=12, color=_t.TEXT_SECONDARY))
        hk2 = QHBoxLayout()
        self.shot_hotkey_display = QLabel(getattr(self._main, "_shot_display", "Alt+A"))
        self.shot_hotkey_display.setStyleSheet(_PILL_QSS)
        self.shot_capture_btn = QPushButton("点击设置")
        self.shot_capture_btn.setFixedSize(88, 32)
        self.shot_capture_btn.setStyleSheet(_PRIMARY_QSS)
        self.shot_capture_btn.clicked.connect(lambda: self._on_capture("screenshot"))
        hk2.addWidget(self.shot_hotkey_display)
        hk2.addWidget(self.shot_capture_btn)
        hk2.addStretch(1)
        b2b.addLayout(hk2)
        self.shot_hotkey_hint = QLabel("")
        self.shot_hotkey_hint.setStyleSheet(_hint())
        b2b.addWidget(self.shot_hotkey_hint)
        body_layout.addWidget(box2b)

        # 本地模型同时开几个（--models-max）
        box2c = self._card()
        b2c = QVBoxLayout(box2c)
        b2c.addWidget(self._label("本地模型", bold=True))
        b2c.addWidget(self._label(
            "翻译、文档识别、本地 AI 共用一套引擎，这里选同时开几个",
            size=12, color=_t.TEXT_SECONDARY))
        seg_row = QHBoxLayout()
        self.models_max_seg = _Segmented(
            [("auto", "自动"), ("1", "1 个"), ("2", "2 个")],
            config.load_models_max())
        self.models_max_seg.changed.connect(self._on_models_max)
        seg_row.addWidget(self.models_max_seg)
        seg_row.addStretch(1)
        b2c.addLayout(seg_row)
        self.models_max_hint = QLabel("")
        self.models_max_hint.setStyleSheet(_hint())
        self.models_max_hint.setWordWrap(True)
        b2c.addWidget(self.models_max_hint)
        body_layout.addWidget(box2c)

        # 插件管理（独立页面：设置里只留入口，列表/开关/安装在那个页面里）
        box3b = self._card()
        b3b = QVBoxLayout(box3b)
        b3b.addWidget(self._label("插件管理", bold=True))
        b3b.addWidget(self._label(
            "启用 / 禁用 / 安装 / 卸载插件，在独立页面里操作",
            size=12, color=_t.TEXT_SECONDARY))
        self.plugin_summary = QLabel("")
        self.plugin_summary.setStyleSheet(_hint())
        b3b.addWidget(self.plugin_summary)
        pb_row = QHBoxLayout()
        self.open_plugins_btn = QPushButton("打开插件管理")
        self.open_plugins_btn.setFixedHeight(32)
        self.open_plugins_btn.setStyleSheet(_PRIMARY_QSS)
        self.open_plugins_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_plugins_btn.clicked.connect(self._on_open_plugins)
        pb_row.addWidget(self.open_plugins_btn)
        pb_row.addStretch(1)
        b3b.addLayout(pb_row)
        body_layout.addWidget(box3b)

        # 开发工具：重置全部数据（只在开发模式出现，打包版没有这个入口）
        if reset.is_dev_mode():
            box_d = self._card()
            bd = QVBoxLayout(box_d)
            bd.addWidget(self._label("开发工具 · 重置", bold=True))
            bd.addWidget(self._label(
                "删除本机全部 LyLaunch 数据：配置、插件、已下载模型、日志。"
                "仅开发模式可见，打包版没有这个入口。",
                size=12, color=_t.TEXT_SECONDARY))
            d_row = QHBoxLayout()
            self.reset_btn = QPushButton("重置所有配置和模型")
            self.reset_btn.setFixedHeight(32)
            self.reset_btn.setStyleSheet(_DANGER_QSS)
            self.reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.reset_btn.clicked.connect(self._on_reset_all)
            d_row.addWidget(self.reset_btn)
            d_row.addStretch(1)
            bd.addLayout(d_row)
            self.reset_hint = QLabel("")
            self.reset_hint.setStyleSheet(_hint())
            self.reset_hint.setWordWrap(True)
            self.reset_hint.setText(self._reset_usage_text())
            bd.addWidget(self.reset_hint)
            body_layout.addWidget(box_d)

        # 关于
        box4 = self._card()
        b4 = QVBoxLayout(box4)
        b4.addWidget(self._label("关于", bold=True))
        self.about = QLabel("")
        self.about.setStyleSheet(_BODY_QSS)
        self.about.setWordWrap(True)
        b4.addWidget(self.about)
        body_layout.addWidget(box4)

        close_btn = QPushButton("关闭")
        close_btn.setFixedSize(80, 32)
        close_btn.setStyleSheet(_PRIMARY_QSS)
        close_btn.clicked.connect(self.accept)
        root.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)

    # ---------- 小工具 ----------
    def _card(self) -> QWidget:
        w = QWidget()
        w.setObjectName("setCard")
        w.setStyleSheet(_CARD_QSS)
        w.setContentsMargins(14, 12, 14, 12)
        return w

    def _label(self, text: str, bold: bool = False, size: int = 14,
               color: str = _t.TEXT_PRIMARY) -> QLabel:
        l = QLabel(text)
        l.setStyleSheet(f"font-size:{size}px; color:{color};" + ("font-weight:bold;" if bold else ""))
        l.setWordWrap(True)
        return l

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
        self.reset_btn.setText("重置所有配置和模型")
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

    # ---------- 插件管理（这里只留入口，列表在独立页面）
    def _refresh_plugin_summary(self) -> None:
        """卡片上只报数量：具体列表/开关去独立页面看。"""
        try:
            plugins = self._plugin_mgr.all_plugins()
        except Exception:
            plugins = []
        on = sum(1 for p in plugins if p.enabled)
        self.plugin_summary.setText(
            f"已安装 {len(plugins)} 个插件 · 已启用 {on} 个" if plugins
            else "还没有安装插件")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._refresh_plugin_summary()

    def _on_open_plugins(self) -> None:
        """打开独立插件管理页；页面里改过插件就同步刷新首页与这里的数量。"""
        from .plugin_manager_window import PluginManagerWindow
        dlg = PluginManagerWindow(self)
        dlg.plugins_changed.connect(self.plugins_changed.emit)
        dlg.exec()
        self._refresh_plugin_summary()
        self.plugins_changed.emit()

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
        self.capture_btn.setText("点击设置")
        self.shot_capture_btn.setText("点击设置")
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
                self.capture_btn.setText("点击设置")
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
                self.shot_capture_btn.setText("点击设置")
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
