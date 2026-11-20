"""分区视图：一个大窗口，把桌面文件按大类铺成卡片（替代桌面浮窗看板）。

设计取舍（为什么不做桌面上贴着的框）
------------------------------------
Windows 上「半透明窗口」与「作为桌面子窗口」互斥（WS_EX_LAYERED 不能当 child），
硬做会出现「SetParent 后完全不可见 / z-order 被 SHELLDLL_DefView 盖住 / 高 DPI 坐标错位」
一连串问题。启动器本来的用法就是「按快捷键 → 找东西 → 关掉」，所以做成大窗口更顺：
零窗口层黑魔法、不挡桌面图标、关闭即释放。

数据来源（两种状态都支持）
--------------------------
- 已经收纳过：读桌面上的分类文件夹（真实位置）；
- 还没收纳：扫描桌面顶层文件，按 ``fences.category_of`` **虚拟分组**（不动文件）。
两者会合并，所以任何时候打开都能看到全部分类的文件。
"""

from __future__ import annotations

import os
import queue
import shutil
import time

from PySide6.QtCore import (
    QEvent, QRect, QFileInfo, QObject, QPoint, QSize, QTimer, QThread, Qt, Signal,
)
from PySide6.QtGui import QCursor, QImage, QKeyEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileIconProvider, QFrame, QGraphicsOpacityEffect,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QScrollArea,
    QSizePolicy, QStackedWidget, QStyle, QToolButton, QVBoxLayout, QWidget,
)

from lylaunch.plugins import theme

try:
    from lylaunch.icons import extract_icon_image
except Exception:      # 插件独立分发时没有主程序模块，退回 Qt 图标
    extract_icon_image = None

try:
    import fences
    import desktop_icons as di
    import ops
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fences
    import desktop_icons as di
    import ops

_COLS = 7          # 每行瓦片数（单列宽卡片，右侧空间够放 7 个）
_TILE_W = 92

# 拖拽状态：手动拖拽期间瓦片捕获了鼠标，滚轮事件落在哪个控件不确定 ——
# 统一由应用级 _WheelFilter 接管转发；tile 引用给 QTimer 轮询用。
_DRAG_STATE: dict = {"scroll": None, "tile": None}
_TILE_H = 88
_ICON = 36
_CARD_COLS = 1    # 一行一个分组卡（占满宽度），左侧索引导航

_SKIP_NAMES = {"desktop.ini", "thumbs.db"}
_SHORTCUT_SUFFIXES = (".lnk", ".url")


def _desktop_dir() -> str:
    try:
        from win32com.shell import shell, shellcon

        return shell.SHGetKnownFolderPath(shellcon.FOLDERID_Desktop)
    except Exception:
        return os.path.expanduser("~/Desktop")


def _human(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 ** 2:
        return f"{size / 1024:.0f} KB"
    if size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024 ** 3:.1f} GB"


class _Item:
    __slots__ = ("path", "name", "is_dir", "is_link", "suffix", "size", "mtime",
                 "group", "hidden", "in_bucket", "from_path")

    def __init__(self, path: str, name: str, is_dir: bool, suffix: str,
                 size: int, mtime: float, group: str,
                 is_link: bool = False, hidden: bool = False,
                 in_bucket: bool = False, from_path: str = "") -> None:
        self.path = path
        self.name = name
        self.is_dir = is_dir
        self.is_link = is_link
        self.suffix = suffix
        self.size = size
        self.mtime = mtime
        self.group = group
        self.hidden = hidden          # 当前是否在桌面上被隐藏
        self.in_bucket = in_bucket    # 是否已被收进分类夹（不能单独移回）
        # 排序身份键：moved 记录里的「最初桌面位置」。收纳/恢复/再收纳来回折腾
        # 时 to 路径可能变（文件名被 unique_path 加 (1)），from 是稳定的 ——
        # 排序用 from 做键，顺序才不会乱。
        self.from_path = (from_path or path).casefold()

    @property
    def display_name(self) -> str:
        """去掉 .lnk/.url 后缀后的显示名。"""
        base = os.path.splitext(self.name)[0]
        return base or self.name

    @property
    def meta(self) -> str:
        parts: list[str] = []
        if self.is_link:
            parts.append("快捷方式")
        elif self.is_dir:
            parts.append("文件夹")
        elif self.suffix:
            parts.append(self.suffix.lstrip(".").upper())
        if not self.is_dir and self.size:
            parts.append(_human(self.size))
        try:
            parts.append(time.strftime("%m-%d %H:%M", time.localtime(self.mtime)))
        except Exception:
            pass
        return " · ".join(parts)


_GROUP_ORDER = ["游戏", "应用", "文件夹"]


def _load_tile_order() -> dict:
    """瓦片排序（分组名 -> [from_path 键, ...]），存在插件配置里。"""
    data = ops.load_plugin_data()
    order = data.get("tile_order")
    return order if isinstance(order, dict) else {}


def _save_tile_order(order: dict, names: dict) -> None:
    data = ops.load_plugin_data()
    data["tile_order"] = order
    data["tile_order_names"] = names   # 显示名兜底表（from_path 匹配不上时用）
    ops.save_plugin_data(data)


def desktop_signature() -> str:
    """桌面顶层文件名集合的签名（呼出窗口时判断内容有没有变，没变就秒开）。"""
    try:
        root = _desktop_dir()
        if os.path.isdir(root):
            return "|".join(sorted(e.name for e in os.scandir(root)))
    except OSError:
        pass
    return ""


def collect_groups() -> list[tuple[str, list[_Item]]]:
    """汇总所有分区：已收纳的文件 + 桌面顶层（含被我们隐藏的快捷方式/文件夹）。

    系统图标（此电脑 / 网络 / 回收站）不是文件系统对象，扫不到，自然不出现在这里。
    """
    root = _desktop_dir()
    bucket_names = {name.casefold() for name in fences.category_names()}

    # moved 记录：to(容器内路径) -> from(最初桌面位置) —— 排序身份键的数据源
    try:
        moved_by_to = {str(m.get("to") or "").casefold(): str(m.get("from") or "")
                       for m in (di.read_mark().get("moved") or [])}
    except Exception:
        moved_by_to = {}

    buckets: dict[str, list[_Item]] = {}
    seen: set[str] = set()
    pending_links: list[tuple[str, str, object]] = []

    def _push(item: _Item) -> None:
        buckets.setdefault(item.group, []).append(item)

    # ① 已收纳：分组夹里的真实文件（空分组也保留 —— 用户刚建的自定义分组要显示出来）
    for folder in fences.managed_folders(root):
        group = os.path.basename(folder)
        buckets.setdefault(group, [])
        try:
            with os.scandir(folder) as iterator:
                for entry in iterator:
                    if entry.name.casefold() in _SKIP_NAMES:
                        continue
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    key = "bucket\\" + entry.name.casefold()
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    _push(_Item(
                        path=entry.path,
                        name=entry.name,
                        is_dir=is_dir,
                        suffix=os.path.splitext(entry.name)[1].lower(),
                        size=stat.st_size,
                        mtime=stat.st_mtime,
                        group=group,
                        in_bucket=True,
                        from_path=moved_by_to.get(entry.path.casefold(), entry.path),
                    ))
        except OSError:
            continue

    # ② 桌面顶层：文件夹 / 散落文件 / 快捷方式（快捷方式延后批量解析）
    if root and os.path.isdir(root):
        try:
            with os.scandir(root) as iterator:
                for entry in iterator:
                    low = entry.name.casefold()
                    if low in _SKIP_NAMES or low in bucket_names:
                        continue
                    if low == fences.CONTAINER_NAME.casefold():
                        continue          # 容器本身不列出，内容由分类夹单独收
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    key = "desk\\" + low
                    if key in seen:
                        continue
                    seen.add(key)
                    hidden = di.is_hidden(entry.path)
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    if low.endswith(_SHORTCUT_SUFFIXES):
                        pending_links.append((entry.path, entry.name, stat))
                    elif is_dir:
                        _push(_Item(entry.path, entry.name, True, "", 0,
                                    stat.st_mtime, "文件夹", hidden=hidden))
                    else:
                        _push(_Item(entry.path, entry.name, False,
                                    os.path.splitext(entry.name)[1].lower(),
                                    stat.st_size, stat.st_mtime,
                                    fences.category_of(entry.name), hidden=hidden))
        except OSError:
            pass

    # ③ 快捷方式：批量解析目标，区分「游戏 / 应用」
    if pending_links:
        targets = fences.resolve_targets([path for path, _name, _st in pending_links])
        for path, name, stat in pending_links:
            group = fences.classify_shortcut(name, targets.get(path, ""))
            _push(_Item(path, name, False,
                        os.path.splitext(name)[1].lower(),
                        stat.st_size, stat.st_mtime, group,
                        is_link=True, hidden=di.is_hidden(path)))

    groups: list[tuple[str, list[_Item]]] = []
    fixed = set(fences.display_order())
    tile_order = _load_tile_order()
    tile_order_names = ops.load_plugin_data().get("tile_order_names") or {}

    def _sorted_items(group: str, items: list[_Item]) -> list[_Item]:
        # 用户手动拖动排过的顺序优先：键 = 最初桌面位置（跨收纳/恢复稳定），
        # from_path 匹配不上再退回显示名（文件被改名/旧数据兼容）
        seq = tile_order.get(group) or []
        name_seq = tile_order_names.get(group) or []
        rank = {name: i for i, name in enumerate(seq)}
        name_rank = {name: i for i, name in enumerate(name_seq)}
        items.sort(key=lambda it: (rank.get(it.from_path, name_rank.get(
            it.display_name, 999)), not it.is_dir, it.display_name.casefold()))
        return items

    for name in fences.display_order():   # 游戏 / 应用 / 文件夹 / 文档… 固定顺序
        items = buckets.get(name)
        if items:
            groups.append((name, _sorted_items(name, items)))
    # 用户自建分组：按名字排序追加在固定分组后面
    for name in sorted(set(buckets) - fixed):
        groups.append((name, _sorted_items(name, buckets[name])))
    return groups


class _WheelFilter(QObject):
    """手动拖拽没有模态循环、事件循环正常 —— 应用级过滤器能收到所有滚轮。

    拖拽期间接管滚轮 → 直接滚动右侧内容区；非拖拽期间完全放行。
    """

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if _DRAG_STATE.get("tile") is not None and event.type() == QEvent.Type.Wheel:
            scroll = _DRAG_STATE.get("scroll")
            if scroll is not None:
                delta = event.angleDelta().y()
                if delta:
                    bar = scroll.verticalScrollBar()
                    bar.setValue(bar.value() - int(delta * 0.8))
            return True
        return False


class FileTile(QWidget):
    """一个文件瓦片：图标 + 名称（双击打开，右键菜单，悬停高亮）。

    用 QLabel 承载图标而不是自绘 paint —— 自绘时 QIcon.paint 会按 icon 内部
    的小 pixmap 缩放，视觉上会碎成马赛克；QLabel 走 Qt 的静态绘制路径更快也更稳。
    """

    _NORMAL_QSS = ("#fileTile{border-radius:9px;background:transparent;}"
                   "#fileTile:hover{background:#E4EDFC;}")
    _SHADOW_QSS = "#fileTile{border-radius:9px;background:#EEF2F8;}"

    def __init__(self, item: _Item, pixmap: QPixmap | None, on_open, on_menu) -> None:
        super().__init__()
        self._item = item
        self._on_open = on_open
        self._on_menu = on_menu
        self._drag_start = QPoint()
        self._drag_pending = False    # 左键按下待判定（必须初始化：右键划过也会走 mouseMove）
        self._dragging = False
        self.setFixedSize(_TILE_W, _TILE_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{item.name}\n{item.meta}\n{item.path}\n\n按住拖动可移到其他分组")
        self.setObjectName("fileTile")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(self._NORMAL_QSS)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self._on_menu(self, self._item, pos)
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 7, 5, 4)
        layout.setSpacing(4)

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(_ICON, _ICON)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setStyleSheet("background:transparent;")
        if pixmap is not None and not pixmap.isNull():
            self.icon_label.setPixmap(pixmap)
        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignHCenter)

        self.name_label = QLabel()
        self.name_label.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.name_label.setStyleSheet(
            "background:transparent;color:#161C28;font-size:11px;")
        layout.addWidget(self.name_label, 0, Qt.AlignmentFlag.AlignHCenter)
        self._apply_name()

    def _apply_name(self) -> None:
        metrics = self.name_label.fontMetrics()
        elided = metrics.elidedText(
            self._item.display_name, Qt.TextElideMode.ElideMiddle, _TILE_W - 12)
        self.name_label.setText(elided)

    def set_icon(self, pixmap: QPixmap) -> None:
        if pixmap is not None and not pixmap.isNull():
            self.icon_label.setPixmap(pixmap)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.pos()
            self._drag_pending = True
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (getattr(self, "_drag_pending", False)
                and not getattr(self, "_dragging", False)
                and hasattr(self, "_drag_start")
                and (event.pos() - self._drag_start).manhattanLength() > 12):
            self._begin_drag()
        if getattr(self, "_dragging", False):
            self._update_drag(event.globalPos())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self._finish_drag(event.globalPos())
            event.accept()
            return
        self._drag_pending = False
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        """手动拖拽时鼠标被本瓦片捕获，滚轮事件也在这里 —— 转发给滚动区。"""
        if self._dragging:
            scroll = _DRAG_STATE["scroll"]
            if scroll is not None:
                delta = event.angleDelta().y()
                if delta:
                    bar = scroll.verticalScrollBar()
                    bar.setValue(bar.value() - int(delta * 0.8))
            self._update_drag(QCursor.pos())
            event.accept()
            return
        super().wheelEvent(event)

    # ---------- 手动拖拽（不用 QDrag.exec：它的模态循环会吞掉滚轮事件）
    def _begin_drag(self) -> None:
        self._dragging = True
        self._drag_pending = False
        self.grabMouse()                      # 捕获鼠标，move/release 都发给自己
        self._ghost = QLabel(self.window())
        self._ghost.setPixmap(self.grab())    # 整张瓦片（图标 + 名字）作为预览
        self._ghost.adjustSize()
        self._ghost.show()
        self._ghost.raise_()
        # 原位置留一个淡化的「影子」占位（主程序同款），不是直接消失
        self._shadow_effect = QGraphicsOpacityEffect(self)
        self._shadow_effect.setOpacity(0.3)
        self.setGraphicsEffect(self._shadow_effect)
        self.setStyleSheet(self._SHADOW_QSS)
        _DRAG_STATE["tile"] = self          # 滚轮过滤器 / 轮询开始工作
        self._drag_win = self.window()
        self._drag_win._drag_active = True  # 全局轮询接管（跨窗口拖出靠它）
        self._update_drag(QCursor.pos())

    def _remove_shadow(self) -> None:
        self.setGraphicsEffect(None)
        self._shadow_effect = None
        self.setStyleSheet(self._NORMAL_QSS)

    def _update_drag(self, gpos) -> None:
        win = self.window()
        ghost = getattr(self, "_ghost", None)
        if ghost is not None:
            ghost.move(win.mapFromGlobal(gpos)
                       - QPoint(ghost.width() // 2, ghost.height() // 2))
        card = win._card_at_global(gpos)
        win._set_drag_highlight(card)
        # 同分组的目标瓦片 → 显示插入位置蓝线；其他情况只高亮卡片
        tile_target = win._tile_at_global(gpos)
        if (tile_target is not None and tile_target is not self
                and getattr(tile_target, "_group_name", "") == self._group_name):
            win._update_drop_indicator(tile_target)
        else:
            win._update_drop_indicator(None)

    def _finish_drag(self, gpos) -> None:
        if not self._dragging:                # 幂等：可能被 poll 和 mouseRelease 双触发
            return
        self._dragging = False               # 无论如何先退出拖拽态
        _DRAG_STATE["tile"] = None
        if getattr(self, "_drag_win", None) is not None:
            self._drag_win._drag_active = False
        self.releaseMouse()
        ghost = getattr(self, "_ghost", None)
        if ghost is not None:
            ghost.close()
            self._ghost = None
        self._remove_shadow()
        win = self.window()
        win._set_drag_highlight(None)
        win._update_drop_indicator(None)
        # 拖出窗口边界 = 「移到桌面」（全局轮询在窗口外也能捕捉松手）
        if not win.frameGeometry().contains(gpos):
            win._move_out_to_desktop(self._item, self)
            return
        card = win._card_at_global(gpos)
        if card is None:
            return
        target_group = card._title
        tile_under = win._tile_at_global(gpos)
        if (tile_under is not None and tile_under is not self
                and getattr(tile_under, "_group_name", "") == self._group_name):
            # 同分组内拖到另一瓦片上 = 把自己插到它前面（排序）
            win._reorder(self._group_name, self._item, tile_under._item)
            return
        if target_group != self._group_name:
            win._move_to_group(target_group, [self._item.path])

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_open(self._item)
            return
        super().mouseDoubleClickEvent(event)


class CategoryCard(QFrame):
    """一张分类卡片：标题 + 数量 + 图标网格。

    高度必须 Maximum —— 默认的 Preferred 会让卡片被网格拉伸到撑满整列，
    少量内容的卡片占掉整屏高度（布局崩坏的根源）。
    """

    def __init__(self, title: str, items: list[_Item], icons, on_open, on_menu,
                 on_tile=None, on_drop=None, on_paste=None) -> None:
        super().__init__()
        self._title = title
        self._on_drop = on_drop
        self._on_paste = on_paste
        self.setObjectName("partitionCard")
        self.setProperty("role", "card")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.setAcceptDrops(True)     # 接受从其他分组拖过来的瓦片
        # 卡片空白处右键：把剪贴板里的文件粘贴进这个分组
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_card_menu)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        self._collapsed = False
        label = QLabel(title)
        label.setStyleSheet("font-size:13px;font-weight:600;color:#161C28;")
        label.setCursor(Qt.CursorShape.PointingHandCursor)
        label.mousePressEvent = lambda _event: self._toggle_fold()   # 点标题也能折叠
        head.addWidget(label)
        head.addStretch(1)
        count = QLabel(f"{len(items)} 项")
        count.setStyleSheet(f"font-size:11px;color:#5F6570;")
        head.addWidget(count)
        self.fold_btn = QToolButton()
        self.fold_btn.setText("▾")
        self.fold_btn.setFixedSize(22, 22)
        self.fold_btn.setStyleSheet(
            "border:none;background:transparent;color:#5F6570;font-size:12px;")
        self.fold_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fold_btn.setToolTip("折叠 / 展开这个分组")
        self.fold_btn.clicked.connect(self._toggle_fold)
        head.addWidget(self.fold_btn)
        layout.addLayout(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(2)
        grid.setVerticalSpacing(4)
        self._grid = grid
        # 网格装进一个容器：折叠时隐藏容器（QGridLayout 本身没有 isVisible）
        body = QWidget()
        body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        body.setStyleSheet("background:transparent;")
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.addLayout(grid)
        self._body = body
        layout.addWidget(body)
        self._expanded = False
        self._total = len(items)
        visible_count = min(len(items), _COLS * 2)   # 默认铺两行，其余收进「展开」
        self._extra_tiles: list[FileTile] = []
        for index, item in enumerate(items):
            row, col = divmod(index, _COLS)
            tile = FileTile(item, icons(item), on_open, on_menu)
            tile._group_name = title
            if on_tile is not None:
                on_tile(tile)
            if index >= visible_count:
                tile.setVisible(False)
                self._extra_tiles.append(tile)
            grid.addWidget(tile, row, col)

        if self._extra_tiles:
            self.exp_btn = QToolButton()
            self.exp_btn.setText(f"展开 {len(self._extra_tiles)} 项 ▾")
            self.exp_btn.setStyleSheet(
                "border:none;background:transparent;color:#2F6BEE;font-size:11px;")
            self.exp_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.exp_btn.clicked.connect(self._toggle_expand)
            btn_row = QHBoxLayout()
            btn_row.addStretch(1)
            btn_row.addWidget(self.exp_btn)
            btn_row.addStretch(1)
            layout.addLayout(btn_row)

    # ---------- 折叠 / 展开
    def _toggle_fold(self) -> None:
        """折叠：只留标题行，内容隐藏；卡片高度随之收缩。

        「展开 N 项」按钮在主布局里（不在 body 内），必须跟着 body 一起隐藏 ——
        否则折叠态下点它只会改瓦片可见性，界面毫无反应（状态还会错乱）。
        """
        self._collapsed = not self._collapsed
        self._body.setVisible(not self._collapsed)
        if hasattr(self, "exp_btn"):
            self.exp_btn.setVisible(not self._collapsed)
        self.fold_btn.setText("▸" if self._collapsed else "▾")
        self.fold_btn.setToolTip("展开这个分组" if self._collapsed else "折叠这个分组")

    def _toggle_expand(self) -> None:
        """铺开默认两行之外的全部瓦片。"""
        self._expanded = not self._expanded
        for tile in self._extra_tiles:
            tile.setVisible(self._expanded)
        self.exp_btn.setText("收起 ▴" if self._expanded
                             else f"展开 {len(self._extra_tiles)} 项 ▾")

    def set_drag_highlight(self, on: bool) -> None:
        """拖拽悬停高亮：淡灰底即可（不要蓝色边框）。"""
        self.setStyleSheet(
            "#partitionCard{background:#EEF2F8;border-color:#EEF2F8;}" if on else "")

    # ---------- 卡片右键：粘贴
    def _show_card_menu(self, pos) -> None:
        mime = QApplication.clipboard().mimeData()
        if not mime.hasUrls():
            return                        # 剪贴板里没有文件，不出菜单
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#FFFFFF;border:1px solid #D8E3F5;border-radius:8px;padding:6px;}"
            "QMenu::item{padding:7px 22px;border-radius:6px;color:#161C28;font-size:12px;}"
            "QMenu::item:selected{background:#E4EDFC;color:#161C28;}")
        act = menu.addAction(f"粘贴到「{self._title}」")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is act and self._on_paste is not None:
            self._on_paste(self._title)

    # ---------- 拖放：把其他分组的瓦片拖进来 = 把真实文件移进这个分组
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet(
                "#partitionCard{border:2px solid #2F6BEE;background:#F2F6FD;}")

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.setStyleSheet("")

    def dropEvent(self, event) -> None:  # noqa: N802
        self.setStyleSheet("")
        # QUrl.toLocalFile() 给的是正斜杠路径，必须 normpath 成反斜杠，
        # 否则「拖回原分组」的跳过判断（字符串比较）永远不命中 → 文件被加 (2)
        paths = [os.path.normpath(u.toLocalFile())
                 for u in event.mimeData().urls() if u.toLocalFile()]
        if paths and self._on_drop is not None:
            event.acceptProposedAction()
            self._on_drop(self._title, paths)


class _IconWorker(QThread):
    """后台提取真实图标（256px），完成后主线程回填瓦片。

    同步提取 70+ 个图标要好几秒，是把分区视图卡成 PPT 的主要原因。
    先显示 Qt 快速占位图标，真实图标一张张补上。
    """

    loaded = Signal(str, QImage)

    def __init__(self) -> None:
        super().__init__()
        self._q: "queue.Queue[str]" = queue.Queue()
        self._stop = False

    def enqueue(self, path: str) -> None:
        self._q.put(path)

    def stop(self) -> None:
        self._stop = True
        self._q.put("__stop__")

    def run(self) -> None:  # noqa: D102
        while True:
            try:
                path = self._q.get(timeout=0.3)
            except queue.Empty:
                continue
            if path == "__stop__" or self._stop:
                break
            try:
                if extract_icon_image is None:
                    continue
                img = extract_icon_image(path, 128)
                if img is not None and not img.isNull():
                    self.loaded.emit(path, img)
            except Exception:
                pass


class _HotkeyCaptureDialog(QDialog):
    """轻量快捷键捕获框：按下组合键 → 确定；Esc / 取消不写入。

    只接受「修饰键 + 主键」的有效组合；用 QEventFilter 抓 Qt 按键，
    不碰 RegisterHotKey（全局热键仍由主窗口按 manifest 注册）。
    """

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.spec = ""
        self.setWindowTitle("设置桌面净化快捷键")
        self.setModal(True)
        self.setFixedSize(320, 130)
        lay = QVBoxLayout(self)
        self.tip = QLabel("请按下新的组合键（如 Alt+D）…")
        self.tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.tip)
        row = QHBoxLayout()
        self.ok = QPushButton("确定")
        self.ok.setEnabled(False)
        self.cancel = QPushButton("取消")
        self.ok.clicked.connect(self.accept)
        self.cancel.clicked.connect(self.reject)
        row.addWidget(self.ok)
        row.addWidget(self.cancel)
        lay.addLayout(row)
        # 必须挂起全部全局热键（含插件自己的 Alt+D）：RegisterHotKey 注册的组合
        # 会被 Windows 从键盘流里抽走改投 WM_HOTKEY，Qt 收不到 KeyPress —— 表现就是
        # 「捕获框开着但按什么都没反应，按 Alt+D 还会把窗口切走」。和设置页同款处理。
        self._main = None
        for w in QApplication.topLevelWidgets():
            # 鸭子类型：有 refresh_plugin_hotkeys 的就是主窗口（比类名稳）
            if callable(getattr(w, "refresh_plugin_hotkeys", None)):
                self._main = w
                break
        suspend = getattr(self._main, "suspend_hotkeys", None)
        if callable(suspend):
            try:
                suspend()
            except Exception:
                pass
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() != QEvent.Type.KeyPress or not self.isVisible():
            return False
        # 不限制 obj：捕获时焦点可能在「确定」按钮上，按键发给的是它而不是对话框
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.reject()
            return True
        if key in (Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Shift,
                   Qt.Key.Key_Meta, Qt.Key.Key_AltGr):
            return True
        from lylaunch.hotkey import format_hotkey, parse_hotkey, vk_from_qt_key
        mods = 0
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            mods |= 0x2      # MOD_CONTROL
        if event.modifiers() & Qt.KeyboardModifier.AltModifier:
            mods |= 0x1      # MOD_ALT
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            mods |= 0x4      # MOD_SHIFT
        if event.modifiers() & Qt.KeyboardModifier.MetaModifier:
            mods |= 0x8      # MOD_WIN
        spec = format_hotkey(mods, vk_from_qt_key(key))
        if parse_hotkey(spec) is None:
            self.tip.setText("需要至少一个修饰键：请按住 Ctrl / Alt / Shift / Win 再按键")
            return True
        self.spec = spec
        self.tip.setText(f"新快捷键：{spec}  （点确定保存）")
        self.ok.setEnabled(True)
        return True

    def done(self, result) -> None:  # noqa: N802
        QApplication.instance().removeEventFilter(self)
        resume = getattr(self._main, "resume_hotkeys", None)
        if callable(resume):
            try:
                resume()      # 幂等：写清单时的 refresh 可能已重注册插件热键
            except Exception:
                pass
        super().done(result)


class _NavButton(QPushButton):
    """侧边导航项：单击滚动定位（父类 clicked），双击发信号打开对应文件夹。

    QPushButton 没有 doubleClicked 信号，重写 mouseDoubleClickEvent 自己发。
    """
    double_clicked = Signal()

    def __init__(self, text: str) -> None:
        super().__init__(text)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)


class PartitionView(QDialog):
    """桌面分区视图（大窗口，非模态）。"""

    def __init__(self, api, hotkey_getter=None, hotkey_setter=None) -> None:
        super().__init__()
        self.api = api
        # 0.43：快捷键入口挪进插件本身；由 main._open_partition 注入读写回调
        self._hotkey_getter = hotkey_getter
        self._hotkey_setter = hotkey_setter
        self._groups: list[tuple[str, list[_Item]]] = []
        self._provider = QFileIconProvider()
        # 异步真实图标：路径 -> 已回填的 QPixmap；瓦片按路径索引以便回填
        self._pixmaps: dict[str, QPixmap] = {}
        self._tiles: dict[str, FileTile] = {}
        self._queued: set[str] = set()
        self._worker = _IconWorker()
        self._worker.loaded.connect(self._on_icon_loaded)
        self._worker.start()

        self.setWindowTitle("桌面净化")
        self.resize(1150, 730)
        self.setMinimumSize(760, 480)
        self.setStyleSheet(theme.DIALOG_QSS + """
QFrame#partitionCard {
    background: #FFFFFF;
    border: 1px solid #D8E3F5;
    border-radius: 12px;
}
QScrollArea { background: transparent; border: none; }
""")
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 16)
        root.setSpacing(12)
        from lylaunch.plugins.window_chrome import install_chrome

        install_chrome(self, "桌面分区", show_minmax=True, resizable=True)

        self._build_header(root)
        self._build_body(root)
        self._build_footer(root)

        # 窗口空白处右键：新建分组 / 刷新 / 收纳 / 恢复
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_window_menu)

        _DRAG_STATE["scroll"] = self.scroll
        # 接受从桌面/资源管理器拖进来的文件（drop 到哪张卡就进哪个分组）
        self.setAcceptDrops(True)
        self._wheel_filter = _WheelFilter()
        QApplication.instance().installEventFilter(self._wheel_filter)
        # 轮询兜底：拖拽中不依赖 mouseMove 事件也持续刷新 ghost / 高亮
        self._drag_timer = QTimer(self)
        self._drag_timer.setInterval(16)
        self._drag_timer.timeout.connect(self._drag_poll)
        self._drag_timer.start()

        # 拖拽排序的「插入位置」指示线（主程序同款：目标瓦片左缘一条蓝线）
        self._drop_line = QLabel(self)
        self._drop_line.setStyleSheet("background:#2F6BEE;border-radius:2px;")
        self._drop_line.setFixedSize(3, _TILE_H - 10)
        self._drop_line.hide()

        self.reload()

    # ---------- UI
    def _build_header(self, root: QVBoxLayout) -> None:
        head = QHBoxLayout()
        head.setSpacing(12)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        title = QLabel("桌面净化")
        title.setProperty("role", "title")
        self.subtitle = QLabel("一键把桌面收进分类文件夹 · 点「恢复原状」随时移回")
        self.subtitle.setProperty("role", "subtitle")
        texts.addWidget(title)
        texts.addWidget(self.subtitle)
        head.addLayout(texts, 1)

        # 0.43：这一行做成可横向收起/展开的导航条。
        # toolbar_body 装状态徽章 + 所有操作按钮；toolbar_toggle 控制显隐。
        self._toolbar_body = QWidget()
        body = QHBoxLayout(self._toolbar_body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)

        self.state_badge = QLabel("读取中")
        self.state_badge.setStyleSheet(
            "background:#F2F6FD;color:#2F6BEE;border-radius:9px;"
            "padding:3px 10px;font-size:11px;font-weight:600;"
        )
        body.addWidget(self.state_badge)

        # 0.43：快捷键设置入口（原先只在设置页 / 插件管理页）
        if self._hotkey_setter is not None:
            spec = (self._hotkey_getter() if self._hotkey_getter else "") or "—"
            self.hotkey_btn = QPushButton(f"快捷键 {spec}")
            self.hotkey_btn.setProperty("role", "soft")
            self.hotkey_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.hotkey_btn.setToolTip("点击后按下新组合键，立即生效（Esc 取消）")
            self.hotkey_btn.clicked.connect(self._on_edit_hotkey)
            body.addWidget(self.hotkey_btn)

        self.collect_btn = QPushButton("收纳桌面文件")
        self.collect_btn.setProperty("role", "primary")
        self.collect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collect_btn.setToolTip("把桌面上所有图标按类型收进「桌面净化」文件夹")
        self.collect_btn.clicked.connect(self._on_collect)
        body.addWidget(self.collect_btn)

        self.restore_btn = QPushButton("恢复原状")
        self.restore_btn.setProperty("role", "soft")
        self.restore_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.restore_btn.setToolTip("把收进文件夹的所有图标移回桌面原位")
        self.restore_btn.clicked.connect(self._on_restore)
        body.addWidget(self.restore_btn)

        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setProperty("role", "soft")
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.setToolTip(
            "重新扫描桌面：有新文件/快捷方式就自动收进「桌面净化」\n"
            "（右键「固定在桌面」的项不会被收走）")
        self.refresh_btn.clicked.connect(self._on_refresh)
        body.addWidget(self.refresh_btn)

        self.new_group_btn = QPushButton("新建分组")
        self.new_group_btn.setProperty("role", "soft")
        self.new_group_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_group_btn.setToolTip("在「桌面净化」文件夹里新建一个自定义分组，"
                                      "然后把瓦片拖进去")
        self.new_group_btn.clicked.connect(self._on_new_group)
        body.addWidget(self.new_group_btn)
        head.addWidget(self._toolbar_body, 1)

        # 收起/展开按钮：横向放在行尾；收起后 body 隐藏，只留这个按钮 + 标题
        self._toolbar_toggle = QPushButton("收起 ▴")
        self._toolbar_toggle.setProperty("role", "soft")
        self._toolbar_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toolbar_toggle.setToolTip("收起 / 展开操作导航条")
        self._toolbar_toggle.clicked.connect(self._toggle_toolbar)
        head.addWidget(self._toolbar_toggle)
        root.addLayout(head)
        self._set_toolbar_expanded(True)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索桌面文件…（空格分词）")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_search)
        row.addWidget(self.search, 1)
        self.count_label = QLabel("")
        self.count_label.setProperty("role", "status")
        row.addWidget(self.count_label)
        root.addLayout(row)

    # ---------- 顶部导航条收起 / 展开
    def _toggle_toolbar(self) -> None:
        self._set_toolbar_expanded(not getattr(self, "_toolbar_expanded", True))

    def _set_toolbar_expanded(self, expanded: bool) -> None:
        self._toolbar_expanded = bool(expanded)
        self._toolbar_body.setVisible(self._toolbar_expanded)
        self._toolbar_toggle.setText("收起 ▴" if self._toolbar_expanded else "展开 ▾")

    # ---------- 快捷键设置（0.43 从设置页挪进插件）
    def _on_edit_hotkey(self) -> None:
        if self._hotkey_setter is None:
            return
        dlg = _HotkeyCaptureDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.spec:
            return
        ok, msg = self._hotkey_setter(dlg.spec)
        if ok and hasattr(self, "hotkey_btn"):
            self.hotkey_btn.setText(f"快捷键 {dlg.spec}")
        # 复用底部状态栏反馈（字段名是 status，不是 status_bar）
        try:
            self.status.setText(msg)
        except Exception:
            pass

    def _build_body(self, root: QVBoxLayout) -> None:
        self.stack = QStackedWidget()

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.viewport().setStyleSheet("background:transparent;")
        self.board = QWidget()
        self.board.setStyleSheet("background:transparent;")
        self.board_layout = QGridLayout(self.board)
        self.board_layout.setContentsMargins(0, 0, 0, 480)   # 底部留白：最后一张卡也能顶格
        self.board_layout.setHorizontalSpacing(14)
        self.board_layout.setVerticalSpacing(14)
        for col in range(_CARD_COLS):
            self.board_layout.setColumnStretch(col, 1)
        self.scroll.setWidget(self.board)
        self.stack.addWidget(self.scroll)

        self.empty = QLabel("桌面上没有可显示的文件")
        self.empty.setProperty("role", "status")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.empty)

        # 左侧分组索引（参考文档站布局）：点击滚动到对应分组卡
        nav_widget = QWidget()
        nav_widget.setStyleSheet("background:transparent;")
        self._nav_lay = QVBoxLayout(nav_widget)
        self._nav_lay.setContentsMargins(0, 6, 0, 6)
        self._nav_lay.setSpacing(2)
        self._nav_lay.addStretch(1)
        nav_scroll = QScrollArea()
        nav_scroll.setWidgetResizable(True)
        nav_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        nav_scroll.setFixedWidth(190)
        nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        nav_scroll.setWidget(nav_widget)

        body = QHBoxLayout()
        body.setSpacing(18)
        body.addWidget(nav_scroll)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

    def _build_footer(self, root: QVBoxLayout) -> None:
        bar = QWidget()
        bar.setProperty("role", "statusBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 6, 0, 0)
        self.status = QLabel("双击打开 · 右键可在资源管理器中定位 · 未收纳时按类型虚拟分组")
        self.status.setProperty("role", "status")
        layout.addWidget(self.status, 1)
        root.addWidget(bar)

    # ---------- 数据
    def reload(self) -> None:
        self._sync_state()
        self._groups = collect_groups()
        self._apply_filter()
        self._last_signature = desktop_signature()

    def _refresh_main(self) -> None:
        """让主程序重建索引（桌面文件位置变了）。"""
        refresh = getattr(self.api, "refresh_index", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                pass

    def _ask_group_name(self) -> str | None:
        """自建输入对话框（按钮是中文，QInputDialog 的 OK/Cancel 改不了）。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("新建分组")
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        label = QLabel("分组名称（作为「桌面净化」里的文件夹名）：")
        edit = QLineEdit()
        layout.addWidget(label)
        layout.addWidget(edit)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        ok_btn = QPushButton("确定")
        ok_btn.setProperty("role", "primary")
        cancel_btn = QPushButton("取消")
        cancel_btn.setProperty("role", "soft")
        buttons.addWidget(ok_btn)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)
        edit.returnPressed.connect(dialog.accept)
        edit.setFocus()
        dialog.resize(380, 120)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        return edit.text().strip() if accepted else None

    def _on_refresh(self) -> None:
        """刷新 = 增量收纳：桌面上有新东西（白名单除外）就收进对应分组，再重扫视图。"""
        root = ops.desktop_dir()
        if not root:
            return
        # 「刷新」看着像只读操作，实际会移动新出现的桌面文件 → 必须确认（0.44）
        if not self._confirm_move(
                "确认刷新并收纳",
                "刷新会重新扫描桌面，并把新增的文件与快捷方式\n"
                "自动收进「桌面净化」文件夹（已固定的项不动）。\n\n确定继续吗？"):
            self.status.setText("已取消刷新")
            return
        _ok, message = ops.collect(root)   # 幂等：桌面上没有新项就原样返回
        self.status.setText(message)
        self._refresh_main()
        self.reload()

    def _on_new_group(self) -> None:
        """新建一个自定义分组（就是在收纳文件夹里建个目录）。"""
        root = ops.desktop_dir()
        if not root:
            self.status.setText("找不到桌面目录")
            return
        name = self._ask_group_name()
        if not name:
            return
        if name.casefold() in _SKIP_NAMES or os.sep in name:
            self.status.setText("分组名称不合法")
            return
        target = os.path.join(fences.container_dir(root), name)
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as exc:
            self.status.setText(f"创建失败：{exc}")
            return
        di.refresh_shell(target)
        self.status.setText(f"分组「{name}」已创建，把瓦片拖进卡片即可归类")
        self.reload()

    def _move_to_group(self, group: str, paths: list[str]) -> None:
        """拖放落点：把拖进来的真实文件移到目标分组文件夹，并同步标记。"""
        root = ops.desktop_dir()
        if not root:
            self.status.setText("找不到桌面目录")
            return
        container = fences.container_dir(root)
        dest_dir = os.path.join(container, group)
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            self.status.setText(f"无法创建分组文件夹：{exc}")
            return

        mark = di.read_mark()
        moved_records = list(mark.get("moved") or [])
        ok_n = fail_n = 0
        for src in paths:
            src = os.path.normpath(src)
            if os.path.isdir(src) and src.casefold() == container.casefold():
                continue                          # 容器本身不能拖进自己的分组
            # 用 normcase/normpath 规范化后再比，避免斜杠/大小写差异漏判
            if (os.path.normcase(os.path.dirname(src))
                    == os.path.normcase(os.path.normpath(dest_dir))):
                continue                          # 已经就在这个分组里，跳过（不再生成 (1)(2)）
            # 找到这条记录就保留最初来源（恢复时仍回桌面原位），只更新落点
            record = next(
                (m for m in moved_records
                 if str(m.get("to") or "").casefold() == src.casefold()), None)
            dst = fences.unique_path(os.path.join(dest_dir, os.path.basename(src)))
            try:
                shutil.move(src, dst)
            except Exception as exc:
                self.status.setText(f"移动失败：{os.path.basename(src)}（{exc}）")
                fail_n += 1
                continue
            di.notify_item_changed(src)
            di.notify_item_changed(dst)
            if record is not None:
                record["to"] = dst
                record["category"] = group
            else:
                moved_records.append(
                    {"from": src, "to": dst, "category": group})
            ok_n += 1

        di.write_mark(True, mode="fences", moved=moved_records,
                      folders=fences.managed_folders(root))
        di.refresh_shell(root, deep=True)
        self.status.setText(f"已移到「{group}」{ok_n} 项" +
                            (f"，{fail_n} 项失败" if fail_n else ""))
        self.reload()
        self._refresh_main()

    def _sync_state(self) -> None:
        """按标记更新操作条：未收纳只能「收纳」，已收纳只能「恢复」。"""
        root = ops.desktop_dir()
        count = ops.collected_count(root) if root else 0
        collected = count > 0
        self.collect_btn.setEnabled(not collected)
        self.restore_btn.setEnabled(collected)
        self.state_badge.setText(f"已收纳 {count} 项" if collected else "未收纳")
        self.state_badge.setStyleSheet(
            "background:#E8F0FE;color:#2F6BEE;border-radius:9px;"
            "padding:3px 10px;font-size:11px;font-weight:600;"
            if collected else
            "background:#E8F7EE;color:#1F7A45;border-radius:9px;"
            "padding:3px 10px;font-size:11px;font-weight:600;"
        )

    def _confirm_move(self, title: str, text: str) -> bool:
        """收纳 / 刷新都会真的移动桌面文件，统一在这里二次确认。

        用主程序的 theme.ask_confirm（自带浅色标题栏 + 项目配色），
        默认焦点在「取消」——误点工具栏不会把桌面文件挪走。
        """
        return theme.ask_confirm(
            self, title, text, default_yes=False,
            yes_text="继续", no_text="取消")

    def _on_collect(self) -> None:
        root = ops.desktop_dir()
        if not root:
            self.status.setText("找不到桌面目录")
            return
        if not self._confirm_move(
                "确认收纳桌面文件",
                "即将把桌面上的文件与快捷方式移动到「桌面净化」文件夹。\n"
                "已固定在桌面的项、白名单项和系统图标不会被移动；\n"
                "收纳后可在插件里点「恢复原状」还原。\n\n确定继续吗？"):
            self.status.setText("已取消收纳")
            return
        self.collect_btn.setEnabled(False)
        self.restore_btn.setEnabled(False)
        self.status.setText("正在收纳…")
        QApplication.processEvents()
        _ok, message = ops.collect(root)
        self.status.setText(message)
        # 0.43：收纳成功后自动把容器文件夹图标换成瓦片样式（不用再手动点右键）。
        # 图标失败不影响收纳结果，但**不再静默**：以前 except: pass 把真实原因吃掉，
        # 安装版看到的只是「黄色文件夹一动没动」，完全没法排查（0.44 修）。
        if _ok:
            try:
                import folder_icon as fi
                _icon_ok, icon_msg = fi.set_folder_icon()
                if not _icon_ok:
                    message = f"{message}（图标未替换：{icon_msg}）"
            except Exception as exc:
                message = f"{message}（图标未替换：{exc}）"
            self.status.setText(message)
        self._sync_state()
        self.reload()
        self._refresh_main()

    def _on_restore(self) -> None:
        root = ops.desktop_dir()
        if not root:
            self.status.setText("找不到桌面目录")
            return
        self.collect_btn.setEnabled(False)
        self.restore_btn.setEnabled(False)
        self.status.setText("正在恢复…")
        QApplication.processEvents()
        _ok, message = ops.restore(root)
        self.status.setText(message)
        self._sync_state()
        self.reload()
        self._refresh_main()

    def _show_window_menu(self, pos) -> None:
        """窗口空白处右键：新建分组 / 刷新 / 收纳 / 恢复。"""
        menu = QMenu(self)
        act_new = menu.addAction("新建分组")
        act_refresh = menu.addAction("刷新")
        menu.addSeparator()
        act_collect = menu.addAction("收纳桌面文件")
        act_restore = menu.addAction("恢复原状")
        menu.addSeparator()
        act_icon = menu.addAction("桌面文件夹换成瓦片图标")
        act_icon_restore = menu.addAction("还原文件夹默认图标")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is act_new:
            self._on_new_group()
        elif chosen is act_refresh:
            self.reload()
        elif chosen is act_collect:
            self._on_collect()
        elif chosen is act_restore:
            self._on_restore()
        elif chosen is act_icon:
            self._on_folder_icon(True)
        elif chosen is act_icon_restore:
            self._on_folder_icon(False)

    def _on_folder_icon(self, apply: bool) -> None:
        """桌面容器文件夹图标：换瓦片样式 / 还原系统默认。"""
        try:
            import folder_icon as fi
            ok, msg = fi.set_folder_icon() if apply else fi.restore_folder_icon()
            self.status.setText(msg)
            if ok and apply:
                self.reload()      # 图标变化后重建分组数据（不动文件）
        except Exception as exc:
            self.status.setText(f"操作失败：{exc}")

    def _on_search(self, _text: str) -> None:
        self._apply_filter()

    def _apply_filter(self) -> None:
        query = self.search.text().strip().casefold()
        tokens = [token for token in query.split() if token]

        total = 0
        shown_groups: list[tuple[str, list[_Item]]] = []
        for name, items in self._groups:
            if tokens:
                matched = [
                    item for item in items
                    if all(token in item.name.casefold() for token in tokens)
                ]
            else:
                matched = items
            total += len(matched)
            if matched:
                shown_groups.append((name, matched))

        self._clear_board()
        if not shown_groups:
            self.empty.setText(
                "没有找到匹配的文件" if tokens else "桌面上没有可显示的文件"
            )
            self.stack.setCurrentWidget(self.empty)
        else:
            self._cards = {}
            for index, (name, items) in enumerate(shown_groups):
                card = CategoryCard(name, items, self._icon_for,
                                    self._open, self._show_menu,
                                    on_tile=self._register_tile,
                                    on_drop=self._move_to_group,
                                    on_paste=self._paste_into)
                self._cards[name] = card
                self.board_layout.addWidget(card, index, 0, Qt.AlignmentFlag.AlignTop)
            self.board_layout.setRowStretch(len(shown_groups), 1)
            self.board_layout.setColumnStretch(0, 1)
            self.stack.setCurrentWidget(self.scroll)
            self._rebuild_nav(shown_groups)

        files = sum(len(items) for _n, items in self._groups)
        self.subtitle.setText(f"{len(self._groups)} 个分区 · {files} 项")
        self.count_label.setText(f"显示 {total} 项" if tokens else f"共 {files} 项")

    def _clear_board(self) -> None:
        while self.board_layout.count():
            item = self.board_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._tiles.clear()
        self._cards = {}
        for r in range(self.board_layout.rowCount()):
            self.board_layout.setRowStretch(r, 0)

    # ---------- 左侧分组索引
    def _rebuild_nav(self, shown_groups) -> None:
        while self._nav_lay.count():
            item = self._nav_lay.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for name, items in shown_groups:
            btn = _NavButton(name)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                "QPushButton{text-align:left;padding:8px 14px;border:none;"
                "border-radius:8px;background:transparent;color:#3D4657;"
                "font-size:12px;}"
                "QPushButton:hover{background:#EEF2F8;color:#161C28;}")
            btn.setToolTip(f"{len(items)} 项 · 单击定位 / 双击打开文件夹")
            btn.clicked.connect(lambda checked=False, n=name: self._scroll_to_group(n))
            btn.double_clicked.connect(
                lambda n=name: self._open_group_folder(n))
            self._nav_lay.addWidget(btn)
        self._nav_lay.addStretch(1)

    def _scroll_to_group(self, name: str) -> None:
        """把目标分组卡顶格滚动到视口顶部（ensureWidgetVisible 只保证可见，
        会停在视口中部 —— 用户要的是「点哪个，哪个顶到最上面」）。"""
        card = (getattr(self, "_cards", None) or {}).get(name)
        if card is None:
            return
        card_top = card.mapTo(self.board, QPoint(0, 0)).y()
        self.scroll.verticalScrollBar().setValue(max(0, card_top - 4))

    def _open_group_folder(self, name: str) -> None:
        """双击侧边导航：直接**进入**该分组对应的文件夹。

        ⚠️ 不能用 api.reveal()：它是 `explorer /select,<路径>`，对文件夹来说是
        「打开父目录并选中它」—— 用户看到的是「桌面净化」根目录，而不是分组本身。
        要进入文件夹必须 os.startfile（= api.launch）。
        """
        try:
            root = ops.desktop_dir()
            folder = os.path.join(fences.container_dir(root), name)
            if not os.path.isdir(folder):
                self.status.setText(f"「{name}」还没有对应的文件夹（未收纳）")
                return
            ok = self.api.launch(folder)
            self.status.setText(
                f"已打开「{name}」文件夹" if ok else f"打开失败：{folder}")
        except Exception as exc:
            self.status.setText(f"打开文件夹失败：{exc}")

    # ---------- 图标
    def _icon_for(self, item: _Item) -> QPixmap:
        """快速占位图标：Qt 系统图标同步给出，真实 256px 图标后台异步回填。

        尺寸照抄主程序 set_pixmap 的做法：36 逻辑像素 × devicePixelRatio
        取足物理像素再 setDevicePixelRatio，高 DPI 屏上才不会发虚。
        """
        key = item.path.casefold()
        cached = self._pixmaps.get(key)
        if cached is not None:
            return cached
        dpr = self.devicePixelRatioF() or 1.0
        target = max(1, int(round(_ICON * dpr)))
        try:
            icon = (self._provider.icon(QFileIconProvider.IconType.Folder)
                    if item.is_dir else self._provider.icon(QFileInfo(item.path)))
            pm = icon.pixmap(QSize(target, target))
        except Exception:
            pm = QPixmap()
        if pm is None or pm.isNull():
            style = self.style() or QApplication.style()
            pm = style.standardIcon(
                QStyle.StandardPixmap.SP_DirIcon if item.is_dir
                else QStyle.StandardPixmap.SP_FileIcon
            ).pixmap(QSize(target, target))
        pm.setDevicePixelRatio(dpr)
        self._pixmaps[key] = pm
        # 真实图标后台补
        if extract_icon_image is not None and key not in self._queued:
            self._queued.add(key)
            self._worker.enqueue(item.path)
        return pm

    def _register_tile(self, tile: "FileTile") -> None:
        self._tiles[tile._item.path.casefold()] = tile

    def _on_icon_loaded(self, path: str, img: QImage) -> None:
        key = path.casefold()
        dpr = self.devicePixelRatioF() or 1.0
        target = max(1, int(round(_ICON * dpr)))
        pm = QPixmap.fromImage(img).scaled(
            target, target, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        pm.setDevicePixelRatio(dpr)
        self._pixmaps[key] = pm
        tile = self._tiles.get(key)
        if tile is not None:
            tile.set_icon(pm)

    # ---------- 拖拽辅助
    def _card_at_global(self, gpos) -> "CategoryCard | None":
        """鼠标全局坐标下的分组卡（拖拽落点判定）。"""
        for i in range(self.board_layout.count()):
            widget = self.board_layout.itemAt(i).widget()
            if isinstance(widget, CategoryCard):
                top = widget.mapToGlobal(QPoint(0, 0))
                if QRect(top, widget.size()).contains(gpos):
                    return widget
        return None

    def _tile_at_global(self, gpos) -> "FileTile | None":
        """鼠标全局坐标下的瓦片（同分组排序落点判定）。"""
        for tile in self._tiles.values():
            if not tile.isVisible():
                continue
            top = tile.mapToGlobal(QPoint(0, 0))
            if QRect(top, tile.size()).contains(gpos):
                return tile
        return None

    def _set_drag_highlight(self, card) -> None:
        if getattr(self, "_hl_card", None) and self._hl_card is not card:
            self._hl_card.set_drag_highlight(False)
        if card is not None and card is not getattr(self, "_hl_card", None):
            card.set_drag_highlight(True)
        self._hl_card = card

    def _update_drop_indicator(self, tile) -> None:
        """显示 / 隐藏排序指示线：落在哪个瓦片前，线就画在它左缘。"""
        line = getattr(self, "_drop_line", None)
        if line is None:
            return
        if tile is None or not tile.isVisible():
            line.hide()
            return
        top = tile.mapTo(self.window(), QPoint(0, 0))
        line.setFixedHeight(tile.height() - 8)
        line.move(top.x() - 4, top.y() + 4)
        line.show()
        line.raise_()

    def _drag_poll(self) -> None:
        """拖拽中的轮询：不依赖 mouseMove 事件，滚轮滚动后高亮也能跟着刷；
        并检测鼠标左键松开（GetAsyncKeyState），跨窗口拖出时也能捕捉松手。"""
        tile = _DRAG_STATE.get("tile")
        if tile is None:
            return
        try:
            tile._update_drag(QCursor.pos())
        except RuntimeError:
            return
        # 左键已松开（grabMouse 在窗口外收不到 release，这里兜底）
        try:
            import ctypes
            if not (ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000):
                tile._finish_drag(QCursor.pos())
        except Exception:
            pass

    def _paste_into(self, group: str) -> None:
        """把剪贴板里的文件（资源管理器复制的也行）复制进指定分组。"""
        mime = QApplication.clipboard().mimeData()
        if not mime.hasUrls():
            self.status.setText("剪贴板里没有文件")
            return
        root = ops.desktop_dir()
        container = fences.container_dir(root)
        dest_dir = os.path.join(container, group)
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            self.status.setText(f"无法访问分组文件夹：{exc}")
            return
        ok_n = fail_n = 0
        for url in mime.urls():
            src = os.path.normpath(url.toLocalFile() or "")
            if not src or not os.path.exists(src):
                continue
            dst = fences.unique_path(os.path.join(dest_dir, os.path.basename(src)))
            try:
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
                di.notify_item_changed(dst)
                ok_n += 1
            except Exception as exc:
                self.status.setText(f"粘贴失败：{os.path.basename(src)}（{exc}）")
                fail_n += 1
        di.refresh_shell(root, deep=True)
        self.status.setText(f"已粘贴 {ok_n} 项到「{group}」"
                            + (f"，{fail_n} 项失败" if fail_n else ""))
        self.reload()
        self._refresh_main()

    # ---------- 同分组排序
    def _reorder(self, group: str, src_item: _Item, target_item: _Item) -> None:
        """同分组内拖动排序：把 src 插到 target 前面，顺序写进插件配置。

        键用 from_path（最初桌面位置）而不是显示名 —— 文件被恢复再收纳时
        文件名可能变（unique_path 加 (1)），路径键才跨周期稳定。
        """
        src_key = src_item.from_path
        target_key = target_item.from_path
        if src_key == target_key:
            return
        groups = dict(self._groups)
        default_seq = [it.from_path for it in (groups.get(group) or [])]
        order = _load_tile_order()
        seq = list(order.get(group) or [])
        # 已排序列 + 默认序列里没排过的项（保持相对顺序）= 完整序列
        seq = [n for n in (seq + [n for n in default_seq if n not in seq])
               if n in default_seq]
        seq = list(dict.fromkeys(seq))   # 去重（旧版本可能写入了重复项）
        if src_key not in seq or target_key not in seq:
            return
        seq.remove(src_key)
        seq.insert(seq.index(target_key), src_key)
        order[group] = seq
        # 显示名兜底表同步：按新顺序记录每个键对应的显示名
        name_map = {it.from_path: it.display_name for it in (groups.get(group) or [])}
        names = ops.load_plugin_data().get("tile_order_names") or {}
        names[group] = [name_map.get(k, "") for k in seq]
        _save_tile_order(order, names)
        self.reload()
        self.status.setText(f"已调整「{src_item.display_name}」的位置")

    # ---------- 接受外部文件拖入（从桌面 / 资源管理器拖进窗口）
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        mime = event.mimeData()
        if not mime.hasUrls():
            return
        paths = [os.path.normpath(u.toLocalFile())
                 for u in mime.urls() if u.toLocalFile()]
        paths = [p for p in paths if p]
        if not paths:
            return
        event.acceptProposedAction()
        pos = event.position().toPoint()
        card = self._card_at_global(self.mapToGlobal(pos))
        if card is not None:
            self._move_to_group(card._title, paths)     # 落在哪张卡就进哪个分组
        else:
            self._collect_paths(paths)                  # 落在空白：按类型收纳进容器
        self.reload()
        self._refresh_main()

    def _collect_paths(self, paths: list[str]) -> None:
        """只收纳拖进来的这几个桌面文件（不动桌面上的其他东西）。"""
        root = ops.desktop_dir()
        if not root:
            return
        container = fences.container_dir(root)
        try:
            os.makedirs(container, exist_ok=True)
        except OSError as exc:
            self.status.setText(f"无法创建收纳容器：{exc}")
            return
        moved, failed, _u = fences.move_files(paths, container)
        mark = di.read_mark()
        moved_records = list(mark.get("moved") or [])
        moved_records.extend(moved)
        di.write_mark(True, mode="fences", moved=moved_records,
                      folders=fences.managed_folders(root))
        di.arrange_desktop_icons()
        di.refresh_shell(root, deep=True)
        self.status.setText(f"已收纳 {len(moved)} 项"
                            + (f"，{len(failed)} 项失败" if failed else ""))

    # ---------- 动作
    def _open(self, item: _Item) -> None:
        try:
            if self.api.launch(item.path):
                self.status.setText(f"已打开：{item.name}")
                return
        except Exception:
            pass
        self.status.setText("没有默认打开方式 —— 可右键 → 在资源管理器中定位")

    def _show_menu(self, anchor: QWidget, item: _Item, pos) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#FFFFFF;border:1px solid #D8E3F5;border-radius:8px;padding:6px;}"
            "QMenu::item{padding:7px 22px;border-radius:6px;color:#161C28;font-size:12px;}"
            "QMenu::item:selected{background:#E4EDFC;color:#161C28;}"
            "QMenu::separator{height:1px;background:#EDF2FB;margin:4px 8px;}"
        )
        action_open = menu.addAction("打开")
        action_reveal = menu.addAction("在资源管理器中定位")
        menu.addSeparator()
        action_copy_file = menu.addAction("复制文件")     # 文件进剪贴板，可在资源管理器粘贴
        action_copy = menu.addAction("复制路径")
        action_trash = menu.addAction("移入回收站")
        menu.addSeparator()
        # 现在只做「移动」，不做隐藏：容器里的项可以单独挪回桌面
        action_to_desktop = menu.addAction("移到桌面") if item.in_bucket else None
        # 桌面上的项可以「固定」：进白名单，之后刷新/收纳都不会把它收进来
        whitelist = ops.load_whitelist()
        pinned = item.name in whitelist
        action_pin = (menu.addAction("取消固定（允许收纳）" if pinned
                                     else "固定在桌面（不收纳）")
                      if not item.in_bucket else None)
        chosen = menu.exec(anchor.mapToGlobal(pos))
        if chosen is action_open:
            self._open(item)
        elif chosen is action_reveal:
            try:
                self.api.reveal(item.path)
            except Exception:
                pass
        elif chosen is action_copy_file:
            from PySide6.QtCore import QMimeData, QUrl
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(item.path)])
            QApplication.clipboard().setMimeData(mime)
            self.status.setText("已复制文件，可在资源管理器中粘贴")
        elif chosen is action_copy:
            try:
                self.api.clipboard_write(item.path)
                self.status.setText("已复制路径")
            except Exception:
                pass
        elif chosen is action_trash:
            self._trash_item(item, anchor)
        elif action_to_desktop is not None and chosen is action_to_desktop:
            self._move_out_to_desktop(item, anchor)
        elif action_pin is not None and chosen is action_pin:
            if pinned:
                whitelist.discard(item.name)
                ops.save_whitelist(whitelist)
                self.status.setText(f"已取消固定「{item.name}」，下次收纳会收进分组")
            else:
                whitelist.add(item.name)
                ops.save_whitelist(whitelist)
                self.status.setText(f"已固定「{item.name}」，刷新/收纳时它会留在桌面")

    def _move_out_to_desktop(self, item: _Item, tile: QWidget) -> None:
        """把容器里的项单独移回桌面根（等于「在桌面显示」）。"""
        root = _desktop_dir()
        dest = fences.unique_path(os.path.join(root, item.name))
        try:
            shutil.move(item.path, dest)
        except Exception as exc:
            self.status.setText(f"移到桌面失败：{exc}")
            return

        # 从 moved 记录里剔除 —— 它已经不在容器里了
        mark = di.read_mark()
        moved = [m for m in (mark.get("moved") or [])
                 if str(m.get("to") or "").casefold() != item.path.casefold()]
        di.write_mark(True, mode="fences", moved=moved,
                      folders=mark.get("folders") or [],
                      hidden_paths=mark.get("hidden_paths") or [],
                      hidden_items=mark.get("hidden_items") or [])

        # 主动放到桌面 = 记入白名单：之后刷新/收纳都不会再把它收回来
        whitelist = ops.load_whitelist()
        final_name = os.path.basename(dest)
        whitelist.add(final_name)
        ops.save_whitelist(whitelist)

        item.path = dest
        item.name = final_name
        item.in_bucket = False
        item.hidden = False
        di.arrange_desktop_icons()
        try:
            tile.update()
        except RuntimeError:
            pass
        self.status.setText(f"已移到桌面：{final_name}（已自动固定，刷新不会再收走）")

    def _trash_item(self, item: _Item, tile: QWidget) -> None:
        """把项移入回收站（走 shell API，可在回收站里还原）。"""
        from win32com.shell import shell, shellcon
        try:
            result = shell.SHFileOperation((
                0, shellcon.FO_DELETE, item.path, None,
                shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION,
                None, None))
            aborted = bool(result[0] if isinstance(result, tuple) else result)
        except Exception as exc:
            self.status.setText(f"移入回收站失败：{exc}")
            return
        if aborted:
            self.status.setText("已取消删除")
            return
        # 从 moved 记录里剔除（它已经不在容器/桌面了）
        mark = di.read_mark()
        moved = [m for m in (mark.get("moved") or [])
                 if str(m.get("to") or "").casefold() != item.path.casefold()]
        di.write_mark(True, mode="fences", moved=moved,
                      folders=mark.get("folders") or [],
                      hidden_paths=mark.get("hidden_paths") or [],
                      hidden_items=mark.get("hidden_items") or [])
        root = _desktop_dir()
        di.refresh_shell(root, deep=True)
        self.status.setText(f"已移入回收站：{item.name}")
        self.reload()
        self._refresh_main()


    # ---------- 生命周期
    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self._worker.stop()
            self._worker.wait(500)
        except Exception:
            pass
        super().closeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.reload()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)
