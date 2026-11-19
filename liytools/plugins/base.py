"""插件基类 + 插件运行上下文（插件作者只需要继承 PluginBase）。

生命周期（由 PluginManager 驱动）：
    加载  → PluginBase.__init__(ctx) → on_load()
    卸载  → on_unload()
    交互  → on_keyword(keyword, query)   关键词命中搜索结果时（后续实现搜索接入）
          → widget()                      返回插件的 Qt 界面（后续实现页面切换）
          → on_execute(query)             用户在首页/结果里点中插件时执行
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget
    from .api import PluginAPI
    from .config import PluginManifest


class PluginContext:
    """插件运行时的"环境"：插件自己是谁、能碰哪些东西。

    - manifest    本插件的清单（plugin.json 解析结果）
    - plugin_dir  本插件目录（放资源就 relative 到这个目录）
    - api         系统能力入口（剪贴板 / 配置 / 启动应用等）
    - settings    插件自己的持久化配置（dict，由 api 读写）
    """

    def __init__(self, manifest: "PluginManifest", plugin_dir: str, api: "PluginAPI") -> None:
        self.manifest = manifest
        self.plugin_dir = plugin_dir
        self.api = api
        self.settings: dict[str, Any] = dict(manifest.config)

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def name(self) -> str:
        return self.manifest.name

    def resource(self, *parts: str) -> str:
        """拼插件目录下的资源文件绝对路径（如 ctx.resource("icon.png")）。"""
        import os
        return os.path.join(self.plugin_dir, *parts)


class PluginBase:
    """所有插件必须继承的基类。子类按需实现钩子方法。"""

    # 类型标注：子类里能直接看到 ctx.api 有哪些能力
    ctx: PluginContext

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx

    # ---------- 生命周期 ----------
    def on_load(self) -> None:
        """插件被加载后调用一次。适合做初始化（读配置、建临时目录等）。"""

    def on_unload(self) -> None:
        """插件被卸载/禁用/退出前调用一次。适合释放资源、保存状态。"""

    # ---------- 交互 ----------
    def on_keyword(self, keyword: str, query: str) -> list:
        """搜索命中本插件关键词时调用。

        返回可显示的条目列表（list[dict]），每项：
            {"title": 主标题, "desc": 副标题, "data": 任意数据}
        后续开发"插件搜索/接管搜索结果"时实现，现在先定义契约。
        """
        return []

    def on_execute(self, query: str = "") -> None:
        """用户选中/点中本插件时执行。query 是当前搜索框里的文字。"""

    def widget(self) -> "QWidget | None":
        """返回插件的 Qt 界面；None 表示插件没有常驻界面（纯命令式）。"""
        return None
