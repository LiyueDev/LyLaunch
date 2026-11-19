"""剪贴板历史插件（LyLaunch 内置，0.43）。

做什么
------
后台定时读剪贴板文本，变化就记一条（相同内容自动提到最前，不重复堆叠），
上限默认 100 条，持久化到插件配置（%LocalAppData%/LyLaunch/plugins_clipboard_history.json），
重启 LyLaunch 也还在。

窗口
----
点插件瓦片 → 非模态窗口：搜索框 + 历史列表 + 暂停/清空。
- 双击 / 回车 / 行内「复制」按钮 → 把该条写回剪贴板
- 行内「删除」/ 右键菜单 → 删单条
- 「暂停记录」→ 临时不记录（例如复制密码时）
- 「清空」→ 确认后清空全部

设计取舍
--------
- 只记**文本**，不碰图片/文件（Windows 剪贴板格式太杂，简单版先不做）。
- 轮询而不是监听 WM_CLIPBOARDUPDATE：主程序已经有一套 700ms 轮询的
  ClipboardWatcher，这里保持一致，避免插件与主程序各挂一套原生监听。
- 写盘节流 1.2s：复制频繁时不至于每 700ms 写一次 JSON。
- 禁止 @dataclass（插件动态加载不注册 sys.modules，Python 3.14 会加载失败）。
"""

from __future__ import annotations

import time

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from liytools.plugins import theme
from liytools.plugins.base import PluginBase
from liytools.plugins.plugin_icons import plugin_icon_svg, render_plugin_svg

# 非模态单例（第十章红线：禁止 dlg.exec()，重复打开只提到前台）
_DLG = None


# ---------------------------------------------------------------- 工具函数
def _preview(text: str, limit: int = 110) -> str:
    """压成一行、超长省略 —— 列表里一条只占一行，好扫视。"""
    one = " ".join((text or "").split())
    return one if len(one) <= limit else one[:limit] + "…"


def _ago(ts: float) -> str:
    delta = max(0.0, time.time() - (ts or 0))
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


# ---------------------------------------------------------------- 数据层
class ClipboardStore(QObject):
    """剪贴板历史：轮询、去重、限长、持久化。

    条目格式 {"t": 文本, "ts": 时间戳}；history[0] 是最新一条。
    """

    changed = Signal()      # 历史有变化（新增/删除/清空）→ 窗口刷新

    def __init__(self, api, settings: dict) -> None:
        super().__init__()
        self._api = api
        self._settings = settings
        self.history: list[dict] = []
        self._last: str | None = None
        self._just_set = False          # 自己写入剪贴板的那次变化要忽略
        self.max_items = max(10, int(settings.get("max_items", 100) or 100))
        self._paused = bool(settings.get("paused", False))
        self._load()

        # 写盘节流：复制很频繁时别每 700ms 写一次 JSON
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(1200)
        self._save_timer.timeout.connect(self._flush)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(max(200, int(settings.get("poll_ms", 700) or 700)))

    # ---- 持久化
    def _load(self) -> None:
        raw = []
        try:
            raw = self._api.settings_get("history", []) or []
        except Exception:
            raw = []
        out: list[dict] = []
        for entry in raw:
            if isinstance(entry, dict) and entry.get("t"):
                out.append({"t": str(entry["t"]), "ts": float(entry.get("ts") or 0)})
            elif isinstance(entry, str) and entry:      # 兼容最初只有字符串的格式
                out.append({"t": entry, "ts": 0.0})
        self.history = out[: self.max_items]
        self._last = self.history[0]["t"] if self.history else None

    def _mark_dirty(self) -> None:
        self._save_timer.start()

    def _flush(self) -> None:
        try:
            self._api.settings_set("history", self.history)
        except Exception:
            pass

    def flush_now(self) -> None:
        self._save_timer.stop()
        self._flush()

    # ---- 采集
    def _tick(self) -> None:
        if self._paused:
            return
        try:
            text = QApplication.clipboard().text()
        except Exception:
            return
        if not text or not text.strip():
            return
        if self._just_set:            # 这次变化是「点历史项回写」造成的
            self._just_set = False
            self._last = text
            return
        if text == self._last:
            return
        self._last = text
        self.add(text)

    def add(self, text: str) -> None:
        text = (text or "").strip("\x00")
        if not text.strip():
            return
        # 已存在同样内容 → 提到最前并刷新时间，不重复堆叠
        for i, entry in enumerate(self.history):
            if entry["t"] == text:
                if i != 0:
                    self.history.pop(i)
                    self.history.insert(0, entry)
                entry["ts"] = time.time()
                self._mark_dirty()
                self.changed.emit()
                return
        self.history.insert(0, {"t": text, "ts": time.time()})
        while len(self.history) > self.max_items:
            self.history.pop()
        self._mark_dirty()
        self.changed.emit()

    # ---- 操作
    def write(self, text: str) -> bool:
        """把某条写回剪贴板；标记本次变化为自己写入，不再记一条。"""
        try:
            self._api.clipboard_write(text)
        except Exception:
            self._just_set = False
            return False
        self._just_set = True
        self._last = text
        return True

    def remove(self, entry: dict) -> None:
        try:
            self.history.remove(entry)
        except ValueError:
            pass
        self._mark_dirty()
        self.changed.emit()

    def clear(self) -> None:
        self.history.clear()
        self._last = None
        self._mark_dirty()
        self.changed.emit()

    # ---- 暂停
    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        self._settings["paused"] = self._paused
        try:
            self._api.settings_set("paused", self._paused)
        except Exception:
            pass


# ---------------------------------------------------------------- 列表行
class _HistoryRow(QWidget):
    def __init__(self, entry: dict, on_copy, on_delete) -> None:
        super().__init__()
        self.entry = entry
        self.setObjectName("clipRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "#clipRow{background:transparent;border-radius:8px;}"
            "#clipRow:hover{background:#E4EDFC;}")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 8, 6)
        lay.setSpacing(8)

        col = QVBoxLayout()
        col.setSpacing(1)
        text = entry.get("t", "")
        self.title = QLabel(_preview(text))
        self.title.setStyleSheet(
            "font-size:13px;color:#161C28;background:transparent;")
        self.title.setToolTip(text[:600])
        meta = QLabel(f"{_ago(entry.get('ts', 0))} · {len(text)} 字")
        meta.setStyleSheet("font-size:11px;color:#5F6570;background:transparent;")
        col.addWidget(self.title)
        col.addWidget(meta)
        lay.addLayout(col, 1)

        for label, slot in (("复制", lambda: on_copy(text)),
                            ("删除", lambda: on_delete(entry))):
            btn = QPushButton(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(26)
            btn.setStyleSheet(
                "QPushButton{border:1px solid #D8E3F5;border-radius:7px;"
                "background:#FFFFFF;color:#3D4657;font-size:11px;padding:0 10px;}"
                "QPushButton:hover{background:#E4EDFC;border-color:#BFD3FA;"
                "color:#161C28;}")
            btn.clicked.connect(slot)
            lay.addWidget(btn)


# ---------------------------------------------------------------- 窗口
class ClipboardDialog(QDialog):
    def __init__(self, store: ClipboardStore, api, parent=None) -> None:
        super().__init__(parent)
        self._store = store
        self._api = api
        self._rows: list[_HistoryRow] = []
        self.resize(700, 580)
        self.setMinimumSize(520, 400)
        self.setWindowTitle("剪贴板历史")

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 14)
        root.setSpacing(12)

        # 必须先 setStyleSheet 再 install_chrome：install_chrome 会往现有样式串
        # 追加外壳 QSS，反过来（后 setStyleSheet）会把外壳规则整个覆盖掉。
        self.setStyleSheet(theme.DIALOG_QSS)
        from liytools.plugins.window_chrome import install_chrome
        install_chrome(self, "剪贴板历史", show_minmax=True, resizable=True)

        self._build_header(root)
        self._build_list(root)
        self._build_footer(root)

        store.changed.connect(self.reload)
        self.reload()

    # ---- UI
    def _build_header(self, root: QVBoxLayout) -> None:
        head = QHBoxLayout()
        head.setSpacing(12)
        icon = QLabel()
        icon.setFixedSize(40, 40)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        icon.setStyleSheet(f"background:{theme.PLUGIN_BG}; border-radius:10px;")
        icon.setPixmap(render_plugin_svg(plugin_icon_svg("clipboard"), 24))
        head.addWidget(icon)
        title = QLabel("剪贴板历史")
        title.setProperty("role", "title")
        head.addWidget(title)
        head.addStretch(1)

        self.pause_btn = QPushButton()
        self.pause_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pause_btn.setToolTip("暂停后不再记录新复制的内容（复制密码时用）")
        self.pause_btn.setStyleSheet(
            "QPushButton{border:1px solid #C3D3EE;border-radius:16px;"
            "background:#FFFFFF;color:#161C28;padding:6px 16px;font-size:12px;}"
            "QPushButton:hover{background:#E4EDFC;border-color:#BFD3FA;}")
        self.pause_btn.clicked.connect(self._toggle_pause)
        head.addWidget(self.pause_btn)

        self.clear_btn = QPushButton("清空")
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.setToolTip("清空全部历史记录")
        self.clear_btn.setStyleSheet(
            "QPushButton{border:1px solid #C3D3EE;border-radius:16px;"
            "background:#FFFFFF;color:#161C28;padding:6px 16px;font-size:12px;}"
            "QPushButton:hover{background:#FDECEA;border-color:#F0B8B2;color:#A33;}")
        self.clear_btn.clicked.connect(self._on_clear)
        head.addWidget(self.clear_btn)
        root.addLayout(head)

        sub = QLabel("双击或回车即可重新复制；数据只存在本机，不联网")
        sub.setProperty("role", "subtitle")
        root.addWidget(sub)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索历史…（不区分大小写）")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self.reload())
        row.addWidget(self.search, 1)
        self.count = QLabel("")
        self.count.setProperty("role", "status")
        row.addWidget(self.count)
        root.addLayout(row)

        self._refresh_pause_btn()

    def _build_list(self, root: QVBoxLayout) -> None:
        self.list = QListWidget()
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setSpacing(2)
        self.list.setWordWrap(False)
        self.list.setStyleSheet(
            "QListWidget{background:transparent;border:none;}"
            "QListWidget::item{background:transparent;border:none;}"
            "QListWidget::item:selected{background:#E4EDFC;border-radius:8px;}"
            "QListWidget::item:hover{background:transparent;}")
        self.list.itemActivated.connect(self._on_activate)   # 回车 / 双击
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._show_menu)
        root.addWidget(self.list, 1)

    def _build_footer(self, root: QVBoxLayout) -> None:
        bar = QWidget()
        bar.setProperty("role", "statusBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 4, 0, 0)
        self.status = QLabel("就绪")
        self.status.setProperty("role", "status")
        lay.addWidget(self.status, 1)
        root.addWidget(bar)

    # ---- 数据刷新
    def _filtered(self) -> list[dict]:
        query = self.search.text().strip().casefold()
        items = list(self._store.history)
        if query:
            items = [e for e in items if query in (e.get("t") or "").casefold()]
        return items

    def reload(self) -> None:
        items = self._filtered()
        self.list.clear()
        self._rows.clear()
        for entry in items:
            item = QListWidgetItem(self.list)
            item.setSizeHint(QSize(0, 54))
            row = _HistoryRow(entry, self._copy_text, self._delete_entry)
            self.list.setItemWidget(item, row)
            self._rows.append(row)
        total = len(self._store.history)
        if self.search.text().strip():
            self.count.setText(f"匹配 {len(items)} / 共 {total} 条")
        else:
            self.count.setText(f"共 {total} 条")
        if not items:
            self.status.setText(
                "没有匹配的记录" if self.search.text().strip()
                else "还没有历史 · 复制点什么就会出现在这里")

    # ---- 操作
    def _copy_text(self, text: str) -> None:
        if self._store.write(text):
            self.status.setText(f"已复制到剪贴板 · {len(text)} 字")
        else:
            self.status.setText("复制失败，剪贴板可能被占用")

    def _delete_entry(self, entry: dict) -> None:
        self._store.remove(entry)
        self.status.setText("已删除这一条")

    def _on_activate(self, item: QListWidgetItem) -> None:
        entry = self._entry_of(item)
        if entry is not None:
            self._copy_text(entry.get("t", ""))

    def _entry_of(self, item: QListWidgetItem) -> dict | None:
        index = self.list.row(item)
        items = self._filtered()
        if 0 <= index < len(items):
            return items[index]
        return None

    def _show_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        entry = self._entry_of(item)
        if entry is None:
            return
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#FFFFFF;border:1px solid #D8E3F5;border-radius:8px;padding:6px;}"
            "QMenu::item{padding:7px 22px;border-radius:6px;color:#161C28;font-size:12px;}"
            "QMenu::item:selected{background:#E4EDFC;color:#161C28;}")
        act_copy = menu.addAction("复制到剪贴板")
        act_del = menu.addAction("删除这一条")
        chosen = menu.exec(self.list.mapToGlobal(pos))
        if chosen is act_copy:
            self._copy_text(entry.get("t", ""))
        elif chosen is act_del:
            self._delete_entry(entry)

    def _toggle_pause(self) -> None:
        self._store.set_paused(not self._store.paused)
        self._refresh_pause_btn()
        self.status.setText(
            "已暂停记录 · 新复制的内容不会进历史"
            if self._store.paused else "已恢复记录")

    def _refresh_pause_btn(self) -> None:
        self.pause_btn.setText("恢复记录" if self._store.paused else "暂停记录")

    def _on_clear(self) -> None:
        if not self._store.history:
            self.status.setText("历史已经是空的")
            return
        ret = QMessageBox.question(
            self, "清空剪贴板历史",
            f"确定清空全部 {len(self._store.history)} 条记录？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret == QMessageBox.StandardButton.Yes:
            self._store.clear()
            self.status.setText("已清空全部历史")


# ---------------------------------------------------------------- 打开
def _open_nonmodal(store: ClipboardStore, api) -> None:
    global _DLG
    if _DLG is not None:
        try:
            _DLG.raise_()
            _DLG.activateWindow()
            return
        except RuntimeError:
            _DLG = None
    dlg = ClipboardDialog(store, api)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    dlg.destroyed.connect(lambda *_: globals().__setitem__("_DLG", None))
    _DLG = dlg
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()


class Plugin(PluginBase):
    def on_load(self) -> None:
        # 采集必须随插件常驻：窗口没开也要记录，否则历史是断的
        self._store = ClipboardStore(self.ctx.api, self.ctx.settings)

    def on_unload(self) -> None:
        store = getattr(self, "_store", None)
        if store is not None:
            try:
                store.flush_now()      # 退出前把节流中的改动立刻落盘
            except Exception:
                pass
            store._timer.stop()

    def on_execute(self, query: str = "") -> None:
        store = getattr(self, "_store", None)
        if store is None:
            self._store = ClipboardStore(self.ctx.api, self.ctx.settings)
            store = self._store
        if query.strip():
            # 从搜索框带词进来：当搜索条件用
            _open_nonmodal(store, self.ctx.api)
            try:
                _DLG.search.setText(query.strip())
            except Exception:
                pass
            return
        _open_nonmodal(store, self.ctx.api)
