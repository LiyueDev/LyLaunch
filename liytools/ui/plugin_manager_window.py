"""插件管理独立页面（从设置页「插件管理」卡片进入）。

为什么拆出来：原来塞在设置页里是一个 150px 高的列表框 + 三个按钮，
「先选中某一行、再点下面按钮」两步操作，图标、来源、失败原因都显示不全。
现在一页看全：每行一个插件卡片，带图标、版本、内置/用户徽章、
加载失败的红色原因，开关直接启用/禁用，卸载在行内。

父窗口是设置页（ApplicationModal），所以这里用 exec() 打开 —— 非模态子窗口
在模态父窗口下容易被系统判定为不可交互。
"""
from __future__ import annotations

import json
import os

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QFrame, QGraphicsDropShadowEffect,
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)
from ..hotkey import format_hotkey, parse_hotkey, vk_from_qt_key

from ..plugins.manager import PluginManager
from ..plugins.plugin_icons import PLUGIN_ICON_BG, plugin_icon_svg, render_plugin_svg
from ..plugins import theme as _t
from .switch import SwitchButton

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

_WIN_W, _WIN_H = 648, 588

_QSS = f"""
QLabel#pmTitle {{ font-size:20px; font-weight:700; color:{_t.TEXT_PRIMARY}; }}
QLabel#pmSub {{ font-size:12px; color:{_t.TEXT_SECONDARY}; }}
QLabel#pmEmpty {{ font-size:12.5px; color:{_t.TEXT_SECONDARY}; padding:26px 0; }}
QLabel#pmHint {{ font-size:11px; color:{_t.TEXT_SECONDARY}; }}

QFrame#plugRow {{
    background:{_t.BG_SURFACE}; border:1px solid {_t.BORDER}; border-radius:10px;
}}
QFrame#plugRow:hover {{ background:#F8FBFF; border-color:{_t.BORDER_STRONG}; }}

QLabel#pName {{ font-size:13.5px; font-weight:600; color:{_t.TEXT_PRIMARY}; }}
QLabel#pMeta {{ font-size:11px; color:{_t.TEXT_SECONDARY}; }}
QLabel#pDesc {{ font-size:11.5px; color:{_t.TEXT_SECONDARY}; }}
QLabel#pErr  {{ font-size:11px; color:{_t.DANGER}; }}

QLabel#pBadgeLocal, QLabel#pBadgeUser {{
    font-size:9.5px; border-radius:4px; padding:1px 5px;
}}
QLabel#pBadgeLocal {{ background:{_t.SOFT_BG}; color:{_t.SOFT_FG}; }}
QLabel#pBadgeUser {{ background:{_t.BG_SUBTLE}; color:{_t.TEXT_SECONDARY};
    border:1px solid {_t.BORDER}; }}

QPushButton#pUninstall {{
    background:transparent; border:1px solid {_t.BORDER}; border-radius:7px;
    font-size:11px; color:{_t.TEXT_SECONDARY}; padding:3px 10px;
}}
QPushButton#pUninstall:hover {{ border-color:{_t.DANGER}; color:{_t.DANGER}; }}
QPushButton#pUninstall:disabled {{ color:{_t.TEXT_PLACEHOLDER}; border-color:#E8EDF6; }}

QPushButton#pInstall {{
    background:{_t.ACCENT}; color:#FFFFFF; border:none; border-radius:8px;
    font-size:12px; padding:0 14px;
}}
QPushButton#pInstall:hover {{ background:{_t.ACCENT_HOVER}; }}
QPushButton#pClose {{
    background:{_t.BG_SUBTLE}; border:1px solid {_t.BORDER}; border-radius:8px;
    font-size:12px; color:{_t.TEXT_PRIMARY}; padding:0 20px;
}}
QPushButton#pClose:hover {{ background:{_t.BG_TILE_HOVER}; }}

/* ---------- 安装包面板（分组：已安装 / 可安装） ---------- */
QFrame#installPanel {{
    background:{_t.BG_SURFACE}; border:1px solid {_t.BORDER}; border-radius:12px;
}}
QLabel#kTitle {{ font-size:12.5px; font-weight:600; color:{_t.TEXT_PRIMARY}; }}
QLabel#kSub {{ font-size:10.5px; color:{_t.TEXT_SECONDARY}; }}
QLabel#kSection {{ font-size:10.5px; color:{_t.TEXT_SECONDARY}; padding:5px 2px 1px 2px; }}
QLabel#kEmpty {{ font-size:11.5px; color:{_t.TEXT_SECONDARY}; padding:10px 2px; }}
QFrame#pkgRow {{ background:transparent; border-radius:8px; }}
QFrame#pkgRow:hover {{ background:{_t.BG_SUBTLE}; }}
QLabel#kName {{ font-size:12.5px; color:{_t.TEXT_PRIMARY}; }}
QLabel#kMeta {{ font-size:10.5px; color:{_t.TEXT_SECONDARY}; }}
QPushButton#browseBtn {{
    background:transparent; border:none; color:{_t.ACCENT};
    font-size:11.5px; padding:6px 0;
}}
QPushButton#browseBtn:hover {{ color:{_t.ACCENT_HOVER}; }}
"""


class _ElidedLabel(QLabel):
    """单行省略标签：QLabel 自带换行会把行高撑乱，这里按宽度截断加「…」。"""

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = text or ""
        self._apply()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply()

    def _apply(self) -> None:
        fm = self.fontMetrics()
        width = max(20, self.width())
        super().setText(fm.elidedText(self._full, Qt.TextElideMode.ElideRight, width))


class _PluginRow(QFrame):
    """一行插件：图标 + 名称/版本/描述 + 开关 + 卸载。"""

    toggled = Signal(str, bool)
    uninstall = Signal(str)
    hotkey_edit = Signal(object)   # 请求修改全局快捷键（传卡片自身）

    def __init__(self, info, parent=None) -> None:
        super().__init__(parent)
        self.info = info
        self.setObjectName("plugRow")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(13)

        icon = QLabel()
        icon.setFixedSize(40, 40)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(f"border-radius:10px; background:{PLUGIN_ICON_BG};")
        try:
            dpr = self.devicePixelRatioF() or 1.0
            icon.setPixmap(render_plugin_svg(plugin_icon_svg(info.id), 22, dpr))
        except Exception:
            pass
        lay.addWidget(icon)

        col = QVBoxLayout()
        col.setSpacing(3)
        col.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        top.setSpacing(7)
        name = QLabel(info.manifest.name)
        name.setObjectName("pName")
        top.addWidget(name)
        badge = QLabel("内置" if info.source == "local" else "用户")
        badge.setObjectName("pBadgeLocal" if info.source == "local" else "pBadgeUser")
        top.addWidget(badge)
        meta = QLabel(f"v{info.manifest.version}")
        meta.setObjectName("pMeta")
        top.addWidget(meta)
        top.addStretch(1)
        col.addLayout(top)

        desc = _ElidedLabel(info.manifest.description or "")
        desc.setObjectName("pDesc")
        col.addWidget(desc)

        if info.error:
            err = QLabel(f"加载失败：{info.error}")
            err.setObjectName("pErr")
            err.setWordWrap(True)
            col.addWidget(err)

        # 全局快捷键（清单 Hotkey 字段）：显示 + 修改入口
        hotkey_spec = (info.manifest.hotkey or "").strip()
        if hotkey_spec:
            hk_row = QHBoxLayout()
            hk_row.setSpacing(6)
            self.hotkey_label = QLabel(f"全局快捷键：{hotkey_spec}")
            self.hotkey_label.setObjectName("pMeta")
            hk_row.addWidget(self.hotkey_label)
            hk_row.addStretch(1)
            self.hotkey_btn = QPushButton("修改")
            self.hotkey_btn.setObjectName("pHotkeyEdit")
            self.hotkey_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.hotkey_btn.setStyleSheet(
                "QPushButton{padding:2px 10px;border-radius:6px;font-size:11px;"
                f"background-color:{_t.SOFT_BG};color:{_t.SOFT_FG};border:none;}}")
            self.hotkey_btn.setToolTip("点击后按下新的组合键，立即生效（Esc 取消）")
            self.hotkey_btn.clicked.connect(
                lambda _c=False: self.hotkey_edit.emit(self))
            hk_row.addWidget(self.hotkey_btn)
            col.addLayout(hk_row)
        lay.addLayout(col, stretch=1)

        right = QVBoxLayout()
        right.setSpacing(6)
        right.setContentsMargins(0, 0, 0, 0)
        self.switch = SwitchButton()
        # 必须先 setChecked 再 connect：setChecked 内部会发 toggled，
        # 先连上的话建列表的瞬间就会触发「启用 + load_all」把插件全重载一遍。
        self.switch.setChecked(info.enabled)
        self.switch.toggled.connect(
            lambda on, pid=info.id: self.toggled.emit(pid, bool(on)))
        right.addWidget(self.switch, alignment=Qt.AlignmentFlag.AlignRight)

        builtin = info.source == "local"
        self.del_btn = QPushButton("卸载")
        self.del_btn.setObjectName("pUninstall")
        self.del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.del_btn.setEnabled(not builtin)
        if builtin:
            self.del_btn.setToolTip("内置插件不能卸载，只能禁用")
        else:
            self.del_btn.clicked.connect(
                lambda _c=False, pid=info.id: self.uninstall.emit(pid))
        right.addWidget(self.del_btn, alignment=Qt.AlignmentFlag.AlignRight)
        lay.addLayout(right)


# 面板内控件用**内联样式**：QScrollArea 的内容 widget 自己 setStyleSheet 之后，
# QSS 继承链在那层被截断，父窗口样式表里的 #kInstall 之类规则匹配不全
# （实测 border / color 生效、background-color 不生效，按钮渲染成白底白字看不见）。
_BADGE_ON = (f"font-size:9.5px; background-color:{_t.SOFT_BG}; color:{_t.SOFT_FG};"
             f" border-radius:4px; padding:2px 6px;")
_BADGE_OLD = (f"font-size:9.5px; background-color:{_t.BG_SUBTLE};"
              f" color:{_t.TEXT_SECONDARY}; border:1px dashed {_t.BORDER_STRONG};"
              f" border-radius:4px; padding:1px 5px;")
_BTN_INSTALL = (f"QPushButton{{background-color:{_t.ACCENT}; color:#FFFFFF; border:none;"
                f" border-radius:7px; font-size:11px; padding:4px 13px; font-weight:600;}}"
                f" QPushButton:hover{{background-color:{_t.ACCENT_HOVER};}}")
_BTN_UPDATE = (f"QPushButton{{background-color:{_t.BG_SUBTLE}; color:{_t.TEXT_PRIMARY};"
               f" border:1px solid {_t.BORDER}; border-radius:7px; font-size:11px;"
               f" padding:3px 12px;}}"
               f" QPushButton:hover{{background-color:{_t.BG_TILE_HOVER};"
               f" border-color:{_t.ACCENT};}}")


class _PkgRow(QFrame):
    """包库一行：名称 + 版本 + 状态（已安装 / 可更新 / 安装按钮）。"""

    action = Signal(str)        # 传 zip 路径

    def __init__(self, pkg: dict, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("pkgRow")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 8, 7)
        lay.setSpacing(10)

        col = QVBoxLayout()
        col.setSpacing(1)
        col.setContentsMargins(0, 0, 0, 0)
        name = QLabel(pkg.get("name") or pkg.get("id") or "")
        name.setObjectName("kName")
        col.addWidget(name)
        ver = QLabel(f"v{pkg.get('version') or '—'}")
        ver.setObjectName("kMeta")
        col.addWidget(ver)
        lay.addLayout(col, stretch=1)

        installed = bool(pkg.get("installed"))
        cur = str(pkg.get("installed_version") or "").strip()
        new = str(pkg.get("version") or "").strip()
        if installed and (not new or cur == new):
            badge = QLabel("已安装")
            badge.setStyleSheet(_BADGE_ON)
            lay.addWidget(badge)
        elif installed:
            # 装过但版本不同 → 给「更新」而不是「安装」，一眼看出能升级
            badge = QLabel(f"已装 {cur or '?'}")
            badge.setStyleSheet(_BADGE_OLD)
            lay.addWidget(badge)
            btn = QPushButton("更新")
            btn.setStyleSheet(_BTN_UPDATE)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(
                lambda _c=False, p=pkg["path"]: self.action.emit(p))
            lay.addWidget(btn)
        else:
            btn = QPushButton("安装")
            btn.setStyleSheet(_BTN_INSTALL)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(
                lambda _c=False, p=pkg["path"]: self.action.emit(p))
            lay.addWidget(btn)


class InstallPanel(QFrame):
    """「安装插件」弹出的包库面板：已安装 / 可安装 分组。

    对应本地 AI 插件里「已下载 / 可选下载」那套分组逻辑：装过的仍列出来
    （显示当前版本，可覆盖更新），没装过的给主色「安装」按钮。
    """

    WIDTH = 384
    MAX_H = 432

    def __init__(self, dlg: "PluginManagerWindow") -> None:
        super().__init__(dlg)
        self.dlg = dlg
        self.setObjectName("installPanel")
        # 子控件要靠这个属性才会用 QSS 画自身背景
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(self.WIDTH)
        self._anchor: QWidget | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(13, 12, 13, 10)
        root.setSpacing(6)

        head = QLabel("安装插件")
        head.setObjectName("kTitle")
        root.addWidget(head)
        self.sub = QLabel("")
        self.sub.setObjectName("kSub")
        root.addWidget(self.sub)
        root.addSpacing(2)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(self.MAX_H)
        scroll.setStyleSheet("QScrollArea{border:none; background:transparent;}")
        root.addWidget(scroll)
        holder = QWidget()
        holder.setStyleSheet("background:transparent;")
        self.items = QVBoxLayout(holder)
        self.items.setContentsMargins(0, 0, 0, 0)
        self.items.setSpacing(2)
        self.items.addStretch(1)
        scroll.setWidget(holder)
        self._scroll = scroll

        self.browse = QPushButton("浏览其他插件包…")
        self.browse.setObjectName("browseBtn")
        self.browse.setCursor(Qt.CursorShape.PointingHandCursor)
        self.browse.clicked.connect(self._on_browse)
        root.addWidget(self.browse)

        # 浮在插件列表上方，给一层柔和投影才读得出是「浮层」
        try:
            sh = QGraphicsDropShadowEffect(self)
            sh.setBlurRadius(22)
            sh.setOffset(0, 6)
            sh.setColor(QColor(28, 34, 48, 52))
            self.setGraphicsEffect(sh)
        except Exception:
            pass
        self.hide()

    # ---------------------------------------------------------------- 填充
    def _clear(self) -> None:
        while self.items.count() > 1:
            item = self.items.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _fill(self) -> None:
        self._clear()
        pkgs = self.dlg._mgr.list_packages()
        have = [p for p in pkgs if p.get("installed")]
        miss = [p for p in pkgs if not p.get("installed")]
        self.sub.setText(f"本地包库共 {len(pkgs)} 个包 · 已安装 {len(have)} 个")

        if not pkgs:
            empty = QLabel("本地还没有插件包。\n点下面的「浏览其他插件包…」选一个 .zip 安装。")
            empty.setObjectName("kEmpty")
            empty.setWordWrap(True)
            self.items.insertWidget(0, empty)
            return

        idx = 0
        for title, group in (("已安装", have), ("可安装", miss)):
            if not group:
                continue
            lab = QLabel(f"{title}（{len(group)}）")
            lab.setObjectName("kSection")
            self.items.insertWidget(idx, lab)
            idx += 1
            for pkg in group:
                row = _PkgRow(pkg, self)
                row.action.connect(self.dlg._install_zip)
                row.action.connect(lambda _p: self._fill())   # 装完就地改成「已安装」
                self.items.insertWidget(idx, row)
                idx += 1
        self.adjustSize()          # 分组数量会变（装完后包换组），高度要跟着收

    def _on_browse(self) -> None:
        self.hide()
        self.dlg._browse()

    # ---------------------------------------------------------------- 弹出
    def popup_at(self, anchor: QWidget) -> None:
        self._anchor = anchor
        self._fill()
        self.adjustSize()
        dlg = self.dlg
        pos = anchor.mapTo(dlg, anchor.rect().topLeft())
        h = self.height()
        y = pos.y() - h - 8                       # 优先弹在按钮上方（按钮在底部）
        if y < 8:
            y = pos.y() + anchor.height() + 8
        y = max(8, min(y, dlg.height() - h - 8))
        x = max(8, min(pos.x(), dlg.width() - self.width() - 8))
        self.move(x, y)
        self.show()
        self.raise_()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def hide(self) -> None:  # noqa: N802
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().hide()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """点面板外面就收起（QFrame 子控件没有 Popup 的自动关闭）。

        0.43：热键捕获分支已迁到 PluginManagerWindow —— 这里的 _on_edit_hotkey
        等方法原先被错放进了 InstallPanel，而信号连的是 PluginManagerWindow，
        导致点「修改」即 AttributeError（插件页打不开）。
        """
        if event.type() == QEvent.Type.MouseButtonPress and self.isVisible():
            w = obj if isinstance(obj, QWidget) else None
            if w is not None and w is not self and not self.isAncestorOf(w):
                # 点的是触发按钮：交给它的 clicked 去 toggle，否则会「按下关、松开又开」
                a = self._anchor
                if a is not None and (w is a or a.isAncestorOf(w)):
                    return super().eventFilter(obj, event)
                self.hide()
        return super().eventFilter(obj, event)

class PluginManagerWindow(QDialog):
    """插件管理页：列表 + 安装 + 启用/禁用 + 卸载。"""

    plugins_changed = Signal()      # 增删/启停后通知主窗口刷新首页插件区块

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._mgr = PluginManager.instance()
        self.setWindowTitle("LyLaunch 插件管理")
        self.setFixedSize(_WIN_W, _WIN_H)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        # ⚠️ flags 里必须带 Qt.WindowType.Dialog：QDialog 一旦有 parent，而
        # setWindowFlags 又不含任何 Window 类标志，它就会退化成**子控件**
        # （"Attempt to set a screen on a child window" + exec() 直接卡死主循环）。
        # 设置窗口没 parent 所以看不出这个坑，这里必须显式给。
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowSystemMenuHint
        )
        self.setStyleSheet(_t.DIALOG_QSS + _QSS)
        _t.apply_light_titlebar(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)

        title = QLabel("插件管理")
        title.setObjectName("pmTitle")
        root.addWidget(title)

        self.sub = QLabel("")
        self.sub.setObjectName("pmSub")
        root.addWidget(self.sub)

        # 列表
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{border:none; background:transparent;}")
        root.addWidget(scroll, stretch=1)
        holder = QWidget()
        holder.setStyleSheet("background:transparent;")
        self.rows = QVBoxLayout(holder)
        self.rows.setContentsMargins(0, 0, 6, 0)
        self.rows.setSpacing(8)
        self.rows.addStretch(1)
        scroll.setWidget(holder)
        self._scroll = scroll

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        self.hint = QLabel("")
        self.hint.setObjectName("pmHint")
        self.hint.setWordWrap(True)
        bottom.addWidget(self.hint, stretch=1)

        self.install_btn = QPushButton("安装插件")
        self.install_btn.setObjectName("pInstall")
        self.install_btn.setFixedHeight(32)
        self.install_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.install_btn.clicked.connect(self._toggle_install_panel)
        bottom.addWidget(self.install_btn)

        close_btn = QPushButton("关闭")
        close_btn.setObjectName("pClose")
        close_btn.setFixedHeight(32)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)
        root.addLayout(bottom)

        self._rebuild()
        self._place_over_parent()

    def _place_over_parent(self) -> None:
        """压在设置窗口上方（居中）；没有可见父窗口就居中屏幕。"""
        p = self.parentWidget()
        try:
            if p is not None and p.isVisible():
                g = p.frameGeometry()
                self.move(g.x() + (g.width() - self.width()) // 2,
                          g.y() + (g.height() - self.height()) // 2)
                return
            scr = self.screen().availableGeometry()
            self.move(scr.x() + (scr.width() - self.width()) // 2,
                      scr.y() + (scr.height() - self.height()) // 2)
        except Exception:
            pass

    # ---------------------------------------------------------------- 列表
    def _on_edit_hotkey(self, card) -> None:
        """进入捕获模式：下一次按键组合成为该插件的新快捷键。"""
        if getattr(self, "_hk_capturing", False):
            self._stop_hk_capture()
        self._hk_card = card
        self._hk_capturing = True
        card.hotkey_btn.setText("按下新组合键…（Esc 取消）")
        QApplication.instance().installEventFilter(self)

    def _stop_hk_capture(self) -> None:
        self._hk_capturing = False
        QApplication.instance().removeEventFilter(self)
        card = getattr(self, "_hk_card", None)
        if card is not None:
            try:
                card.hotkey_btn.setText("修改")
            except RuntimeError:
                pass

    def _persist_hotkey(self, card, spec: str) -> None:
        """写回 plugin.json + 更新内存 manifest + 让主窗口重注册热键。"""
        info = card.info
        path = os.path.join(info.directory, "plugin.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data["Hotkey"] = spec
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            self._set_hint(f"保存快捷键失败：{exc}", True)
            return
        info.manifest.hotkey = spec          # 内存里的 manifest 同步更新
        try:
            card.hotkey_label.setText(f"全局快捷键：{spec}")
        except RuntimeError:
            pass
        main = self._main_window()
        refresh = getattr(main, "refresh_plugin_hotkeys", None)
        if callable(refresh):
            refresh()
            self._set_hint(f"「{info.manifest.name}」快捷键已改为 {spec}，立即生效")
        else:
            self._set_hint(f"已保存为 {spec}（重启软件后生效）")

    def _main_window(self):
        for widget in QApplication.topLevelWidgets():
            if type(widget).__name__ == "MainWindow":
                return widget
        return None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        event.accept()      # 别冒泡到窗口，否则被当成「点了面板外面」


    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """捕获插件快捷键：下一次按键组合成为新快捷键（Esc 取消）。"""
        if getattr(self, "_hk_capturing", False) and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self._stop_hk_capture()
                return True
            if key in (Qt.Key.Key_Control, Qt.Key.Key_Shift,
                       Qt.Key.Key_Alt, Qt.Key.Key_Meta):
                return True
            spec = format_hotkey(_qt_mods_to_win(event.modifiers()),
                                 vk_from_qt_key(key))
            if parse_hotkey(spec) is None:
                return True
            card = getattr(self, "_hk_card", None)
            self._stop_hk_capture()
            if card is not None:
                self._persist_hotkey(card, spec)
            return True
        return super().eventFilter(obj, event)

    def _rebuild(self) -> None:
        """重建所有行（状态变化就整体重排，别做增量同步，容易漏）。"""
        while self.rows.count() > 1:            # 保留末尾的 stretch
            item = self.rows.takeAt(0)
            w = item.widget()
            if w is not None:
                # 只 deleteLater 不够：它要等下一轮事件循环才真删，这期间旧控件
                # 还画在原位置，和新行叠成一团（实测文字互相穿透）。
                # 先 hide + 脱离父级，画面立刻干净。
                w.hide()
                w.setParent(None)
                w.deleteLater()
        plugins = self._mgr.all_plugins()
        for info in plugins:
            row = _PluginRow(info, self)
            row.toggled.connect(self._on_toggle)
            row.uninstall.connect(self._on_uninstall)
            row.hotkey_edit.connect(self._on_edit_hotkey)
            self.rows.insertWidget(self.rows.count() - 1, row)
        if not plugins:
            empty = QLabel("（暂无插件）")
            empty.setObjectName("pmEmpty")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.rows.insertWidget(0, empty)
        n_on = sum(1 for p in plugins if p.enabled)
        self.sub.setText(f"共 {len(plugins)} 个插件 · 已启用 {n_on} 个 · "
                         f"内置插件只能禁用，不能卸载")

    def _set_hint(self, text: str, error: bool = False) -> None:
        self.hint.setText(text)
        self.hint.setStyleSheet(
            f"font-size:11px; color:{_t.DANGER if error else _t.TEXT_SECONDARY};")

    # ---------------------------------------------------------------- 操作
    def _on_toggle(self, pid: str, on: bool) -> None:
        info = self._mgr.get(pid)
        if info is None:
            return
        name = info.manifest.name
        try:
            if on:
                self._mgr.enable(pid)
                self._mgr.load_all()
                self._set_hint(f"已启用「{name}」")
            else:
                self._mgr.disable(pid)
                self._set_hint(f"已禁用「{name}」")
        except Exception as e:  # noqa: BLE001
            self._set_hint(f"操作失败：{e}", error=True)
        self.plugins_changed.emit()
        self._keep_scroll(self._rebuild)

    def _toggle_install_panel(self) -> None:
        """「安装插件」= 弹出包库面板（已安装 / 可安装 分组），不再是一串下拉菜单。"""
        panel = getattr(self, "_pkg_panel", None)
        if panel is None:
            panel = InstallPanel(self)
            self._pkg_panel = panel
        if panel.isVisible():
            panel.hide()
        else:
            panel.popup_at(self.install_btn)

    def _browse(self) -> None:
        path, _f = QFileDialog.getOpenFileName(
            self, "选择插件包（.zip）", "", "插件包 (*.zip);;所有文件 (*.*)")
        if path:
            self._install_zip(path)

    def _install_zip(self, path: str) -> None:
        ok, msg = self._mgr.install_from_zip(path)
        if not ok:
            self._set_hint(f"安装失败：{msg}", error=True)
            return
        info = self._mgr.get(msg)
        name = info.manifest.name if info else msg
        self._set_hint(f"已安装「{name}」")
        self.plugins_changed.emit()
        self._keep_scroll(self._rebuild)

    def _on_uninstall(self, pid: str) -> None:
        info = self._mgr.get(pid)
        if info is None:
            return
        if info.source == "local":
            self._set_hint("内置插件不能卸载，只能禁用")
            return
        if not _t.ask_confirm(
                self, "卸载插件",
                f"确定卸载插件「{info.manifest.name}」吗？\n{info.directory}",
                yes_text="卸载", no_text="取消"):
            return
        # 插件可能带几百 MB 模型（清单里的 Data 声明）→ 单独问一次，默认保留
        purge = False
        size = self._mgr.plugin_data_size(pid)
        if size > 0:
            purge = _t.ask_confirm(
                self, "同时删除模型？",
                f"这个插件还占用了 {size / 1048576:.0f} MB 的模型文件。\n\n"
                "删除后可释放空间，但重新安装时要重新下载。\n\n要一起删除吗？",
                default_yes=False, yes_text="一起删除", no_text="保留")
        ok = self._mgr.uninstall(pid, purge_data=purge)
        if ok:
            self._set_hint(
                f"已卸载（含清理 {size / 1048576:.0f} MB 模型）" if purge and size
                else "已卸载（模型保留，重装可直接用）" if size else "已卸载")
        else:
            self._set_hint("卸载失败，请检查目录权限", error=True)
        self.plugins_changed.emit()
        self._keep_scroll(self._rebuild)

    def _keep_scroll(self, fn) -> None:
        """重建列表时保持滚动位置，别让用户每次都跳回顶部。"""
        bar = self._scroll.verticalScrollBar()
        pos = bar.value()
        fn()
        bar.setValue(min(pos, bar.maximum()))
