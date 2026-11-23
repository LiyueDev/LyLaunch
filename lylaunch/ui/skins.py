"""皮肤视觉 token：把「原版 / 毛玻璃」两套色值与圆角集中在一处。

为什么单独成模块：main_window 里原来散着几十处硬编码色值，做玻璃皮肤时
每一处都要"玻璃一套、原版一套"，散着改必然漏。这里用一张表收口：

- solid 分支**复刻现有值**，保证原版像素级不变（改完用截图 diff 校验）；
- glass 分支走 Apple 那套「液态玻璃」：面板更透 + 顶部高光 + 半透明控件 +
  中性投影，控件不再是纯白实底。

调用方只读 `active()`；窗口切皮肤时调 `set_active(name)`，其余组件下次重绘即可生效。
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor

from .. import config


@dataclass(frozen=True)
class Tokens:
    name: str

    # ---- 面板底 ----
    scrim_top: QColor           # 顶部罩层（含 alpha）
    scrim_bottom: QColor        # 底部罩层
    sheen: QColor               # 顶部"光"，玻璃质感来源
    sheen_height: int = 88      # 光的高度（逻辑 px）

    # ---- 控件（QSS 片段用的字符串）----
    field_bg: str = ""
    field_border: str = ""
    field_focus_bg: str = ""
    field_focus_border: str = ""
    chip_bg: str = ""
    chip_border: str = ""

    # ---- 瓦片（QPainter 用）----
    tile_bg: QColor | None = None
    tile_border: QColor | None = None
    hover_bg: QColor | None = None
    selected_bg: QColor | None = None
    selected_ring: QColor | None = None
    icon_bg: str = ""
    icon_border: str = ""

    # ---- 文字 ----
    text_primary: str = ""
    text_secondary: str = ""
    link: str = ""
    link_hover: str = ""
    link_hover_bg: str = ""

    # ---- 滚动条 / 齿轮 ----
    scroll_handle: str = ""
    scroll_handle_hover: str = ""
    scroll_handle_pressed: str = ""
    gear_hover: QColor | None = None
    gear_pressed: QColor | None = None


# 原版：一个字都不改，全部复刻改造前的值
SOLID = Tokens(
    name="solid",
    scrim_top=QColor(255, 255, 255, 255),
    scrim_bottom=QColor(232, 240, 254, 255),
    sheen=QColor(255, 255, 255, 0),
    field_bg="#FFFFFF",
    field_border="transparent",
    field_focus_bg="#FFFFFF",
    field_focus_border="#2F6BEE",
    chip_bg="#FFFFFF",
    chip_border="#D8E3F5",
    tile_bg=None,               # 原版默认不画底
    tile_border=None,
    hover_bg=QColor("#E4EDFC"),
    selected_bg=QColor("#E4EDFC"),
    selected_ring=None,
    icon_bg="",
    icon_border="",
    text_primary="#161C28",
    text_secondary="#5F6570",
    link="rgba(47,107,238,0.62)",
    link_hover="#2F6BEE",
    link_hover_bg="#E4EDFC",
    scroll_handle="#C3D3EE",
    scroll_handle_hover="#B7C4D8",
    scroll_handle_pressed="#5F6570",
    gear_hover=QColor("#E4EDFC"),
    gear_pressed=QColor("#D6E2F7"),
)

# 毛玻璃「液态玻璃」：更透的面板 + 半透明控件 + 单一强调色 #007AFF
GLASS = Tokens(
    name="glass",
    scrim_top=QColor(255, 255, 255, 200),     # 0.78
    scrim_bottom=QColor(248, 250, 255, 188),  # 0.74
    sheen=QColor(255, 255, 255, 40),          # 0.16，只有顶部一道
    # 控件用"中性灰玻璃"而不是纯白：白压白等于没底，灰一点才读得出层次
    field_bg="rgba(120,120,128,0.16)",
    field_border="rgba(255,255,255,0.70)",
    field_focus_bg="rgba(120,120,128,0.10)",  # 聚焦只加环，不把框刷成白板
    field_focus_border="rgba(0,122,255,0.55)",
    chip_bg="rgba(120,120,128,0.12)",
    chip_border="rgba(255,255,255,0.55)",
    tile_bg=None,                              # 默认不铺底：图标要"浮"在玻璃上
    tile_border=None,
    hover_bg=QColor(255, 255, 255, 120),      # 0.47，压在浅玻璃上像"浮起来"
    selected_bg=QColor(0, 122, 255, 26),      # 选中：极淡的主色，跟原版 #E4EDFC 同族
    selected_ring=QColor(0, 122, 255, 140),   # 0.55
    icon_bg="rgba(255,255,255,0.34)",
    icon_border="rgba(255,255,255,0.60)",
    text_primary="#1D1D1F",
    text_secondary="rgba(60,60,67,0.62)",
    link="rgba(0,122,255,0.86)",
    link_hover="#007AFF",
    link_hover_bg="rgba(0,122,255,0.10)",
    scroll_handle="rgba(0,0,0,0.14)",
    scroll_handle_hover="rgba(0,0,0,0.24)",
    scroll_handle_pressed="rgba(0,0,0,0.34)",
    gear_hover=QColor(255, 255, 255, 115),    # 0.45
    gear_pressed=QColor(255, 255, 255, 160),  # 0.63
)

_TABLE = {"solid": SOLID, "glass": GLASS}


# ---------- QSS 片段 ----------
# 为什么放这里：搜索框 / 输入 chip / 滚动条都是"创建时设一次"的样式，
# 切皮肤时必须重设一次。字符串写在两边会漂移，统一从这里取。
def field_qss(tk: Tokens) -> str:
    return (f"QLineEdit{{background:{tk.field_bg};"
            f" border:1px solid {tk.field_border}; border-radius:12px;"
            f" padding:0 14px; font-size:15px; color:{tk.text_primary};}}"
            f"QLineEdit:focus{{background:{tk.field_focus_bg};"
            f" border:1px solid {tk.field_focus_border};}}")


def chip_qss(tk: Tokens) -> str:
    return (f"background:{tk.chip_bg}; border:1px solid {tk.chip_border};"
            " border-radius:10px;")


def scroller_qss(tk: Tokens) -> str:
    return ("QScrollArea{border:none; background:transparent;}"
            "QScrollBar:vertical{width:3px; margin:0; border:none; background:transparent;}"
            "QScrollBar::groove:vertical{background:transparent; border:none;}"
            f"QScrollBar::handle:vertical{{background:{tk.scroll_handle};"
            " border-radius:1px; min-height:28px;}"
            f"QScrollBar::handle:vertical:hover{{background:{tk.scroll_handle_hover};}}"
            f"QScrollBar::handle:vertical:pressed{{background:{tk.scroll_handle_pressed};}}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical{height:0;}"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical{background:transparent;}")

_active: Tokens | None = None


def tokens(name: str) -> Tokens:
    return _TABLE.get(name, SOLID)


def set_active(name: str) -> None:
    global _active
    _active = tokens(name)


def active() -> Tokens:
    global _active
    if _active is None:
        _active = tokens(config.load_win_skin())
    return _active


def is_glass() -> bool:
    return active().name == "glass"
