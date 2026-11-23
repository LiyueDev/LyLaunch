"""主窗口（对应 C# 的 MainWindow.xaml.cs）。

功能：无边框/置顶/失焦隐藏浮窗；搜索框 + 三区块图标网格（收藏应用/应用/系统应用）；
全局热键唤出；剪贴板历史；真实图标后台加载；键盘操作（↑↓←→/Tab/Enter/Esc）；
右键固定/打开文件位置；右键窗口空白处可退出。

所有 Win32 相关（热键/图标）都封装在 lylaunch 各模块，本文件只做 UI 与编排。
"""

from __future__ import annotations

import os
import subprocess
import ctypes
import math
from ctypes.wintypes import MSG

from PySide6.QtCore import (
    Qt, QEvent, QTimer, Signal, QPoint, QPointF, QRectF, QSize,
    Property, QPropertyAnimation, QMimeData,   # QMimeData 在 QtCore（Qt6 起）
)
from PySide6.QtGui import (QImage, QPixmap, QCursor, QAction, QPainter, QPainterPath,
                         QColor, QFont, QFontMetrics, QRegion, QPolygon, QIcon,
                         QLinearGradient, QPen, QDrag)

from lylaunch.plugins.plugin_icons import (
    PLUGIN_ICON_BG, PLUGIN_ICON_FG, render_plugin_svg, system_icon_svg, action_icon_svg,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QGridLayout, QMenu, QApplication, QSizePolicy,
    QGraphicsOpacityEffect,
)

from .. import config
from ..item import Item
from ..search_index import SearchIndex
from ..clipboard import ClipboardWatcher
from ..icons import IconLoader
from ..hotkey import (register, unregister, format_hotkey, parse_hotkey,
                      HOTKEY_ID, SCREENSHOT_HOTKEY_ID, PLUGIN_HOTKEY_BASE)
from .. import single_instance
from ..plugins.manager import PluginManager
from ..plugins import theme as _t
from .. import paste_payload as paste_mod
from . import backdrop as _backdrop
from . import skins as _skins


def _dbg_print(*args, **kwargs):
    """调试输出已停用；保留调用以维持语句结构。"""
    pass


def _gear_icon(size: int = 22) -> QIcon:
    """自绘单色齿轮图标。

    Windows 会把字符 "⚙" 渲染成彩色 emoji（截图里是紫色齿轮），QSS 的 color
    对彩色 emoji 根本不生效，和蓝色主题格格不入。这里用 QPainter 画一个
    线条齿轮：颜色可控、零外部资源、打包也不用多带一个文件。
    """
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#5F6570"))
    c = size / 2
    r_in, r_out, tooth_w = size * 0.30, size * 0.46, size * 0.14
    painter.drawEllipse(QPointF(c, c), r_in, r_in)
    for i in range(8):
        painter.save()
        painter.translate(c, c)
        painter.rotate(i * 45)
        painter.drawRect(QRectF(-tooth_w / 2, -r_out, tooth_w, r_out - r_in + 1))
        painter.restore()
    # 中心孔必须真的擦成透明（画背景色会在 hover 的灰底上露白点）
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    painter.drawEllipse(QPointF(c, c), size * 0.14, size * 0.14)
    painter.end()
    return QIcon(pix)


class SettingsButton(QPushButton):
    """右上角设置按钮：扁平齿轮 + 克制的 hover 反馈（方案 A）。

    Qt 样式表（QSS）无法对 QWidget 做 scale / rotate，所以这套「轻微放大 + 旋转」
    不能靠纯样式实现，改用属性动画驱动 hover 进度，在 paintEvent 里手写：
    圆形淡背景随 hover 渐入 + 齿轮几何中心轻微放大并旋转，松手回正。
    没有套那套 Uiverse 的 sparkle 发光效果——那是为大号药丸按钮设计的，
    套到这个小齿轮上会破坏启动器干净克制的整体观感。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(40, 40)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setToolTip("设置")
        # 预渲染高分辨率齿轮（4x），paintEvent 里再缩到目标尺寸绘制。
        # 旋转 / 缩放时是对高清位图做降采样，边缘依旧锐利，避免低分辨率位图
        # 采样发虚（之前只用 22px 位图，旋转放大会明显糊）。
        self._pix = _gear_icon(22 * 4).pixmap(QSize(22 * 4, 22 * 4))
        self._t = 0.0  # hover 进度 0..1，由动画驱动
        self._anim = QPropertyAnimation(self, b"hover_t")
        self._anim.setDuration(180)
        # 去掉默认按钮的灰底，背景完全由 paintEvent 控制
        self.setStyleSheet("QPushButton{background:transparent; border:none;}")

    # ----- hover 进度属性（供 QPropertyAnimation 读写）-----
    def _get_t(self) -> float:
        return self._t

    def _set_t(self, v: float) -> None:
        self._t = v
        self.update()

    hover_t = Property(float, fget=_get_t, fset=_set_t)

    def enterEvent(self, _e) -> None:  # 进入：进度 0 → 1
        self._anim.stop()
        self._anim.setStartValue(self._t)
        self._anim.setEndValue(1.0)
        self._anim.start()
        super().enterEvent(_e)

    def leaveEvent(self, _e) -> None:  # 离开：进度 1 → 0
        self._anim.stop()
        self._anim.setStartValue(self._t)
        self._anim.setEndValue(0.0)
        self._anim.start()
        super().leaveEvent(_e)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        t = self._t
        tk = _skins.active()
        # 背景：原版按下 #D6E2F7 实底 / hover #E4EDFC 渐入；
        # 玻璃版走半透明白（gear_hover / gear_pressed），压在玻璃上才像同一层材质。
        # 圆角 10px 与搜索框一致，保持视觉统一。
        if self.isDown():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(tk.gear_pressed)
            p.drawRoundedRect(self.rect(), 10, 10)
        elif t > 0.001:
            bg = QColor(tk.gear_hover)
            bg.setAlphaF(bg.alphaF() * t)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(self.rect(), 10, 10)
        # 齿轮：以几何中心放大 (1 + 0.08t) 并旋转 (20t 度)
        if not self._pix.isNull():
            scale = 1.0 + 0.08 * t
            size = max(1, int(round(22 * scale)))
            pm = self._pix.scaled(
                size, size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            cx, cy = self.width() // 2, self.height() // 2
            p.save()
            p.translate(cx, cy)
            p.rotate(20 * t)
            p.translate(-pm.width() // 2, -pm.height() // 2)
            p.drawPixmap(0, 0, pm)
            p.restore()


WM_HOTKEY = 0x0312
WM_ACTIVATE = 0x0006
WA_INACTIVE = 0  # WM_ACTIVATE 的 wParam 低字：窗口被去激活（失焦）

# Acrylic 的着色层（0-255）：纯模糊会把壁纸的暗部直接拖进来，面板看着发灰；
# 加一层 58% 的白，壁纸只剩"柔和的冷暖倾向"，整体仍是白玻璃 —— 150 是实机
# 扫参数（0 / 90 / 150 × 罩层 0.62 / 0.78）里最接近原版又不发灰的一档。
_ACRYLIC_TINT = 150


def _hash_color(name: str) -> str:
    palette = ["#2F6BEE", "#00B2A9", "#FF8C00", "#7C5CFF",
               "#22C55E", "#0EA5E9", "#EAB308", "#D93A2F"]
    h = 0
    for c in (name or ""):
        h = (h * 31 + ord(c)) & 0x7FFFFFFF
    return palette[h % len(palette)]


# 右键菜单样式：QMenu 是本窗口的子控件，会继承窗口那条 background 样式表，
# 结果"系统高亮的选中行"变成浅底 + 白字，鼠标划过去几乎看不见。这里显式定死：
# 选中行只加深背景，文字颜色始终不变（深色）。
_CONTEXT_MENU_QSS = """
QMenu {
    background: #FFFFFF;
    border: 1px solid #D8E3F5;
    border-radius: 8px;
    padding: 5px;
}
QMenu::item {
    background: transparent;
    color: #161C28;
    font-size: 13px;
    padding: 8px 28px 8px 14px;
    border-radius: 5px;
}
QMenu::item:selected {
    background: #E4E7EC;
    color: #161C28;
}
QMenu::item:disabled {
    color: #A8ADB6;
}
QMenu::separator {
    height: 1px;
    background: #EDEFF2;
    margin: 4px 8px;
}
"""


def _style_menu(menu: QMenu) -> QMenu:
    """给右键菜单套上统一外观（选中行加深背景、文字不变色），并返回它便于链式调用。"""
    menu.setStyleSheet(_CONTEXT_MENU_QSS)
    return menu


# 搜不到任何东西时的首选插件：用户多半是拿一段现成文字来处理的，翻译最常用。
# 把它排到兜底列表第一位，默认就是选中态，回车即翻译。
_PREFERRED_FALLBACK_PLUGIN = "plugin://translator"


def _short_text(text: str, limit: int = 18) -> str:
    """把可能很长的搜索词（如整段剪贴板文本）压成一行短摘要，避免撑坏空状态提示。"""
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[:limit] + "…"


def _badge_pixmap(label: str, color: str = "#2F6BEE") -> QPixmap:
    """搜索框内左侧的「内容类型」小徽章：34×34 圆角色块 + 短标签。"""
    size = 34
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawRoundedRect(0, 0, size, size, 8, 8)
    p.setPen(QColor("#FFFFFF"))
    p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, label)
    p.end()
    return pix


def _thumb_pixmap(path: str) -> QPixmap:
    """把图片文件做成 34×34 圆角缩略图；读图失败返回空 QPixmap。"""
    try:
        img = QImage(path)
        if img.isNull():
            return QPixmap()
        scaled = img.scaled(34, 34, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
        out = QPixmap(34, 34)
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(0, 0, 34, 34, 8, 8)
        p.setClipPath(clip)
        p.drawPixmap((34 - scaled.width()) // 2, (34 - scaled.height()) // 2,
                     QPixmap.fromImage(scaled))
        p.end()
        return out
    except Exception:
        return QPixmap()


def _elide_two_lines(text: str, fm: "QFontMetrics", width: int) -> str:
    """把文本按宽度断成最多两行；放不下时在第二行末尾加 '…' 省略。

    用于应用名称：12px 字体下每行容纳 6 个汉字（72px），最多两行。
    断行策略是**尽量均分**而不是“第一行填满”——避免出现孤字：
    6 字 → 一行；7 字 → 4+3；8 字 → 4+4；9 字 → 5+4；10 字 → 5+5。
    含空格的英文名优先在空格处断行，避免把单词劈成两半。
    """
    if not text:
        return text
    # 整段一行就能放下，直接返回（标签 wordWrap 关闭，按单行渲染）
    if fm.horizontalAdvance(text) <= width:
        return text
    n = len(text)
    # 单行最多能容纳多少字（整字，不切半字）
    max_per_line = 0
    for k in range(1, n + 1):
        if fm.horizontalAdvance(text[:k]) <= width:
            max_per_line = k
        else:
            break
    if max_per_line == 0:
        max_per_line = 1  # 极端：连一个字都超宽，也至少放一个
    # 均分点：第一行取总长的一半（向上取整），且不超过单行容量
    first_end = min(max_per_line, max(1, (n + 1) // 2))
    # 英文名：在均分点附近 ±4 内找最近的空格断行，避免把单词劈开
    if " " in text:
        for d in range(0, 5):
            cands = [first_end] if d == 0 else [first_end - d, first_end + d]
            hit = None
            for cand in cands:
                if 0 < cand <= max_per_line and cand < n and text[cand - 1] == " ":
                    hit = cand
                    break
            if hit is not None:
                first_end = hit
                break
    first_end = max(1, min(first_end, n))
    # 第二行：剩余文本整体做末尾省略（放不下自动产生 '…' 并留出空间）
    second = fm.elidedText(text[first_end:], Qt.TextElideMode.ElideRight, width)
    return text[:first_end] + "\n" + second


class TileWidget(QWidget):
    """单个图标瓦片（对应 C# 的 ListBox ItemTemplate）。"""

    def __init__(self, item: Item, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.item = item
        self.selected = False
        self._hover = False
        self._pixmap: QPixmap | None = None
        # 拖拽排序时作为落点："before" / "after" / ""（paintEvent 画一根蓝色竖条）
        self._drop_side = ""

        layout = QVBoxLayout(self)
        # 横向留白 4px：名称加宽到 72px（6 字）后，瓦片仍需塞进 8 列的列宽里
        layout.setContentsMargins(4, 8, 4, 8)
        layout.setSpacing(6)

        self.icon = QLabel()
        self.icon.setFixedSize(40, 40)   # 0.36 布局 A：图标块 40px（原 36）
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon.setStyleSheet("border-radius:10px;")
        layout.addWidget(self.icon, alignment=Qt.AlignmentFlag.AlignCenter)

        # 名称：固定宽 60px = 5 个汉字/行（单行放满 5 字再换到第二行，设计确认保留）；
        # wordWrap 关闭，由 _update_name_label 手动控制两行省略
        self.name = QLabel()
        self.name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name.setWordWrap(False)
        # 72px = 6 个汉字/行（12px 字体实测单字 12px，6 字 72px 正好一行）
        self.name.setFixedWidth(72)
        # 显式设 12px 像素字体，保证 fontMetrics 度量与显示一致（每行 6 个中文）
        _nf = QFont()
        _nf.setPixelSize(12)
        self.name.setFont(_nf)
        self.name.setStyleSheet(
            f"color:{_skins.active().text_primary}; background:transparent;")
        layout.addWidget(self.name, alignment=Qt.AlignmentFlag.AlignCenter)

        self.setMinimumWidth(76)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        # 水平可拉伸：在网格列里被拉宽时，内部内容仍居中，图标随之向两侧铺开
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._paint_letter()
        self._update_name_label()
        self._refresh_styles()

    def _refresh_styles(self) -> None:
        """状态（选中/悬停）变化：只触发重绘，背景由 paintEvent 统一画。

        为什么不用样式表背景：实测样式表的 background 会级联到子控件——
        名称标签会继承并渲染它，而瓦片本体（自定义 QWidget）反而不渲染，
        结果绿色/灰色只出现在文字后面（调试时抓到的真凶）。
        改用 QPainter 显式绘制，保证图标+文字被同一个圆角背景整体包住。
        """
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        tk = _skins.active()
        glass = tk.name == "glass"
        # 瓦片背景：原版静态不绘制（透出页面整体色），悬停变暗、选中更深；
        # 玻璃版默认也不铺底（苹果的图标是"浮"在玻璃上的，一格格白方块又变回贴纸），
        # 只在悬停/选中时给一层半透明白，选中再补一圈主色细环。
        if self.selected:
            color = tk.selected_bg
        elif self._hover:
            color = tk.hover_bg
        else:
            color = tk.tile_bg
        if color is None and not self._drop_side:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if color is not None:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(self.rect(), 10, 10)
            if self.selected and tk.selected_ring is not None:
                pen = QPen(tk.selected_ring)
                pen.setWidthF(1.5)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 9.0, 9.0)
            # 原版：选中不加描边——浅蓝底已足够，1px 蓝框在深色壁纸上太跳
        if glass:
            # 拖拽落点也用半透明，别在玻璃上突然出现一块实底
            if self._drop_side:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255, 90))
                p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)
                p.setBrush(QColor("#007AFF"))
                x = 1 if self._drop_side == "before" else self.width() - 4
                p.drawRoundedRect(x, 8, 3, max(6, self.height() - 16), 1.5, 1.5)
            return
        # 拖拽落点：淡底 + 左/右边缘一根 3px 主色竖条。
        # 只画竖条时目标瓦片毫无反应，看起来像"没接收到"；补一层底色成本很低。
        if self._drop_side:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(228, 237, 252, 150))
            p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)
            p.setBrush(QColor("#2F6BEE"))
            x = 1 if self._drop_side == "before" else self.width() - 4
            p.drawRoundedRect(x, 8, 3, max(6, self.height() - 16), 1.5, 1.5)

    def _paint_letter(self) -> None:
        # 0.36「图标·线性」：带 icon_svg 的瓦片（插件 / 系统应用 / 粘贴动作）
        # 一律优先画线性 SVG（主色描边 + #F2F6FD 底），替代彩色块 + 首字母，
        # 也不再被 Windows 真实 exe 图标覆盖。11-11 起统一到同一套 UI 库。
        svg = getattr(self.item, "icon_svg", "")
        if svg:
            bg = _skins.active().icon_bg or PLUGIN_ICON_BG
            self.icon.setStyleSheet(f"border-radius:10px; background:{bg};")
            # 图标本体 22px（原型 ico(it.ico, 22)），不是把 40px 底整个填满
            self.icon.setPixmap(render_plugin_svg(svg, 22,
                                                  self.devicePixelRatioF() or 1.0))
            return
        text = self.item.icon_text
        if self._pixmap is not None:
            # 真实图标：背景透明（不加白卡片），图标直接坐在面板色上
            self.icon.setStyleSheet("background:transparent; border-radius:10px;")
            self.icon.setPixmap(self._pixmap)
        else:
            # 取不到真实图标（失效快捷方式、脚本类目标等）：改用插件同款
            # 「浅底 + 主色首字」，不再用饱和色块 + 白大字 —— 那套在浅色面板上太扎眼。
            family = self.icon.font().family()
            bg = _skins.active().icon_bg or PLUGIN_ICON_BG
            self.icon.setStyleSheet(
                f"border-radius:10px; background:{PLUGIN_ICON_BG};"
                f" color:{PLUGIN_ICON_FG}; font-family:'{family}';"
                f" font-size:14px; font-weight:600;"
            )
            self.icon.setText(text)

    def set_pixmap(self, pix: QPixmap) -> None:
        # 按设备像素比（DPI 缩放）取足物理像素再设 devicePixelRatio。
        # 之前先缩到 44px 再由 Qt 在高 DPI 屏上二次放大 → 图标发虚；
        # 现在给足物理像素（36 逻辑 × DPR），Qt 不再放大，图标锐利。
        dpr = self.devicePixelRatioF() or 1.0
        target = max(1, int(round(36 * dpr)))
        scaled = pix.scaled(target, target, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(dpr)
        self._pixmap = scaled
        self._paint_letter()
        self._refresh_styles()

    def _update_name_label(self) -> None:
        """按宽度把名称断成最多两行、末尾省略，并设全称 tooltip。"""
        text = self.item.name
        fm = self.name.fontMetrics()
        width = self.name.width()
        elided = _elide_two_lines(text, fm, width)
        self.name.setText(elided)
        # 悬停名称时弹出全称（移走由 Qt 自动关闭）；名称本身完整时弹同名也无害
        self.name.setToolTip(text)
        # 限制最多两行高度，避免溢出瓦片布局
        self.name.setMaximumHeight(fm.lineSpacing() * 2 + 1)

    def set_selected(self, sel: bool) -> None:
        self.selected = sel
        self._refresh_styles()

    def enterEvent(self, _e) -> None:  # 悬停高亮
        self._hover = True
        self._refresh_styles()

    def leaveEvent(self, _e) -> None:
        self._hover = False
        self._refresh_styles()


class _OuterShadow(QWidget):
    """毛玻璃皮肤的独立投影层（原版投影参数原样搬过来）。

    为什么不能让主窗口自己画：Acrylic 会把**整个窗口矩形**都糊上，只要窗口比
    面板大一圈（为了放投影），那一圈就会变成实心磨砂边。于是玻璃窗口缩到面板大小、
    投影交给这个伴生窗口：不接鼠标、不进任务栏、永远待在主窗下一层。
    """

    PAD = 19
    RADIUS = 14
    DY = 4

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.WindowTransparentForInput |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        inner = self.rect().adjusted(self.PAD, self.PAD, -self.PAD, -self.PAD)
        sh = QPainterPath()
        sh.addRoundedRect(inner.x(), inner.y() + self.DY,
                          inner.width(), inner.height(), self.RADIUS, self.RADIUS)
        p.setBrush(Qt.BrushStyle.NoBrush)
        # 与主窗口原版投影同一套：层数 2*pad、平方衰减、偏蓝浅色
        layers = 2 * self.PAD
        for i in range(1, layers + 1):
            t = 1 - i / layers
            p.setPen(QPen(QColor(50, 80, 145, int(4 * t * t)), i))
            p.drawPath(sh)


class MainWindow(QWidget):
    show_requested = Signal()   # 跨线程（单实例信号）唤出窗口

    def __init__(self) -> None:
        super().__init__()
        _dbg_print("[LyLaunch] MainWindow.__init__ 开始", flush=True)
        self.search_index = SearchIndex()
        _dbg_print("[LyLaunch] A: SearchIndex() 完成", flush=True)
        self.clipboard = ClipboardWatcher()
        _dbg_print("[LyLaunch] B: ClipboardWatcher() 完成", flush=True)
        # 剪贴板预填的"已消费"基线：启动瞬间剪贴板里的内容视为旧内容，
        # 之后新复制的文本（与基线不同）才会在打开面板时被预填一次（对标 uTools）。
        try:
            self._prefill_consumed = QApplication.clipboard().text().strip() or None
        except Exception:
            self._prefill_consumed = None
        self._prefill_text = ""   # 当前被预填进搜索框的剪贴板文本（用于区分"搜索"还是"处理这段文字"）

        # 粘贴图片 / 文件 → 联动插件的载荷状态。
        # 与文本预填是两条完全独立的基线：_prefill_consumed 只管"文本"，
        # _payload_consumed 只管"图片/文件指纹"，互不消费（否则图片会被文本预填误判）。
        self._payload: paste_mod.PastePayload | None = None   # None = 正常文本搜索态
        self._payload_consumed: str = ""
        self._temp_files_all: list[str] = []   # 延后清理池（见 _purge_temp_files）
        # 0.36「全部 >」：当前展开的分类；None = 首页态（应用/系统应用各只显示前 8 个）
        self._expanded_group: str | None = None
        try:
            # 启动瞬间剪贴板里已有的图片/文件视为旧内容，首次唤出不感知。
            self._payload_consumed = paste_mod.clip_fingerprint(
                QApplication.clipboard().mimeData())
        except Exception:
            self._payload_consumed = ""
        paste_mod.cleanup_stale_temp_files()   # 清上次运行残留的临时 PNG

        # 插件系统：与基础功能隔离，单例管理扫描/加载/启用/禁用/卸载
        self.plugins = PluginManager.instance()
        self.plugins.load_all(self.clipboard)
        _dbg_print(f"[LyLaunch] B2: 插件系统加载完成（{len(self.plugins.all_plugins())} 个插件，"
              f"{len(self.plugins.enabled_plugins())} 个启用）", flush=True)
        self.pinned = config.load_pinned()
        self.recents = config.load_recents()
        _dbg_print("[LyLaunch] C: config 加载完成", flush=True)
        self.tiles: list[TileWidget] = []
        self.sel_index = 0
        self._cols = 8
        self._context_menu_open = False
        # 拖拽排序状态：按下的瓦片 / 按下位置 / 当前高亮的落点瓦片
        self._press_tile: TileWidget | None = None
        self._press_pos = None
        self._drop_hint_tile: TileWidget | None = None
        self.setAcceptDrops(True)     # 接收瓦片拖拽（排序）；外部文件不走这里
        # 构造期先读一次真实配置，页脚初始文案才不会闪一下旧的默认值
        _hk = config.load_hotkey()
        self._hotkey_mods = _hk.modifiers
        self._hotkey_vk = _hk.vk
        self._hotkey_display = _hk.display
        # 截图快捷键（默认 Alt+A）
        self._screenshot_active = False   # 截图遮罩打开中：期间忽略再次触发热键/点瓦片
        _shot = config.load_screenshot_hotkey()
        self._shot_mods = _shot.modifiers
        self._shot_vk = _shot.vk
        self._shot_display = _shot.display

        # 隐藏保护：刚被我们主动 show 出来的瞬间，Windows 可能发 WindowDeactivate，
        # 若立刻 hide 会导致浮窗一闪而过。show 后的 300ms 内忽略失焦隐藏。
        self._suppress_hide = False
        self._modal_open = False  # 设置等模态对话框打开时为 True，阻止主窗口被级联隐藏
        self._settings_window = None  # 当前已打开的设置窗口实例（防重入）
        self._hide_guard = QTimer(self)
        self._hide_guard.setSingleShot(True)
        self._hide_guard.timeout.connect(lambda: setattr(self, "_suppress_hide", False))

        self._setup_window()
        _dbg_print("[LyLaunch] D: _setup_window() 完成", flush=True)
        self._setup_ui()
        _dbg_print("[LyLaunch] E: _setup_ui() 完成", flush=True)
        # 热键不在构造时注册（此刻 winId() 会强制创建原生窗口，
        # 与 Frameless+Tool+Translucent 组合在 PySide6 上会崩进程）。
        # 改为窗口首次 show() 之后再注册。
        self._hotkey_registered = False   # 唤出热键当前是否真的注册成功（失败要能重试）
        self._shot_registered = False     # 截图热键当前是否真的注册成功
        self._plugin_hotkeys: dict[int, str] = {}   # 插件全局热键：热键 id -> 插件 Id
        self._hotkeys_suspended = False   # 设置页捕获按键期间临时注销了两把热键（见 suspend_hotkeys）
        self._hotkey_warned = False       # 本次运行是否已提示过热键冲突（避免每次唤出都弹窗）
        _dbg_print("[LyLaunch] 热键将延后到窗口显示后注册", flush=True)

        # 图标加载器：后台取图标，主线程转 QPixmap 显示
        self._icon_loader = IconLoader.instance()
        self._icon_loader.loaded.connect(self._on_icon_loaded)

        # 剪贴板变化：仅当搜索为空时刷新首页
        self.clipboard.changed.connect(lambda _: self.refresh_list() if not self.search_box.text() else None)
        self.clipboard.start()

        # 单实例：等待"显示信号"
        single_instance.listen_for_show(self._on_show_signal)

        _dbg_print("[LyLaunch] 准备构建索引（扫描本机）…", flush=True)
        self.search_index.build()
        _dbg_print("[LyLaunch] 索引构建完成", flush=True)
        self.refresh_list()
        _dbg_print("[LyLaunch] 首屏渲染完成", flush=True)
        # 主窗口是 QWidget，而 theme.py 那套 DIALOG_QSS 全是 QDialog 选择器，
        # 一条都匹配不到 → 字体一路落到平台默认的 MS Shell Dlg，Qt 再把它拆成
        # MS Sans Serif（比例）/ Fixedsys（等宽），DirectWrite 建不出字体面就狂刷警告。
        # 这里显式钉一次字体让子控件继承。
        # 必须放在 __init__ 最后：setFont 会派发 FontChange → 触发 changeEvent，
        # 而 changeEvent 用到的 _modal_open / _suppress_hide 是中途才初始化的。
        self.setFont(QFont("Microsoft YaHei UI", 10))
        _dbg_print("[LyLaunch] MainWindow.__init__ 结束", flush=True)

    # ---------- 窗口初始化 ----------
    def _setup_window(self) -> None:
        self.setWindowTitle("LyLaunch")
        # 皮肤（2026-11-23）：solid=原来的白→淡蓝渐变，glass=系统 Acrylic 毛玻璃。
        # 留白规则：原版要 19px 在窗口内画柔和投影；毛玻璃的投影交给伴生窗口
        # _OuterShadow（Acrylic 会把整个窗口矩形都糊上，窗口内留白会变成一圈实心
        # 磨砂边），所以主窗就是面板本身，pad=0。
        self._skin = config.load_win_skin()
        _skins.set_active(self._skin)
        # 背景材质：show() 之后才尝试开 Acrylic（构造期碰 winId() 有崩溃史）。
        # 系统不支持 / 关掉透明效果时保持 False，paintEvent 走原来的不透明渐变。
        self._glass = False
        self._glass_supported = _backdrop.supported()
        # 毛玻璃的投影由独立伴生窗口画（_OuterShadow），主窗自己不画
        self._shadow_win = None
        self._shadow_pad = 0 if (self._skin == "glass" and self._glass_supported) else 19
        self.setFixedSize(860 + 2 * self._shadow_pad, 520 + 2 * self._shadow_pad)
        # 注意：
        # 1) 不要加 Qt.WindowType.Tool —— 与 Frameless 组合在 PySide6/Windows 上易崩。
        # 2) 不要 WA_TranslucentBackground —— 会让窗口变 layered 窗口，DWM 合成路径
        #    在部分机器上导致 show() 时进程静默崩溃。改用纯色背景 + 样式表圆角。
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setStyleSheet("background:transparent;")  # 底色与渐变由 paintEvent 统一画
        # 0.36 设计说明 根治「圆角黑角 + 锯齿」：
        #   根因 = 非透明窗口用二值遮罩(setMask)，被裁掉的四角 OS 会用黑色背景刷填充
        #         → 黑角；二值遮罩本身没有抗锯齿 → 像素感。
        #   唯一根治 = layered 窗口（透明背景）+ 自绘圆角矩形，圆角外不绘制即透明、
        #         QPainter 抗锯齿让边缘平滑。不再用二值遮罩（setMask）。
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 右键窗口空白处弹出菜单（含“退出”），作为无边框窗口的关闭通道
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_window_context_menu)

    def _hide_from_taskbar(self) -> None:
        """把主面板标记成"工具窗口"：任务栏和 Alt+Tab 里都不再出现它。

        为什么不用 Qt.WindowType.Tool：它和本窗口的 Frameless + WindowStaysOnTopHint
        组合在 PySide6/Windows 上有崩溃史（见 _setup_window 的注释）。这里直接改原生
        扩展样式 WS_EX_TOOLWINDOW —— 不重建窗口、不动 Qt 窗口标志，Windows 会立刻
        把任务栏按钮收掉（Alt+Tab 列表同步移除）。

        幂等：样式已经对了就直接返回；重复调用只在第一次真正改样式。
        必须在 show() 之后调用（此时原生 HWND 才稳定，构造期调 winId() 有崩溃风险）。
        """
        try:
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW = 0x00040000
            SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x1, 0x2, 0x4, 0x20
            user32 = ctypes.windll.user32
            # 显式声明签名：64 位下 HWND 是 8 字节指针，不声明会被 ctypes 截成 int32
            user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
            user32.SetWindowLongW.restype = ctypes.c_long
            user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_uint]
            user32.SetWindowPos.restype = ctypes.c_int

            hwnd = ctypes.c_void_p(int(self.winId()))
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            want = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            if want == style:
                return
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, want)
            # 扩展样式变更后必须让系统重算一次窗口框架，任务栏按钮才会真正消失
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                                SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED)
            _dbg_print("[LyLaunch] 主面板已从任务栏隐藏（WS_EX_TOOLWINDOW）", flush=True)
        except Exception as e:
            print(f"[LyLaunch] 任务栏隐藏失败（不影响使用）: {repr(e)}", flush=True)

    def _apply_dwm_round_corners(self) -> bool:
        """Win11：请求 DWM 原生圆角（系统绘制、自带抗锯齿，比二值遮罩平滑得多）。

        返回是否设置成功；失败（Win10 或系统不支持）时调用方应退回遮罩方案。
        """
        try:
            DWMWA_WINDOW_CORNER_PREFERENCE = 33  # Win11 22000+ 才有
            DWMWCP_ROUND = 2
            dwmapi = ctypes.windll.dwmapi
            pref = ctypes.c_int(DWMWCP_ROUND)
            hr = dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(int(self.winId())),
                ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
                ctypes.byref(pref), ctypes.sizeof(pref))
            return hr == 0
        except Exception as e:
            print(f"[LyLaunch] DWM 圆角设置失败（退回遮罩）: {repr(e)}", flush=True)
            return False

    def _apply_window_corners(self) -> None:
        """统一入口：0.36 设计说明 起窗口已是 layered 透明窗口，
        圆角由 paintEvent 自绘（抗锯齿 + 圆角外透明，无黑角、无锯齿），
        不再需要 OS 级遮罩；此函数保留为入口，但不再做任何事。
        """
        return

    # ---------- 皮肤 ----------
    def _relayout_for_pad(self) -> None:
        """投影留白变了：窗口尺寸与内容边距一起重算（内容到面板边仍保持 16px）。"""
        pad = self._shadow_pad
        self.setFixedSize(860 + 2 * pad, 520 + 2 * pad)
        layout = getattr(self, "_root_layout", None)
        if layout is not None:
            layout.setContentsMargins(16 + pad, 16 + pad, 16 + pad, 8 + pad)

    def _sync_glass(self) -> None:
        """让 Acrylic 状态跟当前皮肤一致；窗口不可见时不碰原生句柄。

        毛玻璃时主窗收到面板大小（pad=0），投影由伴生窗口 `_OuterShadow` 画；
        Acrylic 没开成（系统关了透明效果 / 显卡不支持）就退回"原版那套"：
        19px 留白 + 窗口内柔和投影 + 不透明渐变，不然一块没底的窗口飘在壁纸上会很难看。
        """
        want = self._skin == "glass"
        if want:
            if self.isVisible() and not self._glass:
                self._glass = _backdrop.apply_acrylic(self, "#FFFFFF", _ACRYLIC_TINT)
                if not self._glass:
                    # DWM 偶发不接受合成属性（实测切换瞬间会失败一次），稍后重试一次
                    QTimer.singleShot(60, self._retry_glass)
        else:
            if self._glass:
                _backdrop.clear_acrylic(self)
            self._glass = False
        self._apply_pad()
        self._sync_outer_shadow()
        self.update()

    def _retry_glass(self) -> None:
        """Acrylic 第一次没开成时补一次（成功后同步留白与投影）。"""
        if self._skin != "glass" or self._glass or not self.isVisible():
            return
        self._glass = _backdrop.apply_acrylic(self, "#FFFFFF", _ACRYLIC_TINT)
        self._apply_pad()
        self._sync_outer_shadow()
        self.update()

    def _apply_pad(self) -> None:
        """按"玻璃是否真的开着"决定要不要投影留白。

        窗口隐藏时（比如从设置页切皮肤）没法碰原生句柄，先乐观地按玻璃排版；
        show 之后 `_sync_glass()` 再按真实结果纠正一次。
        """
        glass_now = (self._skin == "glass"
                     and (self._glass or (not self.isVisible() and self._glass_supported)))
        pad = 0 if glass_now else 19
        if pad == getattr(self, "_shadow_pad", None):
            return
        self._shadow_pad = pad
        self._relayout_for_pad()
        if self.isVisible():
            self._center()

    # ---------- 毛玻璃的伴生投影 ----------
    def _sync_outer_shadow(self) -> None:
        """让伴生投影窗跟着主窗走：位置、尺寸、层级、显隐。

        只有"毛玻璃真的开着"时才显示；原版窗口自己就把投影画在里面（pad=19），
        这条路径完全不动。
        """
        want = self._skin == "glass" and self._glass and self.isVisible()
        if not want:
            if self._shadow_win is not None and self._shadow_win.isVisible():
                self._shadow_win.hide()
            return
        if self._shadow_win is None:
            self._shadow_win = _OuterShadow()
        sw = self._shadow_win
        pad = _OuterShadow.PAD
        sw.setFixedSize(self.width() + pad * 2, self.height() + pad * 2)
        sw.move(self.x() - pad, self.y() - pad)
        if not sw.isVisible():
            sw.show()
            self._shadow_toolwindow(sw)
            sw.update()
        self._shadow_below_main()

    @staticmethod
    def _shadow_toolwindow(sw: QWidget) -> None:
        """投影窗别进任务栏 / Alt+Tab（原生扩展样式，不改 Qt 窗口标志）。"""
        try:
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x00000080, 0x00040000
            SWP = 0x0001 | 0x0002 | 0x0004 | 0x0020
            user32 = ctypes.windll.user32
            user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
            user32.SetWindowLongW.restype = ctypes.c_long
            user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_uint]
            user32.SetWindowPos.restype = ctypes.c_int
            hwnd = ctypes.c_void_p(int(sw.winId()))
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            want = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            if want != style:
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, want)
                user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                                    SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED)
        except Exception:
            pass

    def _shadow_below_main(self) -> None:
        """把投影窗插到主窗正下方一层（两者都是 topmost，用 SetWindowPos 定序）。"""
        if self._shadow_win is None:
            return
        try:
            SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_uint]
            user32.SetWindowPos.restype = ctypes.c_int
            user32.SetWindowPos(ctypes.c_void_p(int(self._shadow_win.winId())),
                                ctypes.c_void_p(int(self.winId())),
                                0, 0, 0, 0,
                                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
        except Exception:
            pass

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        self._sync_outer_shadow()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        if self._shadow_win is not None:
            self._shadow_win.hide()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 热键重新唤出时窗口只是 show()，不走 show_window()；投影窗得跟着回来
        self._sync_outer_shadow()

    def apply_win_skin(self, name: str) -> None:
        """设置页切换皮肤：留白、底色、背景材质一起换，即时生效，不需要重启。

        毛玻璃把主窗收到面板大小（pad 19→0，投影改由伴生窗口画），切过去时窗口
        收 38px、切回来再放出去；尺寸变了以后重新居中，避免面板贴着屏幕一角。
        """
        name = name if name in config.WIN_SKINS else config.DEFAULT_WIN_SKIN
        if name == self._skin:
            self._sync_glass()
            return
        self._skin = name
        _skins.set_active(name)
        self._sync_glass()
        if self.isVisible():
            self._center()
        # 瓦片缓存了颜色与图标底样式，换皮肤后整块重建一次最稳（数量级很小）
        try:
            self.refresh_list()
        except Exception:
            pass
        self._restyle_for_skin()

    def _restyle_for_skin(self) -> None:
        """把"只在创建时设一次"的样式按当前皮肤重刷一遍。

        为什么需要：搜索框/输入 chip/滚动条/底栏文字都是构造时写死的 QSS，
        运行时切皮肤不会自动变 —— 以前从原版切到毛玻璃，搜索框还是死白，
        看着像没生效。QSS 片段统一在 skins.py 里生成，和构造时用的是同一段。
        """
        tk = _skins.active()
        self.search_box.setStyleSheet(_skins.field_qss(tk))
        self._input_chip.setStyleSheet(_skins.chip_qss(tk))
        self.scroller.setStyleSheet(_skins.scroller_qss(tk))
        self.empty_hint.setStyleSheet(f"color:{tk.text_secondary}; font-size:13px;")
        self.foot.setStyleSheet(f"color:{tk.text_secondary}; font-size:11px;")
        self.foot_hotkey.setStyleSheet(f"color:{tk.text_secondary}; font-size:11px;")

    def _apply_rounded_mask(self) -> None:
        """把窗口裁成圆角矩形（OS 级别遮罩），无边框也能看到圆角。

        注意：遮罩是**二值裁剪、没有抗锯齿**，所以圆角边缘会有轻微锯齿——
        Win11 上优先走 `_apply_dwm_round_corners()`（系统绘制、平滑），
        本方法作为 Win10/不支持时的兜底。
        手工按角度采样生成圆角多边形（每段圆弧 24 个点）比
        QPainterPath.toFillPolygon() 的默认采样更平滑（后者采点稀疏、棱角明显）。
        """
        r = 14  # 0.36 布局 A：窗口圆角 14（原 16）
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        r = min(r, w // 2, h // 2)
        # 四角圆心与起止角度（屏幕坐标 y 向下）：左上→右上→右下→左下
        corners = [(r, r, 180, 270), (w - r, r, 270, 360),
                   (w - r, h - r, 0, 90), (r, h - r, 90, 180)]
        steps = 32  # 每角弧线采样段数（24→32：回退遮罩后用更密采样弱化锯齿）
        pts: list[QPoint] = []
        for cx, cy, a0, a1 in corners:
            for i in range(steps + 1):
                a = math.radians(a0 + (a1 - a0) * i / steps)
                pts.append(QPoint(int(round(cx + r * math.cos(a))),
                                  int(round(cy + r * math.sin(a)))))
        self.setMask(QRegion(QPolygon(pts)))

    def paintEvent(self, event) -> None:  # noqa: N802
        # 两套皮肤两种画法：
        # - 原版：窗口比面板大一圈（_shadow_pad=19），面板内侧、投影向外羽化；
        # - 毛玻璃：pad=0，整个窗口就是面板；圆角交给 DWM（它连 Acrylic 模糊区
        #   一起裁圆），投影由伴生窗口 _OuterShadow 画。
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = 14
        pad = getattr(self, "_shadow_pad", 19)
        dy = 4  # 阴影向下偏移（偏下投影感）；过大会导致面板与阴影之间出现断档空隙
        rect = self.rect()
        inner = rect.adjusted(pad, pad, -pad, -pad)  # 面板区域（固定 860×520）
        glass = getattr(self, "_glass", False)
        if not glass:
            # 阴影路径：与面板同形、整体下移 dy（上方留白更多、下方阴影更聚拢）
            sh = QPainterPath()
            sh.addRoundedRect(inner.x(), inner.y() + dy, inner.width(), inner.height(), r, r)
            # 柔和阴影：沿阴影路径多层半透明描边；层数加倍 + 平方衰减 → 羽化更柔；
            # 颜色偏蓝（深蓝灰）、整体很浅；扩散范围由 _shadow_pad 控制
            p.setBrush(Qt.BrushStyle.NoBrush)
            layers = 2 * pad
            for i in range(1, layers + 1):
                t = 1 - i / layers
                alpha = int(4 * t * t)  # 平方衰减：越靠外越快变淡 → 柔和羽化
                p.setPen(QPen(QColor(50, 80, 145, alpha), i))  # 偏蓝阴影；线宽 i，最外半宽 = pad
                p.drawPath(sh)
        # 主体：毛玻璃（Acrylic）或原来的不透明白→淡蓝渐变
        if glass:
            # 「液态玻璃」：半透明白罩层（0.78→0.74）+ 顶部一道 88px 白色高光（sheen）。
            # 罩层不能太透：Acrylic 自身带灰调，纯模糊会把壁纸暗部拖进来，面板就发灰；
            # 高光则是苹果玻璃最像的一点——有了它，"一块磨砂"才变成"一片有厚度的玻璃"。
            # 圆角仍交给 DWM 裁（自己画会和系统半径打架露细边）。
            tk = _skins.active()
            grad = QLinearGradient(0, 0, 0, rect.height())
            grad.setColorAt(0.0, tk.scrim_top)
            grad.setColorAt(1.0, tk.scrim_bottom)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawRect(rect)
            if tk.sheen.alpha() > 0:
                sh = QLinearGradient(0, 0, 0, tk.sheen_height)
                sh.setColorAt(0.0, tk.sheen)
                sh.setColorAt(1.0, QColor(tk.sheen.red(), tk.sheen.green(),
                                         tk.sheen.blue(), 0))
                p.setBrush(sh)
                p.drawRect(0, 0, rect.width(), tk.sheen_height)
            # 一圈 1px 内高光：玻璃和壁纸之间要有道"折边"才立得住，
            # 圆角跟 DWM 一致（约 8），用抗锯齿路径描在内侧 0.5px 避免糊边
            pen = QPen(QColor(255, 255, 255, 150))
            pen.setWidthF(1.0)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            hl = QPainterPath()
            hl.addRoundedRect(rect.adjusted(0, 0, -1, -1).toRectF(), 7.5, 7.5)
            p.drawPath(hl)
            return
        path = QPainterPath()
        path.addRoundedRect(inner.x(), inner.y(), inner.width(), inner.height(), r, r)
        grad = QLinearGradient(0, inner.y(), 0, inner.bottom())
        grad.setColorAt(0.0, QColor("#FFFFFF"))
        grad.setColorAt(1.0, QColor("#E8F0FE"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grad)
        p.drawPath(path)

    def _setup_ui(self) -> None:
        _dbg_print("[LyLaunch] E1: _setup_ui 开始", flush=True)
        tk = _skins.active()   # 本次构造用的皮肤 token（切皮肤时由 _restyle_for_skin 重刷）
        self.central = QWidget(self)
        self.central.setStyleSheet("background:transparent;")  # 渐变底在窗口 paintEvent
        # QWidget 没有 setCentralWidget，直接把 central 作为本窗口唯一子控件
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.central)

        root = QVBoxLayout(self.central)
        self._root_layout = root   # 皮肤切换要按新的投影留白重算内容边距
        # 内容边距 = 原 16/8 + 阴影留白：内容距面板边仍保持原 16px，同时把面板推到窗口内侧
        pad = getattr(self, "_shadow_pad", 18)
        root.setContentsMargins(16 + pad, 16 + pad, 16 + pad, 8 + pad)
        root.setSpacing(8)

        # 顶部：搜索框 + 设置按钮（按钮变大 → 搜索框自然收窄，间距 10px）
        top = QHBoxLayout()
        top.setSpacing(10)
        search_box = QLineEdit()
        search_box.setPlaceholderText("搜索应用…")
        search_box.setFixedHeight(46)
        search_box.setStyleSheet(_skins.field_qss(tk))
        top.addWidget(search_box, stretch=1)

        settings_btn = SettingsButton()
        settings_btn.clicked.connect(self.open_settings)
        top.addWidget(settings_btn)
        root.addLayout(top)
        self.search_box = search_box

        # 输入内容预览 chip：内嵌在搜索框左侧，把"当前粘进来的东西"直接摆在
        # 输入框里（类似 uTools）。图片显示真缩略图，文件/文字显示类型徽章。
        # 透明鼠标事件 → 点这块区域仍能正常把焦点交给输入框。
        self._input_chip = QLabel(search_box)
        self._input_chip.setFixedSize(34, 34)
        self._input_chip.move(6, 6)
        self._input_chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._input_chip.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._input_chip.setStyleSheet(_skins.chip_qss(tk))
        self._input_chip.hide()

        # 用事件过滤器拦截搜索框的箭头/回车/Esc，转成窗口级导航
        search_box.installEventFilter(self)
        # 再挂一层到 QApplication：焦点不在搜索框时（点了空白区/焦点在瓦片上），
        # 方向键仍要走“移动选中瓦片”，不能被滚动区吃掉变成翻页/横向滚动。
        QApplication.instance().installEventFilter(self)

        # 结果区（可滚动）
        self.scroller = QScrollArea()
        self.scroller.setWidgetResizable(True)
        self.scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 细滚动条：只留一条 4px 的滑块，轨道/箭头/翻页区全透明。
        # 默认 QScrollBar 会带上下箭头和 add/sub-page 的灰底，不显式清掉时
        # 即使把 width 设成 4px 看起来仍是一条粗灰带（截图里就是这个问题）。
        self.scroller.setStyleSheet(_skins.scroller_qss(tk))
        # 滚动区不接收键盘焦点：否则方向键沿父链传到 QAbstractScrollArea 会被
        # 当成滚动指令，出现“左右键横向拖页面”的现象（页面只应上下翻页）。
        self.scroller.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroller.viewport().setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # 设计说明：滚动条默认隐藏，鼠标移入滚动区才显示（事件过滤器里切 Enter/Leave）
        self.scroller.installEventFilter(self)
        self.scroller.verticalScrollBar().setVisible(False)
        root.addWidget(self.scroller, stretch=1)

        self.result_container = QWidget()
        self.result_layout = QVBoxLayout(self.result_container)
        self.result_layout.setContentsMargins(0, 0, 0, 0)
        self.result_layout.setSpacing(14)
        self.scroller.setWidget(self.result_container)

        # 空状态提示
        self.empty_hint = QLabel("")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setStyleSheet(f"color:{tk.text_secondary}; font-size:13px;")
        self.empty_hint.setWordWrap(True)   # 成本提示较长，窄窗口下允许换行
        self.empty_hint.setVisible(False)
        root.addWidget(self.empty_hint)

        # 底部状态栏：左侧产品名+版本，右侧唤出热键提示（热键改动时实时更新）
        foot_row = QWidget()
        foot_layout = QHBoxLayout(foot_row)
        foot_layout.setContentsMargins(6, 4, 6, 0)
        self.foot = QLabel(f"LyLaunch {config.VERSION}")
        self.foot.setStyleSheet(f"color:{tk.text_secondary}; font-size:11px;")
        foot_layout.addWidget(self.foot)
        foot_layout.addStretch(1)
        self.foot_hotkey = QLabel(f"{self._hotkey_display} 唤出")
        self.foot_hotkey.setStyleSheet(f"color:{tk.text_secondary}; font-size:11px;")
        foot_layout.addWidget(self.foot_hotkey)
        root.addWidget(foot_row)

        # Toast（固定/取消固定提示）
        self.toast = QLabel("")
        self.toast.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.toast.setStyleSheet(
            "background:#FFFFFF; color:#161C28; font-size:13px; font-weight:bold;"
            "padding:8px 14px; border-radius:8px;"
        )
        self.toast.setVisible(False)
        root.addWidget(self.toast)
        self._toast_timer = QTimer(self)
        self._toast_timer.timeout.connect(lambda: self.toast.setVisible(False))

        # 搜索防抖：停止输入 70ms 才刷新
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self.refresh_list)
        search_box.textChanged.connect(self._on_search_text_changed)

    def _on_search_text_changed(self, _text: str) -> None:
        """用户一开始打字就退出载荷态，回到正常文本搜索。

        否则「唤出时自动感知剪贴板」会永久挡住正常搜索：用户复制过一次文件后，
        每次唤出想搜应用都会先看到动作候选。
        """
        if self._payload is not None:
            self._clear_payload_state()
        elif not self._input_chip.isHidden():
            # 复制文字预填时的「文」徽章：用户一开始打字就撤掉，不常占输入框。
            self._clear_input_chip()
        self._search_timer.stop()
        self._search_timer.start(70)

    # ---------- 输入框内容预览（uTools 式） ----------
    def _set_input_chip(self, pix: QPixmap) -> None:
        """在搜索框左侧显示内容徽章，并给文本留出左缩进。"""
        if pix is None or pix.isNull():
            self._clear_input_chip()
            return
        self._input_chip.setPixmap(pix)
        self._input_chip.show()
        self._input_chip.raise_()
        # QSS 已有 padding-left:14px；chip 占 6..40，再留 10px → 文字起点约 50px。
        self.search_box.setTextMargins(36, 0, 0, 0)

    def _clear_input_chip(self) -> None:
        self._input_chip.hide()
        self._input_chip.clear()
        self.search_box.setTextMargins(0, 0, 0, 0)
        self.search_box.setPlaceholderText("搜索应用…")

    def _make_chip_pixmap(self, payload) -> QPixmap:
        """图片给真缩略图；多文件给数量；文档给扩展名；其余给类型徽章。"""
        if payload.is_image and payload.first_path:
            thumb = _thumb_pixmap(payload.first_path)
            if not thumb.isNull():
                return thumb
        if len(payload.paths) > 1:
            return _badge_pixmap(str(len(payload.paths)), "#00B2A9")
        if payload.is_doc:
            ext = os.path.splitext(payload.first_path)[1].lstrip(".").upper()
            return _badge_pixmap(ext[:3] or "文", "#7C5CFF")
        if payload.is_image:
            return _badge_pixmap("图", "#2F6BEE")
        return _badge_pixmap("文", "#2F6BEE")

    def _apply_payload_view(self) -> None:
        """把载荷摆进搜索框：左侧缩略图 + 框内摘要。

        摘要设为全选态 —— 用户一打字就整段替换，同时 textChanged 会退出载荷态，
        回到正常搜索。这样"预览"和"输入"不会打架。
        """
        p = self._payload
        if p is None:
            return
        self._set_input_chip(self._make_chip_pixmap(p))
        self.search_box.blockSignals(True)
        self.search_box.setText(p.summary)
        self.search_box.blockSignals(False)
        self.search_box.selectAll()
        self.search_box.setPlaceholderText("输入即取消预览，或选择下方动作")

    # ---------- 粘贴载荷（图片 / 文件 → 联动插件） ----------
    def _clear_payload_state(self) -> None:
        """退出载荷态。临时文件只登记、不立即删。

        _run_plugin 有 80ms 延迟执行，若在这里删 PNG，插件启动时文件已没了
        （截图插件当年绕剪贴板中转就是栽在同类竞态上）。
        """
        p, self._payload = self._payload, None
        if p and p.temp_files:
            self._temp_files_all.extend(p.temp_files)
        self._clear_input_chip()   # 同时撤掉搜索框里的内容预览

    def _purge_temp_files(self, force: bool = False) -> None:
        """集中清理延后登记的临时 PNG。

        默认只删 5 秒前的：_run_plugin 有 80ms 延迟，若用户刚执行动作又立刻
        粘了新载荷，旧 PNG 可能还没被插件读到，立即删会造成竞态（截图插件
        当年绕剪贴板中转就栽在同类问题上）。程序退出时 force=True 全清。
        """
        import time as _time
        now = _time.time()
        keep: list[str] = []
        for f in self._temp_files_all:
            try:
                if not force and now - os.path.getmtime(f) < 5:
                    keep.append(f)
                    continue
                os.remove(f)
            except Exception:
                pass
        self._temp_files_all = keep

    def _handle_paste(self) -> bool:
        """Ctrl+V 拦截：图片/文件转载荷并返回 True；纯文本返回 False 放行默认粘贴。"""
        try:
            md = QApplication.clipboard().mimeData()
        except Exception:
            return False
        payload = paste_mod.parse_mime(md)
        if not payload.usable:
            return False
        self._purge_temp_files()
        payload.source = "paste"
        self._payload = payload
        # 手动粘贴也更新基线，避免随后唤出时把同一份内容再感知一次。
        try:
            self._payload_consumed = paste_mod.clip_fingerprint(md)
        except Exception:
            pass
        # 摘要文本必须在 blockSignals 下写入（见 _apply_payload_view），否则
        # textChanged 会触发 _on_search_text_changed 把刚设的载荷清掉。
        self._apply_payload_view()
        self.refresh_list()
        return True

    def _plugin_available(self, plugin_id: str) -> bool:
        """可选插件是否已安装且启用。内置 ocr 随主包，无需检测。"""
        try:
            if not self.plugins.is_running():
                return False
            info = self.plugins.get(plugin_id)
            return bool(info is not None and info.enabled)
        except Exception:
            return False

    def _build_paste_actions(self, payload) -> list[Item]:
        """按载荷类型生成候选动作瓦片；第 0 项即回车默认执行项。"""
        out: list[Item] = []

        def add(verb: str, name: str, icon: str, color: str,
                itype: str = "动作") -> None:
            it = Item(
                name=name, path=f"action://{verb}", item_type=itype,
                group_label="粘贴动作", group_order=0,
                icon_text=icon, icon_fg="#FFFFFF", tile_color=color,
            )
            # 0.36 11-11：弃用彩色字母方块，统一挂线性 SVG（与插件区同一套 UI 库）。
            # icon_text / tile_color 保留作 SVG 渲染失败时的兜底。
            it.icon_svg = action_icon_svg(verb)
            out.append(it)

        doc_ok = self._plugin_available("doc_recognize")
        llm_ok = self._plugin_available("llm_chat")

        if payload.is_image:
            # 图片类：内置 OCR 永远排第一 —— 可选插件没装时这就是"自动降级"，
            # 不弹窗、不改变首选，只把不可用的候选剔除。
            add("ocr", "图片转文字", "OCR", "#2F6BEE")
            if doc_ok:
                add("doc_recognize", "整页转文档 (MD)", "MD", "#7C5CFF")
            if llm_ok:
                add("llm_chat", "发给本地 AI", "AI", "#00B2A9")
            add("open", "打开", "开", "#22C55E")
            add("reveal", "资源管理器定位", "位", "#EAB308")
        elif payload.is_doc and not doc_ok:
            # 文档识别没装也有两条可用路径：本地 AI 零模型解析 / 直接打开。
            # 末尾说明瓦片（item_type="提示" 不可点）只标明它是可选增强，不再说"需安装"。
            add("open_settings", "去 设置 → 插件", "⚙", "#7C5CFF")
            if llm_ok:
                add("llm_chat", "发给本地 AI", "AI", "#00B2A9")
            add("open", "打开", "开", "#22C55E")
            add("reveal", "资源管理器定位", "位", "#EAB308")
            add("install_doc", "可选：文档识别（结构化 MD）", "!", "#D93A2F", itype="提示")
        elif payload.is_doc:
            add("doc_recognize", "文档识别", "MD", "#7C5CFF")
            if llm_ok:
                add("llm_chat", "发给本地 AI", "AI", "#00B2A9")
            add("open", "打开", "开", "#22C55E")
            add("reveal", "资源管理器定位", "位", "#EAB308")
        else:
            # 任意其它文件：「打开」作兜底首选。本地 AI 对二进制文件也处理不了
            # （插件自己会提示不支持），故排在打开之后作为可选项。
            add("open", "打开", "开", "#22C55E")
            if llm_ok:
                add("llm_chat", "发给本地 AI", "AI", "#00B2A9")
            add("reveal", "资源管理器定位", "位", "#EAB308")
        return out

    def _run_paste_action(self, verb: str) -> None:
        """动作分派：把载荷路径交给插件（query: str 是唯一通道）。"""
        payload = self._payload
        if payload is None:
            return
        first = payload.first_path
        query = "\n".join(payload.paths)   # 多路径用换行分隔（Windows 路径不含换行）

        if verb == "ocr":
            self._run_plugin("ocr", first)          # OCR 只认单张图片路径
        elif verb == "doc_recognize":
            self._run_plugin("doc_recognize", query)
        elif verb == "llm_chat":
            self._run_plugin("llm_chat", query)
        elif verb == "open":
            self.hide()
            self._launch(first)
        elif verb == "reveal":
            self.hide()
            self._open_file_location(first)
        elif verb == "open_settings":
            # 保留 _payload：装完插件回来 refresh_list 会按新可用性重建候选。
            self.open_settings()
            return
        else:
            return
        self._clear_payload_state()

    # ---------- 热键 ----------
    def _register_hotkey(self) -> None:
        """注册全局热键（窗口 show() 之后调用；失败不锁死，下次唤出会重试）。

        旧实现在调用 register() 后**无条件**把 _hotkey_registered 置 True：
        一旦首次因为"被别的程序占用"失败，就再也不会重试 —— 用户关掉占用它的
        程序后，热键依然失效，只能重启启动器（表现为"快捷键被占用后就彻底不能用了"）。
        现在只在真的注册成功时置位，失败的只提示一次，之后静默重试。
        """
        need_show = not getattr(self, "_hotkey_registered", False)
        need_shot = not getattr(self, "_shot_registered", False)
        need_plugins = self._need_plugin_hotkeys()
        if not need_show and not need_shot and not need_plugins:
            return
        _dbg_print("[LyLaunch] 准备注册热键（窗口已显示）…", flush=True)
        try:
            cfg = config.load_hotkey()
            self._hotkey_mods = cfg.modifiers
            self._hotkey_vk = cfg.vk
            self._hotkey_display = cfg.display
            shot = config.load_screenshot_hotkey()
            self._shot_mods = shot.modifiers
            self._shot_vk = shot.vk
            self._shot_display = shot.display
            # 直接在主窗口（已 show，HWND 稳定）上注册，最可靠。
            hwnd = int(self.winId())
            ok = register(hwnd, self._hotkey_mods, self._hotkey_vk) if need_show else True
            ok_shot = (register(hwnd, self._shot_mods, self._shot_vk, SCREENSHOT_HOTKEY_ID)
                       if need_shot else True)
            self._hotkey_registered = bool(ok)
            self._shot_registered = bool(ok_shot)
            self._register_plugin_hotkeys(hwnd)
            self._refresh_foot_hotkey()
            _dbg_print(f"[LyLaunch] 热键注册完成 hwnd={hwnd} 唤出={ok} 截图={ok_shot}", flush=True)
            if not ok or not ok_shot:
                self._warn_hotkey_conflict(ok, ok_shot)
        except Exception as e:
            print(f"[LyLaunch] 热键注册异常: {repr(e)}", flush=True)
            import traceback
            traceback.print_exc()

    def _need_plugin_hotkeys(self) -> bool:
        """是否还有「启用了、声明了 Hotkey、但尚未注册」的插件。"""
        try:
            infos = self.plugins.enabled_plugins()
        except Exception:
            return False
        done = set(getattr(self, "_plugin_hotkeys", {}).values())
        return any(
            (info.manifest.hotkey or "").strip() and info.id not in done
            for info in infos
        )

    def _register_plugin_hotkeys(self, hwnd: int) -> None:
        """按插件清单的 Hotkey 字段注册全局快捷键（如桌面净化的 Alt+D）。

        - 只登记已启用的插件；禁用后不再注册。
        - 注册失败不阻塞启动（组合可能被别的程序占用），只打日志，下次唤出重试。
        - 热键 id 从 PLUGIN_HOTKEY_BASE 起顺延，映射存 _plugin_hotkeys 供 nativeEvent 分发。
        """
        mapping: dict[int, str] = dict(getattr(self, "_plugin_hotkeys", {}))
        done = set(mapping.values())
        try:
            infos = self.plugins.enabled_plugins()
        except Exception:
            return
        for info in infos:
            spec = (info.manifest.hotkey or "").strip()
            if not spec or info.id in done:
                continue
            parsed = parse_hotkey(spec)
            if parsed is None:
                print(f"[LyLaunch] 插件热键无法解析：{info.id} -> {spec}", flush=True)
                continue
            mods, vk = parsed
            hotkey_id = PLUGIN_HOTKEY_BASE
            while hotkey_id in mapping:
                hotkey_id += 1
            if register(hwnd, mods, vk, hotkey_id):
                mapping[hotkey_id] = info.id
                _dbg_print(f"[LyLaunch] 插件热键已注册：{spec} -> {info.id} (id={hotkey_id})", flush=True)
            else:
                print(f"[LyLaunch] 插件热键注册失败（可能被占用）：{spec} -> {info.id}", flush=True)
        self._plugin_hotkeys = mapping

    def refresh_plugin_hotkeys(self) -> None:
        """插件热键配置变更后重注册：先全部注销，再按（更新后的）清单重读。

        插件管理页改完快捷键调用这里；manifest.hotkey 内存值由调用方先更新。
        """
        try:
            hwnd = int(self.winId())
        except Exception:
            return
        for hotkey_id in list(getattr(self, "_plugin_hotkeys", {}).keys()):
            unregister(hwnd, hotkey_id)
        self._plugin_hotkeys = {}
        self._register_plugin_hotkeys(hwnd)

    def _warn_hotkey_conflict(self, ok: bool, ok_shot: bool) -> None:
        """热键被占用时提示一次（每个组合各自只提示一次，不重复打扰）。

        走 theme.show_warning 而不是 QMessageBox.warning：本窗口挂了
        `background:transparent`，样式表会被子窗口继承 → 原生 QMessageBox 会变成
        黑底黑字（0.37 修）。
        """
        if not ok and not getattr(self, "_warned_show_key", False):
            self._warned_show_key = True
            _t.show_warning(
                self, "热键冲突",
                f"全局热键 {self._hotkey_display} 被其它程序占用，暂时无法用它唤出。\n\n"
                f"你可以：\n"
                f"• 在「设置 → 全局唤起热键」里换一个组合（关掉占用它的程序后也能再改回来）\n"
                f"• 或双击托盘图标唤出 LyLaunch")
        if not ok_shot and not getattr(self, "_warned_shot_key", False):
            self._warned_shot_key = True
            _t.show_warning(
                self, "热键冲突",
                f"截图快捷键 {self._shot_display} 被其它程序占用，暂时无法用它截图。\n"
                f"请在「设置 → 截图快捷键」里换一个组合。")

    def nativeEvent(self, eventType, message):  # noqa: N802  (Qt 要求的名字)
        # 在主窗口自身处理 WM_HOTKEY（窗口已显示，HWND 稳定，投递最可靠）。
        # 不要调用 super().nativeEvent(...)：历史上该路径在部分绑定版本上会递归崩溃；
        # PySide6 实测 super 可用，但保持不调更稳（返回值走下面的 (handled, result) 元组）。
        try:
            if bytes(eventType) == b"windows_generic_MSG":
                ptr = int(message)
                if ptr:
                    msg = MSG.from_address(ptr)
                    if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                        self.toggle_visibility()
                        return True, 0
                    if msg.message == WM_HOTKEY and msg.wParam == SCREENSHOT_HOTKEY_ID:
                        self.start_screenshot()
                        return True, 0
                    if msg.message == WM_HOTKEY:
                        # 插件全局热键（清单 Hotkey 字段，如桌面净化的 Ctrl+L）
                        plugin_id = getattr(self, "_plugin_hotkeys", {}).get(int(msg.wParam))
                        if plugin_id:
                            self._run_plugin(plugin_id)
                            return True, 0
                    # 点击桌面/其它窗口：窗口被 Windows 去激活 → 自动关闭面板。
                    # 这是比 Qt 的 WindowDeactivate 更可靠的原生路径（无边框+置顶窗口下
                    # WindowDeactivate 经常不触发）。排除：设置窗口打开、show 后保护期、
                    # 以及有弹出菜单（右键菜单/托盘菜单）时。
                    if msg.message == WM_ACTIVATE and (msg.wParam & 0xFFFF) == WA_INACTIVE:
                        if (not self._modal_open
                                and not self._suppress_hide
                                and QApplication.activePopupWidget() is None):
                            self.hide()
                            return True, 0
        except Exception:
            pass
        return False, 0

    def suspend_hotkeys(self) -> None:
        """临时注销两把全局热键（设置页进入按键捕获时调用）。

        为什么必须注销：RegisterHotKey 注册的组合会被 Windows **从键盘消息流里抽走**，
        改成往窗口投递 WM_HOTKEY。也就是说捕获期间只要这两把热键还注册着：
        - 按当前「截图快捷键」→ 根本到不了原生过滤器，反而真的去启动截图插件，
          全屏遮罩盖住设置窗口（设置是置顶+应用级模态），鼠标全落在遮罩上 →
          表现为"设置关不掉、快捷键也设不上"；
        - 按当前「唤起热键」→ 同样被吞，什么反馈都没有。
        注销后这些按键就变回普通 WM_KEYDOWN，才能被设置页的捕获逻辑拿到。
        """
        if self._hotkeys_suspended:
            return
        hwnd = int(self.winId())
        if self._hotkey_registered:
            unregister(hwnd, HOTKEY_ID)
        if self._shot_registered:
            unregister(hwnd, SCREENSHOT_HOTKEY_ID)
        for hotkey_id in list(getattr(self, "_plugin_hotkeys", {}).keys()):
            unregister(hwnd, hotkey_id)
        self._plugin_hotkeys = {}
        self._hotkey_registered = False
        self._shot_registered = False
        self._hotkeys_suspended = True

    def resume_hotkeys(self) -> None:
        """恢复注册（捕获结束/取消/设置窗口关闭时调用），幂等。

        只补注册"当前没注册上"的那把：捕获成功时 set_hotkey / set_screenshot_hotkey
        已经把新组合注册好并置位了，这里再注册一次同 id 同组合 Windows 会返回失败，
        反而把状态打歪（页脚会误报"未生效（被占用）"）。
        """
        if not self._hotkeys_suspended:
            return
        self._hotkeys_suspended = False
        hwnd = int(self.winId())
        if not self._hotkey_registered:
            self._hotkey_registered = bool(
                register(hwnd, self._hotkey_mods, self._hotkey_vk, HOTKEY_ID))
        if not self._shot_registered:
            self._shot_registered = bool(
                register(hwnd, self._shot_mods, self._shot_vk, SCREENSHOT_HOTKEY_ID))
        self._register_plugin_hotkeys(hwnd)
        self._refresh_foot_hotkey()

    def set_hotkey(self, mods: int, vk: int) -> tuple[bool, str]:
        """设置新热键，成功返回 (True, '')；失败返回 (False, 原因) 且**保证原热键仍可用**。

        对应 C# 的 TrySetHotkey。三个要点：
        1) 先和"截图快捷键"比一次：同一个 hwnd 上重复注册同组合必然失败，但那种失败
           不是"被别的程序占用"，不先判断就会给出误导性提示；
        2) 失败时把原组合重新注册回来，并**校验回滚是否成功** —— 回滚也失败必须如实
           告知（否则用户会以为原来的快捷键无声无息失效了，等于程序废了一半）；
        3) 成功/失败都如实更新 _hotkey_registered，让页脚状态与真实注册状态一致。
        """
        if (mods, vk) == (self._shot_mods, self._shot_vk):
            return False, "这个组合已经分配给「截图快捷键」了，请换一个组合"
        combo = format_hotkey(mods, vk)
        hwnd = int(self.winId())
        old_mods, old_vk, old_display = self._hotkey_mods, self._hotkey_vk, self._hotkey_display
        had_old = getattr(self, "_hotkey_registered", False)
        unregister(hwnd, HOTKEY_ID)
        if not register(hwnd, mods, vk, HOTKEY_ID):
            restored = register(hwnd, old_mods, old_vk, HOTKEY_ID) if had_old else False
            self._hotkey_registered = bool(restored)
            self._refresh_foot_hotkey()
            if not had_old:
                return False, f"{combo} 已被其它程序占用，请换一个组合"
            if not restored:
                return False, (f"{combo} 已被其它程序占用；原来的 {old_display} 也没能恢复，"
                               f"请再设置一个可用的组合")
            return False, f"{combo} 已被其它程序占用，已保留原来的 {old_display}（可继续使用）"
        self._hotkey_mods = mods
        self._hotkey_vk = vk
        self._hotkey_display = combo
        self._hotkey_registered = True
        self._warned_show_key = False     # 换成功后，下次真冲突还能再提示一次
        self._refresh_foot_hotkey()
        config.save_hotkey(config.HotkeyConfig(mods, vk, self._hotkey_display))
        return True, ""

    def set_screenshot_hotkey(self, mods: int, vk: int) -> tuple[bool, str]:
        """设置截图快捷键（SCREENSHOT_HOTKEY_ID）；失败保证原组合仍可用。"""
        if (mods, vk) == (self._hotkey_mods, self._hotkey_vk):
            return False, "这个组合已经分配给「全局唤起热键」了，请换一个组合"
        combo = format_hotkey(mods, vk)
        hwnd = int(self.winId())
        old_mods, old_vk, old_display = self._shot_mods, self._shot_vk, self._shot_display
        had_old = getattr(self, "_shot_registered", False)
        unregister(hwnd, SCREENSHOT_HOTKEY_ID)
        if not register(hwnd, mods, vk, SCREENSHOT_HOTKEY_ID):
            restored = register(hwnd, old_mods, old_vk, SCREENSHOT_HOTKEY_ID) if had_old else False
            self._shot_registered = bool(restored)
            if not had_old:
                return False, f"{combo} 已被其它程序占用，请换一个组合"
            if not restored:
                return False, (f"{combo} 已被其它程序占用；原来的 {old_display} 也没能恢复，"
                               f"请再设置一个可用的组合")
            return False, f"{combo} 已被其它程序占用，已保留原来的 {old_display}（可继续使用）"
        self._shot_mods = mods
        self._shot_vk = vk
        self._shot_display = combo
        self._shot_registered = True
        self._warned_shot_key = False
        config.save_screenshot_hotkey(config.HotkeyConfig(mods, vk, self._shot_display))
        return True, ""

    def start_screenshot(self) -> None:
        """全局截图快捷键入口：先隐藏主窗口（避免截到面板），再启动截图插件。

        设置页开着也能截：先把设置窗藏起来（否则截出来带设置界面），截完再放回来。
        0.37 之前这里是遇到 `_modal_open` 就直接 return，表现为"在设置页里按截图键
        毫无反应"，用户没法验证刚设好的快捷键。
        """
        if self._screenshot_active:
            return
        sw = self._settings_window
        if sw is None or not sw.isVisible():
            self._run_plugin("screenshot")
            return
        sw.hide()
        self._run_plugin("screenshot", on_done=self._restore_settings_after_shot)

    def _restore_settings_after_shot(self) -> None:
        """截图结束后把之前藏起来的设置页放回去（设置页没被关掉的前提下）。"""
        sw = self._settings_window
        if sw is None:
            return
        try:
            sw.show()
            sw.raise_()
            sw.activateWindow()
        except Exception:
            pass

    def _run_plugin(self, plugin_id: str, query: str = "", on_done=None) -> None:
        """执行插件：先把主窗口收起来，等它真正从屏幕上消失后再交给插件。

        为什么必须在 hide 之后延迟执行：截图 / OCR 这类插件是"抓当前屏幕"的，
        若在面板还可见时执行，截出来的图里就会带一块启动器面板。hide() 到 DWM
        完成合成刷新需要一点时间，延迟 80ms 后抓屏就干净了（用户几乎无感）。
        翻译、OCR 这类弹对话框的插件走同一条路径也无副作用。

        截图防重入：截图插件的 on_execute 是阻塞的（内部 loop.exec() 一直等到
        遮罩关闭），期间 Windows 仍会把 WM_HOTKEY 投递过来 —— 不拦的话每按一次
        快捷键就叠一层新遮罩。这里用 _screenshot_active 把"80ms 排队期 + 整个
        截图过程"都算作进行中，期间的重复触发直接丢弃。

        `on_done`：插件跑完（含异常）后的收尾回调，用于把临时藏起来的设置页放回去。
        """
        is_shot = plugin_id == "screenshot"
        if is_shot and self._screenshot_active:
            return
        query = query or ""
        if is_shot:
            self._screenshot_active = True
        self.hide()
        self._suppress_hide = True   # 期间失焦不再触发 hide 连锁
        self._hide_guard.start(300)

        def _exec() -> None:
            try:
                self.plugins.execute(plugin_id, query)
            finally:
                if is_shot:
                    self._screenshot_active = False
                if on_done is not None:
                    try:
                        on_done()
                    except Exception:
                        pass

        QTimer.singleShot(80, _exec)

    # ---------- 显隐 ----------
    def toggle_visibility(self) -> None:
        _dbg_print(f"[LyLaunch] toggle_visibility 被调用：visible={self.isVisible()} active={self.isActiveWindow()}", flush=True)
        if self._screenshot_active:  # 截图遮罩打开中：别把面板顶到遮罩上面
            return
        if self._modal_open:
            # 设置页开着时按唤出键：用户的意图是"回主面板"。以前这里直接 return，
            # 表现成"按了没反应"（日志却照样打 toggle_visibility）。现在改成把设置页
            # 收起来（走 done() → _on_settings_closed 自动恢复主窗口），
            # 而不是把主面板顶到设置窗前面糊一层。
            # 延后到下一轮事件循环再关：本函数是从原生 WM_HOTKEY 里调进来的，
            # 在原生消息回调里直接结束一个 exec() 嵌套事件循环不安全。
            sw = self._settings_window
            if sw is not None and sw.isVisible():
                QTimer.singleShot(0, sw.reject)
            return
        if self.isVisible() and self.isActiveWindow():
            self.hide()
        else:
            self.show_window()

    def show_window(self) -> None:
        import sys
        caller = sys._getframe(1).f_code.co_name if sys._getframe(1) else "?"
        _dbg_print(f"[LyLaunch] show_window 调用来源: {caller}", flush=True)
        self._suppress_hide = True
        self._hide_guard.start(300)
        self._center()
        _dbg_print("[LyLaunch] G1: _center 完成", flush=True)
        self.show()
        _dbg_print("[LyLaunch] G2: show 完成", flush=True)
        # 面板不进任务栏 / Alt+Tab（原生扩展样式，每次 show 后兜一次，幂等）
        self._hide_from_taskbar()
        # 毛玻璃皮肤：第一次 show() 后原生句柄才稳定，这里再开 Acrylic。
        # 失败（Win10 早期 / 关了透明效果 / 显卡不支持）就退回原版那套不透明渐变 + 投影。
        if self._skin == "glass":
            self._sync_glass()
        # 关掉 DWM 淡出过渡：截图前要 hide() 主面板再抓屏，过渡的半透明帧会被截进图里。
        # 必须在 show() 之后做（构造期碰 winId() 有崩进程的历史），用标志位保证只设一次。
        if not getattr(self, "_no_transition", False):
            _t.disable_window_transitions(self)
            self._no_transition = True
        self._apply_window_corners()
        self.raise_()
        self.activateWindow()
        _dbg_print("[LyLaunch] G3: activate 完成", flush=True)
        # 窗口已 show()，原生句柄存在，此时注册热键才安全
        self._register_hotkey()
        self.search_box.setFocus()
        self._clear_input_chip()   # 每次唤出从干净的输入框开始
        self._expanded_group = None   # 每次唤出回首页，不残留上次的「全部 >」分类视图
        # 唤出时自动感知剪贴板里的图片 / 文件（对标文本预填，但基线完全独立）。
        # 先判 mimeData：命中就进载荷态并跳过文本预填；摘要文本只在 blockSignals
        # 下写入，避免 textChanged 防抖把刚设的载荷冲掉。
        try:
            md = QApplication.clipboard().mimeData()
        except Exception:
            md = None
        try:
            fp = paste_mod.clip_fingerprint(md)
        except Exception:
            fp = ""
        if fp and fp != self._payload_consumed:
            payload = paste_mod.parse_mime(md)
            if payload.usable:
                self._purge_temp_files()
                payload.source = "sense"
                self._payload = payload
                # 同一份内容只感知一次：之后每次唤出都是正常搜索，不被候选打扰。
                self._payload_consumed = fp
                self._apply_payload_view()
                self.refresh_list()
                return

        # 剪贴板预填（对标 uTools）：只有当系统剪贴板出现"新的文本内容"（与上次已预填的
        # 不同，即"刚复制的内容"）时，打开面板把它填进输入框一次；之后没有新的复制内容
        # 则不再重复带入（旧的、空态重开都不会再次填入）。
        clip = ""
        try:
            clip = QApplication.clipboard().text().strip()
        except Exception:
            clip = ""
        # 载荷态下跳过 setText：setText 会触发 textChanged → 清掉载荷。
        if clip and clip != self._prefill_consumed and self._payload is None:
            self._prefill_text = clip    # 记下"这段是预填进来的"，供搜索态判断意图
            self.search_box.setText(clip)
            self._prefill_consumed = clip
            # 复制的文字也摆在输入框里：左侧「文」徽章 + 文字本体（uTools 感）。
            self._set_input_chip(_badge_pixmap("文", "#00B2A9"))
        self.search_box.selectAll()
        if self._payload is not None:
            # 上次唤出留下的载荷（指纹未变，不再重复感知）也要恢复预览。
            self._apply_payload_view()
        self.refresh_list()

    def _on_show_signal(self) -> None:
        # 由单实例监听线程（非 GUI 线程）调用，发信号切回主线程
        _dbg_print("[LyLaunch] _on_show_signal 触发（单实例显示信号）", flush=True)
        self.show_requested.emit()

    def _center(self) -> None:
        screen = self.screen().availableGeometry()
        x = (screen.width() - self.width()) // 2 + screen.x()
        y = (screen.height() - self.height()) // 2 + screen.y()
        self.move(x, y)

    def _on_window_context_menu(self, pos) -> None:  # noqa: N802
        menu = _style_menu(QMenu(self))
        menu.addAction("退出 LyLaunch", self.close)
        menu.exec(self.mapToGlobal(pos))

    # ---------- 列表渲染 ----------
    def _resolve_path(self, name: str, path: str) -> str:
        """路径失效时按名字在索引里找回真实路径。

        桌面被「桌面净化」收纳后，收藏/最近记录里存的旧路径（桌面根）就失效了，
        直接拿它提取图标会退化成字母瓦片。这里按名字去索引里找同名的有效项。
        """
        if not path:
            return path
        low = str(path).lower()
        if low.startswith("plugin://") or low.startswith("ms-settings:"):
            return path
        if os.path.exists(path):
            return path
        for cand in self.search_index.index:
            if cand.name == name and cand.path and os.path.exists(cand.path):
                return cand.path
        return path

    def refresh_list(self) -> None:
        # 载荷态最优先：只渲染「粘贴动作」一组，_render 末尾 _select(0) 必选中首选。
        # 放在 query 之前，且不走搜索/首页分支，避免图片被当成搜索词。
        if self._payload is not None:
            actions = self._build_paste_actions(self._payload)
            if self._payload.is_doc and not self._plugin_available("doc_recognize"):
                if self._plugin_available("llm_chat"):
                    self.empty_hint.setText(
                        "这类文件可直接选「发给本地 AI」（零模型解析）或「打开」；"
                        "想要表格 / 公式 / 多页结构化 Markdown，再到 设置 → 插件 安装「文档识别」")
                else:
                    self.empty_hint.setText(
                        "这类文件可直接「打开」或「定位」；想要结构化识别，"
                        "请到 设置 → 插件 安装「文档识别」或「本地 AI」")
            elif actions:
                # 内容已在输入框里显示，这里只讲怎么操作。
                if self._payload.is_image and self._plugin_available("doc_recognize"):
                    self.empty_hint.setText(
                        f"回车默认「{actions[0].name}」· 轻量纯文本、随包内置；"
                        "方向键选「整页转文档 (MD)」· 结构化 Markdown，首次需下载约 700MB 模型")
                else:
                    self.empty_hint.setText(
                        f"回车执行「{actions[0].name}」，方向键切换动作")
            self.empty_hint.setVisible(True)
            self._render(actions)
            return
        query = self.search_box.text().strip()
        if query:
            # 一打字就退出「全部 >」分类视图，回到正常搜索结果
            self._expanded_group = None
            # 应用与插件混排：两边用同一套相关度打分（search_rank），合并后统一降序，
            # 不再把插件单独列成一行。同分时插件在前（插件是个"动作"，通常更想点）。
            plugin_hits = self.plugins.scored_plugin_items(query)
            app_hits = self.search_index.search_scored(query)

            merged: list[tuple[int, int, Item]] = [(s, 0, it) for s, it in plugin_hits]
            merged += [(s, 1, it) for s, it in app_hits]
            merged.sort(key=lambda p: (-p[0], p[1]))   # 稳定排序：同分保持上面的先后

            items: list[Item] = []
            for _score, _kind, it in merged:
                it.group_label = "搜索结果"
                it.group_order = 0
                items.append(it)

            # 兜底：完全没搜到东西（典型是整段文字被预填 / 手动粘贴进搜索框），或者
            # 搜索词就是刚预填的剪贴板文本 —— 这两种情况用户几乎总是想拿这段文字直接
            # 处理。把插件挂出来，并把"文本翻译"提到第一个：它默认就是选中态，回车即可翻译。
            if not plugin_hits and (not app_hits or query == self._prefill_text):
                fallback = self.plugins.get_plugin_items()
                fallback.sort(key=lambda it: 0 if it.path == _PREFERRED_FALLBACK_PLUGIN else 1)
                for it in fallback:
                    it.group_label = "全部插件"
                    it.group_order = 9
                    items.append(it)

            if not app_hits and not plugin_hits:
                # 兜底列表里翻译排第一时，提示语跟着改：别让用户以为"搜不到 = 没得用"，
                # 实际上回车就能拿这段文字去翻译。只有"这段确实是待处理的文字"时才这么写，
                # 随手打的无意义短词（如 zzz）仍用普通提示，免得误导。
                if (items and items[0].path == _PREFERRED_FALLBACK_PLUGIN
                        and (query == self._prefill_text or len(query) > 6)):
                    self.empty_hint.setText(
                        f'未找到应用，回车即可用「{items[0].name}」处理这段文字')
                else:
                    self.empty_hint.setText(f'未找到“{_short_text(query)}”的相关应用')
                self.empty_hint.setVisible(True)
            else:
                self.empty_hint.setVisible(False)
        else:
            if self._expanded_group:
                # 「全部 >」分类视图：只渲染这一个分类的完整网格（不截断 8 个）
                items = self._build_home(self._expanded_group)
            else:
                items = self._build_home()
                # 首页态插件区块常驻：**只铺一行**，多出来的靠「全部 >」看
                plugins = self._sorted_plugin_items()
                self._plugin_total = len(plugins)
                items.extend(plugins[:self._PLUGIN_ROW_MAX])
            self.empty_hint.setVisible(False)
        self._render(items)

    def _build_home(self, expand_group: str | None = None) -> list[Item]:
        """首页瓦片。

        expand_group=None：正常首页，收藏应用/应用/系统应用各取前 8 个（一行），
                          收藏为空时整块不出现。
        expand_group='收藏应用'/'插件'/'应用'/'系统应用'：「全部 >」分类视图，
                          只返回该分类的**全部项目**（不截断），铺成多行网格。
        """
        # 插件的数据源本来就是「全部已启用插件」，展开时直接返回
        if expand_group == "插件":
            return self._sorted_plugin_items()

        items: list[Item] = []
        full = expand_group is not None
        limit = None if full else 8
        want_apps = not full or expand_group == "应用"
        want_sys = not full or expand_group == "系统应用"

        # ① 收藏应用（首页态最前）——**一个收藏都没有就整块不显示**，
        # 不再插「右键图标固定到此处」占位（设计说明：没收藏就别占地方）。
        # 「全部 >」展开「收藏应用」时才给完整列表，其余分类视图里不显示这一区。
        # 插件线性 SVG 映射：收藏的插件要保留原图标，不能被 make_app_item 重建丢回色块
        pinned_paths = set()
        if not full or expand_group == "收藏应用":
            plugin_svg_by_path = {it.path: it.icon_svg
                                  for it in self._sorted_plugin_items()}
            pin_row: list[Item] = []
            for p in self.pinned:
                if not p.path or p.path in pinned_paths:
                    continue
                path = self._resolve_path(p.name, p.path)
                pinned_paths.add(path)
                pin_item = self.search_index.make_app_item(
                    p.name, path, "收藏应用", launch=p.launch, args=p.args)
                # 收藏的是插件：补回线性 SVG，TileWidget 会优先用它而非色块+首字母
                svg = plugin_svg_by_path.get(p.path) or plugin_svg_by_path.get(path)
                if svg:
                    pin_item.icon_svg = svg
                pin_row.append(pin_item)
            # 首页只铺一行（最多 8 个），多出来的点「全部 >」看；展开态不截断
            if not full and limit:
                pin_row = pin_row[:limit]
            items.extend(pin_row)

        # 排除集合：已收藏的（Path 或 Name）在"应用/系统应用"区不再重复
        pinned_keys = set(pinned_paths)
        for p in self.pinned:
            if p.name:
                pinned_keys.add(p.name)

        # ② 应用行：默认最近打开的优先、其余补齐；再按用户拖出来的顺序重排，
        #    最后才截断成一行（顺序要先排过再截，否则被拖到前面的应用会看不到）
        if want_apps:
            app_seen = set(pinned_keys)
            app_row: list[Item] = []
            for r in sorted(self.recents, key=lambda x: x.last_launch, reverse=True):
                if not r.path or r.path.lower().startswith("ms-settings:"):
                    continue
                # 最近记录里存的是旧路径（桌面根），收纳后要按名字找回
                rpath = self._resolve_path(r.name, r.path)
                if rpath in app_seen or r.name in app_seen:
                    continue
                app_seen.add(rpath)
                app_seen.add(r.name)
                app_row.append(self.search_index.make_app_item(
                    r.name, rpath, "应用", launch=r.launch, args=r.args))
            for a in self.search_index.index:
                if a.path in app_seen or a.name in app_seen:
                    continue
                app_seen.add(a.path)
                app_seen.add(a.name)
                app_row.append(self.search_index.make_app_item(a.name, a.path, "应用"))
            app_row = self._apply_manual_order("应用", app_row)
            if limit:
                app_row = app_row[:limit]
            items.extend(app_row)

        # ③ 系统应用行：最近打开的优先，其余补齐（同样先排顺序再截断）
        if want_sys:
            sys_seen = set(pinned_keys)
            sys_row: list[Item] = []
            for r in sorted(self.recents, key=lambda x: x.last_launch, reverse=True):
                if r.path in sys_seen or not r.path.lower().startswith("ms-settings:"):
                    continue
                sa = next((s for s in self.search_index.system_apps
                           if s.path.lower() == r.path.lower()), None)
                if sa is None:
                    continue
                sys_seen.add(sa.path)
                # 重建 Item 会丢掉 system_apps 上挂的 icon_svg，必须补回来
                it = self.search_index.make_app_item(
                    sa.name, sa.path, "系统应用", launch=sa.launch, args=sa.launch_args)
                it.icon_svg = system_icon_svg(sa.name, sa.path)
                sys_row.append(it)
            for s in self.search_index.system_apps:
                if s.path in sys_seen:
                    continue
                sys_seen.add(s.path)
                it = self.search_index.make_app_item(
                    s.name, s.path, "系统应用", launch=s.launch, args=s.launch_args)
                it.icon_svg = system_icon_svg(s.name, s.path)
                sys_row.append(it)
            sys_row = self._apply_manual_order("系统应用", sys_row)
            if limit:
                sys_row = sys_row[:limit]
            items.extend(sys_row)

        # 注意：首页态插件区块由 refresh_list() 统一追加，不在本方法里，
        # 以保证"剪贴板内容预填 + 插件始终可见"的体验。
        return items

    def _apply_manual_order(self, group: str, row: list[Item]) -> list[Item]:
        """按用户拖出来的顺序重排某个区块。

        排过的条目按保存的顺序靠前；没排过的（新装的、新出现的）保持原有相对
        顺序排在后面 —— sorted 是稳定排序，所以不会把"最近使用优先"整块打乱。
        """
        order = config.load_tile_order(group)
        if not order:
            return row
        rank = {p.lower(): i for i, p in enumerate(order)}
        return sorted(row, key=lambda it: rank.get((it.path or "").lower(), len(order)))

    def _toggle_group(self, label: str) -> None:
        """「全部 >」/「‹ 返回」：在该分类的完整网格与首页之间切换。"""
        self._expanded_group = None if self._expanded_group == label else label
        # 清掉搜索词；blockSignals 避免 textChanged 的防抖定时器多渲染一帧
        self.search_box.blockSignals(True)
        self.search_box.clear()
        self.search_box.blockSignals(False)
        self.refresh_list()
        # 展开 / 返回都从顶部开始看，别停在上次滚动的位置
        self.scroller.verticalScrollBar().setValue(0)

    def _render(self, items: list[Item]) -> None:
        # 清掉旧瓦片：必须**先 hide() 再 deleteLater()**。
        # deleteLater 是延迟删除，控件在事件循环回来之前仍然可见、仍会绘制；
        # 同帧重绘（搜索框边打边过滤 / 剪贴板刷新）时旧图标就会叠在新瓦片上，
        # 表现为截图里那种「图标残影」。不能用 setParent(None)（会造游离顶层窗口）。
        while self.result_layout.count():
            item = self.result_layout.takeAt(0)
            w = item.widget() if item is not None else None
            if w:
                w.hide()
                w.deleteLater()
        self.tiles = []

        # 按区块分组（粘贴动作最前 → 搜索结果 → 收藏应用 → 插件 → 应用 → 系统应用 → 全部插件）
        zones: dict[str, list[Item]] = {}
        order = ["粘贴动作", "搜索结果", "收藏应用", "插件", "应用", "系统应用", "全部插件"]
        for it in items:
            zones.setdefault(it.group_label, []).append(it)

        content_width = self.result_container.width() or (self.width() - 64)
        # 平均每行 8 个应用：窗口够宽即 8 列，窄屏自适应但不超过 8
        # （窗口 860 宽时列间距取 22，列宽 ~78px 才放得下 76px 的瓦片；取 25 会差 1px）
        cols = min(8, max(1, content_width // 92))

        for label in order:
            group = zones.get(label)
            if not group:
                continue
            # 0.36 设计说明：收藏应用 / 插件 / 应用 这三个首页分组上下多留一点呼吸空间
            # Qt 边距只能整数，用「标题上+2 / 网格下+1」≈ 平均 1.5px 的折中
            extra_top = 2 if label in ("收藏应用", "插件", "应用") else 0
            extra_bottom = 1 if label in ("收藏应用", "插件", "应用") else 0
            # 区块标题：原版保留主色竖杠；玻璃皮肤去掉竖杠（那是 Windows/Android 味），
            # 改成苹果那种"小号、正字距、克制"的分组头。
            tk = _skins.active()
            glass_skin = tk.name == "glass"
            header = QWidget()
            hb = QHBoxLayout(header)
            hb.setContentsMargins(4, 6 + extra_top, 4, 4)
            hb.setSpacing(6)
            if not glass_skin:
                accent = QLabel()
                accent.setFixedSize(4, 14)
                accent.setStyleSheet("background:#2F6BEE; border-radius:2px;")
                hb.addWidget(accent)
            title = QLabel(label)
            if glass_skin:
                tf = QFont(title.font())
                tf.setPixelSize(11)          # 小一号
                tf.setWeight(QFont.Weight.DemiBold)
                tf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.8)  # 正字距
                title.setFont(tf)
                # 分组头往下压一级：让 12px 的近黑项目名成为真正的视觉主体
                # （但不能太淡，否则读不出层级）
                title.setStyleSheet("color:rgba(29,29,31,0.82);")
            else:
                title.setStyleSheet("font-size:13px; font-weight:600; color:#161C28;")
            hb.addWidget(title)
            # 数量用真实总数：插件首页只铺一行，写 8 会让人以为只有 8 个
            shown_total = (max(len(group), getattr(self, "_plugin_total", 0))
                           if label == "插件" else len(group))
            count = QLabel(str(shown_total))
            if glass_skin:
                cf = QFont(count.font())
                cf.setPixelSize(11)
                cf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.4)
                count.setFont(cf)
                count.setStyleSheet(f"color:{tk.text_secondary};")
            else:
                count.setStyleSheet("font-size:12px; color:#5F6570;")
            hb.addWidget(count)
            hb.addStretch(1)
            # 「全部 >」：一行（最多 8 个）放得下就不给按钮 —— 空着也是空着。
            # 收藏应用也能展开（0.39.5 起：超过一行时才有按钮）。
            expanded = self._expanded_group == label
            if label == "收藏应用":
                real_total = len(self.pinned)
            elif label == "插件":
                # 首页只铺一行，len(group) 是被截断后的 8，真实总数在 refresh_list 里记着
                real_total = max(len(group), getattr(self, "_plugin_total", 0))
            else:
                real_total = len(group)
            # 应用/系统应用首页按 8 截断，len(group)==8 说明"可能还有"，给按钮；
            # 收藏/插件拿到的是真实总数，超过 8 才给。
            has_more = (real_total > 8) or (
                label in ("应用", "系统应用") and len(group) >= 8)
            if label in ("收藏应用", "插件", "应用", "系统应用") and (has_more or expanded):
                all_btn = QPushButton("‹ 返回" if expanded else "全部 >")
                all_btn.setObjectName("showAllBtn")
                all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                # 关键：按钮不抢键盘焦点，否则方向键导航会被它截走
                all_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                # 设计说明：颜色淡一点、字号小一号、整体往左挪一点。
                # 常态用主色 62% 透明（同色相但更轻），hover 才回到实心主色，
                # 保证"淡"的同时仍看得出可点。
                all_btn.setStyleSheet(
                    "QPushButton{background:transparent; border:none; border-radius:8px;"
                    f" color:{tk.link}; font-size:11px; font-weight:500;"
                    " padding:3px 8px;}"
                    f"QPushButton:hover{{color:{tk.link_hover};"
                    f" background:{tk.link_hover_bg};}}")
                all_btn.setToolTip(
                    "回到首页" if expanded else f"查看全部「{label}」（共 {real_total} 个）")
                all_btn.clicked.connect(
                    lambda _c=False, g=label: self._toggle_group(g))
                hb.addWidget(all_btn)
                # addStretch 已把按钮顶到最右，右侧再留 10px 就整体左移
                hb.addSpacing(10)
            # 不加 AlignLeft：让 header 撑满整行，hb 里的 addStretch 才能把
            # 「全部 >」真正推到最右侧（带 AlignLeft 时 header 只有 sizeHint 宽）
            self.result_layout.addWidget(header)

            n = len(group)
            # 设计说明：首页单行板块与搜索结果统一用 grid——
            # 原 QHBoxLayout + addStretch 会把瓦片挤在左边、右侧留大片空白；
            # grid 按列最小宽 + Expanding 把瓦片均匀铺满整行，间距一致、视觉居中。
            if True:
                # 网格行优先从左到右填满，自动换行
                grid_widget = QWidget()
                grid = QGridLayout(grid_widget)
                grid_widget.setMinimumWidth(content_width)
                grid_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
                grid.setVerticalSpacing(6)   # 0.36 布局 A：网格间距 6px（原 18/16）
                grid.setHorizontalSpacing(6)
                grid.setContentsMargins(24, 0, 24, extra_bottom)
                grid.setAlignment(Qt.AlignmentFlag.AlignTop)
                col_w = (content_width - 48 - (cols - 1) * 6) // cols  # 间距与上面 6px 同步
                for c in range(cols):
                    grid.setColumnMinimumWidth(c, col_w)
                    grid.setColumnStretch(c, 0)
                for idx, it in enumerate(group):
                    tile = TileWidget(it)
                    tile._col_span = cols  # 记录本板块列数，供方向键导航用
                    grid.addWidget(tile, idx // cols, idx % cols)
                    self.tiles.append(tile)
                    # 带线性 SVG 的瓦片（插件 / 系统应用 / 粘贴动作）用内嵌图标，
                    # 不再走 Win32 提取：省一次后台线程，也不会出现彩色 exe 图标盖掉线稿。
                    if not getattr(it, "icon_svg", "") and not it.path.startswith("action://"):
                        IconLoader.instance().enqueue(it, it.icon_path or it.path)
                # 补齐第 0 行的空列：QGridLayout 只给「有 widget 的列」分配宽度，
                # 只有 1~2 颗瓦片时会把整行宽度全塞给那一列（实测单个瓦片
                # 143px vs 满行首颗 96px），看着像没和下面满行的板块对齐。
                # 放 1px 高、不吃鼠标的透明占位，列宽就固定成 col_w 了。
                for c in range(len(group), cols):
                    ph = QWidget()
                    ph.setFixedHeight(1)
                    ph.setMinimumWidth(col_w)
                    ph.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                    grid.addWidget(ph, 0, c)
                self.result_layout.addWidget(grid_widget, alignment=Qt.AlignmentFlag.AlignLeft)

        self.result_layout.addStretch(1)
        if self.tiles:
            self._select(0)

    # ---------- 选择 / 导航 ----------
    def _select(self, index: int) -> None:
        if not self.tiles:
            return
        index = max(0, min(index, len(self.tiles) - 1))
        if 0 <= self.sel_index < len(self.tiles):
            self.tiles[self.sel_index].set_selected(False)
        self.sel_index = index
        self.tiles[self.sel_index].set_selected(True)
        # 仅当焦点不在搜索框时才把焦点交给选中瓦片，
        # 避免搜索输入时列表刷新（每次 _render 都 _select(0)）把焦点从搜索框抢走
        if self.focusWidget() is not self.search_box:
            self.tiles[self.sel_index].setFocus()

    def _move(self, key: int) -> None:
        if not self.tiles:
            return
        cur = self.tiles[self.sel_index]
        cr = cur.geometry()  # 屏幕坐标，跨板块也可直接比较
        cx = cr.x() + cr.width() // 2
        cy = cr.y() + cr.height() // 2
        INF = float("inf")
        best_i, best_score = None, INF
        for i, t in enumerate(self.tiles):
            if t is cur:
                continue
            r = t.geometry()
            tx = r.x() + r.width() // 2
            ty = r.y() + r.height() // 2
            dx = tx - cx
            dy = ty - cy
            # 只保留指定方向上的候选
            if key == Qt.Key.Key_Right and dx <= 0:
                continue
            if key == Qt.Key.Key_Left and dx >= 0:
                continue
            if key == Qt.Key.Key_Down and dy <= 0:
                continue
            if key == Qt.Key.Key_Up and dy >= 0:
                continue
            # 主方向距离优先，垂直方向偏差给更大权重（更希望对齐）
            if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                primary, secondary = abs(dx), abs(dy)
            else:
                primary, secondary = abs(dy), abs(dx)
            score = primary + secondary * 2
            if score < best_score:
                best_score = score
                best_i = i
        if best_i is not None:
            self._select(best_i)

    def _switch_zone(self, backward: bool) -> None:
        if not self.tiles:
            return
        # 找当前选中瓦片属于哪个区块
        current = self.tiles[self.sel_index].item.group_label
        labels = ["收藏应用", "插件", "应用", "系统应用"]
        try:
            idx = labels.index(current)
        except ValueError:
            return
        # 跳过空区块：收藏为空时那一区根本没有瓦片，按过去不能"卡住不动"
        step_dir = -1 if backward else 1
        for step in range(1, len(labels) + 1):
            target = labels[(idx + step_dir * step) % len(labels)]
            for i, t in enumerate(self.tiles):
                if t.item.group_label == target:
                    self._select(i)
                    return

    # ---------- 事件过滤（搜索框里的方向键/回车/Esc）----------
    def _refresh_foot_hotkey(self) -> None:
        """页脚右侧的热键提示跟随当前唤出热键（_setup_ui 之前调用时安全跳过）。

        热键被占用时页脚直接标红"未生效"——用户至少能一眼看出是热键没注册上，
        而不是以为程序卡死了。
        """
        if getattr(self, "foot_hotkey", None) is None:
            return
        if getattr(self, "_hotkey_registered", True):
            self.foot_hotkey.setText(f"{self._hotkey_display} 唤出")
            self.foot_hotkey.setStyleSheet("color:#5F6570; font-size:11px;")
        else:
            self.foot_hotkey.setText(f"{self._hotkey_display} 未生效（被占用）")
            self.foot_hotkey.setStyleSheet("color:#D93A2F; font-size:11px;")

    def _scroll_selected_into_view(self) -> None:
        """方向键导航时把选中的瓦片滚进可视区（只上下翻页，不做横向滚动）。"""
        if not self.tiles or not (0 <= self.sel_index < len(self.tiles)):
            return
        self.scroller.ensureWidgetVisible(self.tiles[self.sel_index], 0, 20)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        # 设计说明：滚动条自动隐藏——鼠标进入滚动区显示、离开隐藏（不拦截事件）。
        # 用 getattr 防护：QApplication 级过滤器在 _setup_ui 完成前就会收到事件，
        # 那时 self.scroller 还不存在（调试时抓到的 AttributeError）。
        scroller = getattr(self, "scroller", None)
        if scroller is not None and obj is scroller:
            t = event.type()
            if t == QEvent.Type.Enter:
                scroller.verticalScrollBar().setVisible(True)
            elif t == QEvent.Type.Leave:
                scroller.verticalScrollBar().setVisible(False)
        if event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(obj, event)
        # 本过滤器装在 QApplication 上：让方向键**无论焦点落在哪个子控件上**
        # （搜索框/瓦片/滚动区/空白处）都用于移动瓦片选中。
        # 不这么做时，焦点离开搜索框后方向键会沿父链被 QScrollArea 吃掉，
        # 表现为“选不了应用，反而左右键把页面横向拖走”（页面只该上下翻页）。
        # 主窗口隐藏（唤出前/设置窗口打开中）一律放行，交给各自窗口处理。
        if not (self.isVisible() and not self._modal_open):
            return super().eventFilter(obj, event)
        key = event.key()
        # Ctrl+V：把图片 / 文件粘成载荷并列出插件动作候选。
        # 用 isAncestorOf 限定范围：过滤器装在 QApplication 上，设置窗口、
        # 插件弹窗、右键菜单都是独立顶层（不是 self 的后代），它们的 Ctrl+V
        # 必须原样放行；同时覆盖"焦点在瓦片上"的情况（obj 是 TileWidget）。
        if (key == Qt.Key.Key_V
                and (event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                and (obj is self or (isinstance(obj, QWidget) and self.isAncestorOf(obj)))):
            if self._handle_paste():
                return True                 # 图片/文件：已消费，别让 QLineEdit 再粘路径文本
            return super().eventFilter(obj, event)   # 纯文本：放行默认粘贴
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Left, Qt.Key.Key_Right):
            self._move(key)
            self._scroll_selected_into_view()
            return True
        if key == Qt.Key.Key_Tab:
            self._switch_zone(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self._scroll_selected_into_view()
            return True
        if key == Qt.Key.Key_Enter or key == Qt.Key.Key_Return:
            if self.tiles:
                self._activate(self.tiles[self.sel_index].item)
            return True
        if key == Qt.Key.Key_Escape:
            # ⚠️ 这里是全局事件过滤器，比 keyPressEvent 先拿到按键并 return True
            # 拦住传递 —— 所以「展开态 Esc 回首页」必须写在这里，写在
            # keyPressEvent 里永远不生效（2026-11-11 实测按 Esc 直接关窗口）。
            if self._expanded_group and not self.search_box.text():
                self._toggle_group(self._expanded_group)
            elif self.search_box.text():
                self.search_box.clear()   # textChanged 会顺带清掉载荷态
            else:
                self._clear_payload_state()
                self.hide()
            return True
        return super().eventFilter(obj, event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (self._press_tile is not None
                and (event.buttons() & Qt.MouseButton.LeftButton)
                and (event.pos() - self._press_pos).manhattanLength() >= 8):
            tile = self._press_tile
            self._press_tile = None      # 先清：QDrag 是阻塞的，回来后不能再判成点击
            self._start_tile_drag(tile)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # 松开：按在当时那颗瓦片上且没拖动过 → 算一次点击
        tile = self._press_tile
        self._press_tile = None
        if tile is not None and self._find_tile(self.childAt(event.pos())) is tile:
            self._activate(tile.item)
            return
        # 点空白区（含拖动窗口）后把焦点交还搜索框，保证能直接继续输入。
        # 方向键已由 QApplication 级过滤器兜底，这里只解决输入焦点问题。
        child = self.childAt(event.pos())
        if (self.isVisible() and not isinstance(child, (QLineEdit, QPushButton, TileWidget))):
            self.search_box.setFocus()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if self._context_menu_open:
            return
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Left, Qt.Key.Key_Right):
            self._move(key)
        elif key == Qt.Key.Key_Tab:
            self._switch_zone(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        elif key == Qt.Key.Key_Enter or key == Qt.Key.Key_Return:
            if self.tiles:
                self._activate(self.tiles[self.sel_index].item)
        elif key == Qt.Key.Key_Escape and not self.search_box.text():
            # 注意：全局 eventFilter 会先吃掉 Esc 并 return True，正常路径到不了这里；
            # 这份只是兜底，主逻辑见 eventFilter 的 Esc 分支。
            if self._expanded_group:
                # 「全部 >」分类视图里 Esc 先回首页，不直接把窗口藏掉
                self._toggle_group(self._expanded_group)
            else:
                self._clear_payload_state()
                self.hide()
        else:
            super().keyPressEvent(event)

    # ---------- 启动 / 激活 ----------
    def _activate(self, item: Item) -> None:
        if item.item_type == "提示" or not item.path:
            return
        if item.item_type == "剪贴板":
            self.clipboard.write(item.clip or "")
            self.hide()
            return
        # 粘贴动作瓦片：从 _payload 取路径。绝不能落到 plugin:// 分支 —— 那里
        # 传的是 search_box.text()，载荷态下为空，OCR 会拿不到图片。
        if item.path.startswith("action://"):
            self._run_paste_action(item.path[len("action://"):])
            return
        # 插件瓦片：交给插件系统执行（不记最近使用、不走 os.startfile）。
        # 必须先收起面板再执行——截图/OCR 类插件会抓屏，面板还在就会把自己截进去。
        if item.path.startswith("plugin://"):
            self._run_plugin(item.path[len("plugin://"):], self.search_box.text())
            return
        self._record_launch(item.path, item.name, item.launch, item.launch_args)
        self._launch(item.launch or item.path, item.launch_args)
        self.hide()

    def _launch(self, path: str, args: str | None = None) -> None:
        try:
            if args:
                subprocess.Popen([path, args], shell=True)
            else:
                os.startfile(path)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _record_launch(self, path: str, name: str, launch=None, args=None) -> None:
        if not path or not os.path.exists(path) or path.lower().startswith("ms-settings:"):
            return
        now = __import__("time").time_ns() // 1_000_000
        existing = next((r for r in self.recents if r.path.lower() == path.lower()), None)
        if existing:
            existing.last_launch = now
            existing.launch = launch
            existing.args = args
        else:
            self.recents.insert(0, config.RecentEntry(
                path=path, name=name or os.path.splitext(os.path.basename(path))[0],
                launch=launch, args=args, last_launch=now))
        while len(self.recents) > 50:
            self.recents.pop()
        config.save_recents(self.recents)
        self.search_index.mark_dirty()

    # ---------- 图标回填 ----------
    def _on_icon_loaded(self, item: Item, image: QImage) -> None:
        pix = QPixmap.fromImage(image)
        for t in self.tiles:
            # 按路径匹配（路径稳定）；搜索刷新会重建瓦片，用 is 比对会失配，故用路径
            if t.item.path and t.item.path == item.path:
                t.set_pixmap(pix)

    # ---------- 右键菜单 ----------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 右键瓦片：弹出固定/打开文件位置菜单
        if event.button() == Qt.MouseButton.RightButton:
            tile = self._find_tile(self.childAt(event.pos()))
            if tile and tile.item.path:
                self._select(self.tiles.index(tile))
                self._show_context_menu(tile)
                return
            super().mousePressEvent(event)
            return
        # 左键：点到瓦片 -> 选中（启动推迟到松开）；搜索框/按钮 -> 正常交互；其余 -> 拖动窗口
        if event.button() == Qt.MouseButton.LeftButton:
            tile = self._find_tile(self.childAt(event.pos()))
            if isinstance(tile, TileWidget):
                self._select(self.tiles.index(tile))
                # 按下不立刻启动：按住拖动是排序，松开且没拖动才算点击
                self._press_tile = tile
                self._press_pos = event.pos()
                return
            child = self.childAt(event.pos())
            if isinstance(child, (QLineEdit, QPushButton)):
                super().mousePressEvent(event)
                return
            if self.windowHandle():
                self.windowHandle().startSystemMove()
            return
        super().mousePressEvent(event)

    # ---------- 拖拽排序（收藏应用 / 插件 / 应用 / 系统应用） ----------
    _TILE_MIME = "application/x-lylaunch-tile"
    # 首页插件区块最多铺一行（列数上限也是 8），多的走「全部 >」
    _PLUGIN_ROW_MAX = 8
    # 可拖动的区块：顺序都存在本地（pinned.json / plugin_order.json / tile_order.json）
    _SORTABLE = ("收藏应用", "插件", "应用", "系统应用")

    @staticmethod
    def _plugin_id_of(item: Item) -> str:
        p = item.path or ""
        return p[len("plugin://"):] if p.startswith("plugin://") else p

    def _sorted_plugin_items(self) -> list[Item]:
        """插件瓦片按用户拖出来的顺序排；没排过的沿用插件扫描顺序（id 字母序）。"""
        items = self.plugins.get_plugin_items()
        order = config.load_plugin_order()
        if not order:
            return items
        rank = {pid: i for i, pid in enumerate(order)}
        # sorted 是稳定排序：order 里没列到的插件保持原来的相对顺序，排在后面
        return sorted(items, key=lambda it: rank.get(self._plugin_id_of(it), len(order)))

    def _start_tile_drag(self, tile: TileWidget) -> None:
        """按住瓦片拖动 → 起内部拖拽，用来调整首页顺序。

        可拖：收藏应用（pinned.json）、插件（plugin_order.json）、
        应用 / 系统应用（tile_order.json）。搜索结果里的瓦片不给拖 ——
        那块顺序是相关度算出来的，拖了也没意义。
        """
        grp = tile.item.group_label
        if grp not in self._SORTABLE:
            self._show_toast("搜索结果按匹配度排序，不支持拖动")
            return
        mime = QMimeData()
        mime.setData(self._TILE_MIME, f"{grp}\n{tile.item.path}".encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        # 拖拽图用**整块瓦片**（图标 + 名称），而不是只抓 40px 的图标：
        # 只抓图标时看着像拽着一个孤零零的小方块，和瓦片本身对不上，很怪。
        pm = tile.grab()
        if not pm.isNull():
            dpr = pm.devicePixelRatio() or 1.0
            # 缩到 92%：比原尺寸略小一点，拖动时更像"拿起来"而不是"盖住"
            small = pm.scaled(int(pm.width() * 0.92), int(pm.height() * 0.92),
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
            small.setDevicePixelRatio(dpr)      # 高 DPI 下不放大、不糊
            drag.setPixmap(small)
            # hotspot 用逻辑坐标：物理像素 / DPR
            drag.setHotSpot(QPoint(int(small.width() / dpr / 2),
                                   int(small.height() / dpr / 2)))
        # 原位置变成"半透明替身"：**不能 hide()** —— 隐藏的控件在 QGridLayout 里
        # 不占位，网格会重排，旁边那些瓦片会整体挪一下（设计说明：拖起来别的跟着动，很怪）。
        # 保持可见 + 降透明度，位置和尺寸都不变，布局完全不动。
        self._set_tile_ghost(tile, True)
        # 拖动期间鼠标可能甩到窗口外：先按住失焦隐藏，松手后再恢复保护期
        self._suppress_hide = True
        self._hide_guard.stop()
        try:
            drag.exec(Qt.DropAction.MoveAction)
        finally:
            self._clear_drop_hint()
            try:
                # 用「还在不在当前瓦片列表」判断有没有重排过：重排会整体重建，
                # 旧瓦片已销毁，再碰它就是野指针。
                if tile in self.tiles:
                    self._set_tile_ghost(tile, False)
            except RuntimeError:
                pass
            self._hide_guard.start(300)

    def _set_tile_ghost(self, tile: TileWidget, on: bool) -> None:
        """把瓦片调成"被拿起来"的半透明替身（原地保留占位，不挤动别的瓦片）。"""
        try:
            if on:
                eff = QGraphicsOpacityEffect(tile)
                eff.setOpacity(0.28)
                tile.setGraphicsEffect(eff)
            else:
                tile.setGraphicsEffect(None)
        except Exception:
            pass

    def _set_drop_hint(self, tile: TileWidget, side: str) -> None:
        if self._drop_hint_tile is tile and tile._drop_side == side:
            return
        self._clear_drop_hint()
        tile._drop_side = side
        self._drop_hint_tile = tile
        tile.update()

    def _clear_drop_hint(self) -> None:
        t = self._drop_hint_tile
        self._drop_hint_tile = None
        if t is not None:
            try:                     # 重排后瓦片可能已被销毁，别踩野指针
                t._drop_side = ""
                t.update()
            except RuntimeError:
                pass

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(self._TILE_MIME):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        md = event.mimeData()
        if not md.hasFormat(self._TILE_MIME):
            event.ignore()
            return
        raw = bytes(md.data(self._TILE_MIME)).decode("utf-8", "ignore")
        src_grp = raw.split("\n", 1)[0]
        tile = self._find_tile(self.childAt(event.position().toPoint()))
        if tile is None or tile.item.group_label != src_grp:
            self._clear_drop_hint()
            event.ignore()
            return
        # 注意：QDragMoveEvent / QDropEvent 只有 position()（相对接收窗口的坐标），
        # globalPosition() 是 QMouseEvent 的接口，drag 事件上调它会 AttributeError。
        centre_x = tile.mapTo(self, tile.rect().center()).x()
        self._set_drop_hint(
            tile, "before" if event.position().x() < centre_x else "after")
        event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._clear_drop_hint()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        md = event.mimeData()
        if not md.hasFormat(self._TILE_MIME):
            event.ignore()
            return
        raw = bytes(md.data(self._TILE_MIME)).decode("utf-8", "ignore")
        src_grp, _, src_path = raw.partition("\n")
        tile = self._find_tile(self.childAt(event.position().toPoint()))
        before = False
        if tile is not None:
            before = event.position().x() < tile.mapTo(self, tile.rect().center()).x()
        self._clear_drop_hint()
        if tile is not None:
            self._reorder_tile(src_grp, src_path, tile, before)
        event.acceptProposedAction()

    def _reorder_tile(self, src_grp: str, src_path: str,
                      target: TileWidget, before: bool) -> None:
        """把 src 瓦片插到 target 前面/后面（只允许同组内移动）。"""
        if target is None or target.item.group_label != src_grp:
            return
        dst_path = target.item.path
        if not src_path or not dst_path or src_path.lower() == dst_path.lower():
            return
        if src_grp == "收藏应用":
            def idx_of(path: str) -> int:
                return next((i for i, p in enumerate(self.pinned)
                             if p.path.lower() == path.lower()), -1)
            si, di = idx_of(src_path), idx_of(dst_path)
            if si < 0 or di < 0:
                return
            entry = self.pinned.pop(si)
            di = idx_of(dst_path)          # pop 之后目标索引会变，重算
            self.pinned.insert(di + (0 if before else 1), entry)
            config.save_pinned(self.pinned)
        elif src_grp == "插件":
            ids = [self._plugin_id_of(it) for it in self._sorted_plugin_items()]
            src_id = self._plugin_id_of_path(src_path)
            dst_id = self._plugin_id_of_path(dst_path)
            if src_id not in ids or dst_id not in ids:
                return
            ids.remove(src_id)
            ids.insert(ids.index(dst_id) + (0 if before else 1), src_id)
            config.save_plugin_order(ids)
        elif src_grp in ("应用", "系统应用"):
            # 当前可见的瓦片就是用户看到的顺序；重排后整份保存成本区块的手动顺序。
            # 首页只有一行时保存的是这 8 个，其余应用排在后面（换「全部 >」里还能再排）
            keys = [t.item.path for t in self.tiles
                    if t.item.group_label == src_grp and t.item.path]
            src_key = next((k for k in keys if k.lower() == src_path.lower()), "")
            dst_key = next((k for k in keys if k.lower() == dst_path.lower()), "")
            if not src_key or not dst_key:
                return
            keys.remove(src_key)
            keys.insert(keys.index(dst_key) + (0 if before else 1), src_key)
            config.save_tile_order(src_grp, keys)
        else:
            return
        self.refresh_list()

    def _plugin_id_of_path(self, path: str) -> str:
        p = path or ""
        return p[len("plugin://"):] if p.startswith("plugin://") else p

    def _find_tile(self, widget) -> TileWidget | None:
        while widget is not None:
            if isinstance(widget, TileWidget):
                return widget
            widget = widget.parent()
        return None

    def _show_context_menu(self, tile: TileWidget) -> None:
        item = tile.item
        self._context_menu_open = True
        menu = _style_menu(QMenu(self))
        pinned = any(p.path.lower() == item.path.lower() for p in self.pinned)
        pin_action = QAction("取消固定" if pinned else "添加至收藏应用")
        pin_action.triggered.connect(lambda: self._toggle_pin(item))
        menu.addAction(pin_action)
        if not item.path.lower().startswith("ms-settings:"):
            open_action = QAction("打开文件位置")
            open_action.triggered.connect(lambda: self._open_file_location(item.path))
            menu.addAction(open_action)
        menu.aboutToHide.connect(lambda: setattr(self, "_context_menu_open", False))
        menu.exec(QCursor.pos())

    def _toggle_pin(self, item: Item) -> None:
        existing = next((p for p in self.pinned if p.path.lower() == item.path.lower()), None)
        if existing:
            self.pinned.remove(existing)
            self._show_toast("已取消固定")
        else:
            self.pinned.insert(0, config.PinnedEntry(
                path=item.path, name=item.name, launch=item.launch, args=item.launch_args))
            self._show_toast("已固定到收藏应用")
        config.save_pinned(self.pinned)
        self.refresh_list()

    def _open_file_location(self, path: str) -> None:
        if path and os.path.exists(path):
            subprocess.Popen(f'explorer.exe /select,"{path}"', shell=True)

    def _show_toast(self, msg: str) -> None:
        self.toast.setText(msg)
        self.toast.setVisible(True)
        self._toast_timer.start(1500)

    # ---------- 设置 ----------
    def open_settings(self) -> None:
        from .settings_window import SettingsWindow  # 延迟导入，避免循环
        # 防重入：若设置窗口已打开，直接置顶返回，避免多次点击叠加出多个窗口
        if self._settings_window is not None and self._settings_window.isVisible():
            self._settings_window.raise_()
            self._settings_window.activateWindow()
            return
        # 让设置成为真正独立的顶层窗口（Qt 父对象设为 None），
        # 这样打开时隐藏主窗口不会把设置一起级联隐藏，且两者不会重叠成“嵌在主页”。
        # 设置窗口内部通过 self._main 持有主窗口引用（用于改热键），与 Qt 父子关系解耦。
        self._modal_open = True
        w = SettingsWindow(self)
        self._settings_window = w
        w.hotkey_display.setText(self._hotkey_display)  # 注意是 setText，别覆盖 QLabel 实例
        w.shot_hotkey_display.setText(self._shot_display)
        w.plugins_changed.connect(self.refresh_list)  # 插件增删/启停后刷新首页插件区块
        w.finished.connect(self._on_settings_closed)
        self.hide()  # 打开设置时隐藏主窗口，设置即为屏幕上唯一窗口（干净、独立）
        w.exec()

    def _on_settings_closed(self) -> None:
        # 设置关闭后：复位标志并恢复主窗口
        self._modal_open = False
        self._settings_window = None
        self._center()
        self.show()
        # 设置里刚切过皮肤时，主窗口一直是隐藏的：show() 之后补一次 Acrylic。
        self._sync_glass()
        self.raise_()
        self.activateWindow()
        self.search_box.setFocus()
        self.refresh_list()

    # ---------- 失焦隐藏 ----------
    def changeEvent(self, event) -> None:  # noqa: N802
        # 有模态对话框（设置窗口）打开时，不要隐藏主窗口——
        # 否则子窗口会随父窗口一起被级联隐藏，表现为“设置打不开/卡住”。
        # 用 getattr 兜底：构造期间（比如 setFont 派发的 FontChange）这些标志
        # 还没建好，一律按“未打开 / 不抑制”处理，别让一次字体事件把启动打断。
        if getattr(self, "_modal_open", False):
            super().changeEvent(event)
            return
        if (event.type() == QEvent.Type.WindowDeactivate
                and self.isVisible() and not getattr(self, "_suppress_hide", False)):
            self.hide()
        super().changeEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        # 关闭按钮 / Alt+F4：默认彻底退出（托盘「退出」走 quit_app）。
        # 设置里选了「最小化到托盘」时只隐藏窗口，进程与全局热键继续常驻。
        if self._close_to_tray_enabled():
            self.hide()
            self._notify_tray_once()
            event.ignore()
            return
        self._purge_temp_files(force=True)   # 退出时全清本运行期落下的临时 PNG
        self._force_exit()
        event.accept()

    def quit_app(self) -> None:
        """真正退出程序（托盘菜单「退出」用）。

        closeEvent 在「最小化到托盘」模式下只隐藏，托盘退出不能复用它，
        否则会变成"点了退出却还在后台"。
        """
        self._purge_temp_files(force=True)
        self._force_exit()

    def _close_to_tray_enabled(self) -> bool:
        """是否应把关闭动作降级成「隐藏到托盘」。

        托盘不可用（远程会话 / 资源管理器未就绪）时一律回落到退出，
        否则窗口藏起来之后就再也叫不回来了。
        """
        try:
            if config.load_close_action() != "tray":
                return False
            from PySide6.QtWidgets import QSystemTrayIcon
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return False
            tray = getattr(self, "_tray", None)
            return tray is not None and tray.isVisible()
        except Exception:
            return False

    def _notify_tray_once(self) -> None:
        """第一次隐藏到托盘时提示一句：否则用户会以为程序被关掉了。"""
        if getattr(self, "_tray_hint_shown", False):
            return
        self._tray_hint_shown = True
        try:
            tray = getattr(self, "_tray", None)
            if tray is not None:
                from PySide6.QtWidgets import QSystemTrayIcon
                tray.showMessage(
                    "LyLaunch 还在后台运行",
                    f"按 {getattr(self, '_hotkey_display', 'Alt+Space')} 可随时唤出，"
                    "托盘右键「退出」才会结束进程。",
                    QSystemTrayIcon.MessageIcon.Information, 4000)
        except Exception:
            pass

    def _force_exit(self) -> None:
        try:
            hwnd = int(self.winId())
            unregister(hwnd)
            unregister(hwnd, SCREENSHOT_HOTKEY_ID)
            for hotkey_id in list(getattr(self, "_plugin_hotkeys", {}).keys()):
                unregister(hwnd, hotkey_id)
        except Exception:
            pass
        try:
            self.clipboard.deleteLater()
        except Exception:
            pass
        # 双保险：显式停掉两套 Router（aboutToQuit 也会调，shutdown 幂等）
        try:
            from lylaunch.engine import shutdown_all
            shutdown_all()
        except Exception:
            pass
        QApplication.quit()
