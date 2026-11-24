"""桌面净化 · 皮肤层。

两套皮肤都只是「外观 + 少量结构开关」，不复制业务逻辑：

    classic  经典皮肤：浅色渐变底 + 白卡片 + 左侧分组导航 + 顶部工具条
    glass    液态玻璃：纯透明深色玻璃底 + 玻璃围栏卡片 + 无左导航 +
             搜索胶囊置于标题行，所有改造功能的入口收进设置页

新增皮肤只要往 _SKINS 里加一条，`partition_view` 会自动按字段渲染 ——
面板代码里不再出现写死的色值。

⚠️ 本模块不用 @dataclass：插件模块被动态加载进 sys.path 时，dataclass 的
类型注解解析在 Python 3.14 下会踩 sys.modules 缺失（main.py 已踩过）。
"""

from __future__ import annotations


def rgba(hex_color: str, alpha: float) -> str:
    """#RRGGBB + 0~1 alpha -> "rgba(r,g,b,a)"（QSS 里可直接用）。"""
    color = hex_color.lstrip("#")
    if len(color) == 3:
        color = "".join(ch * 2 for ch in color)
    r, g, b = (int(color[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{max(0.0, min(1.0, alpha)):.3f})"


class Skin:
    """一套皮肤的全部渲染参数（纯数据 + 几段 QSS 生成）。"""

    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)

    # ---------- 结构开关 ----------
    @property
    def shows_nav(self) -> bool:
        return bool(self.nav_visible)

    @property
    def shows_toolbar(self) -> bool:
        return bool(self.toolbar_visible)

    # ---------- 瓦片 ----------
    def tile_qss(self) -> str:
        return (
            f"#fileTile{{border-radius:{self.tile_radius}px;background:transparent;}}"
            f"#fileTile:hover{{background:{self.tile_hover};}}"
            f"#fileTile[kb=\"true\"]{{background:{self.tile_kb_bg};"
            f"border:1px solid {self.tile_kb_border};}}"
        )

    def tile_drag_qss(self) -> str:
        return (f"#fileTile{{border-radius:{self.tile_radius}px;"
                f"background:{self.tile_shadow};}}")

    def tile_name_qss(self) -> str:
        return f"background:transparent;color:{self.name_color};font-size:11px;"

    # ---------- 卡片 ----------
    def card_head_qss(self, tint: str) -> str:
        return (f"#cardHead{{background:{tint};"
                f"border-radius:{self.head_radius}px;}}")

    def card_drag_qss(self) -> str:
        return (f"#partitionCard{{background:{self.card_drag_bg};"
                f"border-color:{self.card_drag_border};}}")

    def empty_qss(self) -> str:
        return (f"color:{self.hint_color};font-size:11px;background:transparent;"
                f"border:1px dashed {self.empty_border};border-radius:10px;"
                "padding:16px 0;")


def _classic() -> Skin:
    """经典（0.1.4 定稿观感）：这段 QSS 与原来写死在 partition_view 里的完全一致。"""
    return Skin(
        name="classic",
        label="经典",
        desc="浅色渐变 + 白色卡片 + 左侧分组导航，信息密度高，适合日常整理。",
        glass=False,
        paged=False,
        bg_alpha=1.0,
        frosted=False,
        window_qss="""
QFrame#partitionCard {
    background: #FFFFFF;
    border: 1px solid #D8E3F5;
    border-radius: 12px;
}
QScrollArea { background: transparent; border: none; }
""",
        nav_visible=True,
        toolbar_visible=True,
        search_in_header=False,
        show_search=True,          # 顶部显示搜索框
        show_title=True,
        nav_width=190,
        wide_threshold=1100,
        # 经典皮肤**永远单列**（原版就是 `_CARD_COLS = 1`：一行一个分组卡、占满宽度，
        # 左侧那 190px 导航是固定的）。之前写成 2 是靠窗口宽度判断，1150 这种最常见
        # 的尺寸就会分两列 → 每张卡只剩 4 个瓦片、又窄又挤，导航跳转也落不到正位。
        card_cols_wide=1,
        window_margins=(24, 18, 24, 16),
        card_margins=(14, 12, 14, 14),
        card_spacing=8,
        head_radius=8,
        head_tint_alpha=0.07,
        card_radius=12,
        card_bg="#FFFFFF",
        card_border="#D8E3F5",
        card_drag_bg="#EEF2F8",
        card_drag_border="#EEF2F8",
        tile_radius=9,
        tile_w=92,
        tile_h=88,
        tile_cols_max=12,
        icon_size=36,
        name_font=11,
        tile_hover="#E4EDFC",
        tile_shadow="#EEF2F8",
        tile_kb_bg="#EAF1FE",
        tile_kb_border="#2F6BEE",
        name_color="#161C28",
        muted_color="#5F6570",
        hint_color="#9AA3B2",
        empty_border="#D8E3F5",
        accent="#2F6BEE",
        title_color="#161C28",
        subtitle_color="#5F6570",
        head_label_color="#161C28",
        nav_btn_qss=(
            "QPushButton{text-align:left;padding:8px 14px;border:none;"
            "border-radius:8px;background:transparent;color:#3D4657;"
            "font-size:12px;}"
            "QPushButton:hover{background:#EEF2F8;color:#161C28;}"
            # 点过的分组常亮，否则点完不知道停在哪一段
            "QPushButton:checked{background:#E4EDFC;color:#161C28;}"),
        search_qss="",
        search_placeholder_color="#9AA3B2",
        fold_btn_qss="border:none;background:transparent;color:#5F6570;font-size:12px;",
        exp_btn_qss="border:none;background:transparent;color:#2F6BEE;font-size:11px;",
        count_qss="background:transparent;font-size:11px;color:#5F6570;",
    )


def _glass(on_dark: bool = True) -> Skin:
    """macOS 浅色玻璃（Light Glassmorphism）。

    面板近乎全透（在 liquid_glass 里只画 ~4% 白），分组卡片也淡到若有若无 ——
    观感靠的是「壁纸清晰透出 + 一道细白边 + 裸放的图标」，而不是任何厚涂底色。
    旧版的「卡片 30% 白 + 白边 + 高光」会把壁纸糊成白板，这里全部压下去。

    文字色必须跟着背后的亮度走：壁纸深 → 白字；壁纸浅 → 深字。两份只差颜色，
    几何完全一致，故用 on_dark 一个参数生成。
    """
    if on_dark:
        ink = "#FFFFFF"
        panel_hint = "深色壁纸"
        name_c = rgba(ink, 0.96)
        muted_c = rgba(ink, 0.62)
        hint_c = rgba(ink, 0.46)
        card_bg = rgba("#FFFFFF", 0.025)
        card_edge = rgba("#FFFFFF", 0.10)
        hover = rgba("#FFFFFF", 0.10)
        search_bg = rgba("#FFFFFF", 0.12)
        search_edge = rgba("#FFFFFF", 0.20)
        search_focus = rgba("#FFFFFF", 0.18)
        btn_bg = rgba("#FFFFFF", 0.10)
        btn_edge = rgba("#A9D0FF", 0.38)
        btn_text = ink
        kb_bg = rgba("#FFFFFF", 0.18)
        kb_edge = rgba("#FFFFFF", 0.62)
        status_edge = rgba("#FFFFFF", 0.08)
        scroll_c = rgba("#FFFFFF", 0.18)
        fold_c = rgba(ink, 0.55)
        exp_c = rgba("#BFD8FF", 0.95)
        count_c = rgba(ink, 0.58)
        sel_bg = rgba("#FFFFFF", 0.26)
    else:
        ink = "#1B1F27"
        panel_hint = "浅色壁纸"
        name_c = rgba(ink, 0.94)
        muted_c = rgba(ink, 0.60)
        hint_c = rgba(ink, 0.44)
        card_bg = rgba("#FFFFFF", 0.055)
        card_edge = rgba("#000000", 0.055)
        hover = rgba("#FFFFFF", 0.16)
        search_bg = rgba("#FFFFFF", 0.30)
        search_edge = rgba("#000000", 0.06)
        search_focus = rgba("#FFFFFF", 0.42)
        # 亮背景侧重：按钮压得更透，避免在玻璃上一块白方块很扎眼（只在 hover 时抬起）
        btn_bg = rgba("#FFFFFF", 0.16)
        btn_edge = rgba("#8FB6FF", 0.26)
        btn_text = "#2A3B5C"
        kb_bg = rgba("#2F6BEE", 0.12)
        kb_edge = rgba("#2F6BEE", 0.45)
        status_edge = rgba("#000000", 0.055)
        scroll_c = rgba("#000000", 0.14)
        fold_c = rgba(ink, 0.52)
        exp_c = rgba("#2F6BEE", 0.92)
        count_c = rgba(ink, 0.52)
        sel_bg = rgba("#2F6BEE", 0.18)

    return Skin(
        name="glass",
        label="浅色玻璃",
        desc=(f"macOS 浅色玻璃（适配{panel_hint}）：近乎全透的面板 + 背景模糊，"
              "分组卡片淡到若有若无，壁纸清晰透出，裸放图标。"),
        glass=True,
        paged=False,              # 不再"一组一页"：8 组翻 8 页太难用，改成两列卡片 + 滚动
        # 底色与边框全部由 liquid_glass 自绘，chrome 内置渐变必须全透明
        bg_alpha=0.0,
        frosted=False,
        window_qss=f"""
QDialog {{
    background: transparent;
}}
#partitionCard {{
    background: transparent;
    border: none;
    border-radius: 24px;
}}
#cardHead {{
    background: transparent;
    border: none;
}}
#fileTile:hover {{
    background: {hover};
}}
QLineEdit#searchBox {{
    background: {search_bg};
    border: 1px solid {search_edge};
    border-radius: 18px;
    padding: 8px 17px;
    font-size: 12px;
    color: {ink};
    selection-background-color: {sel_bg};
}}
QLineEdit#searchBox:focus {{
    background: {search_focus};
    border: 1px solid {rgba("#8FB6FF", 0.50)};
}}
QPushButton[role="glass"] {{
    background: {btn_bg};
    border: 1px solid {btn_edge};
    border-radius: 17px;
    padding: 7px 20px;
    font-size: 12px;
    color: {btn_text};
}}
QPushButton[role="glass"]:hover {{
    background: {rgba("#FFFFFF", 0.26 if on_dark else 0.75)};
    border: 1px solid {rgba("#8FB6FF", 0.85)};
}}
QPushButton[role="glass"]:pressed {{
    background: {rgba("#FFFFFF", 0.34 if on_dark else 0.85)};
}}
QToolButton[role="page"] {{
    background: {btn_bg};
    border: 1px solid {btn_edge};
    border-radius: 13px;
    color: {ink};
    font-size: 16px;
    min-width: 26px;
    min-height: 26px;
}}
QToolButton[role="page"]:hover {{
    background: {rgba("#FFFFFF", 0.26 if on_dark else 0.75)};
}}
QToolButton[role="page"]:disabled {{ color: {hint_c}; }}
/* 玻璃顶部的小图标按钮（设置齿轮 / 关闭 ×）：平时无底无边，hover 才浮出淡底 */
QToolButton[role="glassIcon"] {{
    background: transparent;
    border: none;
    border-radius: 8px;
    color: {rgba(ink, 0.62)};
    font-size: 15px;
    padding: 0;
}}
QToolButton[role="glassIcon"]:hover {{
    background: {rgba("#FFFFFF", 0.22 if on_dark else 0.52)};
    color: {ink};
}}
QToolButton[role="glassIcon"]:pressed {{
    background: {rgba("#FFFFFF", 0.32 if on_dark else 0.68)};
}}
QLabel[role="page"] {{
    background: transparent; color: {muted_c}; font-size: 12px;
}}
QLabel[role="title"] {{ color: {ink}; }}
QLabel[role="subtitle"] {{ color: {muted_c}; }}
QLabel[role="status"] {{ color: {muted_c}; }}
QDialog QWidget[role="statusBar"] {{ border: none; border-top: none; }}
QLabel#chromeTitle {{ color: {ink}; }}
QToolButton {{ color: {fold_c}; }}
QScrollBar:vertical {{
    background: transparent; width: 8px; margin: 6px 0;
}}
QScrollBar::handle:vertical {{
    background: {scroll_c}; border-radius: 4px; min-height: 30px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
""",
        nav_visible=False,
        toolbar_visible=False,
        search_in_header=True,
        show_search=False,         # 纯玻璃块顶部不要搜索框，只留设置
        show_title=False,
        nav_width=0,
        wide_threshold=1000,
        # 每个分组占满一整行（不左右并排）—— 一行一个区块，纵向铺开更好扫视
        card_cols_wide=1,
        window_margins=(18, 12, 18, 10),
        card_margins=(12, 10, 12, 12),
        card_spacing=8,
        head_radius=10,
        head_tint_alpha=0.0,          # 玻璃皮肤不铺标题底色，只留色点
        card_radius=18,
        card_bg=card_bg,
        card_border=card_edge,
        card_drag_bg=rgba("#FFFFFF", 0.34 if on_dark else 0.70),
        card_drag_border=rgba("#8FB6FF", 0.75),
        tile_radius=10,
        tile_w=78,
        tile_h=74,
        tile_cols_max=12,              # 单列卡片占满整行，一行能排 12 个
        icon_size=32,
        name_font=10,
        tile_hover=hover,
        tile_shadow=rgba("#FFFFFF", 0.16 if on_dark else 0.40),
        tile_kb_bg=kb_bg,
        tile_kb_border=kb_edge,
        name_color=name_c,
        muted_color=muted_c,
        hint_color=hint_c,
        empty_border=(rgba("#FFFFFF", 0.22) if on_dark
                      else rgba("#000000", 0.10)),
        accent="#8FB6FF" if on_dark else "#2F6BEE",
        title_color=ink,
        subtitle_color=muted_c,
        head_label_color=name_c,
        nav_btn_qss="",
        search_qss="",
        search_placeholder_color=hint_c,
        fold_btn_qss=f"border:none;background:transparent;color:{fold_c};font-size:12px;",
        exp_btn_qss=f"border:none;background:transparent;color:{exp_c};font-size:11px;",
        count_qss=f"background:transparent;font-size:11px;color:{count_c};",
    )


_SKINS = {s.name: s for s in (_classic(), _glass(True))}
DEFAULT_SKIN = "classic"


def glass_for_luminance(luminance: float) -> Skin:
    """按窗口背后画面的平均亮度选文字色：暗处用白字，亮处用深字。

    玻璃皮肤只有 15% 白，字压不住底色 —— 同一个面板在深/浅壁纸下必须换墨色，
    否则总有一边看不清。
    """
    return _glass(on_dark=float(luminance) < 0.55)


def names() -> list[str]:
    return list(_SKINS)


def get(name: str | None) -> Skin:
    return _SKINS.get((name or "").strip(), _SKINS[DEFAULT_SKIN])


def current(api) -> Skin:
    """从插件配置里读当前皮肤；读失败一律回经典，皮肤坏了不能开不出窗口。"""
    value = None
    getter = getattr(api, "settings_get", None)
    if callable(getter):
        try:
            value = getter("skin", DEFAULT_SKIN)
        except Exception:
            value = None
    return get(value)
