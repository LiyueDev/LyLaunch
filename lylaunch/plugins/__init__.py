"""LyLaunch 插件系统。

设计目标：与基础功能（搜索 / 剪贴板 / 启动）**完全隔离**。

- 插件 = 一个独立目录：`<id>/plugin.json`（清单）+ `<id>/main.py`（代码）
- 插件存放位置：
  - 内置插件：随程序分发（开发期取仓库 `plugins/`，打包后取 exe 内资源）
  - 用户插件：`%LocalAppData%/LyLaunch/plugins/`（软件内安装/卸载都发生在这）
- 插件通过 `PluginBase` 基类接入，用 `PluginAPI` 调系统能力；
  `PluginManager` 负责扫描 / 加载 / 启用 / 禁用 / 卸载。
- 所有配置集中在 `lylaunch/plugins/config.py`（模型）与
  `plugin_config.json` / `plugins_installed.json`（持久化），后续开发按这些配置走。

对外入口：
    from lylaunch.plugins import manager
    mgr = manager.PluginManager.instance()
"""

from .config import PluginSystemConfig, PluginManifest, InstalledPlugin  # noqa: F401
from .base import PluginBase, PluginContext  # noqa: F401
from .manager import PluginManager  # noqa: F401
