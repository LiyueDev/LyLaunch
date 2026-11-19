"""滑动开关（Switch）组件：轨道 + 滑块圆点，带平滑滑动动画。

用法：
    sw = SwitchButton()
    sw.setChecked(True)               # 程序初始化设置状态
    sw.toggled.connect(self._on_x)    # 用户拨动时触发（bool 参数）
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QVariantAnimation, QEasingCurve
from PySide6.QtGui import QPainter, QColor
from PySide6.QtWidgets import QAbstractButton

# 0.36 定稿色板（色 B 通透干净），与 liytools/plugins/theme.py 的 token 保持一致。
# 这里是逐帧 paintEvent 用的纯常量，不走 QSS，也不引入跨包 import 以免循环依赖。
TRACK_ON = "#2F6BEE"       # --accent
TRACK_HOVER_ON = "#2359D6" # --accent-hover
TRACK_OFF = "#B7C4D8"      # --track-off
TRACK_HOVER_OFF = "#A6B5CD"  # --track-off-hover
KNOB = "#FFFFFF"           # 滑块圆点颜色


class SwitchButton(QAbstractButton):
    """滑动开关：checkable，checked=开。轨道随状态变蓝/灰，圆点平滑滑动。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(48, 26)
        self._offset = 0.0  # 0.0=关（圆点在左），1.0=开（圆点在右）
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.valueChanged.connect(self._on_anim_value)

    def _on_anim_value(self, value) -> None:
        self._offset = float(value)
        self.update()

    # ---------- 状态切换 ----------
    def setChecked(self, checked: bool) -> None:  # noqa: N802
        """程序设置状态：圆点**立即**到位。

        只重写 nextCheckState 的旧实现有个视觉 bug：`setChecked(True)`（设置页初始化
        读注册表、失败回滚都会用）只改了选中状态、没动 `_offset`，于是**轨道变蓝但
        圆点还停在左边**，看着像"开关没打开"——开机自启那一项尤其容易被误认为坏了。
        """
        self._anim.stop()
        super().setChecked(checked)
        self._offset = 1.0 if checked else 0.0
        self.update()

    def nextCheckState(self) -> None:  # noqa: N802
        """用户点击：从旧位置滑到新位置（保留 160ms 动画）。"""
        target = 0.0 if self.isChecked() else 1.0   # 即将切换到的新状态
        super().nextCheckState()                     # 内部会调 setChecked → 圆点已到 target
        self._offset = 1.0 - target                  # 摆回旧位置，动画才有起点
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(target)
        self._anim.start()

    # ---------- 绘制 ----------
    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()
        on = self.isChecked()
        hover = self.underMouse()
        # 轨道
        if on:
            color = TRACK_HOVER_ON if hover else TRACK_ON
        else:
            color = TRACK_HOVER_OFF if hover else TRACK_OFF
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        # 滑块圆点
        pad = 3
        dia = r.height() - pad * 2
        x = pad + self._offset * (r.width() - dia - pad * 2)
        p.setBrush(QColor(KNOB))
        p.drawEllipse(int(x), pad, dia, dia)
        p.end()

    def enterEvent(self, e) -> None:
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self.update()
        super().leaveEvent(e)
