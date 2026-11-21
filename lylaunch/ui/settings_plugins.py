"""设置页「插件」的完整管理区。

原先插件列表在一个独立的模态小窗口里（`plugin_manager_window.py`，1.2.0 已删除）：
一屏一卡、大圆角描边，和设置页「分组 + 行式白卡」是两套视觉语言，来回跳也很费事。
这里把它并进设置「插件」页：一行一个插件（图标 + 名称/内置/版本 + 说明 +
右侧「卸载」+ 开关），包库与目录各成一组。

插件快捷键**不在这页改**：改键入口在 设置 → 快捷键（见 settings_hotkeys.py），
这里只保留一行只读的「快捷键 Alt+D」。

两个必须记住的坑：
1. `SwitchButton.setChecked` 必须在 `connect` **之前** —— 否则建列表的那一瞬间
   就会触发 toggled，把插件全量重载一遍。
2. 行是动态重建的：清场时先 `hide()` + `setParent(None)` 再 `deleteLater()`。
   只 deleteLater 要等下一轮事件循环才真删，这期间旧控件还画在原位，和新行叠成一团。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout, QWidget,
)

from .. import paths, reset
from ..plugins import theme as _t
from ..plugins.manager import PluginManager
from ..plugins.plugin_icons import PLUGIN_ICON_BG, plugin_icon_svg, render_plugin_svg
from .settings_common import ElidedLabel, open_dir, size_text
from .settings_window import _LINE, _PRIMARY_QSS
from .switch import SwitchButton

_BTN_SMALL = (f"QPushButton{{background:#FFFFFF; border:1px solid {_t.BORDER_STRONG};"
              f" border-radius:6px; font-size:11px; color:{_t.TEXT_PRIMARY};"
              f" padding:2px 11px;}}"
              f" QPushButton:hover{{background:{_t.BG_SUBTLE};"
              f" border-color:{_t.ACCENT}; color:{_t.ACCENT};}}"
              f" QPushButton:disabled{{color:{_t.TEXT_PLACEHOLDER}; border-color:#E8EDF6;"
              f" background:#F7F9FC;}}")
_BTN_SMALL_PRIMARY = (f"QPushButton{{background:{_t.ACCENT}; color:#FFFFFF; border:none;"
                      f" border-radius:6px; font-size:11px; padding:2px 12px;"
                      f" font-weight:600;}}"
                      f" QPushButton:hover{{background:{_t.ACCENT_HOVER};}}")
_BADGE_LOCAL = (f"font-size:9.5px; background:{_t.SOFT_BG}; color:{_t.SOFT_FG};"
                f" border-radius:4px; padding:1px 5px;")
_BADGE_USER = (f"font-size:9.5px; background:{_t.BG_SUBTLE}; color:{_t.TEXT_SECONDARY};"
               f" border:1px solid {_t.BORDER}; border-radius:4px; padding:1px 5px;")
_BADGE_ON = (f"font-size:9.5px; background:{_t.SOFT_BG}; color:{_t.SOFT_FG};"
             f" border-radius:4px; padding:2px 6px;")
_BADGE_OLD = (f"font-size:9.5px; background:{_t.BG_SUBTLE}; color:{_t.TEXT_SECONDARY};"
              f" border:1px dashed {_t.BORDER_STRONG}; border-radius:4px; padding:1px 5px;")


class _PluginsSection:
    """「已安装」「插件包库」「目录」三组，由 build_plugins_page() 装进分页。"""

    def __init__(self, host, lay: QVBoxLayout) -> None:
        self.host = host
        self._mgr = PluginManager.instance()
        self._list_rows: list[QWidget] = []
        self._pkg_rows: list[QWidget] = []
        self._build(lay)
        self.refresh()

    # ---------------------------------------------------------------- 装配
    def _build(self, lay: QVBoxLayout) -> None:
        # ── 已安装：汇总行 + 动态插件行 ──
        cv = self.host._group(lay, "已安装")
        install_btn = QPushButton("安装插件")
        install_btn.setFixedSize(84, 30)
        install_btn.setStyleSheet(_PRIMARY_QSS)
        install_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        install_btn.clicked.connect(lambda _=False: self._browse())
        col = self.host._row(cv, "插件列表",
                             "启用 / 禁用即时生效；内置插件只能禁用，不能卸载",
                             install_btn, wide_hint=True)
        self.summary_hint = self.host._hint_label(col, wide=True)
        self.feedback = self.host._hint_label(col, wide=True)

        holder = QWidget()
        holder.setStyleSheet("background:transparent;")
        self._list_box = QVBoxLayout(holder)
        self._list_box.setContentsMargins(0, 0, 0, 4)
        self._list_box.setSpacing(0)
        cv.addWidget(holder)

        # ── 插件包库：本地 .zip 一眼看完，能装的直接装 ──
        cv2 = self.host._group(lay, "插件包库")
        col = self.host._row(cv2, "本地包库",
                             "卸载后 .zip 仍留在这里，随时能装回来",
                             self.host._link_button("打开包库目录", self._open_pkg_dir),
                             wide_hint=True)
        self.pkg_hint = self.host._hint_label(col, wide=True)

        holder2 = QWidget()
        holder2.setStyleSheet("background:transparent;")
        self._pkg_box = QVBoxLayout(holder2)
        self._pkg_box.setContentsMargins(0, 0, 0, 0)
        self._pkg_box.setSpacing(0)
        cv2.addWidget(holder2)

        self.host._row(cv2, "从文件安装", "装一个不在包库里的 .zip 包",
                       self.host._ghost_button("选择文件…", self._browse),
                       divider=True, wide_hint=False)

        # ── 目录：手拷进来的插件从哪读 ──
        cv3 = self.host._group(lay, "目录")
        col = self.host._row(cv3, "插件目录", "每个插件一个子目录（含 plugin.json）",
                             self.host._link_button("打开目录", self._open_plugins_dir),
                             wide_hint=True)
        self.dir_hint = self.host._hint_label(
            col, f"{paths.USER_PLUGINS_DIR}", wide=True)

    # ---------------------------------------------------------------- 刷新
    def refresh(self, keep_feedback: bool = False) -> None:
        if not keep_feedback:
            self.feedback.setText("")      # 别把上次的「已卸载…」带到这次打开
        try:
            plugins = self._mgr.all_plugins()
        except Exception:
            plugins = []
        self._refresh_summary(plugins)
        self._rebuild_plugins(plugins)
        self._rebuild_packages()

    def _refresh_summary(self, plugins) -> None:
        on = sum(1 for p in plugins if p.enabled)
        bad = [p for p in plugins if p.error]
        size = 0
        for d in (paths.USER_PLUGINS_DIR, paths.BUILTIN_PLUGINS_DIR):
            try:
                size += reset.size_of(d)
            except Exception:
                pass
        text = (f"共 {len(plugins)} 个插件 · 已启用 {on} 个 · 占用 {size_text(size)}"
                if plugins else "还没有安装插件；点右上「安装插件」导入一个 .zip")
        if bad:
            text += f"\n有 {len(bad)} 个插件加载失败，行内标出了原因"
        self.summary_hint.setText(text)
        self.summary_hint.set_color(_t.WARNING if bad else None)

    def _flash(self, text: str, error: bool = False) -> None:
        self.feedback.setText(text)
        self.feedback.set_color(_t.DANGER if error else _t.TEXT_SECONDARY)

    def _clear(self, box: QVBoxLayout, store: list[QWidget]) -> None:
        while box.count():
            item = box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()                 # 先离场，避免和新建的行叠一帧
                w.setParent(None)
                w.deleteLater()
        store.clear()

    def _divider(self, box: QVBoxLayout) -> None:
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{_LINE};")
        box.addWidget(line)

    # ---------------------------------------------------------------- 插件行
    def _rebuild_plugins(self, plugins) -> None:
        self._clear(self._list_box, self._list_rows)
        for i, info in enumerate(plugins):
            if i:
                self._divider(self._list_box)
            row = self._plugin_row(info)
            self._list_box.addWidget(row)
            self._list_rows.append(row)
        if not plugins:
            empty = QLabel("（暂无插件）")
            empty.setStyleSheet(f"font-size:12px; color:{_t.TEXT_SECONDARY};")
            empty.setContentsMargins(2, 12, 2, 12)
            self._list_box.addWidget(empty)
            self._list_rows.append(empty)

    def _plugin_row(self, info) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(2, 9, 2, 9)
        h.setSpacing(12)

        # 图标块：和首页插件瓦片同一套线性图标，只是缩到 34px
        icon = QLabel()
        icon.setFixedSize(34, 34)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(f"border-radius:9px; background:{PLUGIN_ICON_BG};")
        try:
            dpr = self.host.devicePixelRatioF() or 1.0
            icon.setPixmap(render_plugin_svg(plugin_icon_svg(info.id), 18, dpr))
        except Exception:
            pass
        h.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)

        col = QVBoxLayout()
        col.setSpacing(3)
        col.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        top.setSpacing(6)
        name = QLabel(info.manifest.name)
        name.setStyleSheet(f"font-size:13px; font-weight:600; color:{_t.TEXT_PRIMARY};")
        top.addWidget(name)
        builtin = info.source == "local"
        badge = QLabel("内置" if builtin else "用户")
        badge.setStyleSheet(_BADGE_LOCAL if builtin else _BADGE_USER)
        top.addWidget(badge)
        ver = QLabel(f"v{info.manifest.version}")
        ver.setStyleSheet(f"font-size:11px; color:#7C8391;")
        top.addWidget(ver)
        top.addStretch(1)
        col.addLayout(top)

        desc = ElidedLabel(info.manifest.description or "")
        col.addWidget(desc)

        hk_label = None
        spec = (info.manifest.hotkey or "").strip()
        if spec:
            # 只读展示：改键入口在 设置 → 快捷键（settings_hotkeys.py）
            hk_label = QLabel(f"快捷键 {spec} · 在「快捷键」页修改")
            hk_label.setStyleSheet(f"font-size:11px; color:#7C8391;")
            col.addWidget(hk_label)

        if info.error:
            err = QLabel(f"加载失败：{info.error}")
            err.setStyleSheet(f"font-size:11px; color:{_t.DANGER};")
            err.setWordWrap(True)
            col.addWidget(err)

        h.addLayout(col, 1)

        # 右侧一行排开：卸载 / 开关。原来「开关在上、按钮在下」竖着堆，
        # 单行被撑到 90px 高，640 宽的内容区里一屏只看得下 5 个插件。
        right = QWidget()
        rh = QHBoxLayout(right)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.setSpacing(8)
        if not builtin:
            # 内置插件不给卸载按钮：9 行全是禁用态灰按钮反而更吵，
            # 「只能禁用」这句话已经写在上方的说明里
            del_btn = QPushButton("卸载")
            del_btn.setStyleSheet(_BTN_SMALL)
            del_btn.setFixedHeight(22)
            del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            del_btn.clicked.connect(lambda _=False, pid=info.id: self._on_uninstall(pid))
            rh.addWidget(del_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        switch = SwitchButton()
        switch.setChecked(bool(info.enabled))          # 必须先 setChecked 再 connect
        switch.toggled.connect(
            lambda on, pid=info.id: self._on_toggle(pid, bool(on)))
        rh.addWidget(switch, 0, Qt.AlignmentFlag.AlignVCenter)

        h.addWidget(right, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    # ---------------------------------------------------------------- 包库行
    def _rebuild_packages(self) -> None:
        self._clear(self._pkg_box, self._pkg_rows)
        try:
            pkgs = self._mgr.list_packages()
        except Exception:
            pkgs = []
        have = sum(1 for p in pkgs if p.get("installed"))
        self.pkg_hint.setText(
            f"包库 {len(pkgs)} 个包 · 已安装 {have} 个" if pkgs
            else "包库还是空的；安装插件时会自动在这里存一份 .zip")
        for i, pkg in enumerate(pkgs):
            if i:
                self._divider(self._pkg_box)
            row = self._pkg_row(pkg)
            self._pkg_box.addWidget(row)
            self._pkg_rows.append(row)

    def _pkg_row(self, pkg: dict) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(2, 8, 2, 8)
        h.setSpacing(8)

        name = QLabel(pkg.get("name") or pkg.get("id") or "（未命名）")
        name.setStyleSheet(f"font-size:12.5px; color:{_t.TEXT_PRIMARY};")
        h.addWidget(name, 0, Qt.AlignmentFlag.AlignVCenter)
        ver = QLabel(f"v{pkg.get('version') or '—'}")
        ver.setStyleSheet(f"font-size:10.5px; color:#7C8391;")
        h.addWidget(ver, 0, Qt.AlignmentFlag.AlignVCenter)
        h.addStretch(1)

        installed = bool(pkg.get("installed"))
        cur = str(pkg.get("installed_version") or "").strip()
        new = str(pkg.get("version") or "").strip()
        if installed and (not new or cur == new):
            badge = QLabel("已安装")
            badge.setStyleSheet(_BADGE_ON)
            h.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
            return row
        if installed:
            # 装过但版本不同：给「更新」而不是「安装」，一眼看出能升级
            badge = QLabel(f"已装 {cur or '?'}")
            badge.setStyleSheet(_BADGE_OLD)
            h.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
            text, qss = "更新", _BTN_SMALL
        else:
            text, qss = "安装", _BTN_SMALL_PRIMARY
        btn = QPushButton(text)
        btn.setStyleSheet(qss)
        btn.setFixedHeight(24)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda _=False, p=pkg.get("path", ""): self._install_zip(p))
        h.addWidget(btn, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

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
            else:
                self._mgr.disable(pid)
            self._flash(f"已启用「{name}」，首页插件区块已刷新" if on
                        else f"已禁用「{name}」，首页不再显示")
        except Exception as exc:  # noqa: BLE001
            self._flash(f"操作失败：{exc}", error=True)
            self._rebuild_plugins(self._mgr.all_plugins())
        # 只改状态不重排列表：重建会让用户刚点的开关当场消失
        self._refresh_summary(self._mgr.all_plugins())
        self.host.plugins_changed.emit()

    def _browse(self) -> None:
        path, _f = QFileDialog.getOpenFileName(
            self.host, "选择插件包（.zip）", "", "插件包 (*.zip);;所有文件 (*.*)")
        if path:
            self._install_zip(path)

    def _install_zip(self, path: str) -> None:
        if not path:
            return
        ok, msg = self._mgr.install_from_zip(path)
        if not ok:
            self._flash(f"安装失败：{msg}", error=True)
            return
        info = self._mgr.get(msg)
        self._flash(f"已安装「{info.manifest.name if info else msg}」")
        self.host.plugins_changed.emit()
        self.refresh(keep_feedback=True)

    def _on_uninstall(self, pid: str) -> None:
        info = self._mgr.get(pid)
        if info is None:
            return
        if info.source == "local":
            self._flash("内置插件不能卸载，只能禁用", error=True)
            return
        if not _t.ask_confirm(
                self.host, "卸载插件",
                f"确定卸载插件「{info.manifest.name}」吗？\n{info.directory}",
                yes_text="卸载", no_text="取消"):
            return
        # 插件可能带几百 MB 模型（清单里的 Data 声明）→ 单独问一次，默认保留
        purge = False
        size = self._mgr.plugin_data_size(pid)
        if size > 0:
            purge = _t.ask_confirm(
                self.host, "同时删除模型？",
                f"这个插件还占用了 {size / 1048576:.0f} MB 的模型文件。\n\n"
                "删除后可释放空间，但重新安装时要重新下载。\n\n要一起删除吗？",
                default_yes=False, yes_text="一起删除", no_text="保留")
        ok = self._mgr.uninstall(pid, purge_data=purge)
        if ok:
            self._flash(
                f"已卸载（含清理 {size / 1048576:.0f} MB 模型）" if purge and size
                else "已卸载（模型保留，重装可直接用）" if size else "已卸载")
        else:
            self._flash("卸载失败，请检查目录权限", error=True)
        self.host.plugins_changed.emit()
        self.refresh(keep_feedback=True)

    # ---------------------------------------------------------------- 目录
    def _open_plugins_dir(self) -> None:
        try:
            paths.USER_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if open_dir(paths.USER_PLUGINS_DIR):
            self.dir_hint.setText(f"已在资源管理器打开：{paths.USER_PLUGINS_DIR}")
            self.dir_hint.set_color(None)
        else:
            self.dir_hint.setText(f"打不开插件目录：{paths.USER_PLUGINS_DIR}")
            self.dir_hint.set_color(_t.DANGER)

    def _open_pkg_dir(self) -> None:
        try:
            paths.PLUGIN_PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if open_dir(paths.PLUGIN_PACKAGES_DIR):
            self.pkg_hint.setText(f"已在资源管理器打开：{paths.PLUGIN_PACKAGES_DIR}")
            self.pkg_hint.set_color(None)
        else:
            self.pkg_hint.setText(f"打不开包库目录：{paths.PLUGIN_PACKAGES_DIR}")
            self.pkg_hint.set_color(_t.DANGER)


def build_plugins_page(host, lay: QVBoxLayout) -> _PluginsSection:
    """设置「插件」页装配入口（settings_window 里只需一行调用）。"""
    host.plugins_section = _PluginsSection(host, lay)
    return host.plugins_section
