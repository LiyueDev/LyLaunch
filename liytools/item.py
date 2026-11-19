"""统一的列表条目（应用 / 系统应用 / 收藏 / 提示 都用它）。

对应 C# 的 Item 类。用 dataclass 存字段，图标用 Qt 的 QPixmap 在后台加载后回填。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtGui import QPixmap


@dataclass
class Item:
    name: str = ""                 # 显示文字（应用名）
    path: str = ""                 # 启动路径（.lnk / .exe）；空则不可启动
    item_type: str = "应用"        # 应用 / 系统应用 / 提示（占位）/ 剪贴板
    clip: Optional[str] = None     # 仅剪贴板项存原文
    launch: Optional[str] = None   # 系统应用启动命令（URI 如 ms-settings:）；空则用 path
    launch_args: Optional[str] = None
    # 瓦片兜底：没有真实图标时显示首字母 / emoji
    icon_text: str = "?"
    icon_fg: str = "#FFFFFF"
    tile_color: str = "#2F6BEE"
    icon_size: int = 17
    # 0.36「图标·线性」：插件瓦片的线性 SVG path（非空则优先于 icon_text / 真实图标）
    icon_svg: str = ""
    # 区块：收藏应用 / 应用 / 系统应用
    group_label: str = "应用"
    group_order: int = 1
    # 拼音搜索用（启动时不算，首次搜索时才填；见 search_index.rebuild_search_cache）
    pinyin: str = ""
    pinyin_full: str = ""
    # 搜索别名（系统应用才有，如"设置"的 "set windows"）；搜索时拼进拼音一起算
    search_alias: str = ""
    # 真实图标（后台加载后回填）
    icon_pixmap: Optional[QPixmap] = field(default=None, repr=False)
    # 真实图标来源（可能和 path 不同，如系统应用取 SystemSettings.exe）
    icon_path: Optional[str] = None

    @property
    def is_system(self) -> bool:
        return bool(self.path and self.path.lower().startswith("ms-settings:"))
