"""截图插件 v2.1（LyLaunch 默认插件）：框选后可调整选区、箭头/文字标注、可直接送 OCR。

用法：点插件瓦片（或全局截图快捷键 Alt+A）→ 全屏遮罩 → 左键拖拽框选
→ 松手进入标注：底部工具栏选工具（选择/箭头/矩形/椭圆/画笔/文字），
Ctrl+Z 撤销，Enter 或「完成」复制到剪贴板，Esc 或「取消」退出；
「识别」按钮=完成并把截图交给 OCR 插件自动识别文字。

实现要点：
- 遮罩继承 QDialog（accept()/finished 只有 QDialog 有）。
- 标注画在**全屏物理像素工作层** `_canvas` 上（初始=抓屏副本），选区 `_sel` 只是
  最终截取范围：调整选区不丢标注。
- **性能**：框选/移动/调整时只局部重绘（旧/新选区并集 + 边距），不整屏刷新；
  撤销栈 8 步，closeEvent 清空释放。
- **焦点**：遮罩 StrongFocus、工具栏按钮全部 NoFocus → Enter/Esc/Ctrl+Z 始终到遮罩。
- HiDPI：抓屏/裁剪物理像素，鼠标坐标逻辑像素，差 devicePixelRatio。
- 纯 Qt，零额外依赖。

能力边界：只通过 self.ctx.api 使用 clipboard / invoke_plugin，不 import 主程序模块。
"""

from __future__ import annotations

import ctypes
import math
import os
import tempfile
from ctypes import wintypes

from PySide6.QtCore import Qt, QRect, QRectF, QPoint, QEventLoop, QPointF
from PySide6.QtGui import (
    QPixmap, QPainter, QColor, QPen, QImage, QPolygonF, QFont, QPainterPath, QCursor,
    QPolygon, QRegion,
)
from PySide6.QtWidgets import (
    QDialog, QApplication, QHBoxLayout, QWidget, QPushButton, QInputDialog, QToolTip, QLabel,
    QSlider, QMenu,
)

from lylaunch.plugins.base import PluginBase


def _dbg_print(*args, **kwargs):
    """调试输出已停用；保留调用以维持语句结构。"""
    pass


# ---------- 标注工具 ----------
TOOL_SELECT = "select"    # 选择/移动/调整选区
TOOL_ARROW = "arrow"      # 箭头
TOOL_RECT = "rect"        # 矩形
TOOL_ELLIPSE = "ellipse"  # 椭圆
TOOL_PEN = "pen"          # 画笔
TOOL_TEXT = "text"        # 文字

# 0.36 定稿色板（与 lylaunch/plugins/theme.py 同步）：浮层是浅色内容，
# 但标注色是功能色，仍保留醒目红；UI 主色统一 #2F6BEE。
MARK_COLOR = QColor("#D93A2F")  # 标注主色（--danger）
ACCENT = QColor("#2F6BEE")      # 选中/悬停主色（--accent）
HANDLE_HALF = 7                 # 选区手柄半宽（逻辑像素，14×14 更好抓）
HIT_TOLERANCE = 12              # 手柄命中容差（放宽，避免"抓不住"的生硬感）
EDGE_TOLERANCE = 6              # 边框条带命中宽度（整条边都能拖拽缩放）
UPDATE_MARGIN = 12              # （保留常量，局部重绘已改为整屏重绘）
_U45 = 0.70710678               # √2/2：自绘图标里 45° 单位向量分量


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _round_top_window(widget: QWidget, radius: int = 10) -> None:
    """给独立浮层窗口加圆角：Win11 优先 DWM 原生圆角（平滑抗锯齿），失败退回多边形遮罩。

    样式表的 border-radius 对顶层 QWidget 不裁剪背景（本机实测），
    所以浮层只能靠系统圆角或遮罩。
    """
    try:
        hwnd = int(widget.winId())
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), 33,  # DWMWA_WINDOW_CORNER_PREFERENCE
                ctypes.byref(pref), ctypes.sizeof(pref)) == 0:
            return
    except Exception:
        pass
    w, h = widget.width(), widget.height()
    if w <= 0 or h <= 0:
        return
    r = min(radius, w // 2, h // 2)
    corners = [(r, r, 180, 270), (w - r, r, 270, 360),
               (w - r, h - r, 0, 90), (r, h - r, 90, 180)]
    pts = []
    for cx, cy, a0, a1 in corners:
        for i in range(17):
            a = math.radians(a0 + (a1 - a0) * i / 16)
            pts.append(QPoint(int(round(cx + r * math.cos(a))),
                              int(round(cy + r * math.sin(a)))))
    widget.setMask(QRegion(QPolygon(pts)))


def _dist_to_seg(p: QPoint, a: QPoint, b: QPoint) -> float:
    """点 p 到线段 ab 的最短距离（用于命中箭头标注）。"""
    ax, ay, bx, by = a.x(), a.y(), b.x(), b.y()
    dx, dy = bx - ax, by - ay
    px, py = p.x(), p.y()
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / float(dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _window_rect_at(px: int, py: int) -> QRect | None:
    """给定**物理屏幕坐标**，返回该点所在顶层窗口的矩形（**逻辑屏幕坐标**）。

    ⚠ 关键：截图遮罩本身是全屏置顶窗口，`WindowFromPoint` 会**命中遮罩自己**
    （表现为"鼠标一动就变成全屏、换不了窗口"）。所以这里按 Z 序向下遍历，
    **跳过本进程的所有窗口**（遮罩/工具栏/主窗口），只认其他程序的可见窗口。

    返回 None：桌面/任务栏外壳、找不到、太小。
    物理 → 逻辑按所在屏幕的 devicePixelRatio 换算（125% 缩放时为 1.25）。
    """
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.WindowFromPoint.argtypes = [_POINT]
        user32.WindowFromPoint.restype = ctypes.c_void_p
        user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetWindow.restype = ctypes.c_void_p
        user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]

        pt = _POINT(px, py)
        hwnd = user32.WindowFromPoint(pt)
        own_pid = kernel32.GetCurrentProcessId()
        pid = wintypes.DWORD()
        target = None
        guard = 0
        while hwnd and guard < 96:
            guard += 1
            user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
            if pid.value != own_pid and user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
                rr = wintypes.RECT()
                user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rr))
                if rr.left <= px < rr.right and rr.top <= py < rr.bottom:
                    target = hwnd
                    break
            hwnd = user32.GetWindow(ctypes.c_void_p(hwnd), 2)  # GW_HWNDNEXT：Z 序下一个
        if not target:
            return None
        hwnd = target
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        hwnd = user32.GetAncestor(ctypes.c_void_p(hwnd), 2) or hwnd  # GA_ROOT：取顶层窗口
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(ctypes.c_void_p(hwnd), cls, 256)
        if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd",
                         "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow"):
            return None
        r = wintypes.RECT()
        got = False
        try:
            # 优先用 DWM 的"可见边框"：GetWindowRect 会带上 Win10/11 的不可见
            # 外框（约 7~8px 阴影），直接框出来会偏大一圈
            dwm = ctypes.windll.dwmapi
            if dwm.DwmGetWindowAttribute(ctypes.c_void_p(hwnd), 9,  # DWMWA_EXTENDED_FRAME_BOUNDS
                                         ctypes.byref(r), ctypes.sizeof(r)) == 0:
                got = True
        except Exception:
            got = False
        if not got:
            user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
        pw, ph = r.right - r.left, r.bottom - r.top
        if pw <= 16 or ph <= 16:
            return None
        # 找该点所在屏幕，按其 dpr 换算成逻辑坐标
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        dpr = 1.0
        if app is not None:
            for scr in app.screens():
                g = scr.geometry()
                d = scr.devicePixelRatio() or 1.0
                if (int(g.x() * d) <= px < int((g.x() + g.width()) * d)
                        and int(g.y() * d) <= py < int((g.y() + g.height()) * d)):
                    dpr = d
                    break
        return QRect(int(round(r.left / dpr)), int(round(r.top / dpr)),
                     int(round(pw / dpr)), int(round(ph / dpr)))
    except Exception:
        return None


def _window_rect_under_cursor() -> QRect | None:
    """鼠标下方顶层窗口的矩形（逻辑坐标）。"""
    try:
        pt = _POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return None
        return _window_rect_at(pt.x, pt.y)
    except Exception:
        return None


class _ToolButton(QPushButton):
    """工具栏图标按钮：checkable，选中高亮，不抢键盘焦点。

    图标用 QPainter **矢量自绘**，不再用 `▢ / ◯ / ↗ / ✎` 这类 Unicode 字形：
    不同字形在同一 font-size 下的实际视觉尺寸天生不一致（▢ 偏小、◯ 偏大），
    对齐基线也各不相同，怎么调字号都对不齐。自绘可以让四个图标统一按
    同一个视觉半径 r 构图，视觉大小严格一致。
    """

    _SIZE = (34, 28)          # 按钮固定尺寸（和文字的胶囊按钮等高）
    _R = 6.0                  # 图标统一视觉半径（矩形/圆形共用）
    _W = 1.8                  # 图标线宽

    def __init__(self, tool: str) -> None:
        super().__init__("")
        self._tool = tool
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(*self._SIZE)
        # 只留底/悬停/选中态，图标与颜色全部交给 paintEvent
        self.setStyleSheet(
            "QPushButton{border:none; border-radius:8px; background:transparent;}"
            "QPushButton:hover{background:#E4EDFC;}"
            "QPushButton:pressed{background:#D6E2F7;}"
            "QPushButton:checked{background:rgba(47,107,238,0.16);}"
        )

    # ---------- 自绘 ----------
    def paintEvent(self, event) -> None:
        super().paintEvent(event)      # 先让样式表画底（悬停/选中/按下）
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor("#2F6BEE" if self.isChecked() else "#161C28")
        pen = QPen(color, self._W)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        cx = self.width() / 2.0
        cy = self.height() / 2.0
        r = self._R

        if self._tool == TOOL_RECT:
            # 圆角矩形：外沿 = r + 线宽/2，和圆形外沿完全一致
            p.drawRoundedRect(
                QRectF(cx - r, cy - r, r * 2, r * 2), 1.6, 1.6)
        elif self._tool == TOOL_ELLIPSE:
            p.drawEllipse(QPointF(cx, cy), r, r)
        elif self._tool == TOOL_ARROW:
            # 45° 斜箭头（左下 → 右上）：杆 + 实心三角头
            ux, uy = _U45, -_U45          # 单位方向：指向右上
            nx, ny = _U45, _U45           # 方向法线
            tailx, taily = cx - 5.4, cy + 5.4        # 尾端
            tipx, tipy = cx + 5.4, cy - 5.4          # 箭尖
            # 底边中点 = 箭尖沿反方向回退 6.2（注意 uy 是负数，减 -0.707 才是往下）
            basex, basey = tipx - ux * 6.2, tipy - uy * 6.2
            half = 2.55                              # 三角底边半宽
            p.drawLine(QPointF(tailx, taily), QPointF(basex, basey))
            p.setBrush(color)
            p.drawPolygon(QPolygonF([
                QPointF(tipx, tipy),
                QPointF(basex + nx * half, basey + ny * half),
                QPointF(basex - nx * half, basey - ny * half),
            ]))
        elif self._tool == TOOL_PEN:
            # 铅笔：斜置笔身 + 削尖的笔头，bbox 与矩形/圆形对齐
            ux, uy = _U45, -_U45          # 笔身方向：左下 → 右上
            nx, ny = _U45, _U45           # 笔身法线
            half_w = 2.15                 # 笔身半宽
            tipx, tipy = cx - 5.6, cy + 5.6          # 笔尖
            neckx, necky = tipx + ux * 2.8, tipy + uy * 2.8
            backx, backy = tipx + ux * 13.4, tipy + uy * 13.4
            p.drawPolygon(QPolygonF([
                QPointF(tipx, tipy),
                QPointF(neckx + nx * half_w, necky + ny * half_w),
                QPointF(backx + nx * half_w, backy + ny * half_w),
                QPointF(backx - nx * half_w, backy - ny * half_w),
                QPointF(neckx - nx * half_w, necky - ny * half_w),
            ]))
        p.end()


class ScreenshotOverlay(QDialog):
    """单屏截图遮罩：框选 → 调整 → 标注 → 完成复制 / 送 OCR。"""

    def __init__(self, screen_image: QPixmap, parent=None,
                 initial_sel: QRect | None = None) -> None:
        super().__init__(parent)
        self._screen_image = screen_image          # 抓屏（物理像素）
        self._canvas: QImage = screen_image.toImage().copy()  # 标注工作层
        self._display: QPixmap | None = None       # 窗口尺寸显示缓存（canvas 缩放一次，拖拽不重建）
        self._sel: QRect | None = initial_sel      # 选区（逻辑坐标）；可预置为"鼠标下的窗口"
        self._tool = TOOL_SELECT
        # 标注改成**对象模型**：每个标注是一条记录（逻辑坐标），不再画死在画布上，
        # 这样才能像 QQ 截图一样"画完还能拖动/调整"
        self._marks: list[dict] = []               # [{"kind":"arrow"|"rect","a":QPoint,"b":QPoint}]
        self._history: list[list[dict]] = []       # 撤销栈（标注列表快照，8 步）
        self._drag_mark: dict | None = None        # 正在画的新标注
        self._drag_mark_idx: int | None = None     # 正在拖动的已有标注索引
        self._drag_mark_off = QPoint(0, 0)         # 拖动时鼠标相对标注起点的偏移
        self.result: QPixmap | None = None
        self._want_ocr = False                     # 完成后把截图交给 OCR 识别
        self._width = 5                            # 当前线宽（画笔/箭头/矩形/圆），默认 5
        self._color = QColor(MARK_COLOR)           # 当前标注颜色

        self._drag_kind = None        # start/move/resize/draw
        self._drag_handle = None
        self._drag_start: QPoint | None = None
        self._drag_cur: QPoint | None = None
        self._last_sel: QRect | None = None        # 上次选区（重绘用）
        self._resize_orig: QRect | None = None     # resize 起始矩形（实时缩放锚点）
        self._hover_handle: str | None = None      # 当前悬停的手柄/边（绘制高亮提示）
        self._right_down = False                   # 右键按下标记（松开才退出，避免泄漏给桌面）
        self._hover_win: QRect | None = None       # 鼠标下窗口的"预选预览"框（QQ 式，未落选中）
        self._press_win: QRect | None = None       # 按下时锁存的预选窗口（松手单击即选中它）

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setCursor(Qt.CursorShape.CrossCursor)
        # ⚠ 必须开启鼠标跟踪：否则 mouseMoveEvent 只在**按住鼠标**时触发，
        # 悬停到边/角时不产生事件 → 光标不变形、也没有高亮提示（踩过）。
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._build_toolbar()

    # ---------- 工具栏 ----------
    def _build_toolbar(self) -> None:
        # 独立 Tool 顶层窗口（不 setParent）：系统级置顶，避免被其他窗口遮
        self.toolbar = QWidget()
        self.toolbar.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            # 关键：工具栏**永不接受焦点** —— 否则点一下工具栏按钮/显示时激活它，
            # 键盘焦点就从全屏遮罩跑到工具栏，Esc / Enter / Ctrl+Z 全部失效（踩过）
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.toolbar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # 浅色浮层（对齐 QQ 截图工具栏）：白底 + 极淡描边；圆角由 _round_top_window 处理
        self.toolbar.setStyleSheet(
            "QWidget{background:#FFFFFF; border-radius:12px;"
            " border:1px solid #D8E3F5;}")
        lay = QHBoxLayout(self.toolbar)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(2)
        self._tool_btns: dict[str, _ToolButton] = {}
        # 工具组：图标按钮（无文字，靠 tooltip 说明）——对齐 QQ 的紧凑观感
        # 不放"选择"按钮：选择是**默认状态**，再点一次当前工具即可切回（见 _toggle_tool）
        for tip, tool in [
            ("箭头标注（再点一次切回选择）", TOOL_ARROW),
            ("矩形标注（再点一次切回选择）", TOOL_RECT),
            ("圆形标注（再点一次切回选择）", TOOL_ELLIPSE),
            ("画笔（自由绘制，再点一次切回选择）", TOOL_PEN),
        ]:
            b = _ToolButton(tool)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, t=tool: self._toggle_tool(t))
            lay.addWidget(b)
            self._tool_btns[tool] = b
        # 粗细按钮：点开浮层调线宽/颜色
        self._width_btn = QPushButton("粗细")
        self._width_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._width_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._width_btn.setStyleSheet(_TOOLBAR_BTN)
        self._width_btn.setToolTip("设置线条粗细与颜色")
        self._width_btn.clicked.connect(self._toggle_width_popup)
        lay.addWidget(self._width_btn)
        lay.addSpacing(2)
        sep = QLabel()   # 分组分隔线（QLabel 会正常绘制样式表背景，普通 QWidget 不会）
        sep.setFixedSize(1, 18)
        sep.setStyleSheet("background:#D8E3F5;")
        lay.addWidget(sep)
        lay.addSpacing(2)
        undo = QPushButton("↶ 撤销")
        undo.setCursor(Qt.CursorShape.PointingHandCursor)
        undo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        undo.setStyleSheet(_TOOLBAR_BTN)
        undo.setToolTip("撤销上一步（Ctrl+Z）")
        undo.clicked.connect(self._undo)
        lay.addWidget(undo)
        ocr = QPushButton("图片转文字")
        ocr.setCursor(Qt.CursorShape.PointingHandCursor)
        ocr.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        ocr.setStyleSheet(_TOOLBAR_BTN)
        ocr.setToolTip("完成并用「图片转文字」提取图中文字")
        ocr.clicked.connect(self._on_ocr)
        lay.addWidget(ocr)
        ok = QPushButton("✓ 完成")
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        ok.setStyleSheet(_TOOLBAR_OK)
        ok.clicked.connect(self._finish)
        lay.addWidget(ok)
        cancel = QPushButton("✕")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        cancel.setStyleSheet(_TOOLBAR_CLOSE)
        cancel.setToolTip("取消截图（Esc / 右键）")
        cancel.clicked.connect(self.reject)
        lay.addWidget(cancel)
        self._build_width_popup()
        self.toolbar.hide()

    def _build_width_popup(self) -> None:
        """粗细/颜色浮层：QQ 式小面板，挂在"粗细"按钮下方。"""
        self.width_pop = QWidget()
        self.width_pop.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.width_pop.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.width_pop.setStyleSheet(
            "QWidget{background:#FFFFFF; border-radius:10px;"
            " border:1px solid #D8E3F5;}")
        lay = QHBoxLayout(self.width_pop)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(8)
        cap = QLabel("粗细")
        cap.setStyleSheet("border:none; background:transparent; color:#161C28;"
                          " font-size:12px; font-weight:600;")
        lay.addWidget(cap)
        self._width_slider = QSlider(Qt.Orientation.Horizontal)
        self._width_slider.setRange(1, 20)
        self._width_slider.setValue(self._width)
        self._width_slider.setFixedWidth(110)
        self._width_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._width_slider.setStyleSheet(
            "QSlider{border:none; background:transparent;}"
            "QSlider::groove:horizontal{height:4px; background:#D8E3F5; border-radius:2px;}"
            "QSlider::sub-page:horizontal{background:#2F6BEE; border-radius:2px;}"
            "QSlider::handle:horizontal{width:14px; height:14px; margin:-5px 0;"
            " background:#FFFFFF; border:2px solid #2F6BEE; border-radius:7px;}"
        )
        self._width_slider.valueChanged.connect(self._on_width_changed)
        lay.addWidget(self._width_slider)
        self._width_val = QLabel(str(self._width))
        self._width_val.setFixedWidth(18)
        self._width_val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._width_val.setStyleSheet("border:none; background:transparent; color:#161C28;"
                                     " font-size:12px; font-weight:600;")
        lay.addWidget(self._width_val)
        self._color_btn = QPushButton()
        self._color_btn.setFixedSize(20, 20)
        self._color_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._color_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._color_btn.setToolTip("标注颜色")
        self._color_btn.clicked.connect(self._pick_color)
        self._refresh_color_btn()
        lay.addWidget(self._color_btn)
        self.width_pop.hide()

    def _refresh_color_btn(self) -> None:
        self._color_btn.setStyleSheet(
            f"QPushButton{{background:{self._color.name()}; border-radius:6px;"
            f" border:1px solid #C3D3EE;}}")

    def _on_width_changed(self, v: int) -> None:
        self._width = int(v)
        self._width_val.setText(str(v))

    def _set_color(self, name: str) -> None:
        self._color = QColor(name)
        self._refresh_color_btn()

    def _pick_color(self) -> None:
        menu = QMenu(self.width_pop)
        menu.setStyleSheet(
            "QMenu{background:#FFFFFF; border:1px solid #D8E3F5;"
            " border-radius:8px; padding:4px;}"
            "QMenu::item{padding:6px 20px; border-radius:5px; color:#161C28;"
            " font-size:12px; font-weight:600;}"
            "QMenu::item:selected{background:#E4EDFC;}")
        for name, col in [("红", "#D93A2F"), ("橙", "#FF9500"), ("黄", "#FFCC00"),
                          ("绿", "#34C759"), ("蓝", "#2F6BEE"), ("紫", "#AF52DE")]:
            act = menu.addAction(name)
            act.triggered.connect(lambda _=False, c=col: self._set_color(c))
        menu.exec(QCursor.pos())

    def _toggle_width_popup(self) -> None:
        if self.width_pop.isVisible():
            self.width_pop.hide()
            return
        self.width_pop.adjustSize()
        tw, th = self.width_pop.width(), self.width_pop.height()
        origin = self._width_btn.mapToGlobal(QPoint(0, self._width_btn.height() + 6))
        x = origin.x() + self._width_btn.width() // 2 - tw // 2
        y = origin.y()
        self.width_pop.setGeometry(x, y, tw, th)
        _round_top_window(self.width_pop, 10)
        self.width_pop.show()
        self.width_pop.raise_()

    def _invalidate_display(self) -> None:
        """canvas 变化后失效显示缓存，下次 paintEvent 重建。"""
        self._display = None

    def _ensure_display(self) -> None:
        """构建显示缓存：物理像素图 + 设置 devicePixelRatio，交给 Qt 做 1:1 渲染。

        ⚠ 这里绝不能缩放！canvas 是物理像素（如 2560×1440），窗口是逻辑像素
        （125% 缩放下只有 2048×1152）。以前用 scaled(width, height) 把大图缩小
        20% 再显示，整屏文字发虚、像打了马赛克（但裁剪取的是原图，所以截图质量
        不受影响）。正确做法：给 QPixmap 设上 dpr，它的逻辑尺寸自然等于窗口尺寸，
        绘制时按物理像素 1:1 映射，清晰度与原图一致。
        """
        if self._display is None:
            pm = QPixmap.fromImage(self._canvas)
            dpr = self.devicePixelRatioF()
            scr = self.screen()
            if scr is not None and scr.devicePixelRatio() > dpr:
                dpr = scr.devicePixelRatio()  # 首帧控件 dpr 可能还没同步到屏幕值
            if dpr <= 0:
                dpr = 1.0
            pm.setDevicePixelRatio(dpr)
            self._display = pm

    # ---------- 状态 ----------
    def _scale(self) -> float:
        """逻辑坐标 → canvas 物理坐标的比例（canvas/窗口实际尺寸，不依赖 dpr）。"""
        return self._canvas.width() / max(1, self.width()) if self._canvas.width() else 1.0

    def _phys(self, p: QPoint) -> QPoint:
        s = self._scale()
        return QPoint(int(p.x() * s), int(p.y() * s))

    def _toggle_tool(self, tool: str) -> None:
        """点工具切换；**再点同一个工具**则回到"选择"模式（工具栏没有单独的选择按钮）。"""
        self._set_tool(TOOL_SELECT if self._tool == tool else tool)

    def _set_tool(self, tool: str) -> None:
        self._tool = tool
        self._cancel_drag()
        self._hover_handle = None  # 切工具后清掉悬停高亮
        if getattr(self, "width_pop", None) is not None:
            self.width_pop.hide()
        for t, b in self._tool_btns.items():
            b.setChecked(t == tool)
        self.setCursor(
            Qt.CursorShape.CrossCursor if tool != TOOL_SELECT else Qt.CursorShape.ArrowCursor)
        self.update()

    @staticmethod
    def _clone_mark(m: dict) -> dict:
        """深拷贝一条标注（历史快照用）。"""
        d = {"kind": m["kind"], "w": m.get("w", 2), "c": QColor(m.get("c") or MARK_COLOR)}
        if "a" in m:
            d["a"] = QPoint(m["a"])
        if "b" in m:
            d["b"] = QPoint(m["b"])
        if m.get("pts"):
            d["pts"] = [QPoint(p) for p in m["pts"]]
        return d

    def _push_history(self) -> None:
        """快照当前标注列表（对象模型时代的历史栈）。"""
        self._history.append([self._clone_mark(m) for m in self._marks])
        if len(self._history) > 8:
            self._history.pop(0)

    def _undo(self) -> None:
        if not self._history:
            QToolTip.showText(self.cursor().pos(), "没有可撤销的操作")
            return
        self._marks = self._history.pop()
        self.update()

    def _cancel_drag(self) -> None:
        self._drag_kind = None
        self._drag_handle = None
        self._drag_start = None
        self._drag_cur = None

    # ---------- 选区手柄 ----------
    def _handles(self, sel: QRect) -> list[tuple[str, QRect]]:
        x, y, w, h = sel.x(), sel.y(), sel.width(), sel.height()
        hs = [
            ("nw", QRect(x - HANDLE_HALF, y - HANDLE_HALF, HANDLE_HALF * 2, HANDLE_HALF * 2)),
            ("n",  QRect(x + w // 2 - HANDLE_HALF, y - HANDLE_HALF, HANDLE_HALF * 2, HANDLE_HALF * 2)),
            ("ne", QRect(x + w - HANDLE_HALF, y - HANDLE_HALF, HANDLE_HALF * 2, HANDLE_HALF * 2)),
            ("e",  QRect(x + w - HANDLE_HALF, y + h // 2 - HANDLE_HALF, HANDLE_HALF * 2, HANDLE_HALF * 2)),
            ("se", QRect(x + w - HANDLE_HALF, y + h - HANDLE_HALF, HANDLE_HALF * 2, HANDLE_HALF * 2)),
            ("s",  QRect(x + w // 2 - HANDLE_HALF, y + h - HANDLE_HALF, HANDLE_HALF * 2, HANDLE_HALF * 2)),
            ("sw", QRect(x - HANDLE_HALF, y + h - HANDLE_HALF, HANDLE_HALF * 2, HANDLE_HALF * 2)),
            ("w",  QRect(x - HANDLE_HALF, y + h // 2 - HANDLE_HALF, HANDLE_HALF * 2, HANDLE_HALF * 2)),
        ]
        return hs

    def _hit_handle(self, pos: QPoint) -> str | None:
        """命中测试：先看 8 个手柄（容差放宽），再整条边框条带。

        以前只能精确压到小方块才能拖，手感很生硬；现在鼠标靠近任意一条边
        的内外 6px 都能触发对应方向的缩放。
        """
        if self._sel is None:
            return None
        for name, r in self._handles(self._sel):
            if r.adjusted(-HIT_TOLERANCE, -HIT_TOLERANCE, HIT_TOLERANCE, HIT_TOLERANCE).contains(pos):
                return name
        sel = self._sel
        e = EDGE_TOLERANCE
        if not sel.adjusted(-e, -e, e, e).contains(pos):
            return None
        # 距哪条边最近就判哪个方向（四角已由上面的手柄分支接管）
        dy_top = abs(pos.y() - sel.top())
        dy_bot = abs(pos.y() - sel.bottom())
        dx_left = abs(pos.x() - sel.left())
        dx_right = abs(pos.x() - sel.right())
        nearest = min(dy_top, dy_bot, dx_left, dx_right)
        if nearest > e:
            return None
        if nearest == dy_top:
            return "n"
        if nearest == dy_bot:
            return "s"
        if nearest == dx_left:
            return "w"
        return "e"

    def _cursor_for_handle(self, name: str):
        return {
            "nw": Qt.CursorShape.SizeFDiagCursor, "se": Qt.CursorShape.SizeFDiagCursor,
            "ne": Qt.CursorShape.SizeBDiagCursor, "sw": Qt.CursorShape.SizeBDiagCursor,
            "n": Qt.CursorShape.SizeVerCursor, "s": Qt.CursorShape.SizeVerCursor,
            "e": Qt.CursorShape.SizeHorCursor, "w": Qt.CursorShape.SizeHorCursor,
        }.get(name, Qt.CursorShape.ArrowCursor)

    def _resize_sel(self, name: str, start: QRect, cur: QPoint) -> QRect:
        """按拖拽的边/角重算选区：**被拖的那条边动，对边固定**。

        ⚠ 原来的实现只改了坐标没同步改尺寸（拖上边时只动 y、height 不变），
        结果底边跟着一起走 → ne/sw 表现为"整块平移"而不是对角缩放。
        正确做法是用四条边的坐标运算：拖 n 只动 top（bottom 固定），
        拖 w 只动 left（right 固定），角就是两条边同时变。
        """
        MIN = 5
        left, top = start.left(), start.top()
        right, bottom = start.right(), start.bottom()
        if "n" in name:
            top = min(cur.y(), bottom - MIN)
        if "s" in name:
            bottom = max(cur.y(), top + MIN)
        if "w" in name:
            left = min(cur.x(), right - MIN)
        if "e" in name:
            right = max(cur.x(), left + MIN)
        return QRect(QPoint(left, top), QPoint(right, bottom)).normalized()

    def _repaint_sel_change(self, old: QRect | None, new: QRect | None) -> None:
        """选区变化时**整屏重绘**（不再只刷新旧∪新选区）。

        ⚠ 为什么放弃局部重绘：窗口贴的是“物理像素图 + devicePixelRatio 1.25”。
        Qt 对**被裁剪的分数缩放 blit** 计算源偏移时会取整，脏区里就贴出别处的
        画面——表现就是拖拽时沿轨迹出现蓝色残影（踩过）。整屏重绘不做裁剪偏移，
        残影消失；全屏 2560×1440 一次 blit 在拖拽节奏下开销可接受。
        """
        if old is None and new is None:
            return
        self.update()

    # ---------- 鼠标事件 ----------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            # 右键退出：**按下只记账，等松开再真正退出**。
            # 若按下就 reject，遮罩立刻消失，这次右键的"松开"就不再由遮罩接收、
            # 会落到下面的窗口（桌面上就弹出右键菜单，踩过）。
            self._right_down = True
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position().toPoint()
        # 在遮罩上按下 → 收起粗细浮层（QQ 式：点别处即关）
        if getattr(self, "width_pop", None) is not None and self.width_pop.isVisible():
            if not self.width_pop.geometry().contains(event.globalPosition().toPoint()):
                self.width_pop.hide()
        if self._sel is None:
            # 初始：先锁存"当前预览的窗口"——松手时若几乎没拖动就选中它（QQ 式单击选中）
            self._press_win = QRect(self._hover_win) if self._hover_win is not None else None
            self._drag_kind = "start"
            self._drag_start = pos
            self._drag_cur = pos
            self._sel = QRect(pos, pos)
            self._last_sel = None
        elif self._tool == TOOL_SELECT:
            handle = self._hit_handle(pos)
            mark_idx = None if handle else self._hit_mark(pos)
            if handle:
                self._drag_kind = "resize"
                self._drag_handle = handle
                self._drag_start = pos
                self._last_sel = QRect(self._sel)
                self._resize_orig = QRect(self._sel)  # 实时缩放的锚点
            elif mark_idx is not None:
                # 压到已有标注上 → 拖动这条标注（QQ 式：画完还能挪）
                self._push_history()
                self._drag_kind = "move_mark"
                self._drag_mark_idx = mark_idx
                m = self._marks[mark_idx]
                anchor = m["a"] if "a" in m else (m["pts"][0] if m.get("pts") else pos)
                self._drag_mark_off = QPoint(pos.x() - anchor.x(), pos.y() - anchor.y())
            elif self._sel.contains(pos):
                self._drag_kind = "move"
                self._drag_start = pos
                self._last_sel = QRect(self._sel)
            else:
                # 选区外点击：重新框选
                self._sel = None
                self._hover_handle = None
                self._history.clear()
                self._marks.clear()
                self._canvas = self._screen_image.toImage().copy()
                self._invalidate_display()
                self.toolbar.hide()
                self.update()
                self._drag_kind = "start"
                self._drag_start = pos
                self._drag_cur = pos
                self._sel = QRect(pos, pos)
                self._last_sel = None
        else:
            # 标注工具：压到已有标注上 → 拖动它（QQ 式，不必先切回"选择"）；否则新建
            idx = self._hit_mark(pos)
            if idx is not None:
                self._push_history()
                self._drag_kind = "move_mark"
                self._drag_mark_idx = idx
                m = self._marks[idx]
                self._drag_mark_off = QPoint(pos.x() - m["a"].x(), pos.y() - m["a"].y())
            else:
                self._push_history()
                self._drag_kind = "draw"
                self._drag_start = pos
                self._drag_cur = pos
                m = {"kind": self._tool, "w": self._width, "c": QColor(self._color)}
                if self._tool == TOOL_PEN:
                    m["pts"] = [QPoint(pos)]          # 画笔：记录轨迹点
                else:
                    m["a"] = QPoint(pos)
                    m["b"] = QPoint(pos)
                self._drag_mark = m
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        if self._drag_kind == "start":
            self._drag_cur = pos
            old = self._last_sel
            self._sel = QRect(self._drag_start, pos).normalized()
            self._last_sel = QRect(self._sel)
            self._repaint_sel_change(old, self._sel)
        elif self._drag_kind == "draw":
            self._drag_cur = pos
            m = self._drag_mark
            if m is not None:
                if m["kind"] == TOOL_PEN:
                    pts = m["pts"]
                    # 相邻点相差 ≥2px 才记录，避免轨迹点爆炸
                    if not pts or abs(pos.x() - pts[-1].x()) + abs(pos.y() - pts[-1].y()) >= 2:
                        pts.append(QPoint(pos))
                else:
                    m["b"] = QPoint(pos)
                self.update()  # 整屏重绘（局部重绘会因分数 dpr 的裁剪偏移产生残影）
        elif self._drag_kind == "move_mark" and self._drag_mark_idx is not None:
            # 拖动已有标注：整体平移（起点跟着光标，终点/轨迹保持相对位置）
            m = self._marks[self._drag_mark_idx]
            na = pos - self._drag_mark_off
            if m["kind"] == TOOL_PEN:
                pts = m.get("pts") or []
                if pts:
                    dx, dy = na.x() - pts[0].x(), na.y() - pts[0].y()
                    m["pts"] = [QPoint(x.x() + dx, x.y() + dy) for x in pts]
            else:
                d = m["b"] - m["a"]
                m["a"] = na
                m["b"] = na + d
            self.update()
        elif self._drag_kind in ("move", "resize"):
            old = self._last_sel
            if old is not None:
                delta = pos - self._drag_start
                if self._drag_kind == "move":
                    self._sel = old.translated(delta).intersected(
                        QRect(0, 0, self.width(), self.height()))
                else:
                    # resize：**实时跟随鼠标**缩放（以前只在松手时才算新矩形，
                    # 拖动过程中选区纹丝不动，手感极生硬）
                    clamped = QPoint(min(max(pos.x(), 0), self.width()),
                                     min(max(pos.y(), 0), self.height()))
                    self._sel = self._resize_sel(
                        self._drag_handle, self._resize_orig or old, clamped).normalized()
                self._drag_cur = pos
                self._repaint_sel_change(old, self._sel)
        else:
            if self._sel is None:
                # 还没有选区：QQ 式预选——鼠标移到哪个窗口就高亮预览哪个（未落选中）
                self._update_hover_window(pos)
            # 悬停提示：光标 + 手柄高亮（让用户明确知道"这里可以拖"）
            if self._sel is not None and self._tool != TOOL_SELECT:
                # 标注工具下压在已有标注上 → 也提示"可拖动"
                if self._hit_mark(pos) is not None:
                    self.setCursor(Qt.CursorShape.SizeAllCursor)
                else:
                    self.setCursor(Qt.CursorShape.CrossCursor)
            if self._sel is not None and self._tool == TOOL_SELECT:
                h = self._hit_handle(pos)
                if h != self._hover_handle:
                    self._hover_handle = h
                    self.update()  # 重绘以高亮命中区域
                if h:
                    self.setCursor(self._cursor_for_handle(h))
                elif self._hit_mark(pos) is not None:
                    # 悬停在已有标注上：提示"可拖动"
                    self.setCursor(Qt.CursorShape.SizeAllCursor)
                elif self._sel.contains(pos):
                    self.setCursor(Qt.CursorShape.SizeAllCursor)
                else:
                    self.setCursor(Qt.CursorShape.CrossCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            # 松开右键才退出：确保按下+松开都被遮罩消费，不泄漏给桌面/下层窗口
            if self._right_down:
                self._right_down = False
                self.reject()
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        pos = event.position().toPoint()
        kind = self._drag_kind
        self._drag_cur = pos
        if kind == "start":
            if self._sel is not None:
                self._sel = self._sel.normalized()
            if self._sel is None or (self._sel.width() < 3 and self._sel.height() < 3):
                # 单击（没有实际拖动）：若鼠标下有窗口，就**选中那个窗口**（QQ 式单击选中）；
                # 都没有则保持空选区（不默认全屏）
                self._sel = QRect(self._press_win) if self._press_win is not None else None
                if self._sel is None:
                    self.toolbar.hide()
                    self.update()
                    self._cancel_drag()
                    return
            if self._sel is not None:
                self.toolbar.show()
                self.toolbar.raise_()
                self._layout_toolbar()
                self.setFocus()
                self.update()
        elif kind == "resize" and self._drag_handle:
            old = self._last_sel
            self._sel = self._resize_sel(
                self._drag_handle, self._resize_orig or old or QRect(self._sel),
                pos).normalized()
            self._last_sel = QRect(self._sel)
            self._repaint_sel_change(old, self._sel)
            self._layout_toolbar()  # 选区大小变了工具栏跟随
        elif kind == "move" and self._drag_start and self._last_sel is not None:
            # mouseMove 期间已实时算过 _sel，这里只把 _last_sel 同步成最终值
            old = self._last_sel
            self._last_sel = QRect(self._sel)
            self._repaint_sel_change(old, self._sel)
            self._layout_toolbar()  # 选区移位后工具栏跟随重新定位
        elif kind == "draw":
            # 松手把新标注入列（太小的当误触丢弃，并退回这次的历史快照）
            m = self._drag_mark
            self._drag_mark = None
            if m is not None and self._sel is not None:
                if m["kind"] == TOOL_PEN:
                    ok = len(m.get("pts") or []) >= 2
                else:
                    a, b = m["a"], m["b"]
                    ok = abs(b.x() - a.x()) > 3 or abs(b.y() - a.y()) > 3
                if ok:
                    self._marks.append(m)
                elif self._history:
                    self._history.pop()
            self.update()
        elif kind == "move_mark":
            self._drag_mark_idx = None
            self._drag_mark_off = QPoint(0, 0)
        self._cancel_drag()
        # 拖拽/标注结束：把工具栏重新顶到最前（鼠标在遮罩上按下会 activate 遮罩，
        # 全屏遮罩会把独立 Tool 工具栏盖住，看起来像"工具栏消失了"）
        if getattr(self, "toolbar", None) is not None and self.toolbar.isVisible():
            self.toolbar.raise_()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        """选区内双击 = 直接完成截图（等价回车）。

        仅在选择工具下响应——画箭头/矩形后快速再点一下（例如想切工具）不会被
        误判成双击完成截图（那会让工具栏直接关闭）。非选择工具下双击直接吞掉，
        不转发给 super()（QWidget 默认会把双击转发成 mousePressEvent，导致点一下
        又画一笔标注）。
        """
        if self._tool != TOOL_SELECT:
            event.accept()
            return
        if self._sel is not None and self._sel.contains(event.position().toPoint()):
            self._finish()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            # Esc = 直接退出截图（不管有没有正在拖拽/标注；对齐 QQ/微信）
            self.reject()
            return
        if event.key() == Qt.Key.Key_Return or event.key() == Qt.Key.Key_Enter:
            self._finish()
            return
        if event.key() == Qt.Key.Key_Z and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self._undo()
            return
        super().keyPressEvent(event)

    # ---------- 标注：对象模型（画完还能拖动/调整） ----------
    def _paint_mark(self, p: QPainter, m: dict) -> None:
        """在 painter 当前坐标系里画一条标注（坐标存的是逻辑像素，线宽/颜色随标注保存）。"""
        w = int(m.get("w", 2))
        pen = QPen(m.get("c") or MARK_COLOR, w, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        kind = m["kind"]
        if kind == TOOL_ARROW:
            self._draw_arrow(p, QPointF(m["a"]), QPointF(m["b"]))
        elif kind == TOOL_PEN:
            pts = m.get("pts") or []
            if len(pts) >= 2:
                p.drawPolyline(QPolygonF([QPointF(x) for x in pts]))
        elif kind == TOOL_ELLIPSE:
            p.drawEllipse(QRectF(QPointF(m["a"]), QPointF(m["b"])).normalized())
        else:
            p.drawRect(QRectF(QPointF(m["a"]), QPointF(m["b"])).normalized())

    def _hit_mark(self, pos: QPoint) -> int | None:
        """命中已有标注：靠近边框/线段（含画笔轨迹）8px 内算命中，后画的优先。"""
        tol = 8
        for i in range(len(self._marks) - 1, -1, -1):
            m = self._marks[i]
            kind = m["kind"]
            if kind == TOOL_PEN:
                pts = m.get("pts") or []
                for j in range(len(pts) - 1):
                    if _dist_to_seg(pos, pts[j], pts[j + 1]) <= tol:
                        return i
                continue
            a, b = m["a"], m["b"]
            if kind in (TOOL_RECT, TOOL_ELLIPSE):
                r = QRect(a, b).normalized()
                if r.adjusted(-tol, -tol, tol, tol).contains(pos) and \
                        not r.adjusted(tol, tol, -tol, -tol).contains(pos):
                    return i
            elif _dist_to_seg(pos, a, b) <= tol:
                return i
        return None

    @staticmethod
    def _draw_arrow(p: QPainter, start: QPointF, end: QPointF) -> None:
        p.drawLine(start, end)
        angle = math.atan2(end.y() - start.y(), end.x() - start.x())
        size = 14.0
        a1 = angle + math.radians(150)
        a2 = angle - math.radians(150)
        arrow = QPolygonF([
            end,
            end + QPointF(math.cos(a1) * size, math.sin(a1) * size),
            end + QPointF(math.cos(a2) * size, math.sin(a2) * size),
        ])
        p.setBrush(p.pen().color())  # 箭头头部跟随当前标注颜色（不再写死红色）
        p.drawPolygon(arrow)
        p.setBrush(Qt.BrushStyle.NoBrush)

    # ---------- 绘制 ----------
    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # display 是带 dpr 的物理像素图：按 (0,0) 原点绘制即 1:1 物理像素映射，
        # 不做任何缩放（drawPixmap(rect, pm) 会拉伸，正是之前发虚的来源）
        self._ensure_display()
        p.drawPixmap(0, 0, self._display)
        if self._sel is not None and self._sel.width() > 0 and self._sel.height() > 0:
            sel = self._sel
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 120))
            for r in self._dimmer_rects(sel):
                p.drawRect(r)
            # 标注（对象模型）：全部画在选区范围内，画到选区外的部分会被裁掉（导出同样裁）
            p.save()
            p.setClipRect(sel)
            for m in self._marks:
                self._paint_mark(p, m)
            if self._drag_mark is not None:
                self._paint_mark(p, self._drag_mark)
            p.restore()
            # 选区边框（细蓝线，参考图样式）
            p.setPen(QPen(ACCENT, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(sel)
            # 尺寸提示：白色圆角药丸 + 深色文字（贴在选区左上外侧；上方没空间就放进内部）
            tip = f"{int(sel.width() * self._scale())} × {int(sel.height() * self._scale())}"
            bf = QFont(self.font())
            bf.setPixelSize(13)
            bf.setBold(True)
            p.setFont(bf)
            fm = p.fontMetrics()
            bh = 28
            bw = fm.horizontalAdvance(tip) + 26
            bx = float(sel.x())
            by = float(sel.y() - bh - 8)
            if by < 2:
                by = float(sel.y() + 8)
            badge = QRectF(bx, by, float(bw), float(bh))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 240))
            p.drawRoundedRect(badge, bh / 2.0, bh / 2.0)
            p.setPen(QColor("#161C28"))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, tip)
            p.setFont(self.font())  # 恢复默认字体，别影响后续绘制
            # 手柄：蓝色实心圆 + 白色描边 + 白色圆心（悬停时放大，参考图样式）
            if self._tool == TOOL_SELECT:
                for name, hr in self._handles(sel):
                    hot = name == self._hover_handle
                    rad = 7.0 if hot else 5.5
                    c = QPointF(hr.center())
                    p.setPen(QPen(QColor(255, 255, 255, 235), 1.5))
                    p.setBrush(ACCENT)
                    p.drawEllipse(c, rad, rad)
                    # 白色圆心按 55% 圈径绘制：圆心更大 → 蓝色环更细（参考图观感）
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor("#FFFFFF"))
                    p.drawEllipse(c, rad * 0.55, rad * 0.55)
                # 悬停在整条边上：那条边整条加粗高亮（手柄在边中点，远端也看得见）
                if self._hover_handle in ("n", "s", "w", "e"):
                    p.setPen(QPen(ACCENT, 3))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    if self._hover_handle == "n":
                        p.drawLine(sel.topLeft(), sel.topRight())
                    elif self._hover_handle == "s":
                        p.drawLine(sel.bottomLeft(), sel.bottomRight())
                    elif self._hover_handle == "w":
                        p.drawLine(sel.topLeft(), sel.bottomLeft())
                    else:
                        p.drawLine(sel.topRight(), sel.bottomRight())
        elif self._hover_win is not None:
            # QQ 式预选预览：压暗其余区域 + 蓝色**虚线**框（单击后才变成实线选中）
            hw = self._hover_win
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 120))
            for r in self._dimmer_rects(hw):
                p.drawRect(r)
            p.setPen(QPen(ACCENT, 1.5, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(hw)
        p.end()

    def _dimmer_rects(self, sel: QRect) -> list[QRect]:
        w, h = self.width(), self.height()
        return [
            QRect(0, 0, w, sel.y()),
            QRect(0, sel.y() + sel.height(), w, h - sel.y() - sel.height()),
            QRect(0, sel.y(), sel.x(), sel.height()),
            QRect(sel.x() + sel.width(), sel.y(), w - sel.x() - sel.width(), sel.height()),
        ]

    # ---------- 工具栏布局 / 完成 ----------
    def _update_hover_window(self, pos: QPoint) -> None:
        """QQ 式预选：把鼠标所在的窗口算出来做"预览高亮"（不落选中）。

        仅在**还没有选区**时生效；鼠标移出所有窗口（桌面）时清空预览。
        """
        if self._sel is not None or self._drag_kind is not None:
            return
        origin = self.mapToGlobal(QPoint(0, 0))
        g = self.mapToGlobal(pos)
        dpr = self.devicePixelRatioF() or 1.0
        wr = _window_rect_at(int(round(g.x() * dpr)), int(round(g.y() * dpr)))
        local = None
        if wr is not None:
            cand = wr.translated(-origin.x(), -origin.y()).intersected(self.rect())
            if cand.width() >= 16 and cand.height() >= 16:
                local = cand
        if local != self._hover_win:
            self._hover_win = local
            self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._sel is not None:
            # 预置了选区 → 直接把工具栏摆出来
            self.toolbar.show()
            self._layout_toolbar()
        else:
            # QQ 式：刚打开时先预览"光标所在的窗口"，不落选中
            self._update_hover_window(self.mapFromGlobal(QCursor.pos()))
        self.setFocus()

    def resizeEvent(self, event) -> None:
        self._invalidate_display()
        super().resizeEvent(event)

    def _layout_toolbar(self) -> None:
        """工具栏跟随选区：优先选区正下方，下方放不下则放上方，不遮住小选区。"""
        self.toolbar.adjustSize()
        if self._sel is None:
            return
        pos = self.mapToGlobal(QPoint(0, 0))
        tw, th = self.toolbar.width(), self.toolbar.height()
        # 横向：选区上方居中，越界回卷到窗口内
        x = pos.x() + self._sel.x() + (self._sel.width() - tw) // 2
        x = max(pos.x(), min(x, pos.x() + self.width() - tw))
        # 纵向：优先选区下方，放不下则选区上方
        y = pos.y() + self._sel.y() + self._sel.height() + 8
        if y + th > pos.y() + self.height() - 4:
            y = pos.y() + self._sel.y() - th - 8
        if y < pos.y() + 4:
            y = pos.y() + 4
        self.toolbar.setGeometry(x, y, tw, th)
        _round_top_window(self.toolbar, 12)  # 浮层圆角（DWM 优先，遮罩兜底）
        # 确保工具栏在遮罩之上，否则点击会被全屏遮罩吃掉、工具切不动
        self.toolbar.raise_()
        # ⚠ 这里不能 activateWindow()：会把键盘焦点从遮罩抢走，
        # 导致 Esc/Enter/Ctrl+Z 失效。工具栏已设 WindowDoesNotAcceptFocus，保持遮罩焦点。

    def _finish(self) -> None:
        if self._sel is None or self._sel.width() < 1 or self._sel.height() < 1:
            self.reject()
            return
        # 用 canvas 与窗口的实际比例裁剪（不硬依赖 devicePixelRatio：
        # HiDPI 下 grabWindow 返回图可能带 dpr 或已是逻辑分辨率，比例换算最稳）
        scale_x = self._canvas.width() / max(1, self.width())
        scale_y = self._canvas.height() / max(1, self.height())
        phys = QRect(
            int(self._sel.x() * scale_x), int(self._sel.y() * scale_y),
            int(self._sel.width() * scale_x), int(self._sel.height() * scale_y),
        ).intersected(self._canvas.rect())
        # 导出时才把标注"烤"进图里：按 canvas/窗口比例把逻辑坐标放大到物理像素
        img = self._canvas.copy()
        if self._marks:
            painter = QPainter(img)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setClipRect(phys)                     # 选区外不导出
            painter.scale(scale_x, scale_y)               # 逻辑 → 物理
            for m in self._marks:
                self._paint_mark(painter, m)
            painter.end()
        self.result = QPixmap.fromImage(img.copy(phys))
        self.accept()

    def done(self, r) -> None:  # noqa: N802  (QDialog 约定名)
        """accept()/reject() 的共同出口：必须关掉独立工具栏/浮层，否则截图后残留。"""
        if getattr(self, "toolbar", None) is not None:
            self.toolbar.close()
        if getattr(self, "width_pop", None) is not None:
            self.width_pop.close()
        super().done(r)

    def _on_ocr(self) -> None:
        """完成截图并交给 OCR 插件识别（截图已在剪贴板，OCR 会自动读取）。"""
        self._want_ocr = True
        self._finish()

    def closeEvent(self, event) -> None:
        self._canvas = QImage()
        self._history.clear()
        if self.toolbar is not None:
            self.toolbar.close()
        if getattr(self, "width_pop", None) is not None:
            self.width_pop.close()
        super().closeEvent(event)


_TOOLBAR_BTN = (
    "QPushButton{border:none; border-radius:8px; padding:6px 11px;"
    " background:transparent; color:#161C28; font-size:12px; font-weight:600;}"
    "QPushButton:hover{background:#E4EDFC;}"
    "QPushButton:pressed{background:#D6E2F7;}"
)
_TOOLBAR_OK = (
    "QPushButton{border:none; border-radius:8px; padding:6px 14px;"
    " background:#2F6BEE; color:#FFFFFF; font-size:12px; font-weight:700;}"
    "QPushButton:hover{background:#2359D6;}"
    "QPushButton:pressed{background:#1E4FBE;}"
)
# 关闭按钮：✕ 字形本身偏小，字号放大 1px 才能和加粗后的文字等视觉重量
_TOOLBAR_CLOSE = (
    "QPushButton{border:none; border-radius:8px; padding:6px 12px;"
    " background:transparent; color:#161C28; font-size:14px; font-weight:600;}"
    "QPushButton:hover{background:#E4EDFC;}"
    "QPushButton:pressed{background:#D6E2F7;}"
)


class Plugin(PluginBase):
    def on_load(self) -> None:
        _dbg_print(f"[插件] 已加载: {self.ctx.name} v{self.ctx.manifest.version}", flush=True)

    def on_execute(self, query: str = "") -> None:
        # 重入保护（插件侧兜底）：遮罩已经打开时不叠第二层截图。
        # 主窗口已拦掉"快捷键/瓦片重复触发"，但插件也可能经跨插件路由（api.invoke_plugin）
        # 被再次调用，这里再护一层，避免多个全屏遮罩互相抢焦点、截出带遮罩的二次图。
        if getattr(self, "_busy", False):
            return
        self._busy = True
        try:
            self._do_execute(query)
        finally:
            self._busy = False

    def _do_execute(self, query: str = "") -> None:
        app = QApplication.instance()
        if app is None:
            return
        overlays: list[ScreenshotOverlay] = []
        done = {"ok": False, "result": None, "overlay": None}

        def _collect(ov: ScreenshotOverlay, code: int) -> None:
            if done["ok"]:
                return
            done["ok"] = True
            if code == QDialog.DialogCode.Accepted and ov.result is not None:
                done["result"] = ov.result
                done["overlay"] = ov
            for o in overlays:
                if o is not ov:
                    o.reject()

        for screen in app.screens():
            img = screen.grabWindow(0)
            # HiDPI：grabWindow 返回的 QPixmap 可能自带 devicePixelRatio，
            # 归一成 1.0，让 _canvas 按物理像素处理，避免后续坐标/质量错乱
            img.setDevicePixelRatio(1.0)
            # 不预置选区：打开后走 QQ 式"悬停预览窗口 → 单击选中"（overlay 自己算）
            ov = ScreenshotOverlay(img)
            ov.setGeometry(screen.geometry())
            ov.finished.connect(lambda code, o=ov: _collect(o, code))
            overlays.append(ov)

        for ov in overlays:
            ov.show()
            ov.raise_()
            ov.activateWindow()
            ov.setFocus()

        loop = QEventLoop()
        remaining = [len(overlays)]

        def _tick(_code: int) -> None:
            remaining[0] -= 1
            if remaining[0] <= 0:
                loop.quit()

        for ov in overlays:
            ov.finished.connect(_tick)
        loop.exec()

        result = done["result"]
        if result is not None:
            self.ctx.api.clipboard_write_image(result)
            if done["overlay"] is not None and done["overlay"]._want_ocr:
                # 把截图存成临时 PNG 直接把**路径**交给 OCR：不再依赖剪贴板中转
                # （写剪贴板失败、或被别的程序抢走时，OCR 仍拿得到图）。
                shot_path = ""
                try:
                    shot_path = os.path.join(tempfile.gettempdir(), "lylaunch_shot.png")
                    result.save(shot_path, "PNG")
                except Exception:
                    shot_path = ""
                self.ctx.api.notify("截图", "截图已送「图片转文字」…")
                self.ctx.api.invoke_plugin("ocr", shot_path)
            else:
                self.ctx.api.notify("截图", "已截取到剪贴板，可直接粘贴")
