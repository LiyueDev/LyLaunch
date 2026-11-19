"""插件弹窗统一设计系统（0.36 定稿：色 B 通透干净 + 布局 A 基准）。

**事实来源**：设计原型的 `html[data-scheme="air"]` 色板与 token 表；
本文件集中定义插件弹窗的设计 token，改动时请保持各弹窗一致。

插件弹窗直接 `setStyleSheet(theme.DIALOG_QSS)` 即可得到统一观感。定稿窗口底是
白→淡蓝纵向渐变（qlineargradient），卡片纯白 + 1px 外描边，顶部内高光交给
QSS `border-top`；主色 #2F6BEE、次要文字 #5F6570（WCAG AA 达标）。

**角色（role）约定** —— 用 `widget.setProperty("role", "...")` 声明，QSS 自动套样式。
注意：role 必须在 `setStyleSheet` 之前设置，QSS 才能匹配上；QWidget 子类想让
QSS 画出自身背景，还要 `setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)`。

    QLabel:
        title       弹窗大标题（18px 加粗）
        subtitle    标题下的一句话说明（12px 次要灰）
        section     分区小标题，如「原文」「译文」（12px 加粗）
        badge       小徽章，如「中 → 英」（浅蓝底主色字）
        status      底部状态行（12px 次要灰）
        preview     图片预览占位卡（白底描边圆角）
        phTitle     插件窗口通用壳标题（16px 600）
        phSub       通用壳副标题（12px 次要灰）
        phSection   通用壳分区标题（12px 600）
        phNote      通用壳注释（11px 次要灰）
    QWidget / QFrame（需 WA_StyledBackground）:
        card        通用内容卡（白底 + 1px 描边 + 圆角 10）
        cardSoft    次级内容卡（#F2F6FD 底）
        head        图标头部容器（透明）
        statusBar   底部单一状态栏（1px 上描边）
        iconBox     40×40 线性图标底（#F2F6FD + 圆角 10）
    QPushButton:
        primary     主操作（实心主色 + 白字）
        soft        次操作胶囊（#F2F6FD 底 + 主色字）
        （默认）     次操作（白底 1px 描边）
        ghost       行内轻操作，如「复制」「清空」（无底无框，hover 才出现浅蓝）
        danger      警示操作（红字红框）

颜色常量供代码内联使用（如需要逐像素控制的 QLabel / QPainter）。旧常量名全部
保留为别名，改值即可让既有插件无感升级；新增常量对齐原型 token。
"""

from __future__ import annotations

# ── 定稿色板（色 B 通透干净）──────────────────────────────────────────
BG_WINDOW = "#FFFFFF"          # --bg-window / --bg-surface
BG_SURFACE = "#FFFFFF"
BG_SUBTLE = "#F2F6FD"          # --bg-subtle：次级底（热键胶囊、代码块、只读框）
BG_TILE_HOVER = "#E4EDFC"      # --bg-tile-hover：悬停
BG_PRESSED = "#D6E2F7"         # --bg-pressed：按下
PANEL_BOTTOM = "#E8F0FE"       # --panel-fill 渐变终点

TEXT_PRIMARY = "#161C28"       # --text-primary
TEXT_SECONDARY = "#5F6570"     # --text-secondary（AA 达标，替换全部 #8A919F）
TEXT_PLACEHOLDER = "#9AA3B2"   # 占位文字：比次要再淡一档，仍可读

ACCENT = "#2F6BEE"             # --accent
ACCENT_HOVER = "#2359D6"       # --accent-hover
ACCENT_PRESSED = "#1E4FBE"     # 按下再深一档
ACCENT_DISABLED = "#BFD3FA"

BORDER = "#D8E3F5"             # --border：常规描边
BORDER_STRONG = "#C3D3EE"      # --border-strong：强描边 / 滚动条
TRACK_OFF = "#B7C4D8"          # 开关关闭态轨道
TRACK_OFF_HOVER = "#A6B5CD"

SOFT_BG = "#DCE9FF"            # --soft-bg：仅选中态 / 徽章
SOFT_FG = "#2359D6"            # --soft-fg：选中态文字
PLUGIN_BG = "#F2F6FD"          # --plugin-bg：主面板与插件窗口的图标底
SIDE_BG = "#F8FBFF"            # --side-bg：AI 会话栏底
USER_BUBBLE_BG = "#EDF3FF"     # --user-bubble-bg：AI 用户气泡

DANGER = "#D93A2F"             # --danger
DANGER_BORDER = "#F5B3AD"
DANGER_SOFT = "#FDECEA"
SUCCESS = "#22C55E"
WARNING = "#E8A33D"

RADIUS_CARD = 10               # 布局 A：卡片 10 / Tile 10 / 窗口 14
RADIUS_WINDOW = 14
RADIUS_BTN = 8

FONT_FAMILY = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif'

# ── 向后兼容别名（既有插件读这些名字）────────────────────────────────
PRIMARY = ACCENT
PRIMARY_HOVER = ACCENT_HOVER
PRIMARY_PRESSED = ACCENT_PRESSED
PRIMARY_DISABLED = ACCENT_DISABLED
PRIMARY_SOFT = SOFT_BG
BG = BG_WINDOW
CARD = BG_SURFACE
CARD_READONLY = BG_SUBTLE
TEXT = TEXT_PRIMARY
TEXT_BODY = TEXT_PRIMARY
TEXT_SECTION = TEXT_SECONDARY
TEXT_SUB = TEXT_SECONDARY
HOVER = BG_TILE_HOVER
PRESSED = BG_PRESSED

# 供不读 QSS 的代码内联取色（如 tray 图标、生成器）
TOKENS = {
    "bg_window": BG_WINDOW, "bg_surface": BG_SURFACE, "bg_subtle": BG_SUBTLE,
    "bg_tile_hover": BG_TILE_HOVER, "bg_pressed": BG_PRESSED,
    "text_primary": TEXT_PRIMARY, "text_secondary": TEXT_SECONDARY,
    "accent": ACCENT, "accent_hover": ACCENT_HOVER,
    "border": BORDER, "border_strong": BORDER_STRONG,
    "soft_bg": SOFT_BG, "soft_fg": SOFT_FG, "plugin_bg": PLUGIN_BG,
    "side_bg": SIDE_BG, "user_bubble_bg": USER_BUBBLE_BG,
    "danger": DANGER, "success": SUCCESS, "warning": WARNING,
}

def apply_light_titlebar(widget) -> None:
    """Windows：把系统标题栏统一成项目风格（纯白底 + 深色字 + 无彩色边框）。

    0.36 定稿所有窗口都是浅色内容（白→淡蓝渐变），若系统开了深色主题，
    QDialog 的原生标题栏会变成深色，和白底割裂。DWMWA_USE_IMMERSIVE_DARK_MODE(20)
    置 0 即可单独把该窗口的标题栏改回浅色。

    0.39.x：进一步用 DWMWA_CAPTION_COLOR(35) / TEXT_COLOR(36) / BORDER_COLOR(37)
    把标题栏染成内容区顶部同色 **#FFFFFF**、文字 #161C28、边框也 #FFFFFF，
    消掉「白色系统条 + 浅蓝内容区」之间那条扎眼的分隔线（用户反馈割裂）。
    这几个属性只有 Win11 22H2+ 支持，旧系统返回错误码，静默忽略即可。
    调用时机：窗口构造完成后（winId() 会创建原生句柄），show() 之前即可。
    """
    try:
        import ctypes
        hwnd = ctypes.c_void_p(int(widget.winId()))
        dwmapi = ctypes.windll.dwmapi
        val = ctypes.c_int(0)
        dwmapi.DwmSetWindowAttribute(
            hwnd, ctypes.c_uint(20), ctypes.byref(val), ctypes.sizeof(val))
        # COLORREF 是 0x00BBGGRR（不是 RGB）
        for attr, colorref in (
                (35, 0x00FFFFFF),   # caption  #FFFFFF，与渐变顶部一致
                (36, 0x00281C16),   # text     #161C28
                (37, 0x00FFFFFF)):  # border   #FFFFFF，弱化分隔线
            c = ctypes.c_uint(colorref)
            dwmapi.DwmSetWindowAttribute(
                hwnd, ctypes.c_uint(attr), ctypes.byref(c), ctypes.sizeof(c))
    except Exception:
        pass


def disable_window_transitions(widget) -> None:
    """Windows：关掉该窗口 show/hide 的 DWM 过渡动画。

    为什么需要：截图要"先把窗口藏起来，等它真从屏幕上消失，再抓屏"。
    默认 hide() 之后 DWM 还会做一次淡出过渡（约 100~200ms），过渡帧是半透明的 ——
    这时候抓屏就会把**半透明的窗口残影**截进图里（0.37 实测，设置页截图带残影）。
    关掉过渡后 hide() 立即生效，抓屏不用靠"猜一个够长的延迟"。

    DWMWA_TRANSITIONS_FORCEDISABLED = 3（Win8+）；非 Windows / 属性不支持时静默跳过。
    调用时机：窗口构造完成后（winId() 会创建原生句柄）。
    """
    try:
        import ctypes
        dwmapi = ctypes.windll.dwmapi
        val = ctypes.c_int(1)
        dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(int(widget.winId())), ctypes.c_uint(3),
            ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass


DIALOG_QSS = f"""
QDialog {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 {BG_WINDOW}, stop:1 {PANEL_BOTTOM});
    font-family: {FONT_FAMILY};
}}
QDialog QLabel {{
    color: {TEXT_PRIMARY};
    font-size: 13px;
}}

/* ── 文字层级 ─────────────────────────────────────── */
QDialog QLabel[role="title"] {{
    font-size: 18px;
    font-weight: 600;
    color: {TEXT_PRIMARY};
}}
QDialog QLabel[role="subtitle"] {{
    font-size: 12px;
    color: {TEXT_SECONDARY};
}}
QDialog QLabel[role="section"] {{
    font-size: 12px;
    font-weight: 600;
    color: {TEXT_SECONDARY};
}}
QDialog QLabel[role="status"] {{
    font-size: 12px;
    color: {TEXT_SECONDARY};
}}
QDialog QLabel[role="badge"] {{
    background: {SOFT_BG};
    color: {SOFT_FG};
    border-radius: 9px;
    padding: 2px 9px;
    font-size: 11px;
    font-weight: 600;
}}
QDialog QLabel[role="preview"] {{
    background: {BG_SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CARD}px;
    color: {TEXT_SECONDARY};
    font-size: 12px;
}}

/* ── 插件窗口通用壳：图标头部 + 卡片内容 + 单一底部状态栏 ── */
QDialog QLabel[role="phTitle"] {{
    font-size: 16px; font-weight: 600; color: {TEXT_PRIMARY};
}}
QDialog QLabel[role="phSub"] {{
    font-size: 12px; color: {TEXT_SECONDARY};
}}
QDialog QLabel[role="phSection"] {{
    font-size: 12px; font-weight: 600; color: {TEXT_SECONDARY};
}}
QDialog QLabel[role="phNote"] {{
    font-size: 11px; color: {TEXT_SECONDARY};
}}
QDialog QWidget[role="card"] {{
    background: {BG_SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CARD}px;
}}
QDialog QWidget[role="cardSoft"] {{
    background: {BG_SUBTLE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CARD}px;
}}
QDialog QWidget[role="head"] {{
    background: transparent;
}}
QDialog QWidget[role="iconBox"] {{
    background: {PLUGIN_BG};
    border-radius: {RADIUS_CARD}px;
}}
QDialog QWidget[role="statusBar"] {{
    background: transparent;
    border-top: 1px solid {BORDER};
}}

/* ── 输入 / 输出：本体就是一张卡片，聚焦时描边转主色 ── */
QDialog QLineEdit, QDialog QPlainTextEdit, QDialog QTextEdit {{
    background: {BG_SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CARD}px;
    padding: 10px 12px;
    font-size: 13px;
    color: {TEXT_PRIMARY};
    selection-background-color: {ACCENT};
    placeholder-text-color: {TEXT_PLACEHOLDER};
}}
QDialog QLineEdit:focus, QDialog QPlainTextEdit:focus, QDialog QTextEdit:focus {{
    border: 1px solid {ACCENT};
}}
QDialog QPlainTextEdit[readOnly="true"], QDialog QTextEdit[readOnly="true"] {{
    background: {BG_SUBTLE};
}}
QDialog QPlainTextEdit[readOnly="true"]:focus,
QDialog QTextEdit[readOnly="true"]:focus {{
    border: 1px solid {BORDER};
}}

/* ── 按钮：主 / 次 / 软胶囊 / 幽灵 / 危险 ─────────── */
QDialog QPushButton {{
    background: {BG_SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_BTN}px;
    padding: 7px 14px;
    font-size: 12px;
    color: {TEXT_PRIMARY};
}}
QDialog QPushButton:hover {{
    background: {BG_TILE_HOVER};
    border-color: {BORDER_STRONG};
}}
QDialog QPushButton:pressed {{ background: {BG_PRESSED}; }}
QDialog QPushButton:disabled {{
    color: {TEXT_SECONDARY};
    background: {BG_SUBTLE};
    border-color: {BORDER};
}}
QDialog QPushButton[role="primary"] {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: #FFFFFF;
    font-weight: 600;
    padding: 8px 14px;
}}
QDialog QPushButton[role="primary"]:hover {{
    background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER};
}}
QDialog QPushButton[role="primary"]:pressed {{
    background: {ACCENT_PRESSED}; border-color: {ACCENT_PRESSED};
}}
QDialog QPushButton[role="primary"]:disabled {{
    background: {ACCENT_DISABLED}; border-color: {ACCENT_DISABLED};
    color: #FFFFFF;
}}
QDialog QPushButton[role="soft"] {{
    background: {BG_SUBTLE};
    border: 1px solid {BORDER};
    color: {ACCENT_HOVER};
}}
QDialog QPushButton[role="soft"]:hover {{ background: {BG_TILE_HOVER}; }}
QDialog QPushButton[role="ghost"] {{
    background: transparent;
    border: none;
    color: {TEXT_SECONDARY};
    padding: 4px 8px;
    font-size: 12px;
}}
QDialog QPushButton[role="ghost"]:hover {{
    background: {BG_TILE_HOVER};
    color: {TEXT_PRIMARY};
}}
QDialog QPushButton[role="danger"] {{
    background: {BG_SURFACE};
    border: 1px solid {DANGER_BORDER};
    color: {DANGER};
}}
QDialog QPushButton[role="danger"]:hover {{ background: {DANGER_SOFT}; }}

/* ── 下拉框：和输入框同一套外观 ───────────────────── */
QDialog QComboBox {{
    background: {BG_SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_BTN}px;
    padding: 7px 10px;
    font-size: 13px;
    color: {TEXT_PRIMARY};
    min-width: 96px;
}}
QDialog QComboBox:hover {{ border-color: {BORDER_STRONG}; }}
QDialog QComboBox:focus {{ border-color: {ACCENT}; }}
QDialog QComboBox::drop-down {{ border: none; width: 18px; }}
QDialog QComboBox QAbstractItemView {{
    background: {BG_SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_BTN}px;
    padding: 4px;
    outline: none;
    selection-background-color: {BG_TILE_HOVER};
    selection-color: {TEXT_PRIMARY};
}}

/* ── 进度条：细条 + 主色，用于引擎准备/模型下载 ───── */
QDialog QProgressBar {{
    background: {BG_SUBTLE};
    border: none;
    border-radius: 3px;
    color: transparent;
}}
QDialog QProgressBar::chunk {{
    background: {ACCENT};
    border-radius: 3px;
}}

/* ── 滚动条：6px 细、圆、无箭头（对齐原型） ───────── */
QDialog QScrollBar:vertical {{
    background: transparent;
    width: 6px;
    margin: 2px 2px 2px 0;
}}
QDialog QScrollBar::handle:vertical {{
    background: {BORDER_STRONG};
    border-radius: 3px;
    min-height: 28px;
}}
QDialog QScrollBar::handle:vertical:hover {{ background: {TRACK_OFF_HOVER}; }}
QDialog QScrollBar::add-line:vertical, QDialog QScrollBar::sub-line:vertical {{ height: 0; }}
QDialog QScrollBar::add-page:vertical, QDialog QScrollBar::sub-page:vertical {{
    background: transparent;
}}

/* ── 提示气泡 ─────────────────────────────────────── */
QDialog QToolTip {{
    background: {TEXT_PRIMARY};
    color: #FFFFFF;
    border: none;
    padding: 5px 8px;
    font-size: 12px;
}}
"""


# ── 消息框（QMessageBox）───────────────────────────────────────────────
# 为什么不能直接 QMessageBox.warning(parent, ...)：
#   MainWindow 上挂了 setStyleSheet("background:transparent;")（底色靠 paintEvent 自绘），
#   Qt 的样式表会被**子窗口继承** —— 于是 QMessageBox 拿到 background:transparent，
#   窗口自己什么都不画 → 露出黑色窗口底，配深色文字就是"黑底黑字"，按钮也几乎看不见。
# 解决：消息框必须自带完整样式（不依赖继承），并且单独把系统标题栏改成浅色。
_MESSAGE_BOX_QSS = DIALOG_QSS + f"""
QMessageBox {{
    background: {BG_WINDOW};
}}
QMessageBox QLabel {{
    color: {TEXT_PRIMARY};
    background: transparent;
    font-size: 13px;
}}
QMessageBox QPushButton {{
    min-width: 72px;
    min-height: 26px;
    padding: 4px 16px;
    font-size: 12px;
    color: {TEXT_PRIMARY};
    background: {BG_SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_BTN}px;
}}
QMessageBox QPushButton:hover {{
    background: {BG_TILE_HOVER};
    border-color: {BORDER_STRONG};
}}
QMessageBox QPushButton:pressed {{ background: {BG_PRESSED}; }}
QMessageBox QPushButton:default {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: #FFFFFF;
    font-weight: 600;
}}
QMessageBox QPushButton:default:hover {{
    background: {ACCENT_HOVER};
    border-color: {ACCENT_HOVER};
}}
"""


def _message_box(parent, title: str, text: str, icon):
    """建一个带项目配色 + 浅色标题栏的 QMessageBox（未 exec）。"""
    from PySide6.QtWidgets import QMessageBox
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(icon)
    box.setStyleSheet(_MESSAGE_BOX_QSS)
    apply_light_titlebar(box)   # 系统标题栏跟随浅色，别白底配黑标题栏
    return box


def show_warning(parent, title: str, text: str) -> None:
    """统一警告框（黑底黑字问题的替代品）。"""
    from PySide6.QtWidgets import QMessageBox
    box = _message_box(parent, title, text, QMessageBox.Icon.Warning)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    btn = box.button(QMessageBox.StandardButton.Ok)
    if btn is not None:
        btn.setText("知道了")
    box.exec()


def ask_confirm(parent, title: str, text: str, *, default_yes: bool = False,
                yes_text: str = "确定", no_text: str = "取消") -> bool:
    """统一确认框：返回 True = 用户选了「确定」。"""
    from PySide6.QtWidgets import QMessageBox
    box = _message_box(parent, title, text, QMessageBox.Icon.Question)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(
        QMessageBox.StandardButton.Yes if default_yes else QMessageBox.StandardButton.No)
    b_yes = box.button(QMessageBox.StandardButton.Yes)
    b_no = box.button(QMessageBox.StandardButton.No)
    if b_yes is not None:
        b_yes.setText(yes_text)
    if b_no is not None:
        b_no.setText(no_text)
    return box.exec() == QMessageBox.StandardButton.Yes
