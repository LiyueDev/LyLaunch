"""液态玻璃背景：实时系统模糊（DWM 合成）+ 自绘奶白雾与描边 + 柔和投影。

为什么不用第三方库：
- `py-window-styles` 之类要装依赖；frozen 版主程序不打包 pip 包，插件也不能
  假设它们存在。
- 改 DWM 的全局工具（DWMBlurGlass 等）会影响整个系统，早前已否决。

0.2.3 的关键修正（之前两版都走错了）
----------------------------------
旧版用「抓屏 → 缩图模糊 → 缓存大图裁切」冒充毛玻璃，结果是**背景被冻结**：
只有移动窗口时才重抓一次，背后窗口/视频变化完全看不到。这不符合「透明渲染」。

真机实测（.dev/live_blur_probe.py，判据是 对比度/方差/均值）：

    acrylic  AccentFlags=2  tint=0   contrast 0.84  var   50  → 不透明白雾（白板）
    acrylic  AccentFlags=2  tint=48  contrast 0.84  var   50  → 同上
    BlurBehind（state 3）            contrast 1.31  var  250  → 压黑
    DwmEnableBlurBehindWindow        contrast 7.42  var 7474 → 没生效（全锐）
    acrylic  AccentFlags=0  tint=0   contrast 1.20  var 3839 → **糊开 + 颜色保真 + 实时**

结论：`AccentFlags=0` 才是系统模糊，`AccentFlags=2` 走的是「窗口化背景」分支，
会铺一层不透明噪点 —— 之前判「系统模糊不可用」就是踩了这个标志位。

所以现在：背景完全交给 DWM 实时合成（背后内容一动就跟着变），本层只负责
奶白雾 + 一道白描边 +（独立窗口的）柔和投影。

另：0.3.0 试过「BitBlt 抓屏 + 自绘模糊」（圆角 100% 正确），但每帧 ~11ms 的
GDI 抓屏让交互明显卡顿，已整体回滚。不要重做这条路。
"""

from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QEvent, QLineF, QObject, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
    QRadialGradient, QRegion,
)
from PySide6.QtWidgets import QWidget

RADIUS = 30                       # 大圆角


def _q(color: str, alpha: float) -> QColor:
    c = QColor(color)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


class GlassBackdrop(QObject):
    """在窗口 Paint 时铺一层毛玻璃：奶白雾 + 白描边（模糊由 DWM 实时提供）。

    这里**不抓屏、不缓存任何背景图** —— 窗口挂上 acrylic 之后，DWM 会把窗口
    背后的桌面实时模糊并合成到窗口里；本层画的白雾只是把模糊层提亮成“奶白”，
    所以背后任何变化（拖动别的窗口、视频、壁纸切换）都会立刻反映出来。
    """

    _VEIL_MAX = 0.34      # 磨砂 100%（默认）时的奶白雾；blurbehind 不漂白，白雾要略浓
    _VEIL_MIN = 0.02      # 磨砂接近 0 时几乎不叠白
    # 低于这个强度算“清玻璃”：系统模糊关掉，只留白雾（雾厚度与阈值处对齐，
    # 所以跨过阈值时只有“糊 → 清”这一件事在变，不会连带亮度一起跳）
    _CLEAR_BELOW = 0.50
    _VEIL_TINT = "#FFFFFF"

    def __init__(self, window) -> None:
        super().__init__(window)
        self._win = window
        self._level = 1.0        # 磨砂强度 0.0 ~ 1.0，默认满档
        self._blur_active = True # 系统模糊是否真的生效（失败则退回极淡白）

    # ---------- 参数
    def set_level(self, level: float) -> None:
        """磨砂强度：1.0 = 最磨砂（奶白雾最浓），0.0 = 最通透（几乎不叠白）。"""
        try:
            level = float(level)
        except (TypeError, ValueError):
            level = 1.0
        self._level = max(0.0, min(1.0, level))
        self._win.update()

    def level(self) -> float:
        return self._level

    def set_blur_active(self, active: bool) -> None:
        """系统模糊是否可用。不可用时退回极淡白通透玻璃，避免 30% 白雾糊成白板。"""
        self._blur_active = bool(active)
        self._win.update()

    def set_frost(self, enabled: bool) -> None:
        """兼容旧调用：开 = 满档磨砂，关 = 极淡白。"""
        self.set_blur_active(bool(enabled))
        if enabled:
            self.set_level(1.0)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._win and event.type() == QEvent.Type.Paint:
            self._paint()
        return False

    def _paint(self) -> None:
        win = self._win
        w, h = win.width(), win.height()
        if w <= 0 or h <= 0:
            return
        p = QPainter(win)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        outer = QPainterPath()
        outer.addRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), RADIUS, RADIUS)

        # ⓪ 底：系统模糊生效时铺奶白雾（浓度 = 磨砂强度，默认满档）；
        #    模糊关掉（低于阈值走"清玻璃" / 系统不支持模糊）时铺白雾，
        #    但雾厚度在阈值处与模糊态**完全对齐**，再随强度线性淡到 0。
        #    这样跨过阈值时只有"糊 → 清"在变，亮度不跳，观感是"越拖越不糊"。
        p.save()
        p.setClipPath(outer)
        if self._blur_active:
            base = self._VEIL_MIN + (self._VEIL_MAX - self._VEIL_MIN) * self._level
            span = 0.06 * self._level
        else:
            k = min(1.0, self._level / self._CLEAR_BELOW)
            base = (self._VEIL_MIN
                    + (self._VEIL_MAX - self._VEIL_MIN) * self._CLEAR_BELOW) * k
            span = 0.06 * self._CLEAR_BELOW * k
        veil = QLinearGradient(0, 0, 0, h)
        veil.setColorAt(0.0, _q(self._VEIL_TINT, base + span))
        veil.setColorAt(0.6, _q(self._VEIL_TINT, base))
        veil.setColorAt(1.0, _q(self._VEIL_TINT, base - span * 0.66))
        p.fillRect(QRectF(0, 0, w, h), veil)
        p.restore()

        # 注：DWM 模糊层按窗口矩形铺、不认圆角，四角会露方形糊底；
        # SetWindowRgn / corner preference / hRgnBlur 全实测无效。
        # 自绘抓屏能修圆角但交互卡顿，已回滚 —— 这块方形糊底是已知取舍。

        p.save()
        p.setClipPath(outer)

        # ① 上沿散射亮带 + 左上柔光：只留一点点，多了会把壁纸洗白；
        #    这些"高光"整体按磨砂强度缩放，0 档就彻底干净，只剩描边
        hl = self._level
        band = QLinearGradient(0, 0, 0, h * 0.30)
        band.setColorAt(0.0, _q("#FFFFFF", 0.030 * hl))
        band.setColorAt(0.45, _q("#FFFFFF", 0.008 * hl))
        band.setColorAt(1.0, _q("#FFFFFF", 0.0))
        p.fillRect(QRectF(0, 0, w, h * 0.30), band)
        halo = QRadialGradient(QPointF(w * 0.18, -h * 0.10), max(w, h) * 0.85)
        halo.setColorAt(0.0, _q("#FFFFFF", 0.030 * hl))
        halo.setColorAt(0.50, _q("#FFFFFF", 0.008 * hl))
        halo.setColorAt(1.0, _q("#FFFFFF", 0.0))
        p.fillRect(QRectF(0, 0, w, h), halo)

        # ② 内沿柔光带：玻璃的“厚度”，只保留极淡的一笔
        rim = QLinearGradient(0, 0, 0, h)
        rim.setColorAt(0.0, _q("#FFFFFF", 0.05 * hl))
        rim.setColorAt(0.45, _q("#FFFFFF", 0.02 * hl))
        rim.setColorAt(1.0, _q("#FFFFFF", 0.008 * hl))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(rim, 6.0))
        p.drawPath(outer)
        p.restore()

        # ③ 主描边 1.2px：白色垂直渐变（上亮下薄），窗口只有这一道边
        inner = QPainterPath()
        inner.addRoundedRect(
            QRectF(1.7, 1.7, w - 3.4, h - 3.4), RADIUS - 1.4, RADIUS - 1.4)
        edge = QLinearGradient(0, 0, 0, h)
        edge.setColorAt(0.00, _q("#FFFFFF", 0.72))
        edge.setColorAt(0.20, _q("#FFFFFF", 0.50))
        edge.setColorAt(0.60, _q("#FFFFFF", 0.30))
        edge.setColorAt(1.00, _q("#FFFFFF", 0.18))
        p.setPen(QPen(edge, 1.2))
        p.drawPath(inner)

        p.save()
        p.setClipPath(outer)

        # ④ 角部光斑
        for cx in (RADIUS * 1.05, w - RADIUS * 1.05):
            spot = QRadialGradient(QPointF(cx, RADIUS * 0.6), RADIUS * 1.8)
            spot.setColorAt(0.0, _q("#FFFFFF", 0.10 * hl))
            spot.setColorAt(0.45, _q("#FFFFFF", 0.025 * hl))
            spot.setColorAt(1.0, _q("#FFFFFF", 0.0))
            p.fillRect(
                QRectF(cx - RADIUS * 1.9, 0, RADIUS * 3.8, RADIUS * 2.6), spot)

        # ⑤ 左右极淡色散
        p.setPen(QPen(_q("#A9D0FF", 0.05 * hl), 1.2))
        p.drawLine(QLineF(1.8, RADIUS * 1.5, 1.8, h - RADIUS * 1.6))
        p.setPen(QPen(_q("#DCC8FF", 0.04 * hl), 1.2))
        p.drawLine(QLineF(w - 1.8, RADIUS * 1.5, w - 1.8, h - RADIUS * 1.6))

        p.restore()
        p.end()


def install_liquid_glass(window) -> GlassBackdrop:
    """挂上自绘玻璃层（必须在 install_chrome 之前调用）。"""
    backdrop = GlassBackdrop(window)
    window.installEventFilter(backdrop)
    return backdrop


class SoftShadow(QWidget):
    """面板下方的柔和扩散阴影（独立窗口）。

    为什么不直接画在面板里：面板近乎全透，任何画在里面的暗色都会被看成
    “面板脏了”。所以阴影单独一层、永远在面板底下，只在面板**外侧**画。
    窗口本身鼠标穿透（WA_TransparentForMouseEvents + WS_EX_TRANSPARENT），
    不会在面板周围制造一圈点不动的死区。

    怎么画出“扩散”：先把实心圆角矩形画在一张 1/BLUR_DIV 的小图上，再用平滑
    缩放放大回来 —— 等价于一次廉价高斯，不会有细线笔画那种一圈圈的叠痕。
    """

    PAD = 30          # 阴影扩散半径
    OFFSET = 8        # 整体下移，光从上方来的感觉
    BLUR_DIV = 8      # 缩图比例：越小越糊
    CORE_ALPHA = 110  # 阴影核心不透明度（放大后会被摊薄很多）

    def __init__(self, target) -> None:
        # 不用 WindowStaysOnTopHint（切换窗口时置顶阴影会浮在别的窗口上多露一圈边），
        # 也不用 Tool（Tool 在 Windows 上 Z 序天然高于普通窗口，会盖到面板上面）。
        # 改成普通无边框窗口 + WA_ShowWithoutActivating，和面板同层，Z 序由
        # showEvent 里的「先 shadow.show() 再 panel.raise_()」明确控制。
        super().__init__(None, Qt.WindowType.FramelessWindowHint)
        self._target = target
        self._pm = None
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.sync()

    # ---------- 几何跟随
    def sync(self) -> None:
        g = self._target.geometry()
        self.setGeometry(g.x() - self.PAD, g.y() - self.PAD,
                         g.width() + self.PAD * 2, g.height() + self.PAD * 2)
        self._pm = None
        self.update()

    def _radius(self) -> float:
        return float(getattr(self._target, "_skin_radius", RADIUS))

    def _shadow_pixmap(self) -> QPixmap:
        if self._pm is not None:
            return self._pm
        w, h = max(1, self.width()), max(1, self.height())
        div = self.BLUR_DIV
        sw, sh = max(1, w // div), max(1, h // div)
        img = QImage(sw, sh, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pad = self.PAD / div
        off = self.OFFSET / div
        r = self._radius() / div
        core = QRectF(pad, pad + off, sw - pad * 2, sh - pad * 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(8, 12, 24, self.CORE_ALPHA))
        p.drawRoundedRect(core, r, r)
        p.end()
        self._pm = QPixmap.fromImage(img.scaled(
            w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        return self._pm

    def paintEvent(self, event) -> None:  # noqa: N802
        t = self._target
        # 根本保险：面板隐藏后，即使本阴影窗口因 Z 序/时序没被 hide 成功，
        # 也绝不再画面板外侧的阴影 —— 否则桌面会残留一圈 30px 的边要再关一次。
        if not t.isVisible():
            return
        g = t.geometry()
        base = QRectF(t.x() - self.x(), t.y() - self.y(), t.width(), t.height())
        hole = QPainterPath()
        hole.addRoundedRect(base.adjusted(0.5, 0.5, -0.5, -0.5),
                            self._radius(), self._radius())

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 只画面板外侧：面板是透明的，阴影绝不能被它透出来
        p.setClipRegion(QRegion(self.rect())
                        - QRegion(hole.toFillPolygon().toPolygon()))
        p.drawPixmap(0, 0, self._shadow_pixmap())
        p.end()


def install_soft_shadow(window) -> SoftShadow:
    """给窗口挂一层柔和投影，返回对象供调用方 show/hide/sync。"""
    shadow = SoftShadow(window)
    try:
        # WS_EX_TRANSPARENT：鼠标穿透，面板周围不会出现点不到的死区
        user32 = ctypes.windll.user32
        hwnd = int(shadow.winId())
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_TOOLWINDOW = 0x00000080   # 不进 Alt+Tab / 任务栏（不改变 Qt Z 序）
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                              style | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
                              | WS_EX_TOOLWINDOW)
    except Exception:
        pass
    return shadow


# ---------------------------------------------------------------- 系统模糊（实时）
class _AccentPolicy(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_int),
        ("AccentFlags", ctypes.c_int),
        ("GradientColor", ctypes.c_uint),
        ("AnimationId", ctypes.c_int),
    ]


class _WinCompAttrData(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.c_void_p),
        ("SizeOfData", ctypes.c_ulong),
    ]


_WCA_ACCENT_POLICY = 19
_ACCENT_DISABLED = 0
_ACCENT_BLURBEHIND = 3
_ACCENT_ACRYLICBLURBEHIND = 4

# AccentFlags 必须是 0：
#   0 = 真正的背景模糊（DWM 实时合成，颜色保真）
#   2 = 「窗口化背景」分支，会铺一层不透明噪点 → 面板变白板
_ACCENT_FLAGS_BLUR = 0
_TINT_RGB = 0x00FFFFFF       # 0xAABBGGRR，白
DEFAULT_TINT = 0x00          # 不额外染色


def _set_accent(hwnd: int, state: int, tint_alpha: int = DEFAULT_TINT,
                flags: int = _ACCENT_FLAGS_BLUR) -> bool:
    try:
        user32 = ctypes.windll.user32
    except Exception:
        return False
    gradient = (int(tint_alpha) << 24) | _TINT_RGB
    policy = _AccentPolicy(state, int(flags), gradient, 0)
    data = _WinCompAttrData(_WCA_ACCENT_POLICY,
                            ctypes.cast(ctypes.pointer(policy), ctypes.c_void_p),
                            ctypes.sizeof(policy))
    try:
        ok = user32.SetWindowCompositionAttribute(
            ctypes.c_void_p(hwnd), ctypes.byref(data))
        return bool(ok)
    except Exception:
        return False


def apply_blur(window, enabled: bool = True, tint_alpha: int = DEFAULT_TINT) -> bool:
    """给窗口开 / 关**实时**背景模糊（Win10 1803+ / Win11）。

    必须在窗口 show() 之后调用 —— 这里要 winId()，构造期创建原生句柄在部分
    机器上会让 layered 窗口静默崩溃（window_chrome 里踩过）。
    背景由 DWM 合成，窗口一动、背后的画面一变就立刻更新，不存在“冻结帧”。
    失败返回 False，调用方按「极淡白通透玻璃」降级，功能不回退。
    """
    if sys.platform != "win32":
        return False
    try:
        hwnd = int(window.winId())
    except Exception:
        return False
    if not enabled:
        return _set_accent(hwnd, _ACCENT_DISABLED)
    # 先用 BLURBEHIND：DWM 同样实时合成，但**不往画面上叠白**——颜色保真。
    # 这点很关键：acrylic 自带漂白，四角那块按窗口矩形铺的糊底会比壁纸明显
    # 更白（圆角处露一个方形白角）；BLURBEHIND 的糊底≈壁纸本色，肉眼几乎
    # 看不出边界。白度统一交给自绘 veil 控制。
    if _set_accent(hwnd, _ACCENT_BLURBEHIND, tint_alpha, _ACCENT_FLAGS_BLUR):
        return True
    return _set_accent(hwnd, _ACCENT_ACRYLICBLURBEHIND, tint_alpha,
                       _ACCENT_FLAGS_BLUR)


# ---------------------------------------------------------------- 圆角结论
# 0.2.8 实测（五种组合，脚本在 .dev/）：DWM 的模糊层**只按窗口矩形铺** ——
#   ① layered + acrylic + SetWindowRgn          → 区域被忽略，四角仍直角
#   ② 非 layered + acrylic + SetWindowRgn        → 同上
#   ③ DwmEnableBlurBehindWindow(hRgnBlur)        → Win11 不出真模糊，区域也不裁
#   ④ DWMWA_WINDOW_CORNER_PREFERENCE = 2 / 3     → 直角不变
#   ⑤ DWMWA_SYSTEMBACKDROP_TYPE = 3              → 灰板 + 直角
# 所以「窗口区域裁圆角」这条兜底是无效的，SetWindowRgn 相关代码已整体删除，
# 不要再加回来。四角那块 30px 楔形的色差只能靠换"不漂白的 accent"压小
#（acrylic → BlurBehind：Δ82 → Δ29），或改自绘抓屏模糊（见进度文档 0.3）。

