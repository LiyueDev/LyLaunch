"""系统托盘（对应 C# 的 SetupTray）。

常驻通知区：右键菜单"显示 / 退出"，双击唤出窗口。
托盘图标按优先级取：``assets/tray.ico``（新 logo）→ exe 内嵌图标 →
代码自绘的同款 logo 兜底。最后一层不依赖任何外部文件，避免 .ico 丢失时
任务栏里露出旧项目名时代的字母图标。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import paths

_BRAND = QColor(0x2F, 0x6B, 0xEE)   # #2F6BEE，0.36 定稿主色，与 brand/*.svg 一致

# brand/appicon.svg 里的标志路径：一段 M + 11 段二次贝塞尔。
# SVG 的 group transform 把原始坐标映射到 1024 画布：
#     canvas = (x * 4.754 - 96.5, y * 4.754 - 94.1)
# 下面在任意尺寸的 pixmap 上按同一比例重绘，与 tools/gen_launch_icon.py 同源。
_MARK_START = (70, 54)
_MARK_QUADS = [
    ((80, 36), (92, 46)),
    ((102, 56), (94, 90)),
    ((84, 132), (82, 160)),
    ((80, 182), (96, 182)),
    ((114, 182), (124, 160)),
    ((134, 138), (136, 114)),
    ((142, 144), (154, 166)),
    ((164, 180), (176, 162)),
    ((184, 148), (184, 134)),
    ((186, 170), (174, 200)),
    ((164, 220), (138, 212)),
]
_SCALE, _TX, _TY, _CANVAS, _STROKE = 4.754, -96.5, -94.1, 1024.0, 18


def _to_px(point: tuple[int, int], size: int) -> QPointF:
    x, y = point
    return QPointF((x * _SCALE + _TX) / _CANVAS * size,
                   (y * _SCALE + _TY) / _CANVAS * size)


def _make_default_icon(size: int = 64) -> QIcon:
    """兜底图标：不读外部文件，按 appicon.svg 的几何自绘白底蓝色 Ly。

    旧的兜底画的是字母 "M"（C# 时代 MyLauncher 的遗留），.ico 读不到时
    任务栏会突然冒出一个跟产品完全无关的图标。这里改成和新 logo 同一套路径。

    ``size`` 可传更大值（如 160）当高清源，供界面按 DPI 取图，避免放大发糊。
    """
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # 圆角白底（appicon.svg 的 rx=224 / 1024）
    radius = 224 / _CANVAS * size
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(255, 255, 255)))
    painter.drawRoundedRect(0, 0, size, size, radius, radius)

    path = QPainterPath()
    path.moveTo(_to_px(_MARK_START, size))
    for ctrl, end in _MARK_QUADS:
        path.quadTo(_to_px(ctrl, size), _to_px(end, size))
    pen = QPen(_BRAND)
    pen.setWidthF(_STROKE * _SCALE / _CANVAS * size)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)
    painter.end()
    return QIcon(pix)


def _try_file_icon() -> QIcon | None:
    try:
        ico = paths.resource_path("assets", "tray.ico")
        if ico and os.path.exists(ico):
            icon = QIcon(ico)
            if not icon.isNull():
                return icon
    except Exception:
        pass
    return None


def _try_exe_icon() -> QIcon | None:
    try:
        if QApplication.applicationFilePath():
            icon = QApplication.windowIcon()  # 若 main.py 已 setWindowIcon 取过 exe 图标则复用
            if icon and not icon.isNull():
                return icon
    except Exception:
        pass
    return None


def setup_tray(show_cb, exit_cb) -> QSystemTrayIcon:
    tray = QSystemTrayIcon()
    # 优先级：磁盘 ico 文件 → exe 内嵌图标 → 自绘新 logo 兜底
    tray.setIcon(_try_file_icon() or _try_exe_icon() or _make_default_icon())
    tray.setToolTip("LyLaunch")

    menu = QMenu()
    show_action = menu.addAction("显示")
    show_action.triggered.connect(show_cb)
    exit_action = menu.addAction("退出")
    exit_action.triggered.connect(exit_cb)
    tray.setContextMenu(menu)
    tray._menu = menu  # 强引用：PySide6 下局部 QMenu 可能被 GC，导致右键无菜单

    tray.activated.connect(
        lambda reason: show_cb()
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None
    )
    tray.show()
    return tray
