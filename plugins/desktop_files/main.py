"""桌面净化 —— 隐藏/显示桌面图标 + 搜索并打开桌面上的任意文件/文件夹。

窗口结构（非模态，严禁 exec）
------------------------------
    头部：标题 + 副标题 + 常驻「恢复显示」按钮
    开关卡片：隐藏桌面图标（滑块）+ 状态徽章
    提示条：仅在隐藏时出现（桌面图标已隐藏 · 退出会自动恢复）
    搜索框：实时过滤（120ms 防抖）
    列表：图标 + 名称 + 类型/大小/修改时间；双击/回车打开，右键定位
    底部：操作提示

五层恢复（隐藏后一定能回来）
------------------------------
    L1 closeEvent（restore_on_window_close，默认开，方便调试）
    L2 QApplication.aboutToQuit（正常退出）
    L3 on_unload（设置页禁用/卸载插件）
    L4 下次 on_load 读标记文件自动恢复（崩溃/断电）
    L5 桌面右键 → 查看 → 显示桌面图标（系统原生兜底）
    另有 CLI 兜底：python desktop_icons.py show

⚠️ 入口禁用 @dataclass（插件动态加载不注册 sys.modules，Python 3.14 会加载失败）。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
import time
from ctypes import wintypes

from PySide6.QtCore import (
    QFileInfo, QPoint, QRect, QRectF, QSize, Qt, QTimer,
)
from PySide6.QtGui import (
    QAction, QColor, QFont, QFontMetrics, QIcon, QPainter, QPen,
)
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileIconProvider, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QPushButton, QStackedWidget, QStyle, QStyledItemDelegate,
    QStyleOptionViewItem, QVBoxLayout, QWidget,
)

from lylaunch.plugins import theme
from lylaunch.plugins.base import PluginBase

# 插件目录已被 manager 插入 sys.path；单独跑本文件时补一次
try:
    import desktop_icons as di
    import fences
    import ops
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import desktop_icons as di
    import fences
    import ops


# ---------------------------------------------------------------- 常量
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4
_SKIP_NAMES = {"desktop.ini", "thumbs.db"}
_SHORTCUT_SUFFIXES = (".lnk", ".url")   # 快捷方式：不是真实文件，默认不收纳
_ROW_HEIGHT = 46
_ICON_SIZE = 24
_ICON_BATCH = 40          # 每个 tick 预热多少个图标
_POLL_MS = 1500

_FILE_ATTRIBUTE_NORMAL = 0x80
_SHGFI_TYPENAME = 0x000000400
_SHGFI_USEFILEATTRIBUTES = 0x000000010
_TYPE_CACHE: dict[str, str] = {}


class _SHFILEINFO(ctypes.Structure):
    _fields_ = [
        ("hIcon", ctypes.c_void_p),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", wintypes.DWORD),
        ("szDisplayName", wintypes.WCHAR * 260),
        ("szTypeName", wintypes.WCHAR * 80),
    ]


_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_shell32.SHGetFileInfoW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_SHFILEINFO),
    wintypes.UINT, wintypes.UINT,
]
_shell32.SHGetFileInfoW.restype = ctypes.c_void_p


# ---------------------------------------------------------------- 小工具
def _known_folder(folder_id: str) -> str:
    """取 Windows 已知文件夹（SHGetKnownFolderPath，兼容 OneDrive 重定向与中文路径）。"""
    try:
        from win32com.shell import shell, shellcon

        return shell.SHGetKnownFolderPath(getattr(shellcon, folder_id))
    except Exception:
        return ""


def desktop_dir() -> str:
    return _known_folder("FOLDERID_Desktop")


def public_desktop_dir() -> str:
    return _known_folder("FOLDERID_PublicDesktop")


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 ** 2:
        return f"{size / 1024:.0f} KB"
    if size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024 ** 3:.1f} GB"


def shell_type_name(suffix: str) -> str:
    """取 Windows 对该扩展名的类型描述（如 .pdf -> "PDF 文档"），按扩展名缓存。

    用 SHGFI_USEFILEATTRIBUTES + 假文件名，纯查注册表、不碰磁盘，开销可忽略。
    """
    key = (suffix or "").casefold()
    cached = _TYPE_CACHE.get(key)
    if cached is not None:
        return cached
    name = ""
    if key:
        try:
            info = _SHFILEINFO()
            _shell32.SHGetFileInfoW(
                "x" + key,
                _FILE_ATTRIBUTE_NORMAL,
                ctypes.byref(info),
                ctypes.sizeof(info),
                _SHGFI_TYPENAME | _SHGFI_USEFILEATTRIBUTES,
            )
            name = str(info.szTypeName or "").strip()
        except Exception:
            name = ""
    if not name:
        name = (key.lstrip(".").upper() + " 文件") if key else "文件"
    _TYPE_CACHE[key] = name
    return name


class DesktopEntry:
    """桌面上的一个条目（普通类，不用 @dataclass —— 见模块头注释）。"""

    __slots__ = ("name", "path", "is_dir", "is_lnk", "suffix", "mtime", "size",
                 "stem", "group")

    def __init__(self, name: str, path: str, is_dir: bool, is_lnk: bool,
                 suffix: str, mtime: float, size: int, group: str = "") -> None:
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.is_lnk = is_lnk
        self.suffix = suffix
        self.mtime = mtime
        self.size = size
        self.stem = os.path.splitext(name)[0]
        self.group = group        # 来自 Fences 分类夹时的分类名（如 "文档"）

    @property
    def kind(self) -> str:
        if self.is_dir:
            return "文件夹"
        if self.is_lnk:
            return "快捷方式"
        if self.suffix:
            return self.suffix.lstrip(".").upper()
        return "文件"

    @property
    def meta(self) -> str:
        parts: list[str] = []
        if self.group:
            parts.append(self.group)
        parts.append(self.kind)
        if not self.is_dir and self.size:
            parts.append(human_size(self.size))
        try:
            parts.append(time.strftime("%m-%d %H:%M", time.localtime(self.mtime)))
        except Exception:
            pass
        return " · ".join(parts)


def scan_desktop(include_public: bool = True, show_hidden: bool = False,
                 max_scan: int = 4000, exclude_shortcuts: bool = True,
                 keep_hidden=None, skip_dirs=None,
                 extra_dirs=None) -> list[DesktopEntry]:
    """扫描桌面（不递归）。用户桌面 + 公共桌面，同名以用户桌面优先。

    exclude_shortcuts=True（默认）：只收真实文件/文件夹，跳过 .lnk / .url 快捷方式。
    keep_hidden：被「桌面净化」自己设成隐藏的路径 —— 带 HIDDEN 也要列出来。
    skip_dirs：不当作条目列出、但其内容要收的目录（我们的 Fences 分类夹）。
    extra_dirs：额外收集内容的目录（分类夹），收纳后仍能在插件里搜索并打开。
    """
    roots: list[str] = [desktop_dir()]
    if include_public:
        roots.append(public_desktop_dir())

    kept = {str(p).casefold() for p in (keep_hidden or ())}
    skipped = {str(p).casefold() for p in (skip_dirs or ())}
    entries: list[DesktopEntry] = []
    seen: set[str] = set()

    def _collect(directory: str, group: str = "") -> bool:
        """扫一层；返回 False 表示已达数量上限，调用方应停止。"""
        try:
            with os.scandir(directory) as iterator:
                for item in iterator:
                    name = item.name
                    if name.casefold() in _SKIP_NAMES:
                        continue
                    if exclude_shortcuts and name.casefold().endswith(_SHORTCUT_SUFFIXES):
                        continue
                    # 分类夹本身不列出（它的内容由 extra_dirs 单独收）
                    if not group and item.path.casefold() in skipped:
                        continue
                    try:
                        stat = item.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    attrs = getattr(stat, "st_file_attributes", 0)
                    if not show_hidden and attrs & (_FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM):
                        if item.path.casefold() not in kept:
                            continue
                    key = (group + "\\" + name).casefold() if group else name.casefold()
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        is_dir = item.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    entries.append(DesktopEntry(
                        name=name,
                        path=item.path,
                        is_dir=is_dir,
                        is_lnk=name.casefold().endswith(".lnk"),
                        suffix=os.path.splitext(name)[1].lower(),
                        mtime=stat.st_mtime,
                        size=stat.st_size,
                        group=group,
                    ))
                    if len(entries) >= max_scan:
                        return False
        except OSError:
            pass
        return True

    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        if not _collect(root):
            return entries

    for extra in extra_dirs or ():
        if not extra or not os.path.isdir(extra):
            continue
        if not _collect(extra, group=os.path.basename(extra)):
            break

    return entries


def match_score(entry: DesktopEntry, tokens: list[str]) -> int:
    """空格分词 AND；命中越靠前分越高。"""
    stem = entry.stem.casefold()
    name = entry.name.casefold()
    suffix = entry.suffix.lstrip(".").casefold()
    score = 0
    for token in tokens:
        if stem.startswith(token):
            score += 100
        elif name.startswith(token):
            score += 90
        elif token in stem:
            score += 60
        elif token in name:
            score += 40
        elif suffix and token in suffix:
            score += 20
        else:
            return 0
    return score


def filter_entries(entries: list[DesktopEntry], query: str,
                   sort_by: str = "type") -> list[DesktopEntry]:
    tokens = [token for token in query.casefold().split() if token]
    if tokens:
        scored = [(match_score(entry, tokens), entry) for entry in entries]
        scored = [pair for pair in scored if pair[0] > 0]
        scored.sort(key=lambda pair: (-pair[0], not pair[1].is_dir, pair[1].name.casefold()))
        return [entry for _score, entry in scored]
    if sort_by == "mtime":
        return sorted(entries, key=lambda entry: (-entry.mtime, entry.name.casefold()))
    if sort_by == "type":
        # 与资源管理器「排序方式 → 类型」一致：
        # 文件夹最前 → 其余按系统类型名聚在一起 → 组内按名称
        return sorted(
            entries,
            key=lambda entry: (
                not entry.is_dir,
                "" if entry.is_dir else shell_type_name(entry.suffix).casefold(),
                entry.name.casefold(),
            ),
        )
    return sorted(entries, key=lambda entry: (not entry.is_dir, entry.name.casefold()))


# ---------------------------------------------------------------- 样式补充
_MENU_QSS = """
QMenu {
    background: #FFFFFF;
    border: 1px solid #D8E3F5;
    border-radius: 8px;
    padding: 6px;
}
QMenu::item {
    padding: 7px 24px 7px 14px;
    border-radius: 6px;
    color: #161C28;
    font-size: 12px;
}
QMenu::item:selected {
    background: #E4EDFC;
    color: #161C28;
}
QMenu::separator {
    height: 1px;
    background: #EDF2FB;
    margin: 4px 8px;
}
"""

_EXTRA_QSS = """
QListWidget#deskList {
    background: #FFFFFF;
    border: 1px solid #D8E3F5;
    border-radius: 10px;
    outline: none;
    padding: 2px;
}
QListWidget#deskList::item {
    border-radius: 8px;
    margin: 1px 3px;
}
QListWidget#deskList::item:selected {
    background: #E4EDFC;
    color: #161C28;
}
QListWidget#deskList::item:hover {
    background: #F2F6FD;
}
"""


# ---------------------------------------------------------------- 行绘制
class _RowDelegate(QStyledItemDelegate):
    """两行：图标 + 名称（中略）+ 右侧 meta。"""

    def __init__(self, dialog: "DesktopFilesDialog") -> None:
        super().__init__(dialog)
        self._dialog = dialog

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), _ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        entry = self._dialog.entry_at(index.row())
        if entry is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = option.rect.adjusted(10, 0, -12, 0)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#E4EDFC" if selected else "#F2F6FD"))
            painter.drawRoundedRect(QRectF(rect).adjusted(-2, 3, 2, -3), 8, 8)

        icon = self._dialog.icon_for(entry)
        icon_rect = QRect(rect.left() + 4, rect.top() + (_ROW_HEIGHT - _ICON_SIZE) // 2,
                          _ICON_SIZE, _ICON_SIZE)
        if not icon.isNull():
            icon.paint(painter, icon_rect, Qt.AlignmentFlag.AlignCenter)

        text_left = icon_rect.right() + 12
        meta = entry.meta
        meta_font = QFont(theme.FONT_FAMILY, 9)
        meta_width = QFontMetrics(meta_font).horizontalAdvance(meta) + 6

        name_right = rect.right() - meta_width - 10
        name_rect = QRect(text_left, rect.top() + 5, max(40, name_right - text_left), 20)
        name_font = QFont(theme.FONT_FAMILY, 10)
        name_font.setWeight(QFont.Weight.Medium)
        painter.setFont(name_font)
        painter.setPen(QColor("#161C28"))
        elided = QFontMetrics(name_font).elidedText(
            entry.name, Qt.TextElideMode.ElideMiddle, name_rect.width()
        )
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided)

        sub_rect = QRect(text_left, rect.top() + 24, max(40, rect.width() - (text_left - rect.left()) - 6), 16)
        painter.setFont(meta_font)
        painter.setPen(QColor("#5F6570"))
        painter.drawText(sub_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, meta)

        painter.restore()


# ---------------------------------------------------------------- 主窗口
class DesktopFilesDialog(QDialog):
    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self._entries: list[DesktopEntry] = []
        self._visible: list[DesktopEntry] = []
        self._icon_cache: dict[str, QIcon] = {}
        self._icon_cursor = 0
        self._busy = False
        self._last_query = ""

        self._provider = QFileIconProvider()

        self.setWindowTitle("桌面净化")
        self.resize(760, 620)
        self.setMinimumSize(560, 420)
        self.setStyleSheet(theme.DIALOG_QSS + _EXTRA_QSS)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 16)
        root.setSpacing(12)
        # 外壳必须在 root 布局建好之后装（否则拿不到 layout 会静默回退系统栏）
        from lylaunch.plugins.window_chrome import install_chrome

        install_chrome(self, "桌面净化", show_minmax=True, resizable=True)

        self._build_header(root)
        self._build_action_card(root)
        self._build_hint(root)
        self._build_search(root)
        self._build_list(root)
        self._build_footer(root)

        self._icon_timer = QTimer(self)
        self._icon_timer.setInterval(16)
        self._icon_timer.timeout.connect(self._load_icon_chunk)

        # 搜索防抖：必须用窗口持有的 QTimer（红线 3），不用 singleShot 捕获 self
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(120)
        self._debounce.timeout.connect(self._apply_filter_debounced)

        self._poll = QTimer(self)
        self._poll.setInterval(_POLL_MS)
        self._poll.timeout.connect(self._on_poll)

        self._update_sort_button()
        self._refresh_files()
        self._sync_state_from_system()
        self._poll.start()

    # ---------- UI 构建
    def _cfg(self, key: str, default=None):
        try:
            value = self.ctx.api.settings_get(key, None)
        except Exception:
            value = None
        if value is None:
            return self.ctx.settings.get(key, default)
        return value

    def _build_header(self, root: QVBoxLayout) -> None:
        head = QHBoxLayout()
        head.setSpacing(12)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        title = QLabel("桌面净化")
        title.setProperty("role", "title")
        self.subtitle = QLabel("正在读取桌面…")
        self.subtitle.setProperty("role", "subtitle")
        texts.addWidget(title)
        texts.addWidget(self.subtitle)
        head.addLayout(texts, 1)

        self.board_btn = QPushButton("分区视图")
        self.board_btn.setProperty("role", "soft")
        self.board_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.board_btn.setToolTip("大窗口按类型铺开桌面文件（双击打开）")
        self.board_btn.clicked.connect(self._toggle_partition)
        head.addWidget(self.board_btn)
        root.addLayout(head)

    def _build_action_card(self, root: QVBoxLayout) -> None:
        """操作条（单行）：名称 + 状态徽章 + 「收纳 / 恢复」两个按钮。

        刻意和右上角的「分区视图」分开：那个是查看，这里是改桌面。
        说明文字放按钮 tooltip，避免一行行文案把窗口塞满。
        """
        card = QWidget()
        card.setProperty("role", "cardSoft")
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 8, 12, 8)
        row.setSpacing(8)

        mode = str(self._cfg("hide_mode", "fences"))
        if mode == "fences":
            label_text = "桌面整理"
            tip = "按类型移入桌面上的分类文件夹（文档 / 图片 / 压缩包…），快捷方式留在桌面"
            do_text, undo_text = "收纳桌面文件", "恢复原状"
        elif mode == "all":
            label_text = "桌面图标"
            tip = "隐藏全部桌面图标（含快捷方式）"
            do_text, undo_text = "隐藏桌面图标", "恢复显示"
        else:
            label_text = "桌面文件"
            tip = "只隐藏真实文件，快捷方式留在桌面；仍可在这里搜索打开"
            do_text, undo_text = "隐藏桌面文件", "恢复显示"

        label = QLabel(label_text)
        label.setProperty("role", "section")
        row.addWidget(label)

        self.badge = QLabel("读取中")
        self.badge.setProperty("role", "badge")
        row.addWidget(self.badge)
        row.addStretch(1)

        self.collect_btn = QPushButton(do_text)
        self.collect_btn.setProperty("role", "primary")
        self.collect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collect_btn.setToolTip(tip)
        self.collect_btn.clicked.connect(self._on_collect_clicked)
        row.addWidget(self.collect_btn)

        self.restore_btn = QPushButton(undo_text)
        self.restore_btn.setProperty("role", "soft")
        self.restore_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.restore_btn.setToolTip("把收纳/隐藏的文件全部还原")
        self.restore_btn.clicked.connect(self._on_restore_clicked)
        row.addWidget(self.restore_btn)

        root.addWidget(card)

    def _build_hint(self, root: QVBoxLayout) -> None:
        self.hint = QLabel("桌面图标已隐藏 · 退出 LyLaunch 会自动恢复显示")
        self.hint.setProperty("role", "cardSoft")
        self.hint.setWordWrap(True)
        self.hint.setVisible(False)
        root.addWidget(self.hint)

    def _build_search(self, root: QVBoxLayout) -> None:
        row = QHBoxLayout()
        row.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索桌面文件…（空格分词，回车打开第一项）")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_text_changed)
        self.search.returnPressed.connect(self._open_current)
        row.addWidget(self.search, 1)

        self.sort_btn = QPushButton()
        self.sort_btn.setProperty("role", "soft")
        self.sort_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sort_btn.setToolTip("切换排序方式（按 Windows 系统类型 / 名称 / 修改时间）")
        self.sort_btn.clicked.connect(self._show_sort_menu)
        row.addWidget(self.sort_btn)

        self.count_label = QLabel("")
        self.count_label.setProperty("role", "status")
        row.addWidget(self.count_label)
        root.addLayout(row)

    # ---------- 排序
    _SORT_LABELS = {"type": "按类型", "name": "按名称", "mtime": "按时间"}

    def _update_sort_button(self) -> None:
        key = str(self._cfg("sort_by", "type"))
        self.sort_btn.setText(f"排序：{self._SORT_LABELS.get(key, '按类型')}")

    def _show_sort_menu(self) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(_MENU_QSS)
        current = str(self._cfg("sort_by", "type"))
        for key in ("type", "name", "mtime"):
            action = QAction(self._SORT_LABELS[key], menu)
            action.setCheckable(True)
            action.setChecked(key == current)
            action.triggered.connect(lambda _checked=False, k=key: self._set_sort(k))
            menu.addAction(action)
        menu.exec(self.sort_btn.mapToGlobal(QPoint(0, self.sort_btn.height() + 2)))

    def _set_sort(self, key: str) -> None:
        try:
            self.ctx.api.settings_set("sort_by", key)
        except Exception:
            pass
        self.ctx.settings["sort_by"] = key
        self._update_sort_button()
        self._apply_filter()

    def _build_list(self, root: QVBoxLayout) -> None:
        self.stack = QStackedWidget()

        self.list = QListWidget()
        self.list.setObjectName("deskList")
        self.list.setItemDelegate(_RowDelegate(self))
        self.list.setUniformItemSizes(True)
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.itemActivated.connect(self._on_item_activated)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_context_menu)
        self.stack.addWidget(self.list)

        self.empty = QLabel("桌面没有可显示的文件")
        self.empty.setProperty("role", "status")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.empty)

        root.addWidget(self.stack, 1)

    def _build_footer(self, root: QVBoxLayout) -> None:
        bar = QWidget()
        bar.setProperty("role", "statusBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 6, 0, 0)
        self.status = QLabel("双击或回车打开 · 右键可在资源管理器中定位")
        self.status.setProperty("role", "status")
        layout.addWidget(self.status, 1)
        root.addWidget(bar)

    # ---------- 数据
    def entry_at(self, row: int) -> DesktopEntry | None:
        if 0 <= row < len(self._visible):
            return self._visible[row]
        return None

    def _refresh_files(self) -> None:
        kept: set[str] = set()
        folders: list[str] = []
        try:
            mark = di.read_mark()
            if mark.get("hidden_by_us"):
                mode = str(mark.get("mode") or "")
                if mode == "files_only":
                    # 被我们自己隐藏的文件要照常列出来（否则一隐藏列表就空了）
                    kept = {str(p) for p in (mark.get("paths") or [])}
                elif mode == "fences":
                    folders = [str(p) for p in (mark.get("folders") or [])]
        except Exception:
            pass

        # 标记丢了也要能看到分类夹里的文件（只认我们定义的分类名）
        if not folders:
            try:
                folders = fences.managed_folders(desktop_dir())
            except Exception:
                folders = []

        # 容器本身不当作条目列出（内容已经由 extra_dirs 收进来了）
        skip = list(folders)
        try:
            container = fences.container_dir(desktop_dir())
            if os.path.isdir(container):
                skip.append(container)
        except Exception:
            pass

        self._entries = scan_desktop(
            include_public=bool(self._cfg("include_public_desktop", True)),
            show_hidden=bool(self._cfg("show_hidden_files", False)),
            exclude_shortcuts=bool(self._cfg("exclude_shortcuts", True)),
            keep_hidden=kept,
            skip_dirs=skip,
            extra_dirs=folders,
        )
        self._apply_filter()

    def _apply_filter(self) -> None:
        query = self.search.text().strip() if hasattr(self, "search") else ""
        sort_by = str(self._cfg("sort_by", "type"))
        matched = filter_entries(self._entries, query, sort_by)
        limit = max(1, int(self._cfg("max_items", 500) or 500))
        self._visible = matched[:limit]

        self.list.clear()
        for index, entry in enumerate(self._visible):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setSizeHint(QSize(0, _ROW_HEIGHT))
            self.list.addItem(item)

        total = len(matched)
        shown = len(self._visible)
        if not self._entries:
            self.empty.setText("桌面没有可显示的文件")
            self.stack.setCurrentWidget(self.empty)
        elif not matched:
            hint = "没有找到匹配的文件"
            if query:
                hint += f" · 试试更短的关键词"
            self.empty.setText(hint)
            self.stack.setCurrentWidget(self.empty)
        else:
            self.stack.setCurrentWidget(self.list)

        self.count_label.setText(f"共 {total} 项" if shown == total else f"共 {total} 项 · 显示前 {shown} 项")
        note = "（不含快捷方式）" if self._cfg("exclude_shortcuts", True) else ""
        self.subtitle.setText(f"桌面 · {len(self._entries)} 项{note}")

        self._icon_cursor = 0
        if not self._icon_timer.isActive() and self._visible:
            self._icon_timer.start()

    # ---------- 图标
    def icon_for(self, entry: DesktopEntry) -> QIcon:
        key = self._icon_key(entry)
        cached = self._icon_cache.get(key)
        if cached is not None:
            return cached
        # 未预热时先给标准图标，避免首屏卡在 shell 调用上
        style = self.style() or QApplication.style()
        if entry.is_dir:
            return style.standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        if entry.is_lnk:
            return style.standardIcon(QStyle.StandardPixmap.SP_FileLinkIcon)
        return style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)

    @staticmethod
    def _icon_key(entry: DesktopEntry) -> str:
        if entry.is_dir or entry.is_lnk:
            return entry.path.casefold()
        return (entry.suffix or "__file__").casefold()

    def _load_icon_chunk(self) -> None:
        if self._icon_cursor >= len(self._visible):
            self._icon_timer.stop()
            return
        pending: list[int] = []
        end = min(len(self._visible), self._icon_cursor + _ICON_BATCH)
        for row in range(self._icon_cursor, end):
            entry = self._visible[row]
            key = self._icon_key(entry)
            if key in self._icon_cache:
                continue
            try:
                if entry.is_dir:
                    icon = self._provider.icon(QFileIconProvider.IconType.Folder)
                else:
                    icon = self._provider.icon(QFileInfo(entry.path))
            except Exception:
                icon = QIcon()
            self._icon_cache[key] = icon
            pending.append(row)
        self._icon_cursor = end
        if pending and self.list.count():
            top = self.list.indexAt(QPoint(0, 0)).row() if self.list.count() else 0
            bottom = min(len(self._visible) - 1, top + 20)
            for row in pending:
                if top <= row <= bottom:
                    self.list.viewport().update()
                    break
        if self._icon_cursor >= len(self._visible):
            self._icon_timer.stop()

    # ---------- 状态同步
    def _sync_state_from_system(self) -> None:
        info = di.status()
        mode = str(self._cfg("hide_mode", "fences"))
        mark = info.get("mark") or {}
        marked = bool(mark.get("hidden_by_us"))
        moved_count = len(mark.get("moved") or [])
        folders = [str(p) for p in (mark.get("folders") or [])]
        registered = info.get("registry")
        visible = info.get("visible")
        shows_hidden = bool(info.get("shows_hidden"))

        if mode == "all":
            hidden = registered == 1 or visible is False
            pending = registered == 1 and visible is True   # 注册表已隐藏但桌面还没刷新
        elif mode == "fences":
            hidden = marked and moved_count > 0
            pending = False
        else:
            hidden = marked
            pending = False

        # 按钮可用性随状态变：已整理只能「恢复」，未整理只能「收纳」
        try:
            self.collect_btn.setEnabled(not hidden)
            self.restore_btn.setEnabled(bool(hidden) or bool(pending))
        except Exception:
            pass

        if pending:
            self.badge.setText("待生效")
            self._style_badge("#FFF3E0", "#8A5A00")
            self.hint.setVisible(True)
            self.hint.setText("已记录隐藏，但桌面还没刷新 · 按 F5 或重启资源管理器后生效")
            self.status.setText("提示：可按 F5 刷新桌面")
        elif hidden and mode == "fences":
            cats = "、".join(os.path.basename(f) for f in folders) or "分类夹"
            self.badge.setText("已收纳")
            self._style_badge("#E8F0FE", "#2F6BEE")
            self.hint.setVisible(True)
            self.hint.setText(
                f"已收纳 {moved_count} 个文件到桌面分类夹（{cats}）· 点「恢复原状」可移回"
            )
        elif hidden and mode != "all":
            count = int(info.get("mark_paths") or 0)
            self.badge.setText("已隐藏")
            self._style_badge("#FFF3E0", "#8A5A00")
            self.hint.setVisible(True)
            if shows_hidden:
                self.hint.setText(
                    f"已隐藏 {count} 个文件 · 但资源管理器开着「显示隐藏的项目」，桌面上看不出变化"
                )
            else:
                self.hint.setText(
                    f"已隐藏 {count} 个真实文件 · 快捷方式仍在桌面 · 退出 LyLaunch 会自动恢复"
                )
        elif hidden:
            self.badge.setText("已隐藏")
            self._style_badge("#FFF3E0", "#8A5A00")
            self.hint.setVisible(True)
            self.hint.setText("全部桌面图标已隐藏 · 退出 LyLaunch 会自动恢复显示")
        else:
            self.badge.setText("已显示")
            self._style_badge("#E8F7EE", "#1F7A45")
            self.hint.setVisible(False)

    def _style_badge(self, background: str, color: str) -> None:
        self.badge.setStyleSheet(
            f"background:{background};color:{color};border-radius:9px;"
            f"padding:3px 10px;font-size:11px;font-weight:600;"
        )

    def _on_poll(self) -> None:
        if self._busy or not self.isVisible():
            return
        self._sync_state_from_system()

    def _toggle_partition(self) -> None:
        if _PARTITION is not None:
            try:
                if _PARTITION.isVisible():
                    _PARTITION.close()
                    return
                _PARTITION.reload()
                _PARTITION.show()
                _PARTITION.raise_()
                return
            except RuntimeError:
                pass
        _open_partition(self.ctx)

    # ---------- 操作
    def _on_collect_clicked(self) -> None:
        """「收纳桌面文件」按钮：把桌面上的真实文件按大类收进分类夹。"""
        if self._busy:
            return
        if di.read_mark().get("hidden_by_us") or di.registry_hidden() == 1:
            self.status.setText("已经整理过了，如需重来请先点「恢复原状」")
            return
        self._apply_hidden(True)

    def _on_restore_clicked(self) -> None:
        if self._busy:
            return
        if not (di.read_mark().get("hidden_by_us")
                or di.registry_hidden() == 1
                or di.icons_visible() is False):
            self.status.setText("桌面图标本来就是显示的")
            self._sync_state_from_system()
            return
        self._apply_hidden(False)

    def _apply_hidden(self, desired: bool) -> None:
        self._busy = True
        for button in (self.collect_btn, self.restore_btn):
            button.setEnabled(False)
        try:
            mode = str(self._cfg("hide_mode", "fences"))
            if mode == "fences":
                ok, message = self._apply_fences(desired)
            elif mode == "all":
                ok, message = self._apply_hidden_all(desired)
            else:
                ok, message = self._apply_hidden_files(desired)
            self._refresh_files()          # 状态变了要重扫（keep_hidden 才会生效）
            self._sync_state_from_system()
            self._refresh_main_index()     # 让主程序重扫（收纳/恢复都移动了文件）
            if not ok:
                theme.show_warning(self, "桌面净化", message or "操作失败")
            else:
                self.status.setText(message)
                # 关键提醒：只有「设隐藏属性」模式才受"显示隐藏的项目"影响
                if desired and mode == "files_only" and di.windows_shows_hidden():
                    theme.show_warning(
                        self, "隐藏效果不可见",
                        "你的资源管理器开着「显示隐藏的项目」，所以桌面图标只会变半透明、"
                        "不会消失。\n\n"
                        "请到：资源管理器 → 查看 → 显示 → 取消勾选「隐藏的项目」，"
                        "隐藏才会真正生效。\n\n"
                        "（文件没有被移动或删除，随时可以恢复）",
                    )
        finally:
            self._busy = False
            for button in (self.collect_btn, self.restore_btn):
                button.setEnabled(True)
            try:
                self._sync_state_from_system()   # 按真实状态重设按钮可用性
            except Exception:
                pass

    def _refresh_main_index(self) -> None:
        """让主程序重建索引 —— 否则移动过快捷方式后，图标会退化成字母瓦片。"""
        refresh = getattr(self.ctx.api, "refresh_index", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                pass

    def _apply_fences(self, desired: bool) -> tuple[bool, str]:
        """收纳 / 恢复核心已抽到 ops.py，窗口与分区视图共用同一份逻辑。"""
        root = ops.desktop_dir()
        if not root:
            return False, "找不到桌面目录"
        if desired:
            return ops.collect(root)
        return ops.restore(root)

    def _apply_hidden_all(self, desired: bool) -> tuple[bool, str]:
        """模式 all：系统级隐藏全部桌面图标（含快捷方式）。"""
        if desired:
            di.write_mark(True, mode="all", paths=[])
        ok, immediate, message = di.set_hidden(
            desired,
            allow_showwindow_fallback=bool(self._cfg("allow_showwindow_fallback", False)),
        )
        if not ok:
            return False, message or "切换失败"
        di.write_mark(bool(desired), mode="all", paths=[])
        if not immediate:
            return True, message or "已记录，桌面未即时刷新（按 F5 试试）"
        return True, ("全部桌面图标已隐藏（退出程序会自动恢复）"
                      if desired else "桌面图标已恢复显示")

    def _apply_hidden_files(self, desired: bool) -> tuple[bool, str]:
        """模式 files_only：只让真实文件/文件夹的桌面图标消失，快捷方式保留。"""
        paths = [entry.path for entry in self._entries if not entry.is_lnk]
        if desired:
            # 先写标记（含完整路径）再改属性：中途失败也能靠标记全部还原
            di.write_mark(True, mode="files_only", paths=paths)
            done, failed = di.set_paths_hidden(paths, True)
            di.arrange_desktop_icons()     # 图标消失后重排，补上空位
            note = ""
            if di.windows_shows_hidden():
                note = "（资源管理器开着「显示隐藏的项目」，桌面上看不到效果）"
            if failed:
                return False, f"{len(failed)} 项未能隐藏（可能无权限）{note}"
            return True, f"已隐藏 {len(done)} 个真实文件，快捷方式保持显示{note}"

        recorded = [str(p) for p in (di.read_mark().get("paths") or [])]
        targets = recorded or paths          # 优先用标记里的路径（可能已不在桌面）
        done, failed = di.set_paths_hidden(targets, False)
        di.write_mark(False)
        if failed:
            return False, f"{len(failed)} 项未能恢复显示（可能无权限）"
        return True, f"已恢复 {len(done)} 项显示"

    # ---------- 搜索与打开
    def _on_text_changed(self, _text: str) -> None:
        self._debounce.start()

    def _apply_filter_debounced(self) -> None:
        current = self.search.text().strip()
        if current == self._last_query:
            return
        self._last_query = current
        self._apply_filter()

    def _open_current(self) -> None:
        row = self.list.currentRow()
        if row < 0 and self._visible:
            row = 0
        entry = self.entry_at(row)
        if entry:
            self._open(entry)

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        index = item.data(Qt.ItemDataRole.UserRole)
        entry = self.entry_at(int(index)) if index is not None else None
        if entry:
            self._open(entry)

    def _open(self, entry: DesktopEntry) -> None:
        ok = False
        try:
            ok = bool(self.ctx.api.launch(entry.path))
        except Exception:
            ok = False
        if ok:
            self.status.setText(f"已打开：{entry.name}")
        else:
            self.status.setText("没有默认打开方式 —— 可右键 → 在资源管理器中定位")

    def _reveal(self, entry: DesktopEntry) -> None:
        try:
            self.ctx.api.reveal(entry.path)
            self.status.setText(f"已在资源管理器中定位：{entry.name}")
        except Exception:
            self.status.setText("定位失败")

    def _on_context_menu(self, pos: QPoint) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        index = item.data(Qt.ItemDataRole.UserRole)
        entry = self.entry_at(int(index)) if index is not None else None
        if entry is None:
            return

        menu = QMenu(self)
        menu.setStyleSheet(_MENU_QSS)

        act_open = QAction("打开", menu)
        act_open.triggered.connect(lambda: self._open(entry))
        menu.addAction(act_open)

        act_reveal = QAction("在资源管理器中定位", menu)
        act_reveal.triggered.connect(lambda: self._reveal(entry))
        menu.addAction(act_reveal)

        menu.addSeparator()
        act_copy = QAction("复制路径", menu)
        act_copy.triggered.connect(lambda: self._copy_path(entry))
        menu.addAction(act_copy)

        menu.exec(menu.mapToGlobal(pos))

    def _copy_path(self, entry: DesktopEntry) -> None:
        try:
            self.ctx.api.clipboard_write(entry.path)
            self.status.setText("已复制路径")
        except Exception:
            self.status.setText("复制失败")

    # ---------- 生命周期
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._refresh_files()
        self._sync_state_from_system()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._poll.stop()
        self._icon_timer.stop()
        # L1：关窗恢复（配置项，调试期默认开）
        try:
            if self._cfg("restore_on_window_close", True) and di.read_mark().get("hidden_by_us"):
                di.force_show_all()
        except Exception:
            pass
        super().closeEvent(event)


# ---------------------------------------------------------------- 非模态
_DLG: DesktopFilesDialog | None = None
_PARTITION = None      # 分区视图单例（partition_view.PartitionView）
_QUIT_HOOKED = False


def _restore_on_app_quit() -> None:
    """L2：退出程序时恢复。只做文件属性/注册表 + 文件 IO，绝不碰任何控件。

    注意：fences（收纳）模式**不**在退出时移回 —— 文件就摆在桌面的分类夹里，
    随时可见可访问，属于"常驻整理"而不是"隐藏"，移回反而让收纳白做。
    """
    try:
        mark = di.read_mark()
        if not mark.get("hidden_by_us"):
            return
        if str(mark.get("mode") or "") == "fences":
            return
        di.force_show_all()
    except Exception:
        pass


def _open_nonmodal(ctx) -> None:
    """头号红线：show() 不是 exec()；模块级单例防 GC + 重复打开只提到前台。"""
    global _DLG
    if _DLG is not None:
        try:
            _DLG.raise_()
            _DLG.activateWindow()
            return
        except RuntimeError:
            _DLG = None
    dialog = DesktopFilesDialog(ctx)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    dialog.destroyed.connect(lambda *_: globals().__setitem__("_DLG", None))
    _DLG = dialog
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()


def _make_hotkey_setter(ctx):
    """返回一个 spec -> (ok, msg) 的回调，供窗口里那颗「快捷键」按钮使用。

    0.43：快捷键入口从设置页挪进插件本身。这里写回 plugin.json 的 Hotkey、
    同步内存 manifest，再让主窗口按新清单重注册（立即生效，不用重启）。
    """
    def _set(spec: str):
        import json
        from PySide6.QtWidgets import QApplication
        from lylaunch.hotkey import parse_hotkey
        if parse_hotkey(spec) is None:
            return False, "无效的按键组合（至少带一个 Ctrl / Alt / Shift / Win）"
        path = os.path.join(ctx.plugin_dir, "plugin.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data["Hotkey"] = spec
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            return False, f"保存失败：{exc}"
        ctx.manifest.hotkey = spec
        main = None
        for w in QApplication.topLevelWidgets():
            if callable(getattr(w, "refresh_plugin_hotkeys", None)):
                main = w
                break
        refresh = getattr(main, "refresh_plugin_hotkeys", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                pass
        return True, f"快捷键已改为 {spec}，立即生效"

    return _set


def _open_partition(ctx) -> None:
    """开关「桌面净化」主窗口（已存在就 hide/show 复用，快捷键再按一次 = 关闭）。

    不用 close 销毁：close 会杀掉后台图标线程，下次打开要重建整个 UI（卡几秒）；
    hide 只藏窗口，二次打开零成本 —— 真正退出程序时随进程一起销毁。
    reload 放到 singleShot：先显示上次的内容，再刷新数据，呼出不阻塞。
    """
    global _PARTITION
    if _PARTITION is not None:
        try:
            if _PARTITION.isVisible():
                _PARTITION.hide()
                return
            from partition_view import desktop_signature
            # 内容没变 → 直接 show（零卡顿零闪烁）；变了 → 隐藏状态下
            # 重建完再显示（setUpdatesEnabled 兜住中间态，不会闪出错乱）
            if getattr(_PARTITION, "_last_signature", None) != desktop_signature():
                _PARTITION.setUpdatesEnabled(False)
                _PARTITION.reload()
                _PARTITION.setUpdatesEnabled(True)
            _PARTITION.show()
            _PARTITION.raise_()
            _PARTITION.activateWindow()
            return
        except RuntimeError:
            _PARTITION = None
    try:
        from partition_view import PartitionView
    except Exception:
        return
    _PARTITION = PartitionView(ctx.api, hotkey_getter=lambda: ctx.manifest.hotkey or "",
                               hotkey_setter=_make_hotkey_setter(ctx))
    _PARTITION.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    _PARTITION.destroyed.connect(lambda *_: globals().__setitem__("_PARTITION", None))
    _PARTITION.show()
    _PARTITION.raise_()
    _PARTITION.activateWindow()


def _close_partition() -> None:
    global _PARTITION
    if _PARTITION is not None:
        try:
            _PARTITION.close()
        except RuntimeError:
            pass
        _PARTITION = None


class Plugin(PluginBase):
    def on_load(self) -> None:
        global _QUIT_HOOKED
        app = QApplication.instance()
        if app is not None and not _QUIT_HOOKED:
            app.aboutToQuit.connect(_restore_on_app_quit)
            _QUIT_HOOKED = True
        # L4：异常退出后的处理
        try:
            mark = di.read_mark()
            if mark.get("hidden_by_us"):
                if str(mark.get("mode") or "") == "fences":
                    # 收纳是常驻语义：重启后保持不动（不自动弹窗，避免打扰）。
                    # 只有当分类夹已经被用户删掉时，标记才作废。
                    folders = [str(p) for p in (mark.get("folders") or [])]
                    if not any(os.path.isdir(f) for f in folders):
                        di.write_mark(False)
                elif bool(self.ctx.settings.get("auto_restore_on_exit", True)):
                    if di.restore_if_marked():
                        self.ctx.api.notify("桌面净化", "上次退出时桌面图标处于隐藏状态，已自动恢复显示")
        except Exception:
            pass

    def on_unload(self) -> None:
        # L3：设置页禁用 / 卸载插件时恢复
        global _QUIT_HOOKED
        app = QApplication.instance()
        if app is not None and _QUIT_HOOKED:
            try:
                app.aboutToQuit.disconnect(_restore_on_app_quit)
            except Exception:
                pass
            _QUIT_HOOKED = False
        try:
            if di.read_mark().get("hidden_by_us"):
                di.force_show_all()
        except Exception:
            pass

    def on_execute(self, query: str = "") -> None:
        # 桌面净化已并入分区窗口：主入口直接打开它
        _open_partition(self.ctx)
