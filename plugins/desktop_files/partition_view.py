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

import math
import os
import queue
import shutil
import time

from PySide6.QtCore import (
    QEvent, QPointF, QRect, QRectF, QFileInfo, QObject, QPoint, QSize, QTimer,
    QThread, Qt, Signal,
)
from PySide6.QtGui import (
    QBrush, QColor, QCursor, QIcon, QImage, QKeyEvent, QKeySequence, QLinearGradient,
    QPainter, QPainterPath, QPalette, QPen, QPixmap, QShortcut,
)
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
    import icon_cache
    import liquid_glass
    import ops
    import skin as skin_mod
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fences
    import desktop_icons as di
    import icon_cache
    import liquid_glass
    import ops
    import skin as skin_mod

_COLS = 7          # 每行瓦片数（单列宽卡片，右侧空间够放 7 个）

# 拖拽状态：手动拖拽期间瓦片捕获了鼠标，滚轮事件落在哪个控件不确定 ——
# 统一由应用级 _WheelFilter 接管转发；tile 引用给 QTimer 轮询用。
_DRAG_STATE: dict = {"scroll": None, "tile": None}
# 关闭窗口时图标线程若还卡在某个 shell 调用上，不能直接析构它（QThread 运行中被
# delete 会 abort 整个进程，用户看到的就是「卡死然后闪退」）——先托管在这儿。
_ORPHAN_WORKERS: list = []
# 尺寸 / 阈值 / 导航宽度全部搬到 skin.py：每套皮肤的瓦片大小、列数阈值不同
_TILE_GAP = 2            # 瓦片网格横向间距（与 CategoryCard 里的 setHorizontalSpacing 一致）

_SKIP_NAMES = {"desktop.ini", "thumbs.db"}
_SHORTCUT_SUFFIXES = (".lnk", ".url")


def _gear_icon(color: str, size: int = 16, alpha: float = 0.58) -> QIcon:
    """画一个齿轮图标（纯 QPainter，不依赖字体/系统图标，颜色跟皮肤墨色走）。

    为什么是「实心齿 + 中间挖洞」而不是细线齿轮：17px 下细线齿轮读起来像太阳/
    花，实心齿才一眼认得出是设置。alpha 0.66 —— 默认态不抢视线，hover 时按钮
    浮出淡底、图标自然被衬出来。
    """
    scale = 3.0                       # 3x 出图，缩到 17 逻辑像素不糊
    px = int(round(size * scale))
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.translate(px / 2.0, px / 2.0)
    c = QColor(color)
    c.setAlphaF(alpha)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(c)
    # 齿：8 个圆角小方块，绕中心旋转铺开（外缘到 r=8.4 / 17 的设计稿半径）
    for i in range(8):
        angle = i * 45.0
        p.save()
        p.rotate(angle)
        p.drawRoundedRect(
            QRectF(-1.35 * scale, -8.4 * scale, 2.7 * scale, 3.1 * scale),
            0.85 * scale, 0.85 * scale)
        p.restore()
    # 盘身 + 中间挖空的孔
    p.drawEllipse(QPointF(0, 0), 5.55 * scale, 5.55 * scale)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    p.drawEllipse(QPointF(0, 0), 2.55 * scale, 2.55 * scale)
    p.end()
    pm.setDevicePixelRatio(scale)
    return QIcon(pm)


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

# 分组色点：给每张卡片一个一眼能认出来的身份色（纯 QSS 画，不加资源）
_GROUP_COLORS = {
    "游戏": "#8B5CF6",
    "应用": "#2F6BEE",
    "文件夹": "#0EA5A4",
    "文档": "#3B82F6",
    "图片": "#06B6D4",
    "压缩包": "#E8A33D",
    "音视频": "#EC4899",
    "程序脚本": "#64748B",
    "其他": "#94A3B8",
}
_GROUP_COLOR_DEFAULT = "#2F6BEE"


def _group_color(name: str) -> str:
    return _GROUP_COLORS.get(name, _GROUP_COLOR_DEFAULT)


def _tint(hex_color: str, alpha: float) -> str:
    """'#2F6BEE' + 0.07 → 'rgba(47,107,238,0.07)'（QSS 只认 rgba，不认 #RRGGBBAA）。"""
    text = (hex_color or "").strip().lstrip("#")
    try:
        r, g, b = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except (ValueError, IndexError):
        r, g, b = 47, 107, 238
    return f"rgba({r},{g},{b},{max(0.0, min(1.0, float(alpha))):.3f})"


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


class _GlassDragFilter(QObject):
    """纯玻璃块的窗口拖动：把命中「空白」的按下事件交给回调处理。

    用真正的 QObject 子类（而不是给实例动态挂 eventFilter），PySide6 才会
    把 Python 重写接到 C++ 虚函数上。
    """

    def __init__(self, parent, callback) -> None:
        super().__init__(parent)
        self._callback = callback

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        return bool(self._callback(obj, event))


class FileTile(QWidget):
    """一个文件瓦片：图标 + 名称（双击打开，右键菜单，悬停高亮）。

    用 QLabel 承载图标而不是自绘 paint —— 自绘时 QIcon.paint 会按 icon 内部
    的小 pixmap 缩放，视觉上会碎成马赛克；QLabel 走 Qt 的静态绘制路径更快也更稳。

    尺寸与配色全部来自皮肤（skin）：经典 92×88 / 图标 36，玻璃 108×112 / 图标 56。
    """

    def __init__(self, item: _Item, pixmap: QPixmap | None, on_open, on_menu,
                 skin=None) -> None:
        super().__init__()
        self._skin = skin if skin is not None else skin_mod.get("classic")
        self._item = item
        self._on_open = on_open
        self._on_menu = on_menu
        self._drag_start = QPoint()
        self._drag_pending = False    # 左键按下待判定（必须初始化：右键划过也会走 mouseMove）
        self._dragging = False
        self._kb = False              # 键盘焦点态（搜索框 ↑↓ 选中）
        self.setFixedSize(self._skin.tile_w, self._skin.tile_h)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{item.name}\n{item.meta}\n{item.path}\n\n按住拖动可移到其他分组")
        self.setObjectName("fileTile")
        self.setProperty("kb", "false")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(self._skin.tile_qss())
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self._on_menu(self, self._item, pos)
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 7, 5, 4)
        layout.setSpacing(4)

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(self._skin.icon_size, self._skin.icon_size)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setStyleSheet("background:transparent;")
        # 图标/名字都是纯展示：必须让鼠标穿透，否则按下事件被 QLabel 吃掉，
        # FileTile 收不到 press → 拖不动瓦片、双击也不灵（用户反馈过）。
        self.icon_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        if pixmap is not None and not pixmap.isNull():
            self.icon_label.setPixmap(pixmap)
        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignHCenter)

        self.name_label = QLabel()
        self.name_label.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.name_label.setStyleSheet(self._skin.tile_name_qss())
        self.name_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.name_label, 0, Qt.AlignmentFlag.AlignHCenter)
        self._apply_name()

    def _apply_name(self) -> None:
        metrics = self.name_label.fontMetrics()
        elided = metrics.elidedText(
            self._item.display_name, Qt.TextElideMode.ElideMiddle,
            self._skin.tile_w - 12)
        self.name_label.setText(elided)

    def set_icon(self, pixmap: QPixmap) -> None:
        if pixmap is not None and not pixmap.isNull():
            self.icon_label.setPixmap(pixmap)

    def set_kb_focus(self, on: bool) -> None:
        """键盘焦点态（搜索框 ↑↓ 选中瓦片）。

        走动态属性 + repolish，而不是换整份 QSS —— 瓦片有 hover 态，
        直接 setStyleSheet 会把 :hover 一起覆盖掉，鼠标一划过就露馅。
        """
        if self._kb == bool(on):
            return
        self._kb = bool(on)
        self.setProperty("kb", "true" if self._kb else "false")
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.pos()
            self._drag_pending = True
        # 【关键】必须 accept：不 accept 的话按下事件会逐级冒泡到 board，
        # 应用级「空白拖窗口」过滤器会把 board 的这次按下当成空白 →
        # startSystemMove() 让 Windows 原生接管鼠标，瓦片就再也拖不动了
        # （用户反馈「拖瓦片变成拖整个窗口」就是这条冒泡链）。
        event.accept()

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
        self.setStyleSheet(self._skin.tile_drag_qss())
        _DRAG_STATE["tile"] = self          # 滚轮过滤器 / 轮询开始工作
        self._drag_win = self.window()
        self._drag_win._drag_active = True  # 全局轮询接管（跨窗口拖出靠它）
        self._update_drag(QCursor.pos())

    def _remove_shadow(self) -> None:
        self.setGraphicsEffect(None)
        self._shadow_effect = None
        self.setStyleSheet(self._skin.tile_qss())

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
                 on_tile=None, on_drop=None, on_paste=None,
                 cols: int = _COLS, rows: int = 2, skin=None) -> None:
        super().__init__()
        self._skin = skin if skin is not None else skin_mod.get("classic")
        self._title = title
        self._on_drop = on_drop
        self._on_paste = on_paste
        self._cols = max(1, int(cols))
        # rows <= 0 = 不限行数：全部瓦片一次铺开，不生成「展开 N 项」折叠按钮
        self._rows = int(rows)
        self._unlimited = self._rows <= 0
        self.setObjectName("partitionCard")
        self.setProperty("role", "card")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.setAcceptDrops(True)     # 接受从其他分组拖过来的瓦片
        # 卡片空白处右键：把剪贴板里的文件粘贴进这个分组
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_card_menu)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*self._skin.card_margins)
        layout.setSpacing(self._skin.card_spacing - 2)

        # 标题条：左侧分组色点 + 数量。经典皮肤铺一层分组色浅底，
        # 玻璃皮肤只留色点（标题条继续带底色会破坏玻璃围栏的清透感）。
        color = _group_color(title)
        head = QWidget()
        head.setObjectName("cardHead")
        # QWidget 子类要让 QSS 画出自身背景，必须显式开 WA_StyledBackground
        head.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        if self._skin.head_tint_alpha > 0:
            head.setStyleSheet(self._skin.card_head_qss(
                _tint(color, self._skin.head_tint_alpha)))
        else:
            head.setStyleSheet(self._skin.card_head_qss("transparent"))
        head_row = QHBoxLayout(head)
        head_row.setContentsMargins(9, 4, 4, 4)
        head_row.setSpacing(8)
        self._collapsed = False

        dot = QLabel()
        dot.setFixedSize(6, 6)
        dot.setStyleSheet(f"background:{color};border-radius:3px;")
        head_row.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)

        label = QLabel(title)
        label.setStyleSheet(
            "background:transparent;font-size:13px;font-weight:600;"
            f"color:{self._skin.head_label_color};")
        label.setCursor(Qt.CursorShape.PointingHandCursor)
        label.mousePressEvent = lambda _event: self._toggle_fold()   # 点标题也能折叠
        head_row.addWidget(label)
        head_row.addStretch(1)
        count = QLabel(f"{len(items)} 项" if items else "空")
        count.setStyleSheet(self._skin.count_qss)
        head_row.addWidget(count)
        self.fold_btn = QToolButton()
        self.fold_btn.setText("▾")
        self.fold_btn.setFixedSize(22, 22)
        self.fold_btn.setStyleSheet(self._skin.fold_btn_qss)
        self.fold_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fold_btn.setToolTip("折叠 / 展开这个分组")
        self.fold_btn.clicked.connect(self._toggle_fold)
        head_row.addWidget(self.fold_btn)
        layout.addWidget(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(_TILE_GAP)
        grid.setVerticalSpacing(4)
        self._grid = grid
        # 网格装进一个容器：折叠时隐藏容器（QGridLayout 本身没有 isVisible）
        body = QWidget()
        body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        body.setStyleSheet("background:transparent;")
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.addLayout(grid)
        if not items:
            # 空分组（多半是刚建的自定义分组）：给个落点提示，
            # 否则界面上只剩一条细标题条，用户不知道该往哪拖。
            placeholder = QLabel("把文件拖到这里")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet(self._skin.empty_qss())
            body_lay.addWidget(placeholder)
        self._body = body
        layout.addWidget(body)
        self._expanded = False
        self._total = len(items)
        # 默认铺几行：单列时两行（卡片本来就宽，一屏能看到 14 个）；
        # 双列时卡片被压窄，两行只剩 8 个，多数分组都得点「展开」—— 铺三行。
        visible_count = (len(items) if self._unlimited
                         else min(len(items), self._cols * self._rows))
        self._extra_tiles: list[FileTile] = []
        for index, item in enumerate(items):
            row, col = divmod(index, self._cols)
            tile = FileTile(item, icons(item), on_open, on_menu, skin=self._skin)
            tile._group_name = title
            if on_tile is not None:
                on_tile(tile)
            if index >= visible_count:
                tile.setVisible(False)
                self._extra_tiles.append(tile)
            grid.addWidget(tile, row, col)

        # 项数不足一行时，末尾补一根伸缩列，把多余宽度吸走 —— 否则
        # QGridLayout 会把 2~3 个瓦片平摊到整张卡宽，图标之间空出大片
        if 0 < self._total < self._cols:
            grid.setColumnStretch(self._cols, 1)

        if self._extra_tiles:
            self.exp_btn = QToolButton()
            self.exp_btn.setText(f"展开 {len(self._extra_tiles)} 项 ▾")
            self.exp_btn.setStyleSheet(self._skin.exp_btn_qss)
            self.exp_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.exp_btn.clicked.connect(self._toggle_expand)
            btn_row = QHBoxLayout()
            btn_row.addStretch(1)
            btn_row.addWidget(self.exp_btn)
            btn_row.addStretch(1)
            layout.addLayout(btn_row)

    # ---------- 玻璃围栏自绘
    def paintEvent(self, event) -> None:  # noqa: N802
        """玻璃皮肤下给卡片补一圈 iOS 液态玻璃边。

        QSS 只能给一条纯色边框，做不出「上亮下暗 + 内沿柔光」的厚度，所以边框
        叠一层自绘：1.2px 垂直渐变描边 + 上沿 48px 内柔光。先 super() 让 QSS
        背景先落地，再往上画，经典皮肤完全不受影响（直接 return）。
        """
        super().paintEvent(event)
        if not getattr(self._skin, "glass", False):
            return
        # 纯玻璃块：分组卡不再画自己的框，图标直接裸放在面板上（拖拽时由
        # set_drag_highlight 临时给一层高亮当落点提示，松手即恢复隐形）
        if getattr(self._skin, "paged", False) and not self.property("dragHover"):
            return
        w, h = self.width(), self.height()
        if w <= 4 or h <= 4:
            return
        radius = float(self._skin.card_radius)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.7, 0.7, w - 1.4, h - 1.4), radius, radius)

        p.setBrush(Qt.BrushStyle.NoBrush)
        edge = QLinearGradient(0, 0, 0, h)
        # 极淡的细边：只勾出卡片轮廓，不再叠一层白光把壁纸洗掉
        edge.setColorAt(0.0, QColor(255, 255, 255, 72))
        edge.setColorAt(0.22, QColor(255, 255, 255, 34))
        edge.setColorAt(0.70, QColor(255, 255, 255, 12))
        edge.setColorAt(1.0, QColor(255, 255, 255, 8))
        p.setPen(QPen(QBrush(edge), 1.0))
        p.drawPath(path)

        p.save()
        p.setClipPath(path)
        glow_h = min(40.0, h / 2)
        band = QLinearGradient(0, 0, 0, glow_h)
        band.setColorAt(0.0, QColor(255, 255, 255, 16))
        band.setColorAt(0.45, QColor(255, 255, 255, 4))
        band.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(QRectF(0, 0, w, glow_h), band)
        p.restore()
        p.end()

    # ---------- 折叠 / 展开
    def tiles(self) -> list["FileTile"]:
        """按网格顺序返回当前可见的瓦片（键盘导航与「回车打开第一项」用）。"""
        out: list[FileTile] = []
        grid = getattr(self, "_grid", None)
        if grid is None:
            return out
        for index in range(grid.count()):
            widget = grid.itemAt(index).widget()
            # 用 isHidden() 而不是 isVisible()：窗口还没 show 时子控件一律
            # isVisible()=False，但「用户主动收起」的瓦片才该排除。
            if isinstance(widget, FileTile) and not widget.isHidden():
                out.append(widget)
        return out

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
        """拖拽悬停高亮：只换底色/描边，配色跟皮肤走。"""
        self.setProperty("dragHover", bool(on))
        self.setStyleSheet(self._skin.card_drag_qss() if on else "")
        self.update()

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
    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 卡片内的按下不往上传：一旦冒泡到 board，「空白拖窗口」过滤器就会
        # startSystemMove()，卡片里的瓦片和标题都会变成"拖窗口"。
        event.accept()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet(self._skin.card_drag_qss())

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
                # 64px 就够（瓦片图标 32 逻辑像素 × 最多 2.5 倍 DPI），
                # 而且 64px 走的是快路径：18.7ms/张 → 3.4ms/张。
                img = extract_icon_image(path, 64)
                if img is not None and not img.isNull():
                    icon_cache.save(path, img)      # 在后台线程写盘，不占 UI 线程
                    self.loaded.emit(path, img)
            except Exception:
                pass


class _IconPrewarm(QThread):
    """插件加载后在后台把桌面图标烤进缓存，第一次开面板也不用等白纸回填。

    只在缓存 miss 时提取；整体限时 12s，摸到断网快捷方式也不会一直挂着。
    """

    def run(self) -> None:  # noqa: D102
        if extract_icon_image is None:
            return
        try:
            paths = [it.path for _title, group in collect_groups() for it in group]
        except Exception:
            return
        deadline = time.time() + 12.0
        for path in paths:
            if time.time() > deadline:
                break
            try:
                if icon_cache.load(path) is not None:
                    continue
                img = extract_icon_image(path, 64)
                if img is not None and not img.isNull():
                    icon_cache.save(path, img)
            except Exception:
                continue


_PREWARM: dict = {"started": False, "worker": None}


def prewarm_icon_cache(delay_ms: int = 1500) -> None:
    """幂等：插件加载时调一次，延迟一点起后台线程（别抢启动时的资源）。"""
    if _PREWARM["started"]:
        return
    _PREWARM["started"] = True

    def _start() -> None:
        try:
            worker = _IconPrewarm()
            _PREWARM["worker"] = worker      # 留引用：QThread 运行中被回收会 abort
            worker.start()
        except Exception:
            pass

    QTimer.singleShot(max(0, int(delay_ms)), _start)


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
        # 没有主程序图标提取器时的兜底队列：显示后每次换 3 张，别压住打开路径
        self._fill_queue: list[_Item] = []
        self._real_filled: set[str] = set()
        self._fill_timer = QTimer(self)
        self._fill_timer.setInterval(25)
        self._fill_timer.timeout.connect(self._fill_real_icons)
        # 0.2.3：背景毛玻璃改成 DWM 实时合成，不再需要抓屏定时器 / 缓存大图。
        # 重建重入保护：重建期间若又被要求重建（搜索/尺寸变化撞在一起），
        # 只记一个标记，等这一轮结束再补一次，绝不嵌套重建
        self._rebuilding = False
        self._rebuild_again = False
        self._last_signature: str | None = None
        self._worker = _IconWorker()
        self._worker.loaded.connect(self._on_icon_loaded)
        self._worker.start()

        self._skin = skin_mod.current(api)
        self._on_open_settings = None      # 由 main 注入：打开插件内设置页
        # 分页（玻璃皮肤）：一组一页，翻页只切可见分组，不动任何文件
        self._page_index = 0
        self._page_total = 1
        self.setWindowTitle("桌面净化")
        self.resize(1150, 730)
        self.setMinimumSize(760, 480)
        self._restore_geometry()
        if self._skin.glass:
            # 玻璃面板只有 15% 白：字色必须跟着底色走，深壁纸白字、浅壁纸深字。
            # 此刻窗口还没显示，抓到的就是真实的背后画面。
            self._skin = skin_mod.glass_for_luminance(self._backdrop_luminance())
            self._skin_radius = liquid_glass.RADIUS
        self._shadow = None
        self.setStyleSheet(theme.DIALOG_QSS + self._skin.window_qss)
        root = QVBoxLayout(self)
        root.setContentsMargins(*self._skin.window_margins)
        root.setSpacing(12)
        # 液态玻璃：自绘层必须比 chrome 先装 —— Qt 的事件过滤器是后装先跑，
        # 这样 chrome 的浅色底先画、玻璃层随后覆盖，纯透明才不会被白底顶掉。
        if self._skin.glass:
            self._glass = liquid_glass.install_liquid_glass(self)
            # 磨砂强度来自设置页滑杆（0~100，默认 100 = 最磨砂）
            self._glass.set_level(self._glass_level() / 100.0)
        from lylaunch.plugins.window_chrome import install_chrome

        install_chrome(self, "桌面分区", show_minmax=True,
                       # 纯玻璃块不要右下角的 QSizeGrip 把手（那几个小方块很突兀）；
                       # 面板尺寸按 window_geometry 固定，不再拉伸。经典皮肤保留。
                       resizable=not self._skin.glass,
                       bg_alpha=self._skin.bg_alpha,
                       frosted=self._skin.frosted,
                       title_bar=not self._skin.glass)
        if self._skin.glass:
            # 纯玻璃块没有标题栏：拖动 / 关闭由面板自己接管
            self._install_glass_chrome()
            # 柔和投影单独一层窗口，在面板底下；不能画在面板里（15% 白会被染脏）
            self._shadow = liquid_glass.install_soft_shadow(self)

        self._build_header(root)
        self._build_body(root)
        if self._skin.glass:
            self._build_glass_close_btn()
        self._build_footer(root)

        # 搜索防抖（120ms）：每敲一个字就重建全部卡片代价太大 —— 200 项以上
        # 会明显卡顿，瓦片图标也会跟着闪。
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(120)
        self._search_timer.timeout.connect(self._apply_filter)

        # 键盘导航：搜索框里 ↑↓ 在瓦片间移动，回车打开当前项
        self._kb_tiles: list[FileTile] = []
        self._kb_index = -1
        self._cards: dict[str, CategoryCard] = {}
        self._shown_groups: list[tuple[str, list[_Item]]] = []
        # 布局参数（卡片列数 / 每卡瓦片列数），_apply_filter 里按窗口宽度重算
        self._card_cols = 1
        self._tile_cols = _COLS
        self._layout_state = (self._card_cols, self._tile_cols)
        # 宽度跨阈值 / 拖拽改大小时重建卡片，但不要每像素都重建一次
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.setInterval(180)
        self._layout_timer.timeout.connect(self._apply_layout_if_needed)
        self.search.installEventFilter(self)
        # Ctrl+F 聚焦搜索框（和大多数列表工具一致）
        self._find_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        self._find_shortcut.activated.connect(self._focus_search)

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
        self._drop_line.setFixedSize(3, self._skin.tile_h - 10)
        self._drop_line.hide()

        self.reload()

    # ---------- UI
    def _build_header(self, root: QVBoxLayout) -> None:
        """标题区。经典皮肤 = 标题 + 工具条 + 搜索行；玻璃皮肤 = 搜索胶囊 + 设置。

        两套皮肤都会把操作按钮造出来（`_sync_state` 要靠它们启用/禁用），
        玻璃皮肤只是不排进布局 —— 功能入口统一收进设置页。
        """
        skin = self._skin
        head = QHBoxLayout()
        head.setSpacing(12)

        if skin.show_title:
            texts = QVBoxLayout()
            texts.setSpacing(2)
            title = QLabel("桌面净化")
            title.setProperty("role", "title")
            self.subtitle = QLabel("一键把桌面收进分类文件夹 · 点「恢复原状」随时移回")
            self.subtitle.setProperty("role", "subtitle")
            texts.addWidget(title)
            texts.addWidget(self.subtitle)
            head.addLayout(texts)
        else:
            # 玻璃皮肤：窗口标题栏已经有名字，这里只留一个隐藏的落点，
            # `_apply_filter` 继续往里写「N 个分区 · M 项」
            self.subtitle = QLabel("")
            self.subtitle.setVisible(False)

        self._toolbar_body = self._build_toolbar()
        if skin.shows_toolbar:
            head.addWidget(self._toolbar_body, 1)
            self._toolbar_toggle = QPushButton("收起 ▴")
            self._toolbar_toggle.setProperty("role", "soft")
            self._toolbar_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
            self._toolbar_toggle.setToolTip("收起 / 展开操作导航条")
            self._toolbar_toggle.clicked.connect(self._toggle_toolbar)
            head.addWidget(self._toolbar_toggle)
        else:
            self._toolbar_body.setVisible(False)
            self._toolbar_toggle = None

        if skin.search_in_header:
            # 搜索框控件始终创建（过滤 / 键盘导航 / Ctrl+F 都依赖它），
            # 但 skin.show_search=False 时只建不排、不显示（玻璃皮肤顶部只要设置）。
            self._build_search_box()
            if getattr(skin, "show_search", True):
                # GlassDesk 的搜索胶囊只占左上一条（上限 440px，不铺满整行）
                self.search.setMaximumWidth(440)
                head.addWidget(self.search, 1)
            else:
                self.search.setVisible(False)
            self.count_label = QLabel("")
            self.count_label.setProperty("role", "status")
            # 没有搜索框时，统计文字挪到最左边 —— 否则左侧整片空白、所有元素
            # 挤在右端，视觉失衡。
            if getattr(skin, "show_search", True):
                head.addStretch(1)
                head.addWidget(self.count_label)
            else:
                head.insertWidget(0, self.count_label)
                head.addStretch(1)

        # 玻璃皮肤：一组一页的翻页控件（图2 右上「第 1 页」+ 左右箭头）
        if skin.paged:
            self._build_pager(head)

        if skin.glass:
            # 玻璃皮肤：右上角一个极简齿轮，和 × 同尺寸的「幽灵按钮」——
            # 平时没有底也没有边，hover 才浮出一块淡淡的底。原来的「设置」
            # 文字按钮带描边+白底，压在纯玻璃上很扎眼（用户反馈过）。
            self.settings_btn = QToolButton()
            self.settings_btn.setProperty("role", "glassIcon")
            self.settings_btn.setFixedSize(24, 24)
            # 14px / alpha 0.55：和右上角那个细 × 的视觉重量最接近（16px 实心
            # 齿轮在浅玻璃上明显更重，会比 × 抢视线 —— 用户原话「太突兀」）。
            self.settings_btn.setIcon(_gear_icon(skin.title_color, 14, 0.55))
            self.settings_btn.setIconSize(QSize(14, 14))
            self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.settings_btn.setToolTip(
                "设置：皮肤 / 磨砂强度 / 收纳 / 桌面图标 / 快捷键")
            self.settings_btn.clicked.connect(self._on_settings_clicked)
            head.addWidget(self.settings_btn, 0, Qt.AlignmentFlag.AlignVCenter)
            # × 是绝对定位浮在右上角的（x = 右 18 + 24 + 2），留出 32px 让两个
            # 图标之间保持 6px 间距，且不和 × 叠在一起。
            head.addSpacing(32)
        else:
            self.settings_btn = QPushButton("设置")
            self.settings_btn.setProperty(
                "role", "soft" if skin.shows_toolbar else "glass")
            self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.settings_btn.setToolTip("打开设置：皮肤 / 收纳 / 桌面图标 / 快捷键")
            self.settings_btn.clicked.connect(self._on_settings_clicked)
            head.addWidget(self.settings_btn)
        root.addLayout(head)

        if skin.shows_toolbar:
            self._set_toolbar_expanded(True)
        if not skin.search_in_header:
            row = QHBoxLayout()
            row.setSpacing(10)
            self._build_search_box()
            row.addWidget(self.search, 1)
            self.count_label = QLabel("")
            self.count_label.setProperty("role", "status")
            row.addWidget(self.count_label)
            root.addLayout(row)

    def _build_toolbar(self) -> QWidget:
        """工具条：状态徽章 + 快捷键 + 收纳 / 恢复 / 重扫 / 新建分组。"""
        bar = QWidget()
        body = QHBoxLayout(bar)
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

        self.refresh_btn = QPushButton("重扫")
        self.refresh_btn.setProperty("role", "soft")
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.setToolTip(
            "只重新扫描桌面、刷新视图，不移动任何文件\n"
            "（要把新增项收进分组，请用「收纳桌面文件」）")
        self.refresh_btn.clicked.connect(self._on_rescan)
        body.addWidget(self.refresh_btn)

        self.new_group_btn = QPushButton("新建分组")
        self.new_group_btn.setProperty("role", "soft")
        self.new_group_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_group_btn.setToolTip("在「桌面净化」文件夹里新建一个自定义分组，"
                                      "然后把瓦片拖进去")
        self.new_group_btn.clicked.connect(self._on_new_group)
        body.addWidget(self.new_group_btn)
        body.addStretch(1)
        return bar

    def _build_search_box(self) -> None:
        """搜索框：玻璃皮肤走 #searchBox 胶囊样式，经典沿用主题默认输入框。"""
        self.search = QLineEdit()
        self.search.setObjectName("searchBox")
        self.search.setPlaceholderText(
            "搜索桌面文件…（空格分词）" if self._skin.shows_toolbar else "搜索")
        self.search.setClearButtonEnabled(True)
        if self._skin.search_qss:
            self.search.setStyleSheet(self._skin.search_qss)
        # QSS 管不到 placeholder 颜色，用调色板补
        palette = self.search.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText,
                         QColor(self._skin.search_placeholder_color))
        self.search.setPalette(palette)
        self.search.textChanged.connect(self._on_search)
        self.search.returnPressed.connect(self._open_kb_target)

    # ---------- 纯玻璃块：拖动与关闭（标题栏已去掉）
    def _install_glass_chrome(self) -> None:
        """没有标题栏后，按住面板「空白」区域拖动窗口。

        判断落点：点到瓦片 / 按钮 / 输入控件 / 卡片标题时不接管，避免抢走点击、
        拖拽排序和文本编辑；其余空白（含搜索行空处、卡片之间的空隙）才拖动。
        """
        self._drag_handle = _GlassDragFilter(self, self._glass_drag_filter)
        QApplication.instance().installEventFilter(self._drag_handle)
        # 拖动过滤器随窗口销毁一并移除，避免全局过滤器泄漏
        self.destroyed.connect(
            lambda *_: QApplication.instance().removeEventFilter(self._drag_handle))

    def _glass_drag_filter(self, obj, event) -> bool:  # noqa: N802
        if event.type() != QEvent.Type.MouseButtonPress:
            return False
        if obj is self or not isinstance(obj, QWidget):
            return False
        # 【关键】只接管**本面板这个窗口**里的按下事件。
        # 这个过滤器装在 QApplication 上（应用级），设置窗、其他插件窗口的点击
        # 同样会流到这里；以前不判窗口，设置页里按一下（列表/滑杆/卡片/复选框）
        # 就被当成"面板空白"拉去 startSystemMove，事件还被 return True 吃掉 ——
        # 表现就是"面板和设置一起开着时，设置页点不动"。
        if obj.window() is not self:
            return False
        # 【关键】几何兜底：按下点落在任何卡片（= 瓦片区域）里就绝不拖窗口。
        # 光靠控件类型判断不够 —— 瓦片/卡片里未处理的按下会沿父链冒泡到 board，
        # 而 board 在下面的白名单里，于是「拖瓦片」变成「拖窗口」（用户反馈的 bug）。
        gpos = event.globalPosition().toPoint()
        card = self._card_at_global(gpos)
        if card is not None:
            return False
        # 只接管面板自身/滚动区视口/卡片外的空白；子控件各有自己的交互
        if obj is not self and obj not in (
                getattr(self, "scroll", None), getattr(self, "board", None),
                getattr(self, "stack", None)):
            # 有自己交互的控件不接管
            if isinstance(obj, (QPushButton, QToolButton, QLineEdit)):
                return False
            # 瓦片 / 卡片**及其所有子控件**（图标和名字都是 QLabel！）都不接管，
            # 否则在图标上按住会变成拖窗口，瓦片就再也拖不动了。
            # 反过来，顶部的「共 84 项」这类独立 QLabel 要允许拖 —— 它就是标题栏。
            ancestor = obj
            while ancestor is not None:
                if isinstance(ancestor, (FileTile, CategoryCard)):
                    return False
                ancestor = ancestor.parentWidget()
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        # 卡片下方的空隙（滚动区里、卡片之间）允许拖窗口
        handle = self.windowHandle()
        if handle is not None:
            handle.startSystemMove()
            return True
        return False

    def _build_glass_close_btn(self) -> None:
        """极小的半透明关闭按钮（悬浮时才明显），替代标题栏的 ×。

        和设置齿轮同款「幽灵按钮」：平时只有一个 62% 墨色的 ×，hover 才浮出淡底，
        这样右上角两个图标重量一致，不会一个带描边一个不带。
        """
        btn = QToolButton(self)
        btn.setText("×")
        btn.setProperty("role", "glassIcon")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip("关闭面板（也可按 Esc / 再按一次快捷键）")
        btn.setFixedSize(24, 24)
        btn.clicked.connect(self.close)
        btn.raise_()
        # 放到搜索行右端上方：按当前布局 margins 定位，窗口缩放时同步
        self._glass_close_btn = btn
        self._position_glass_close_btn()

    def _position_glass_close_btn(self) -> None:
        btn = getattr(self, "_glass_close_btn", None)
        if btn is None:
            return
        m = self._skin.window_margins
        btn.move(self.width() - m[2] - btn.width() - 2, m[1] - 4)

    # ---------- 分页（玻璃皮肤，一组一页）
    def _build_pager(self, head: QHBoxLayout) -> None:
        """在标题行塞入 ‹ 第 N/M 页 ›；按钮禁用态由 _sync_pager 控制。"""
        self.prev_page_btn = QToolButton()
        self.prev_page_btn.setText("‹")
        self.prev_page_btn.setProperty("role", "page")
        self.prev_page_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.prev_page_btn.setToolTip("上一组（← / PageUp）")
        self.prev_page_btn.clicked.connect(lambda: self._goto_page(self._page_index - 1))

        self.page_label = QLabel("第 1 页")
        self.page_label.setProperty("role", "page")
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_label.setMinimumWidth(64)

        self.next_page_btn = QToolButton()
        self.next_page_btn.setText("›")
        self.next_page_btn.setProperty("role", "page")
        self.next_page_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.next_page_btn.setToolTip("下一组（→ / PageDown）")
        self.next_page_btn.clicked.connect(lambda: self._goto_page(self._page_index + 1))

        head.addWidget(self.prev_page_btn)
        head.addWidget(self.page_label)
        head.addWidget(self.next_page_btn)

    def _sync_pager(self) -> None:
        """翻页后刷新页码与按钮可用态（只有一页时两个箭头都灰掉）。"""
        if not getattr(self._skin, "paged", False):
            return
        self.page_label.setText(
            f"第 {self._page_index + 1}/{self._page_total} 页")
        if getattr(self, "bottom_page", None) is not None:
            self.bottom_page.setText(f"{self._page_index + 1}/{self._page_total}")
        self.prev_page_btn.setEnabled(self._page_total > 1 and self._page_index > 0)
        self.next_page_btn.setEnabled(
            self._page_total > 1 and self._page_index < self._page_total - 1)

    def _goto_page(self, index: int) -> None:
        """切到指定页（0-based）；只重建卡片，不改分组数据/文件。"""
        if not getattr(self._skin, "paged", False):
            return
        target = max(0, min(index, self._page_total - 1))
        if target == self._page_index:
            return
        self._page_index = target
        self._apply_filter()
        self._sync_pager()

    def _on_settings_clicked(self) -> None:
        if callable(self._on_open_settings):
            self._on_open_settings()
        else:
            self.status.setText("设置页未挂载")

    # ---------- 顶部导航条收起 / 展开
    def _toggle_toolbar(self) -> None:
        self._set_toolbar_expanded(not getattr(self, "_toolbar_expanded", True))

    def _set_toolbar_expanded(self, expanded: bool) -> None:
        self._toolbar_expanded = bool(expanded)
        if getattr(self, "_toolbar_body", None) is not None:
            self._toolbar_body.setVisible(self._toolbar_expanded
                                          and self._skin.shows_toolbar)
        if getattr(self, "_toolbar_toggle", None) is not None:
            self._toolbar_toggle.setText(
                "收起 ▴" if self._toolbar_expanded else "展开 ▾")

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
        skin = self._skin
        self.stack = QStackedWidget()

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.viewport().setStyleSheet("background:transparent;")
        if getattr(skin, "glass", False):
            # 纯玻璃块不要可见滚动条（那根竖线会破坏干净观感，主题样式还会盖掉
            # 皮肤里的 QScrollBar 规则变成一条蓝杠）；滚轮照样能滚。
            self.scroll.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.board = QWidget()
        self.board.setStyleSheet("background:transparent;")
        self.board_layout = QGridLayout(self.board)
        self.board_layout.setContentsMargins(0, 0, 0, 480)   # 底部留白：最后一张卡也能顶格
        self.board_layout.setHorizontalSpacing(skin.card_spacing)
        self.board_layout.setVerticalSpacing(0)
        for col in range(skin.card_cols_wide):
            self.board_layout.setColumnStretch(col, 1)
        # 卡片不是直接塞进网格，而是分列装进竖直容器（瀑布流）：
        # 网格里同一行的两张卡会被强行拉成同高，短卡下面空一大片；
        # 各列自己往下堆就不会有洞。
        self._columns: list[QWidget] = []
        self._column_layouts: list[QVBoxLayout] = []
        self.scroll.setWidget(self.board)
        self.stack.addWidget(self.scroll)

        self.empty = QLabel("桌面上没有可显示的文件")
        self.empty.setProperty("role", "status")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.empty)

        body = QHBoxLayout()
        body.setSpacing(18)
        # 玻璃皮肤走 GlassDesk 式整片围栏网格，没有左侧索引列
        self._nav_lay = None
        self._nav_active = None
        if skin.shows_nav:
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
            nav_scroll.setFixedWidth(skin.nav_width)
            nav_scroll.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            nav_scroll.setWidget(nav_widget)
            body.addWidget(nav_scroll)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

    def _build_footer(self, root: QVBoxLayout) -> None:
        bar = QWidget()
        bar.setProperty("role", "statusBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 6, 0, 0)
        self.status = QLabel("" if self._skin.glass else
                             "双击打开 · 右键可在资源管理器中定位 · 未收纳时按类型虚拟分组")
        self.status.setProperty("role", "status")
        layout.addWidget(self.status, 1)
        # 分页皮肤：底部居中的「N/M」页码（目标图底部那行），status 仍留左侧反馈
        if getattr(self._skin, "paged", False):
            self.bottom_page = QLabel("1/1")
            self.bottom_page.setProperty("role", "page")
            self.bottom_page.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(self.bottom_page)
        root.addWidget(bar)

    # ---------- 数据
    def reload(self) -> None:
        self._sync_state()
        self._groups = collect_groups()
        self._apply_filter()
        self._last_signature = desktop_signature()

    # ---------- 自适应布局 / 窗口几何
    def _metrics(self) -> tuple[int, int]:
        """按当前窗口宽度算 (卡片列数, 每张卡片的瓦片列数)。

        窗口 ≥1100 双列（单列时 1150 宽的右半边整块是空的）；瓦片列数由卡片
        实际可用宽度反推，保证既不空一大条、也不挤到换行。
        """
        skin = self._skin
        width = max(self.width(), 760)
        # 卡片区域真正能用的宽度：扣掉左右外边距、左侧导航和它与内容之间的 18px
        content = (width - skin.window_margins[0] - skin.window_margins[2]
                   - skin.nav_width - (18 if skin.nav_width else 0))
        # 分页皮肤一组一页：卡片独占整行，瓦片按整块内容宽度铺开
        if getattr(skin, "paged", False):
            card_cols = 1
        else:
            # 【关键】双列判定必须用**卡片区域宽度**，不能用窗口宽度。
            # 经典皮肤左侧有 190px 导航：按窗口宽度算，1150 这种最常见的
            # 尺寸就会分两列，扣掉导航后每张卡只剩 4 个瓦片 —— 又窄又挤，
            # 侧边导航跳转也会落到一个"通道半空"的位置（用户反馈的回归）。
            # 改成 content ≥ 阈值才双列：1150 单列 8 瓦片，约 1370 以上才双列。
            card_cols = skin.card_cols_wide if content >= skin.wide_threshold else 1
        card_width = (content - skin.card_spacing * (card_cols - 1)) / card_cols
        usable = (card_width - skin.card_margins[0] - skin.card_margins[2] - 12)
        tiles = int((usable + _TILE_GAP) // (skin.tile_w + _TILE_GAP))
        # 分页皮肤用皮肤自带的列数上限（疏朗），经典保持 12
        cap = getattr(skin, "tile_cols_max", 12)
        return card_cols, max(2, min(cap, tiles))

    def _rows_per_card(self) -> int:
        """每组铺几行瓦片（超出会折叠成「展开 N 项」）。

        - 玻璃皮肤：**0 = 不限行数**，所有瓦片一次铺开，没有折叠按钮
        - 经典 / 双列：沿用原口径（双列卡片窄，铺 3 行才不至于一点就展开）
        """
        if getattr(self._skin, "glass", False) and self._card_cols == 1:
            return 0
        return 3 if self._card_cols > 1 else 2

    def _apply_layout_if_needed(self) -> None:
        """宽度跨过/跌破阈值时才重建卡片（普通拖动只重启计时器，不重建）。"""
        if self._metrics() != self._layout_state:
            self._apply_filter()

    def _backdrop_luminance(self) -> float:
        """抓窗口身后那块屏的平均亮度（0~1）。

        构造期窗口还没显示，`grabWindow` 拿到的就是真实的壁纸/桌面。玻璃皮肤
        只有 15% 白，同一个面板在深色和浅色壁纸下必须换墨色，否则总有一边看不清。
        抓不到就返回 0（按深色处理，白字更抗噪）。
        """
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return 0.0
        rect = self.geometry()
        try:
            shot = screen.grabWindow(0, rect.x(), rect.y(), rect.width(), rect.height())
        except Exception:
            return 0.0
        if shot is None or shot.isNull():
            return 0.0
        img = shot.toImage().scaled(
            24, 24, Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        total = 0.0
        count = 0
        for y in range(img.height()):
            for x in range(img.width()):
                c = img.pixelColor(x, y)
                total += (0.2126 * c.redF() + 0.7152 * c.greenF()
                          + 0.0722 * c.blueF())
                count += 1
        return (total / count) if count else 0.0

    def _restore_geometry(self) -> None:
        """恢复上次关闭时的位置与尺寸；数据损坏或整窗跑到屏幕外就退回默认居中。"""
        saved = None
        getter = getattr(self.api, "settings_get", None)
        if callable(getter):
            try:
                saved = getter("window_geometry", None)
            except Exception:
                saved = None
        if isinstance(saved, (list, tuple)) and len(saved) == 4:
            try:
                x, y, w, h = (int(v) for v in saved)
            except (TypeError, ValueError):
                w = 0
            if w >= 760 and h >= 480:
                rect = QRect(x, y, w, h)
                if any(screen.availableGeometry().intersects(rect)
                       for screen in QApplication.screens()):
                    self.setGeometry(rect)
                    return
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.center() - self.rect().center())

    def _save_geometry(self) -> None:
        setter = getattr(self.api, "settings_set", None)
        if not callable(setter):
            return
        try:
            geo = self.normalGeometry() if self.isMaximized() else self.geometry()
            setter("window_geometry", [geo.x(), geo.y(), geo.width(), geo.height()])
        except Exception:
            pass

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

    def _on_rescan(self) -> None:
        """只读刷新：重扫桌面、重建视图，**绝不移动任何文件**。

        以前这颗按钮叫「刷新」，实际做的是增量收纳（真移动文件），而右键菜单
        里同名的那项只做重扫 —— 同一个词两种后果，很容易误点。
        现在拆开：「收纳桌面文件」负责写，「重扫」只负责读。
        """
        self.reload()
        self.status.setText(f"已重新扫描 · {len(self._groups)} 个分区")

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
        if self._skin.glass:
            self.state_badge.setStyleSheet(
                "background:rgba(255,255,255,0.10);color:#FFFFFF;border-radius:11px;"
                "padding:4px 12px;font-size:11px;font-weight:600;")
        else:
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
        """窗口空白处右键：新建分组 / 重扫 / 收纳 / 恢复 / 隐藏图标。"""
        menu = QMenu(self)
        act_new = menu.addAction("新建分组")
        act_rescan = menu.addAction("重新扫描（不动文件）")
        menu.addSeparator()
        act_collect = menu.addAction("收纳桌面文件")
        act_restore = menu.addAction("恢复原状")
        menu.addSeparator()
        icons_hidden = di.icons_visible() is False
        act_icons = menu.addAction("恢复桌面图标" if icons_hidden
                                   else "隐藏桌面图标（退出自动恢复）")
        menu.addSeparator()
        act_icon = menu.addAction("桌面文件夹换成瓦片图标")
        act_icon_restore = menu.addAction("还原文件夹默认图标")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is act_new:
            self._on_new_group()
        elif chosen is act_rescan:
            self._on_rescan()
        elif chosen is act_collect:
            self._on_collect()
        elif chosen is act_restore:
            self._on_restore()
        elif chosen is act_icons:
            self._on_toggle_icons(not icons_hidden)
        elif chosen is act_icon:
            self._on_folder_icon(True)
        elif chosen is act_icon_restore:
            self._on_folder_icon(False)

    def _on_toggle_icons(self, hide: bool) -> None:
        """隐藏 / 恢复全部桌面图标（原来是独立小窗里的开关，现在收进右键菜单）。

        只动注册表 + 发翻转命令，不动任何文件；状态记在独立标记里，
        和收纳账本（moved）互不干扰 —— 退出程序会自动恢复。
        """
        allow = False
        try:
            allow = bool(self.api.settings_get("allow_showwindow_fallback", False))
        except Exception:
            allow = False
        # 切换要走 Win32 并回读校验，最坏几百毫秒：先把状态栏文案刷出去再阻塞。
        # 这里只能 repaint()，绝不能用 processEvents() —— 那会在菜单回调里再
        # 派发一轮事件，可能把重建/热键/菜单操作重新卷进来（重入卡死）。
        self.status.setText("正在切换桌面图标…")
        self.status.repaint()
        ok, message = ops.set_desktop_icons_hidden(hide, allow_fallback=allow)
        self.status.setText(message)
        if not ok:
            theme.show_warning(self, "桌面净化", message)

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
        # 搜索是新的结果集：回到第一页，否则匹配项落在别的页上会「看起来没搜到」
        if getattr(self._skin, "paged", False):
            self._page_index = 0
        self._search_timer.start()

    def _focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    # ---------- 键盘导航
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """搜索框内的键盘导航：↑↓ 移动瓦片焦点，Esc 先清空再交给窗口关闭。"""
        if obj is self.search and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self._move_kb_cursor(1 if key == Qt.Key.Key_Down else -1)
                return True
            if key == Qt.Key.Key_Escape and self.search.text():
                self.search.clear()
                return True
            return False
        return super().eventFilter(obj, event)

    def _rebuild_kb_tiles(self) -> None:
        """按当前卡片/展开状态重建键盘导航序列（卡片展开后要能继续 ↓ 过去）。"""
        tiles: list[FileTile] = []
        for name, _items in getattr(self, "_shown_groups", []):
            card = self._cards.get(name)
            if card is not None:
                tiles.extend(card.tiles())
        self._kb_tiles = tiles

    def _flush_pending_search(self) -> None:
        """防抖还没落地时先立刻应用一次，否则 ↑↓ / 回车会按上一次结果走。"""
        if self._search_timer.isActive():
            self._search_timer.stop()
            self._apply_filter()

    def _move_kb_cursor(self, step: int) -> None:
        self._flush_pending_search()
        self._rebuild_kb_tiles()
        if not self._kb_tiles:
            self.status.setText("没有可选择的项")
            return
        if self._kb_index < 0:
            index = 0 if step > 0 else len(self._kb_tiles) - 1
        else:
            index = (self._kb_index + step) % len(self._kb_tiles)
        self._set_kb_index(index)

    def _set_kb_index(self, index: int) -> None:
        if not self._kb_tiles:
            return
        index = max(0, min(index, len(self._kb_tiles) - 1))
        previous = (self._kb_tiles[self._kb_index]
                    if 0 <= self._kb_index < len(self._kb_tiles) else None)
        if previous is not None and previous is not self._kb_tiles[index]:
            previous.set_kb_focus(False)
        self._kb_index = index
        tile = self._kb_tiles[index]
        tile.set_kb_focus(True)
        self.scroll.ensureWidgetVisible(tile, 0, 24)
        self.status.setText(f"{tile._item.display_name} · 回车打开")

    def _open_kb_target(self) -> None:
        """回车：打开键盘选中的瓦片；还没选就打开第一个。"""
        self._flush_pending_search()
        self._rebuild_kb_tiles()
        if not self._kb_tiles:
            return
        tile = (self._kb_tiles[self._kb_index]
                if 0 <= self._kb_index < len(self._kb_tiles)
                else self._kb_tiles[0])
        self._open(tile._item)

    def _apply_filter(self) -> None:
        """重建卡片的唯一入口（带重入保护）。

        搜索防抖、尺寸变化、右键菜单动作可能在同一轮事件里前后脚触发重建；
        嵌套重建会一边 deleteLater 一边往同一个 layout 里加控件，轻则闪烁，
        重则卡在布局里出不来 —— 所以撞上时只记标记，等这一轮跑完再补。
        """
        if self._rebuilding:
            self._rebuild_again = True
            return
        self._rebuilding = True
        try:
            self._apply_filter_body()
        finally:
            self._rebuilding = False
            if self._rebuild_again:
                self._rebuild_again = False
                QTimer.singleShot(0, self._apply_filter)

    def _apply_filter_body(self) -> None:
        query = self.search.text().strip().casefold()
        tokens = [token for token in query.split() if token]

        # 布局参数按当前宽度算（窗口拉宽/最大化时卡片可能从 1 列变 2 列）
        self._card_cols, self._tile_cols = self._metrics()
        self._layout_state = (self._card_cols, self._tile_cols)

        total = 0
        shown_groups: list[tuple[str, list[_Item]]] = []
        for name, items in self._groups:
            if tokens:
                matched = [
                    item for item in items
                    if all(token in item.name.casefold() for token in tokens)
                ]
                if matched:
                    total += len(matched)
                    shown_groups.append((name, matched))
            else:
                # 没搜索时把空分组也铺出来：刚建的自定义分组要能看到、
                # 能当拖放落点，而不是从界面上凭空消失。
                total += len(items)
                shown_groups.append((name, items))

        # 分页（玻璃皮肤）：一组一页。搜索结果也按组分页，页码随之重算；
        # 切页只改可见分组，_groups / 文件都在内存里，不碰磁盘。
        if getattr(self._skin, "paged", False):
            self._page_total = max(1, len(shown_groups))
            self._page_index = max(0, min(self._page_index, self._page_total - 1))
            shown_groups = shown_groups[self._page_index:self._page_index + 1]

        # 重建期间关重绘：卡片是「上一批 deleteLater + 这一批 new」，不关会看到
        # 中间态闪一下；顺带保留滚动位置，过滤的观感是"筛掉"而不是"重开"。
        bar = self.scroll.verticalScrollBar()
        offset = bar.value() if self.stack.currentWidget() is self.scroll else 0
        self.board.setUpdatesEnabled(False)
        try:
            self._clear_board()
            if not shown_groups:
                self.empty.setText(
                    "没有找到匹配的文件" if tokens else "桌面上没有可显示的文件"
                )
                self.stack.setCurrentWidget(self.empty)
            else:
                self._ensure_columns(self._card_cols)
                for index, (name, items) in enumerate(shown_groups):
                    card = CategoryCard(name, items, self._icon_for,
                                        self._open, self._show_menu,
                                        on_tile=self._register_tile,
                                        on_drop=self._move_to_group,
                                        on_paste=self._paste_into,
                                        cols=self._tile_cols,
                                        rows=self._rows_per_card(),
                                        skin=self._skin)
                    self._cards[name] = card
                    lay = self._column_layouts[index % self._card_cols]
                    lay.insertWidget(lay.count() - 1, card)   # stretch 之前
                self.stack.setCurrentWidget(self.scroll)
                self._rebuild_nav(shown_groups)
            # 键盘导航顺序 = 卡片顺序 + 卡片内网格顺序（只收展开中的瓦片）
            self._shown_groups = shown_groups
            self._rebuild_kb_tiles()
            self._kb_index = -1
            bar.setValue(min(offset, bar.maximum()))
        finally:
            self.board.setUpdatesEnabled(True)

        files = sum(len(items) for _n, items in self._groups)
        self.subtitle.setText(f"{len(self._groups)} 个分区 · {files} 项")
        if getattr(self._skin, "paged", False):
            self._sync_pager()
            self.count_label.setText(
                f"找到 {total} 项" if tokens else f"共 {self._page_total} 组 · {files} 项")
        else:
            self.count_label.setText(f"显示 {total} 项" if tokens else f"共 {files} 项")

    def _ensure_columns(self, count: int) -> None:
        """保证有 count 个竖直列容器（瀑布流列），返回前把多出来的删掉。"""
        count = max(1, int(count))
        if len(self._columns) == count:
            return
        while self._columns:
            widget = self._columns.pop()
            self._column_layouts.pop()
            self.board_layout.removeWidget(widget)
            widget.deleteLater()
        spacing = self._skin.card_spacing
        for col in range(count):
            box = QWidget()
            box.setStyleSheet("background:transparent;")
            box.setSizePolicy(QSizePolicy.Policy.Preferred,
                              QSizePolicy.Policy.Maximum)
            lay = QVBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(spacing)
            lay.addStretch(1)
            self.board_layout.addWidget(box, 0, col, Qt.AlignmentFlag.AlignTop)
            self._columns.append(box)
            self._column_layouts.append(lay)
            self.board_layout.setColumnStretch(col, 1)

    def _clear_board(self) -> None:
        # 只清卡片，列容器留着复用（重建时不再闪一下列宽变化）
        for lay in self._column_layouts:
            while lay.count() > 1:          # 末尾那根 stretch 不动
                item = lay.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
        self._tiles.clear()
        self._cards = {}

    # ---------- 左侧分组索引
    def _rebuild_nav(self, shown_groups) -> None:
        if getattr(self, "_nav_lay", None) is None:
            return                       # 玻璃皮肤没有左侧索引，直接跳过
        while self._nav_lay.count():
            item = self._nav_lay.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        active = getattr(self, "_nav_active", None)
        names = [name for name, _ in shown_groups]
        if active not in names:          # 分组被搜掉 / 改名后旧高亮作废
            active = None
            self._nav_active = None
        for name, items in shown_groups:
            btn = _NavButton(name)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(self._skin.nav_btn_qss)
            btn.setToolTip(f"{len(items)} 项 · 单击定位 / 双击打开文件夹")
            # 单选高亮：点过哪个分组，列表里就停在哪一项，否则点完完全看不出
            # 自己停在哪（导航"不好用"的观感来源之一）。
            btn.setCheckable(True)
            btn.setChecked(name == active)
            btn.clicked.connect(lambda checked=False, n=name: self._scroll_to_group(n))
            btn.double_clicked.connect(
                lambda n=name: self._open_group_folder(n))
            self._nav_lay.addWidget(btn)
        self._nav_lay.addStretch(1)

    def _sync_nav_active(self, name: str | None) -> None:
        """把侧边导航的选中态同步到 name（没有就全部取消）。"""
        self._nav_active = name
        lay = getattr(self, "_nav_lay", None)
        if lay is None:
            return
        for i in range(lay.count()):
            btn = lay.itemAt(i).widget()
            if btn is not None:
                btn.setChecked(btn.text() == name)


    def _scroll_to_group(self, name: str) -> None:
        """把目标分组卡顶格滚动到视口顶部（ensureWidgetVisible 只保证可见，
        会停在视口中部 —— 用户要的是「点哪个，哪个顶到最上面」）。"""
        card = (getattr(self, "_cards", None) or {}).get(name)
        if card is None:
            return
        card_top = card.mapTo(self.board, QPoint(0, 0)).y()
        self.scroll.verticalScrollBar().setValue(max(0, card_top - 4))
        self._sync_nav_active(name)

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
        """占位图标：只用 Qt 内置标准图标，**这里绝不碰 shell**。

        QFileIconProvider.icon() 底下是 SHGetFileInfo，单次 5–30ms；桌面上若
        有指向断网共享盘 / 离线 OneDrive 的 .lnk，这个调用会一直阻塞（几秒到
        几十秒）。而这段代码跑在 UI 线程的打开路径里（构造 → reload → 每个瓦片
        都要取一次图标），表现就是「按 Alt+D 之后整个程序卡住、键盘毫无响应」。
        所以打开时一律给标准图标，真实 256px 图标交给后台线程一张张回填。

        尺寸照抄主程序 set_pixmap 的做法：36 逻辑像素 × devicePixelRatio
        取足物理像素再 setDevicePixelRatio，高 DPI 屏上才不会发虚。
        """
        key = item.path.casefold()
        cached = self._pixmaps.get(key)
        if cached is not None:
            return cached
        dpr = self.devicePixelRatioF() or 1.0
        # 先看磁盘缓存：命中就完全不碰 shell，也不会闪一下白纸占位
        cached_img = icon_cache.load(item.path)
        if cached_img is not None:
            pm = self._icon_pixmap(cached_img, dpr)
            if pm is not None:
                self._pixmaps[key] = pm
                self._real_filled.add(key)
                return pm
        pm = self._placeholder_pixmap(item.is_dir, dpr)
        self._pixmaps[key] = pm
        # 真实图标后台补：有主程序的提取器就用它，否则排进打开之后再慢慢换的队列
        if key not in self._queued:
            self._queued.add(key)
            if extract_icon_image is not None:
                self._worker.enqueue(item.path)
            else:
                self._fill_queue.append(item)
                if self._fill_queue and not self._fill_timer.isActive():
                    self._fill_timer.start()
        return pm

    def _fill_real_icons(self) -> None:
        """没有主程序图标提取器时的兜底：窗口显示完之后，每次换 3 张。

        仍然绕不开 QFileIconProvider 会碰 shell，但已经移出打开路径、并且
        分片执行 —— 最坏情况只是图标一张张慢慢变清晰，不会把窗口锁死。
        """
        batch = self._fill_queue[:3]
        del self._fill_queue[:3]
        if not self.isVisible():      # 窗口收起来了就先不做，别偷偷吃 CPU
            return
        dpr = self.devicePixelRatioF() or 1.0
        target = max(1, int(round(self._skin.icon_size * dpr)))
        for item in batch:
            key = item.path.casefold()
            if key in self._real_filled:
                continue
            self._real_filled.add(key)
            try:
                icon = (self._provider.icon(QFileIconProvider.IconType.Folder)
                        if item.is_dir else self._provider.icon(QFileInfo(item.path)))
                pm = icon.pixmap(QSize(target, target))
            except Exception:
                continue
            if pm is None or pm.isNull():
                continue
            pm.setDevicePixelRatio(dpr)
            self._pixmaps[key] = pm
            tile = self._tiles.get(key)
            if tile is not None:
                tile.set_icon(pm)
        if not self._fill_queue:
            self._fill_timer.stop()

    def _placeholder_pixmap(self, is_dir: bool, dpr: float) -> QPixmap:
        """文件和文件夹各只做一张占位图标，所有瓦片共用。

        QStyle.standardIcon() 单次就要 ~30ms（Windows 样式会现做一张图标），
        一个桌面 80 项就是 2 秒多 —— 必须缓存成模板，不能每个瓦片现取一张。
        """
        attr = "_std_dir_pm" if is_dir else "_std_file_pm"
        pm = getattr(self, attr, None)
        if pm is not None and abs(pm.devicePixelRatio() - dpr) < 0.01:
            return pm
        target = max(1, int(round(self._skin.icon_size * dpr)))
        style = self.style() or QApplication.style()
        pm = style.standardIcon(
            QStyle.StandardPixmap.SP_DirIcon if is_dir
            else QStyle.StandardPixmap.SP_FileIcon
        ).pixmap(QSize(target, target))
        pm.setDevicePixelRatio(dpr)
        setattr(self, attr, pm)
        return pm

    def _register_tile(self, tile: "FileTile") -> None:
        self._tiles[tile._item.path.casefold()] = tile

    def _on_icon_loaded(self, path: str, img: QImage) -> None:
        key = path.casefold()
        pm = self._icon_pixmap(img, self.devicePixelRatioF() or 1.0)
        if pm is None:
            return
        self._pixmaps[key] = pm
        self._real_filled.add(key)
        tile = self._tiles.get(key)
        if tile is not None:
            tile.set_icon(pm)

    def _icon_pixmap(self, img: QImage, dpr: float) -> QPixmap | None:
        """缓存的原始图 / 后台线程取到的图 → 瓦片要的尺寸（含 devicePixelRatio）。"""
        if img is None or img.isNull():
            return None
        target = max(1, int(round(self._skin.icon_size * dpr)))
        pm = QPixmap.fromImage(img).scaled(
            target, target, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        if pm.isNull():
            return None
        pm.setDevicePixelRatio(dpr)
        return pm

    # ---------- 拖拽辅助
    def _card_at_global(self, gpos) -> "CategoryCard | None":
        """鼠标全局坐标下的分组卡（拖拽落点判定）。"""
        for card in self._cards.values():
            if not card.isVisible():
                continue
            top = card.mapToGlobal(QPoint(0, 0))
            if QRect(top, card.size()).contains(gpos):
                return card
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
        # 分页皮肤下一页只有一组，没法直接拖到别的组 —— 用右键子菜单兜底跨组移动
        cross_actions = {}
        if getattr(self._skin, "paged", False) and len(self._groups) > 1:
            sub = menu.addMenu("移到分组")
            for group_name, _group_items in self._groups:
                if group_name == item.group:
                    continue
                act = sub.addAction(group_name)
                cross_actions[act] = group_name
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
        elif chosen in cross_actions:
            target = cross_actions[chosen]
            self._move_to_group(target, [item.path])
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
        self._save_geometry()
        self._fill_timer.stop()
        # 图标缓存超上限时按访问时间清旧的：放到关窗之后再扫，别拖慢关闭
        QTimer.singleShot(0, icon_cache.trim)
        if self._shadow is not None:
            shadow = self._shadow
            try:
                shadow.close()
            except Exception:
                pass
            # 兜底：独立顶层阴影窗口若没被 close 掉，延迟再 hide 一次
            QTimer.singleShot(0, lambda: shadow.hide() if shadow.isVisible() else None)
            self._shadow = None
        try:
            self._worker.stop()
            # 图标线程可能正卡在某个 .lnk 的 shell 解析上，等不到就别硬等：
            # 交给 _ORPHAN_WORKERS 托管，让它自己收尾，避免「运行中的 QThread
            # 被析构」把整个进程 abort 掉。
            if not self._worker.wait(500):
                worker = self._worker
                _ORPHAN_WORKERS.append(worker)
                # 等它自然跑完再放手：此刻线程已结束，析构才安全。
                # （不能连 deleteLater 一起用：deferred delete 和 Python 侧引用
                #   归零可能撞在一起，反而变成「运行中被销毁」）
                worker.finished.connect(
                    lambda: _ORPHAN_WORKERS.remove(worker)
                    if worker in _ORPHAN_WORKERS else None)
        except Exception:
            pass
        super().closeEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        # Alt+D 再按一次只是 hide（窗口不销毁），几何要在隐藏时落盘，
        # 否则「关掉窗口」和「重启程序」两种场景会丢掉尺寸。
        self._save_geometry()
        if self._shadow is not None:
            self._shadow.hide()
            # 双保险：某些 Z 序/事件顺序下独立顶层窗口的 hide 会被随后的
            # raise/activate 抢回来，下一轮事件再强制 hide 一次，杜绝残影。
            shadow = self._shadow
            QTimer.singleShot(0, lambda: shadow.hide() if shadow.isVisible() else None)
        super().hideEvent(event)

    def moveEvent(self, event) -> None:  # noqa: N802
        if getattr(self, "_shadow", None) is not None:
            self._shadow.sync()      # 投影层跟着面板走（拖窗口时逐帧跟随）
        super().moveEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._position_glass_close_btn()
        if getattr(self, "_shadow", None) is not None:
            self._shadow.sync()
        if getattr(self, "_layout_timer", None) is not None:
            self._layout_timer.start()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 系统模糊必须在原生句柄存在之后（show 之后）设，且每次 show 都要重设：
        # DWM 偶尔会在窗口重新显隐后丢掉 composition attribute。
        self.apply_backdrop_blur()
        if self._shadow is not None:
            self._shadow.sync()
            self._shadow.show()      # 先显示投影，再把面板抬到它上面
            self.raise_()
        # 桌面内容没变就只刷操作条状态，不再整版重建。
        # 原来这里无条件 reload()，每次按 Alt+D 都要把几十个瓦片连同图标重造一遍，
        # 是「开窗慢、有时候像卡死」的直接来源之一。
        try:
            signature = desktop_signature()
        except Exception:
            signature = None
        if self._last_signature is None or signature != self._last_signature:
            self.reload()
        else:
            self._sync_state()

    def _glass_level(self) -> int:
        """磨砂强度 0~100（默认 100 = 最磨砂）。读不到配置就按满档。"""
        try:
            value = int(self.api.settings_get("glass_level", 100))
        except Exception:
            value = 100
        return max(0, min(100, value))

    def _blur_threshold(self) -> int:
        """低于这个强度就关系统模糊，变成"清玻璃"（完全透明）。"""
        return int(liquid_glass.GlassBackdrop._CLEAR_BELOW * 100)

    def apply_glass_level(self, level: int | None = None) -> bool:
        """磨砂强度：数值越小 → 白雾越淡 → 到阈值以下连模糊都关掉，走向全透明。

        - 100~阈值：系统模糊开，白雾 0.34 → 0.13 线性
        - 阈值~0：系统模糊关，白雾按强度线性淡出到 0（只剩描边，壁纸原样透出）
        返回系统模糊当前是否生效（False = 处于清玻璃档）。
        """
        glass = getattr(self, "_glass", None)
        if not self._skin.glass or glass is None:
            return False
        lv = self._glass_level() if level is None else int(level)
        lv = max(0, min(100, lv))
        glass.set_level(lv / 100.0)
        wants_blur = lv >= self._blur_threshold()
        ok = True
        if self.isVisible():
            ok = liquid_glass.apply_blur(self, wants_blur)
        glass.set_blur_active(bool(wants_blur and ok))
        self.update()
        return bool(wants_blur and ok)

    def apply_backdrop_blur(self, enabled: bool | None = None) -> bool:
        """窗口每次显示时同步一次：模糊开/关 + 磨砂强度（参数保留但恒为开）。

        背景模糊交给 DWM 实时合成（acrylic，AccentFlags=0，不染色），本层只叠
        奶白雾；强度滑杆拉到底时会主动关掉模糊变成全透明清玻璃。
        """
        return self.apply_glass_level()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        # 分页翻页：焦点在搜索框时左右键留给光标移动，只在非输入态响应
        if getattr(self._skin, "paged", False) and self.search.hasFocus():
            super().keyPressEvent(event)
            return
        if getattr(self._skin, "paged", False) and event.key() in (
                Qt.Key.Key_Left, Qt.Key.Key_PageUp):
            self._goto_page(self._page_index - 1)
            return
        if getattr(self._skin, "paged", False) and event.key() in (
                Qt.Key.Key_Right, Qt.Key.Key_PageDown):
            self._goto_page(self._page_index + 1)
            return
        super().keyPressEvent(event)
