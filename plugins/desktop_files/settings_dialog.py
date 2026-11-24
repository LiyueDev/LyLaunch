"""桌面净化 · 设置页（插件内独立小窗，非模态）。

为什么要独立一个设置页：
- GlassDesk 式面板把「浏览」和「管理」分得很开，面板上只留搜索 + 设置入口；
- 收纳 / 恢复 / 重扫 / 新建分组 / 桌面图标 / 文件夹图标 / 快捷键 / 皮肤切换
  都是低频动作，堆在面板工具条上会挤掉内容区；
- 插件主窗口禁 `exec()`（项目硬约束），所以这里一律 `show()` 非模态，
  和面板可以同时开着。

面板对象可能因为切换皮肤被整体重建，所以这里不持有 panel 实例，
而是每次动作前通过 `get_panel()` 取当前那一个。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QSlider, QStackedWidget, QStyle,
    QStyleOptionSlider, QVBoxLayout, QWidget,
)

from lylaunch.plugins import theme

import ops
import skin as skin_mod


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "subtitle")
    label.setWordWrap(True)
    return label


def _section(title: str, hint: str = "") -> QWidget:
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    head = QLabel(title)
    head.setStyleSheet("font-size:13px;font-weight:600;")
    lay.addWidget(head)
    if hint:
        lay.addWidget(_hint(hint))
    return box


def _button(text: str, slot, role: str = "soft", tip: str = "") -> QPushButton:
    btn = QPushButton(text)
    btn.setProperty("role", role)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if tip:
        btn.setToolTip(tip)
    btn.clicked.connect(slot)
    return btn


class _JumpSlider(QSlider):
    """点哪儿就跳到哪儿的滑杆。

    Qt 原生 QSlider 在水平滑杆上"点槽"默认不动（只有拖手柄才变），用它当
    「磨砂强度进度条」手感很别扭。这里按下时按 x 位置换算成值，再交回父类
    继续处理拖拽，效果就是点一下跳过去、按住还能拖。
    """

    def _value_at(self, x: float) -> int:
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, opt,
            QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, opt,
            QStyle.SubControl.SC_SliderHandle, self)
        span = groove.width() - handle.width()
        if span <= 0:      # QSS 样式下可能量不出来，退化成整条宽度
            groove = self.rect().adjusted(6, 0, -6, 0)
            handle = handle if handle.width() else self.rect()
            span = max(1, groove.width() - handle.width())
        pos = int(x - groove.x() - handle.width() / 2)
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), pos, span, opt.upsideDown)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if (event.button() == Qt.MouseButton.LeftButton
                and self.orientation() == Qt.Orientation.Horizontal
                and self.isEnabled()):
            self.setValue(self._value_at(event.position().x()))
            self.setSliderDown(True)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self.isSliderDown() and self.isEnabled():
            self.setValue(self._value_at(event.position().x()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self.isSliderDown():
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class SettingsWindow(QDialog):
    """左侧分类 + 右侧内容的设置窗。"""

    def __init__(self, get_panel, api, on_switch_skin=None) -> None:
        super().__init__()
        self._get_panel = get_panel
        self.api = api
        self._on_switch_skin = on_switch_skin
        self.setWindowTitle("桌面净化 · 设置")
        self.resize(720, 640)
        self.setMinimumSize(640, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(12)
        # 设置窗沿用主程序的浅色外壳，不和面板皮肤绑死（面板可以是很透明的玻璃，
        # 设置页需要稳定的可读性）
        self.setStyleSheet(theme.DIALOG_QSS + """
QDialog QWidget[role="settingCard"] {
    background: #FFFFFF;
    border: 1px solid #D8E3F5;
    border-radius: 12px;
}
""")
        from lylaunch.plugins.window_chrome import install_chrome

        install_chrome(self, "桌面净化 · 设置", show_minmax=False, resizable=False)

        body = QHBoxLayout()
        body.setSpacing(14)
        self.nav = QListWidget()
        self.nav.setFixedWidth(150)
        self.nav.setUniformItemSizes(True)          # 每行同高，省掉逐项量高
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.setStyleSheet(
            "QListWidget{background:transparent;border:none;font-size:12px;outline:0;}"
            "QListWidget::item{padding:9px 10px;border-radius:8px;color:#3D4657;}"
            # 没有悬停反馈时，鼠标划过完全没反应，像"点了没点中"
            "QListWidget::item:hover{background:#F2F6FC;}"
            # :!active 也要给同样的底色：焦点跑到右侧内容区后，选中项默认会
            # 被系统画成"失活"样式（底色消失），看起来就像"点了不生效"。
            "QListWidget::item:selected{background:#E4EDFC;color:#161C28;}"
            "QListWidget::item:selected:!active{background:#E4EDFC;color:#161C28;}")
        self.pages = QStackedWidget()
        # 懒加载：只给占位页，真正那一页在第一次点到时才建（5 页全建要 50ms，
        # 且每页都有卡片/滑杆，窗口一开就全部参与布局与样式计算）。
        self._page_builders = (
            ("外观", self._page_look),
            ("整理", self._page_organize),
            ("桌面图标", self._page_icons),
            ("行为", self._page_behavior),
            ("关于", self._page_about),
        )
        self._page_cache: dict[int, QWidget] = {}
        for title, _builder in self._page_builders:
            item = QListWidgetItem(title)
            item.setSizeHint(QSize(150, 36))
            self.nav.addItem(item)
            self.pages.addWidget(QWidget())         # 空占位，先占住 stack 下标
        self.nav.currentRowChanged.connect(self._on_nav_row)
        # itemClicked 再兜一层：连点已选中的那一项时 currentRowChanged 不会发信号，
        # 但页面若因为别的原因没同步（比如刚切过皮肤），这一下必须是"再点也生效"。
        self.nav.itemClicked.connect(
            lambda item: self._switch_page(self.nav.row(item)))
        self._on_nav_row(0)
        body.addWidget(self.nav)
        body.addWidget(self.pages, 1)
        root.addLayout(body, 1)

        self.foot = QLabel("")
        self.foot.setProperty("role", "status")
        root.addWidget(self.foot)
        self._refresh_state()

    # ---------------------------------------------------------------- 页面切换
    def _on_nav_row(self, row: int) -> None:
        if row < 0:
            return
        self._switch_page(row)

    def _switch_page(self, row: int) -> None:
        """切到第 row 页；那一页没建过就先建（懒加载）。"""
        if row < 0 or row >= len(self._page_builders):
            return
        self._ensure_page(row)
        # 连点同一个下标也要保证显示的是它（皮肤切换后 index 可能没跟上）
        if self.pages.currentIndex() != row:
            self.pages.setCurrentIndex(row)

    def _ensure_page(self, row: int) -> None:
        """建第 row 页（没建过才建），不负责显示。"""
        if row in self._page_cache:
            return
        builder = self._page_builders[row][1]
        page = builder()
        self._page_cache[row] = page
        # 顶掉占位页，再插回同一个下标，stack 顺序保持不变
        placeholder = self.pages.widget(row)
        self.pages.removeWidget(placeholder)
        if placeholder is not None:
            placeholder.deleteLater()
        self.pages.insertWidget(row, page)
        if row == 0:                     # 外观页里的皮肤按钮要反映当前皮肤
            self._schedule_refresh()

    def _prebuild_pages(self, row: int = 1) -> None:
        """开窗之后趁空闲把其余页逐张建好。

        懒加载解决了「开窗慢」，但第一次点到某一页仍要现场建那一页（10ms 左右，
        加上布局和样式计算就是一次可感知的顿挫）。这里用 0 延时单发串起来，
        每建一页就让事件循环喘一次，用户基本碰不到那个第一次。
        """
        if not self.isVisible() or row >= len(self._page_builders):
            return
        self._ensure_page(row)
        QTimer.singleShot(30, lambda: self._prebuild_pages(row + 1))


    # ---------------------------------------------------------------- 取面板
    def _panel(self):
        try:
            return self._get_panel()
        except Exception:
            return None

    def _schedule_refresh(self) -> None:
        """合并刷新：同一轮里被调多次只做一次。

        `_refresh_state` 要读图标标记文件、问面板当前皮肤、再挨个按钮改选中态；
        构造 / 显示 / 每个动作结束都会碰到它，直接同步调用就是一串重复工作。
        """
        timer = getattr(self, "_refresh_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(0)
            timer.timeout.connect(self._refresh_state)
            self._refresh_timer = timer
        timer.start()

    def _refresh_state(self) -> None:
        panel = self._panel()
        mark_icons = ops.read_icons_flag()
        skin_name = None
        if panel is not None:
            try:
                skin_name = panel._skin.name
            except Exception:
                skin_name = None
        if skin_name is None:
            try:
                skin_name = self.api.settings_get("skin", None)
            except Exception:
                skin_name = None
        self.foot.setText(
            f"当前皮肤：{skin_mod.get(skin_name).label}"
            f" · 桌面图标：{'已隐藏' if mark_icons else '正常'}"
        )
        for name, btn in getattr(self, "_skin_buttons", {}).items():
            btn.setChecked(name == skin_name)
        # 磨砂强度只对玻璃皮肤有意义
        slider = getattr(self, "_level_slider", None)
        if slider is not None:
            slider.setEnabled(skin_name == "glass")

    # ---------------------------------------------------------------- 外观
    def _page_look(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        lay.addWidget(_section(
            "面板皮肤",
            "切换后当前面板会关闭并按新皮肤重建（保留窗口位置与尺寸），下次按快捷键直接生效。"))
        self._skin_buttons = {}
        for name in skin_mod.names():
            skin = skin_mod.get(name)
            card = QFrame()
            card.setProperty("role", "settingCard")
            row = QHBoxLayout(card)
            row.setContentsMargins(14, 12, 14, 12)
            row.setSpacing(10)
            text = QVBoxLayout()
            text.setSpacing(2)
            title = QLabel(f"{skin.label}皮肤")
            title.setStyleSheet("font-size:13px;font-weight:600;")
            text.addWidget(title)
            text.addWidget(_hint(skin.desc))
            row.addLayout(text, 1)
            btn = QPushButton("使用")
            btn.setProperty("role", "primary")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, n=name: self._switch_skin(n))
            row.addWidget(btn)
            self._skin_buttons[name] = btn
            lay.addWidget(card)

        lay.addWidget(self._make_glass_level_card())

        lay.addStretch(1)
        return page

    def _switch_skin(self, name: str) -> None:
        try:
            self.api.settings_set("skin", name)
        except Exception:
            pass
        # 切皮肤会「关掉旧面板 + 按新皮肤重建」，这中间面板引用是临时空的：
        # 在这里连刷两次状态纯属白干（还会让外观页的按钮闪一下选中态）。
        # 所以先让重建跑完，再统一刷新一次。
        if callable(self._on_switch_skin):
            self._on_switch_skin(name)
        self._schedule_refresh()

    def _make_glass_level_card(self) -> QWidget:
        """磨砂强度：数值越小 → 白雾越淡 → 到阈值以下连模糊也关掉，走向全透明。"""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(_section(
            "玻璃磨砂",
            "100 = 最磨砂（背景实时模糊 + 奶白雾）。往左拖：白雾逐渐变淡，"
            "到 35 以下会连系统模糊一起关掉，变成清玻璃（壁纸清晰透出），"
            "再往左白度继续淡出，0 = 完全透明只剩一道描边。"))

        card = QFrame()
        card.setProperty("role", "settingCard")
        row = QVBoxLayout(card)
        row.setContentsMargins(14, 12, 14, 12)
        row.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("磨砂强度")
        title.setStyleSheet("font-size:13px;font-weight:600;")
        head.addWidget(title)
        head.addStretch(1)
        self._level_value = QLabel("100%")
        self._level_value.setProperty("role", "subtitle")
        head.addWidget(self._level_value)
        row.addLayout(head)

        self._level_slider = _JumpSlider(Qt.Orientation.Horizontal)
        self._level_slider.setRange(0, 100)
        self._level_slider.setSingleStep(5)
        self._level_slider.setPageStep(10)
        self._level_slider.setTickInterval(25)
        self._level_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._level_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self._level_slider.setToolTip(
            "数值越小越通透：白雾逐渐变淡，35 以下关掉背景模糊变成清玻璃"
            "（壁纸清晰透出），0 = 完全透明只剩一道描边。")
        self._level_slider.setValue(self._glass_level_value())
        self._level_value.setText(f"{self._level_slider.value()}%")
        self._level_slider.valueChanged.connect(self._on_level_changed)
        row.addWidget(self._level_slider)

        bar = QHBoxLayout()
        left = _hint("0 · 全透明")
        right = _hint("100 · 最磨砂")
        right.setAlignment(Qt.AlignmentFlag.AlignRight)
        bar.addWidget(left, 1)
        bar.addWidget(right)
        row.addLayout(bar)

        # 滑杆每动一格就写盘太频繁：120ms 之后统一落一次
        self._level_timer = QTimer(self)
        self._level_timer.setSingleShot(True)
        self._level_timer.setInterval(120)
        self._level_timer.timeout.connect(self._commit_level)
        # 每动一格就调一次面板的 apply_glass_level（内部是 Win32 accent 调用 +
        # 面板整窗重绘 10ms 级），拖一下滑杆就是几十次 —— 手感直接变成"卡"。
        # 这里改成最多每 33ms 应用一次最新值（拖动中仍然是连续变化）。
        self._level_apply_timer = QTimer(self)
        self._level_apply_timer.setSingleShot(True)
        self._level_apply_timer.setInterval(33)
        self._level_apply_timer.timeout.connect(self._apply_level_now)

        lay.addWidget(card)
        return box

    def _glass_level_value(self) -> int:
        try:
            value = int(self.api.settings_get("glass_level", 100))
        except Exception:
            value = 100
        return max(0, min(100, value))

    def _on_level_changed(self, value: int) -> None:
        self._level_value.setText(f"{value}%")
        apply_timer = getattr(self, "_level_apply_timer", None)
        if apply_timer is not None:
            apply_timer.start()          # 33ms 合并，别每个像素都重设模糊
        if getattr(self, "_level_timer", None) is not None:
            self._level_timer.start()

    def _apply_level_now(self) -> None:
        panel = self._panel()
        apply = getattr(panel, "apply_glass_level", None)
        if callable(apply):
            apply(int(self._level_slider.value()))

    def _commit_level(self) -> None:
        # 落盘前把还没应用的那一档先补上，松手时看到的和存下的必然一致
        apply_timer = getattr(self, "_level_apply_timer", None)
        if apply_timer is not None and apply_timer.isActive():
            apply_timer.stop()
            self._apply_level_now()
        try:
            self.api.settings_set("glass_level",
                                  int(self._level_slider.value()))
        except Exception:
            pass

    # ---------------------------------------------------------------- 整理
    def _page_organize(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        lay.addWidget(_section(
            "收纳与恢复",
            "收纳会把桌面上的文件与快捷方式移进「桌面净化」文件夹再按类型分组；"
            "恢复会按记录把每一项移回原位。两个动作都会先弹确认框。"))
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(_button("收纳桌面文件", self._do("_on_collect"),
                              role="primary",
                              tip="把桌面图标按类型收进「桌面净化」文件夹"))
        row.addWidget(_button("恢复原状", self._do("_on_restore"),
                              tip="把收进文件夹的图标移回桌面原位"))
        row.addStretch(1)
        lay.addLayout(row)

        lay.addWidget(_section(
            "视图",
            "「重扫」只重新读取桌面与分类夹，不改动任何文件；"
            "「新建分组」会在分类夹里建一个空文件夹，之后把瓦片拖进去即可。"))
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addWidget(_button("重新扫描（不动文件）", self._do("_on_rescan")))
        row2.addWidget(_button("新建分组", self._do("_on_new_group")))
        row2.addStretch(1)
        lay.addLayout(row2)
        lay.addStretch(1)
        return page

    def _do(self, method_name: str):
        """设置页按钮 -> 面板上的动作方法（面板可能已被皮肤切换重建）。"""
        def _run():
            panel = self._panel()
            method = getattr(panel, method_name, None)
            if not callable(method):
                self.foot.setText("面板未就绪，请先按一次快捷键打开面板")
                return
            try:
                method()
            except Exception as exc:
                self.foot.setText(f"操作失败：{exc}")
                return
            self._schedule_refresh()
        return _run

    # ---------------------------------------------------------------- 图标
    def _page_icons(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        lay.addWidget(_section(
            "全部桌面图标",
            "隐藏 / 显示整个桌面的图标（注册表 + 发翻转命令，不动任何文件）。\n"
            "标记写在独立文件里，退出程序会自动恢复，不会和收纳账本打架。"))
        self._icons_btn = _button("切换桌面图标显示", self._toggle_icons, role="primary")
        lay.addWidget(self._icons_btn)

        lay.addWidget(_section(
            "分类夹图标",
            "把桌面上的「桌面净化」文件夹换成瓦片样式图标，或还原成系统默认。"))
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(_button("换成瓦片图标", self._set_folder_icon(True)))
        row.addWidget(_button("还原默认图标", self._set_folder_icon(False)))
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        return page

    def _set_folder_icon(self, apply: bool):
        """分类夹图标：换瓦片样式 / 还原系统默认（面板上是同一个方法加参数）。"""
        def _run():
            panel = self._panel()
            method = getattr(panel, "_on_folder_icon", None)
            if not callable(method):
                self.foot.setText("面板未就绪，请先按一次快捷键打开面板")
                return
            try:
                method(apply)
            except Exception as exc:
                self.foot.setText(f"操作失败：{exc}")
                return
            self._schedule_refresh()
        return _run

    def _toggle_icons(self) -> None:
        panel = self._panel()
        toggle = getattr(panel, "_on_toggle_icons", None)
        if not callable(toggle):
            self.foot.setText("面板未就绪，请先按一次快捷键打开面板")
            return
        try:
            toggle(not ops.read_icons_flag())
        except Exception as exc:
            self.foot.setText(f"切换失败：{exc}")
            return
        self._schedule_refresh()

    # ---------------------------------------------------------------- 行为
    def _page_behavior(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        lay.addWidget(_section(
            "快捷键",
            "按一次全局热点打开 / 关闭面板；点下面的按钮可以直接改成别的组合键。"))
        row = QHBoxLayout()
        row.setSpacing(8)
        spec = ""
        panel = self._panel()
        getter = getattr(panel, "_hotkey_getter", None)
        if callable(getter):
            try:
                spec = getter() or ""
            except Exception:
                spec = ""
        self._hotkey_label = QLabel(f"当前：{spec or '未设置'}")
        row.addWidget(self._hotkey_label, 1)
        row.addWidget(_button("修改快捷键", self._edit_hotkey))
        lay.addLayout(row)

        self._restore_box = QCheckBox("程序退出时自动把隐藏的桌面图标恢复显示")
        try:
            self._restore_box.setChecked(
                bool(self.api.settings_get("auto_restore_on_exit", True)))
        except Exception:
            self._restore_box.setChecked(True)
        self._restore_box.toggled.connect(
            lambda on: self._set_setting("auto_restore_on_exit", bool(on)))
        lay.addWidget(self._restore_box)
        lay.addStretch(1)
        return page

    def _set_setting(self, key: str, value) -> None:
        try:
            self.api.settings_set(key, value)
        except Exception:
            pass

    def _edit_hotkey(self) -> None:
        panel = self._panel()
        edit = getattr(panel, "_on_edit_hotkey", None)
        if not callable(edit):
            self.foot.setText("面板未就绪，请先按一次快捷键打开面板")
            return
        edit()
        getter = getattr(panel, "_hotkey_getter", None)
        if callable(getter):
            try:
                self._hotkey_label.setText(f"当前：{getter() or '未设置'}")
            except Exception:
                pass

    # ---------------------------------------------------------------- 关于
    def _page_about(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addWidget(_section(
            "桌面净化",
            "按类型收纳桌面 + 分区浏览 + 临时隐藏桌面图标。\n"
            "面板皮肤：经典 / 液态玻璃（参照 GlassDesk 的观感，纯自绘 + 系统窗口模糊，"
            "不依赖任何第三方库）。"))
        lay.addWidget(_hint("状态文件：%LOCALAPPDATA%\\LyLaunch\\desktop_files_state.json\n"
                            "图标标记：%LOCALAPPDATA%\\LyLaunch\\desktop_files_icons.json"))
        lay.addStretch(1)
        return page

    # ---------------------------------------------------------------- 生命周期
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._schedule_refresh()
        # 首次显示后逐张预热其余页（见 _prebuild_pages 的说明）
        if len(self._page_cache) < len(self._page_builders):
            QTimer.singleShot(60, self._prebuild_pages)

    def closeEvent(self, event) -> None:  # noqa: N802
        # 设置窗只是工具窗：关掉不销毁，下次点齿轮直接复用（避免反复建窗）
        event.ignore()
        self.hide()
