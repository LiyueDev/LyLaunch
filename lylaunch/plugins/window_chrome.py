"""插件窗口统一外壳：无边框 + 自绘导航栏 + 大圆角（0.39 定稿）。

用法（构造期，show() 之前）：

    from lylaunch.plugins.window_chrome import install_chrome
    install_chrome(self, "文本翻译", icon="translator")
    install_chrome(self, "本地 AI 对话", icon="llm_chat",
                   show_minmax=True, resizable=True)

设计取舍：
- 不再用系统标题栏（白色 DWM 条和白→淡蓝内容区割裂）。保留系统栏的
  拖动 / 关闭 / 最小化 / 最大化 / 右下角缩放，全部由自绘导航栏接管。
- 圆角靠 QSS 的 border-radius + WA_TranslucentBackground 裁剪，不用 setMask
  （0.36 踩过黑角/锯齿），也不用 paintEvent 自绘（避免每个插件各写一份）。
- **构造期绝不碰 winId()**：主窗口曾因 layered 窗口在部分机器 show() 静默崩溃，
  原因就是构造期创建原生句柄。这里只设置 flags / 属性 / 布局。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent, QObject, QRectF
from PySide6.QtGui import (
    QColor, QLinearGradient, QPainter, QPainterPath, QPen, QBrush, QPixmap,
)
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

# 窗口四角圆角：系统标题栏自带约 8~10px，这里统一放到 16px，"增大一点"但不夸张
CHROME_RADIUS = 16

_CHROME_QSS = f"""
/* 背景交给 _ChromePainter 画（QSS 的 border-radius 对顶层窗口不裁剪）；
   这里必须把各插件 QSS 里的方角 QDialog 背景清掉，否则四角仍是不透明的。 */
QDialog#lylaunchPluginWindow {{ background: transparent; border: none; }}
QWidget#chromeBar {{ background: transparent; }}
QLabel#chromeTitle {{
    font-size: 13px; font-weight: 600; color: #161C28; background: transparent;
}}
"""


class _ChromePainter(QObject):
    """在窗口 Paint 事件里先画圆角渐变底 + 1px 描边（和主窗口自绘同一套）。

    用事件过滤器而不是要求每个插件类重写 paintEvent；返回 False 让控件继续
    正常绘制自己的内容。QSS 的 border-radius 对顶层窗口不会裁剪，只有自绘
    才能真正得到透明的四个圆角。
    """

    def __init__(self, window: QDialog, solid: bool = False,
                 bg_alpha: float = 1.0, frosted: bool = False) -> None:
        super().__init__(window)
        self._win = window
        self._solid = solid
        # 0.43：桌面净化要求背景灰黑半透明 + 磨砂感。只影响自绘渐变底，
        # 描边与控件保持原样；默认 1.0 / frosted=False = 和以前完全一样。
        self._bg_alpha = max(0.0, min(1.0, float(bg_alpha)))
        self._frosted = frosted
        self._noise = self._make_noise() if frosted else None

    @staticmethod
    def _make_noise(tile: int = 64) -> "QPixmap":
        """生成一张小噪点图平铺用（视觉磨砂，不依赖系统 blur）。"""
        import random
        pm = QPixmap(tile, tile)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        rnd = random.Random(20261116)
        for _ in range(tile * tile // 5):
            x, y = rnd.randrange(tile), rnd.randrange(tile)
            a = rnd.randint(10, 26)
            p.setPen(QColor(255, 255, 255, a))
            p.drawPoint(x, y)
        p.end()
        return pm

    def _bg(self, hex_color: str) -> QColor:
        c = QColor(hex_color)
        if self._bg_alpha < 1.0:
            c.setAlphaF(self._bg_alpha)
        return c

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Paint and obj is self._win:
            w, h = obj.width(), obj.height()
            p = QPainter(obj)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            path = QPainterPath()
            path.addRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0),
                                CHROME_RADIUS, CHROME_RADIUS)
            grad = QLinearGradient(0, 0, 0, h)
            if self._frosted:
                # 灰黑磨砂：深灰 → 更深的炭黑，半透明透出桌面
                grad.setColorAt(0.0, self._bg("#3A3D42"))
                grad.setColorAt(1.0, self._bg("#222429"))
            else:
                grad.setColorAt(0.0, self._bg("#FFFFFF"))
                # solid=True：本地 AI 这种「自己铺满白底」的窗口，底部状态栏/空隙
                # 若透出淡蓝渐变会和上半部白色割裂，整块保持纯白。
                grad.setColorAt(1.0, self._bg("#FFFFFF" if self._solid else "#E8F0FE"))
            p.fillPath(path, grad)
            if self._frosted and self._noise is not None:
                # 细噪点平铺：把「透明玻璃」变成磨砂质感（纯 Qt，无系统 blur 依赖）
                p.save()
                p.setClipPath(path)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(self._noise))
                p.drawRect(0, 0, w, h)
                p.restore()
                # 顶部 1px 内高光，强化玻璃边缘厚度感
                p.save()
                p.setClipPath(path)
                p.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
                p.drawLine(1, 1, w - 2, 1)
                p.restore()
            p.setPen(QPen(QColor("#5A5F68" if self._frosted else "#C3D3EE"), 1.0))
            p.drawPath(path)
            p.end()
        return False


class ChromeButton(QPushButton):
    """统一规格的窗口按钮：34×30，几何图形全用 QPainter 画，不用字符。

    之前用「— □ ×」三个字符，字体渲染出来的视觉大小/粗细天然不一致，
    看着像三颗散落的符号（设计说明）。现在统一 10px 图形 + 1.4px 线宽，
    hover 才铺底色，关闭键 hover 变红。
    """

    W, H = 34, 30

    def __init__(self, kind: str, win: QDialog, parent=None) -> None:
        super().__init__("", parent)
        self._kind = kind          # "min" / "max" / "close"
        self._win = win
        self.setFixedSize(self.W, self.H)
        self.setObjectName("chromeBtn")
        # 不给按钮任何平台默认底/边框，只有 paintEvent 画的图形；hover 时才铺底
        self.setStyleSheet("background:transparent; border:none; padding:0;")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setToolTip({"min": "最小化", "max": "最大化 / 还原",
                         "close": "关闭"}[kind])

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        hover = self.underMouse()
        close = self._kind == "close"
        if hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#EC5F53" if close else "#E4EDFC"))
            p.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 7, 7)
        fg = QColor("#FFFFFF" if (hover and close)
                    else ("#2F6BEE" if hover else "#5F6570"))
        pen = QPen(fg, 1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        cx, cy = self.W / 2, self.H / 2
        if self._kind == "min":
            p.drawLine(cx - 5, cy, cx + 5, cy)             # 10px 横线
        elif self._kind == "max":
            if self._win.isMaximized():
                # 还原：两个叠着的 8px 方框
                p.drawRoundedRect(cx - 4, cy - 6, 8, 8, 1.2, 1.2)
                p.drawRoundedRect(cx - 2, cy - 4, 8, 8, 1.2, 1.2)
            else:
                p.drawRoundedRect(cx - 5, cy - 5, 10, 10, 1.5, 1.5)
        else:
            p.drawLine(cx - 4.5, cy - 4.5, cx + 4.5, cy + 4.5)
            p.drawLine(cx + 4.5, cy - 4.5, cx - 4.5, cy + 4.5)
        p.end()


class ChromeTitleBar(QWidget):
    """44px 自绘导航栏：产品 logo + 标题 + 可选最小化/最大化 + 关闭。

    按住空白处走 Qt 的 startSystemMove()（Windows 原生拖动，支持贴边最大化），
    按钮自己吃掉鼠标事件，不会误触发拖动。
    """

    def __init__(self, window: QDialog, title: str, icon_name: str = "",
                 show_minmax: bool = False) -> None:
        super().__init__(window)
        self.setObjectName("chromeBar")
        self.setFixedHeight(44)
        self._win = window

        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 6, 10, 2)
        lay.setSpacing(8)

        logo = QLabel()
        logo.setFixedSize(20, 20)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        try:
            from lylaunch.tray import _make_default_icon
            logo.setPixmap(_make_default_icon(20).pixmap(20, 20))
        except Exception:
            try:
                from lylaunch.plugins.plugin_icons import (
                    plugin_icon_svg, render_plugin_svg)
                logo.setPixmap(render_plugin_svg(plugin_icon_svg(icon_name), 18))
            except Exception:
                logo.setText("✦")
        lay.addWidget(logo)

        t = QLabel(title)
        t.setObjectName("chromeTitle")
        lay.addWidget(t)
        lay.addStretch(1)

        # 按钮单独放一组（组内 2px），主布局的 8px 间距留给 logo/标题，不受影响
        btn_row = QHBoxLayout()
        btn_row.setSpacing(2)
        btn_row.setContentsMargins(0, 0, 0, 0)
        if show_minmax:
            for kind, slot in (("min", self._on_min), ("max", self._on_max)):
                b = ChromeButton(kind, window)
                b.clicked.connect(slot)
                btn_row.addWidget(b)
        b = ChromeButton("close", window)
        b.clicked.connect(window.close)
        btn_row.addWidget(b)
        lay.addLayout(btn_row)

    def _on_min(self) -> None:
        self._win.showMinimized()

    def _on_max(self) -> None:
        if self._win.isMaximized():
            self._win.showNormal()
        else:
            self._win.showMaximized()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self._win.windowHandle()
            if handle is not None:
                handle.startSystemMove()
                return
            # 退化路径（个别平台拿不到 windowHandle）：自己算位移
            self._press = event.globalPosition().toPoint()
            self._origin = self._win.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (getattr(self, "_press", None) is not None
                and (event.buttons() & Qt.MouseButton.LeftButton)):
            self._win.move(self._origin + event.globalPosition().toPoint() - self._press)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._press = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self._on_max()


def install_chrome(window: QDialog, title: str, icon_name: str = "",
                   show_minmax: bool = False, resizable: bool = False,
                   solid: bool = False, side_margin: int = 14,
                   bg_alpha: float = 1.0, frosted: bool = False,
                   title_bar: bool = True):
    """给插件窗口装上统一外壳。须在构造期、show() 之前调用。

    ``resizable=True`` 用 QDialog.setSizeGripEnabled() 补回右下角缩放把手
    （无边框窗口没有系统缩放边框；本地 AI 依赖它拖窗口大小）。
    ``title_bar=False`` 只设无边框 / 透明 / 圆角，不插标题栏 —— 给「纯玻璃块」
    式面板用（拖动/关闭由调用方自己接管）。默认 True，不影响其他插件。
    """
    root = window.layout()
    if not isinstance(root, QVBoxLayout):
        # 插不进导航栏就**不要**去系统栏，否则窗口连关闭按钮都没有。
        # QMessageBox / 自定义布局的弹窗走这条保守路径。
        return None

    window.setWindowFlags(window.windowFlags() | Qt.WindowType.FramelessWindowHint)
    window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    # QWidget 子类要让 QSS 画出自身背景，必须显式开 WA_StyledBackground
    window.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    window.setObjectName("lylaunchPluginWindow")
    # QSS 只负责清掉各插件的方角背景 + 导航栏按钮样式；圆角底由 _ChromePainter 画
    window.setStyleSheet((window.styleSheet() or "") + _CHROME_QSS)
    # painter=False：插件自己画圆角/描边（如纯玻璃块），不要再叠一层 chrome 圆角，
    # 否则出现内外两圈同心边框。无边框/透明等属性仍照常设置。
    if title_bar or bg_alpha > 0.0:
        window.installEventFilter(_ChromePainter(window, solid, bg_alpha, frosted))

    bar = None
    if title_bar:
        bar = ChromeTitleBar(window, title, icon_name, show_minmax)
        root.insertWidget(0, bar)
    # 导航栏左右各留一点，避免内容贴到圆角上
    cm = root.contentsMargins()
    # 左右留边要够圆角（16px），否则方角子控件会把圆角盖掉；但本地 AI 侧栏
    # 自己还有一层 margin，两下叠加就太宽了，所以允许单独调小。
    root.setContentsMargins(max(cm.left(), side_margin), cm.top(),
                            max(cm.right(), side_margin),
                            max(cm.bottom(), 14))
    if resizable:
        window.setSizeGripEnabled(True)
    return bar
