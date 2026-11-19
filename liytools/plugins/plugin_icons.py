"""0.36「图标·线性」：插件瓦片的 24×24 线性 SVG（Fluent / Lucide 风格，MIT / ISC，可商用）。

path 数据取自统一的图标路径定义 ICON_PATHS。
渲染走 QSvgRenderer；着色直接把 stroke 写进 SVG 字符串（当前只有浅色面板，主色固定）。
键匹配用宽松包含，避免 plugin.json 的 id 写法差异导致漏配。
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

PLUGIN_ICON_BG = "#F2F6FD"   # --plugin-bg：贴近面板背景的插件图标底
_PLUGIN_FG = "#2F6BEE"       # --accent 主色描边
PLUGIN_ICON_FG = _PLUGIN_FG  # 公开别名：瓦片兜底（取不到真实图标时）也用同款主色字

# 键为语义片段（与 plugin id 宽松包含匹配），值为 24×24 线性 path
_PATHS: dict[str, str] = {
    "translat": '<path d="M3 5h10M8 3v2c0 4-2 6.5-5 8"/><path d="M5 9c1 2.5 3 4 6 5"/><path d="M13 20l4-10 4 10M14.5 16.5h5"/>',
    "ocr": '<path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3"/><path d="M4 12h16"/>',
    "doc": '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v5h4M9 13h6M9 17h6"/>',
    "chat": '<path d="M4 5h16v11H9l-5 4z"/><path d="M8 9h8M8 12h5"/>',
    "screen": '<path d="M6 2v16h16M2 6h16v16"/>',
    "shot": '<path d="M6 2v16h16M2 6h16v16"/>',
    # 硬件信息插件：芯片/主板造型，避免落 _DEFAULT 四角星
    "hwinfo": '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 1.5v2.5M15 1.5v2.5M9 20v2.5M15 20v2.5M1.5 9H4M1.5 15H4M20 9h2.5M20 15h2.5"/>',
    # 桌面文件插件：文件夹 + 两行文件条目
    "desktop": '<path d="M3 6h6l2 2h10v11H3z"/><path d="M8 12h8M8 15h5"/>',
    # 剪贴板历史插件（0.43）：夹板 + 顶部夹扣
    "clipboard": '<rect x="8" y="2.5" width="8" height="4" rx="1.2"/>'
                 '<path d="M16 4.5h1.8A2.2 2.2 0 0 1 20 6.7v13.1A2.2 2.2 0 0 1 17.8 22H6.2A2.2 2.2 0 0 1 4 19.8V6.7'
                 'A2.2 2.2 0 0 1 6.2 4.5H8"/>'
                 '<path d="M8 11h8M8 15h5"/>',
}
_DEFAULT = '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/>'

# ── 通用线性图标（0.36 11-11）：系统应用 / 粘贴动作复用插件那套视觉语言 ──
# 和 _PATHS 同一规范：24×24 viewBox、fill=none、stroke-width 1.8、圆头圆角。
# 图形为 Fluent / Lucide 风格的简笔路径，MIT / ISC 可商用，不引第三方资源。
_GENERIC: dict[str, str] = {
    "terminal": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M12 15h5"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v2.6M12 18.9v2.6M2.5 12h2.6M18.9 12h2.6M5.3 5.3l1.9 1.9M16.8 16.8l1.9 1.9M18.7 5.3l-1.9 1.9M7.2 16.8l-1.9 1.9"/>',
    "package": '<path d="M4 7h16v12H4z"/><path d="M4 7l2-3h12l2 3M9 11h6"/>',
    "note": '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>',
    "calc": '<rect x="5" y="2" width="14" height="20" rx="2"/><path d="M8 6h8M8 11h2M14 11h2M8 15h2M14 15h2M8 19h8"/>',
    "activity": '<path d="M3 13h4l3-7 4 12 3-6h4"/>',
    "folder": '<path d="M3 6h6l2 2h10v11H3z"/>',
    "sliders": '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
    "tree": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18M12 8h5M12 12h5M12 16h5"/>',
    "brush": '<path d="M4 20c3 0 3-2 6-2M13 5l6 6-8 8-6-6z"/><path d="M9 11l4 4"/>',
    "home": '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10M9 20v-6h6v6"/>',
    "monitor": '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
    "bluetooth": '<path d="M7 7l10 10-5 4V3l5 4L7 17"/>',
    "wifi": '<path d="M2 8a16 16 0 0 1 20 0M5 12a11 11 0 0 1 14 0M8.5 16a6 6 0 0 1 7 0"/><circle cx="12" cy="20" r="1"/>',
    "refresh": '<path d="M20 11a8 8 0 1 0-2.4 6.3"/><path d="M20 4v7h-7"/>',
    "palette": '<path d="M12 3a9 9 0 1 0 0 18h1.4a2 2 0 0 0 0-4H12a2 2 0 0 1 0-4h6a3 3 0 0 0 3-3 7 7 0 0 0-9-7z"/><circle cx="7.5" cy="11" r=".8"/><circle cx="10" cy="7.5" r=".8"/><circle cx="15" cy="7.5" r=".8"/>',
    "volume": '<path d="M4 9v6h4l5 4V5L8 9z"/><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11"/>',
    "power": '<path d="M12 3v9"/><path d="M6.5 6.5a8 8 0 1 0 11 0"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "grid": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    "database": '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "shield": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    "open": '<path d="M13 4h7v7M20 4l-9 9"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>',
    "locate": '<circle cx="12" cy="12" r="7"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/><circle cx="12" cy="12" r="2"/>',
    "alert": '<path d="M12 3l9 16H3z"/><path d="M12 9v5M12 17h.01"/>',
    # ── 硬件部件（0.39.1 hwinfo B 档）：概览每行左侧的线性图标 ──
    # 同一规范：24×24 / fill=none / stroke-width 1.8 / 圆头圆角，Fluent·Lucide 风格自绘可商用。
    "os": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M6.5 6.5h.01M9.5 6.5h.01"/>',
    "cpu": '<rect x="6" y="6" width="12" height="12" rx="1"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
    "gpu": '<rect x="2" y="6" width="15" height="12" rx="2"/><circle cx="8" cy="12" r="3"/><path d="M17 9h5v6h-5M13 10h2"/>',
    "ram": '<rect x="2" y="7" width="20" height="9" rx="1"/><path d="M5 16v3M9 16v3M13 16v3M17 16v3M5 10h3M10 10h3M15 10h3"/>',
    "motherboard": '<rect x="3" y="3" width="18" height="18" rx="2"/><rect x="6" y="6" width="6" height="6" rx="1"/><path d="M16 6h2M16 9h2M6 15h4M13 13h5v5h-5z"/>',
    "disk": '<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M3 14h18"/><circle cx="17" cy="10" r="1"/>',
}

# 系统应用：中文名 → 图标键（search_index._build_system_apps 的 24 条一一对应）
_SYSTEM_BY_NAME: dict[str, str] = {
    "命令提示符": "terminal", "设置": "settings", "卸载或更改程序": "package",
    "记事本": "note", "计算器": "calc", "任务管理器": "activity",
    "文件资源管理器": "folder", "控制面板": "sliders", "注册表编辑器": "tree",
    "画图": "brush", "主页": "home", "系统": "monitor", "蓝牙": "bluetooth",
    "网络": "wifi", "更新": "refresh", "个性化": "palette", "显示": "monitor",
    "声音": "volume", "电源和睡眠": "power", "账户": "user", "应用": "grid",
    "存储": "database", "时间与语言": "clock", "隐私和安全性": "shield",
    "关于": "info",
}
# ms-settings: URI 段兜底（名字没命中时用）
_SYSTEM_BY_URI: dict[str, str] = {
    "bluetooth": "bluetooth", "network": "wifi", "windowsupdate": "refresh",
    "personalization": "palette", "display": "monitor", "sound": "volume",
    "powersleep": "power", "accounts": "user", "appsfeatures": "grid",
    "storage": "database", "dateandtime": "clock", "privacy": "shield",
    "about": "info", "system": "monitor",
}
# 粘贴动作 verb → 图标键（ocr / doc / chat 直接复用插件那套）
_ACTION_BY_VERB: dict[str, str] = {
    "ocr": "ocr", "doc_recognize": "doc", "llm_chat": "chat",
    "open": "open", "reveal": "locate", "open_settings": "settings",
    "install_doc": "alert",
}


def plugin_icon_svg(plugin_id: str) -> str:
    """插件 id → 线性 SVG path 片段（宽松包含匹配，未命中给 sparkle 兜底）。"""
    pid = (plugin_id or "").lower()
    for key, paths in _PATHS.items():
        if key in pid:
            return paths
    return _DEFAULT


def _key_to_svg(key: str) -> str:
    """图标键 → path：先查通用库，再复用插件库，最后 sparkle 兜底。"""
    return _GENERIC.get(key) or _PATHS.get(key) or _DEFAULT


def generic_icon_svg(key: str) -> str:
    """按语义键取线性 SVG path（公开入口，供插件内部按部件名取图标）。

    例如 hwinfo 概览的 cpu / gpu / ram / motherboard / disk / monitor / os。
    配合 render_plugin_svg(paths, size, dpr) 渲染；未命中键给 sparkle 兜底。
    """
    return _key_to_svg(key)


def system_icon_svg(name: str, path: str = "") -> str:
    """系统应用 → 线性 SVG path（按中文名，再按 ms-settings URI 段兜底）。

    这样首页分组、搜索结果、「全部 >」分类页里的系统应用都是同一套线性图标，
    不再混用 Windows 的彩色 exe 图标。
    """
    key = _SYSTEM_BY_NAME.get(name or "")
    if not key and (path or "").startswith("ms-settings:"):
        seg = (path or "").split(":", 1)[-1].strip("/").lower()
        for frag, k in _SYSTEM_BY_URI.items():
            if frag in seg:
                key = k
                break
    return _key_to_svg(key or "settings")


def action_icon_svg(verb: str) -> str:
    """粘贴动作 verb → 线性 SVG path（ocr/doc/chat 直接复用插件图标）。"""
    return _key_to_svg(_ACTION_BY_VERB.get(verb or "", "info"))


def _svg_document(paths: str) -> str:
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            f'stroke="{_PLUGIN_FG}" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round">' + paths + "</svg>")


def render_plugin_svg(paths: str, size: int, dpr: float = 1.0) -> QPixmap:
    """线性 SVG → 透明底 QPixmap（给足物理像素再设 devicePixelRatio，避免发虚）。"""
    px = max(1, int(round(size * (dpr or 1.0))))
    renderer = QSvgRenderer(QByteArray(_svg_document(paths).encode("utf-8")))
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    renderer.render(p)
    p.end()
    pm.setDevicePixelRatio(dpr or 1.0)
    return pm
