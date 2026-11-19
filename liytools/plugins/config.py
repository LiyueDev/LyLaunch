"""插件系统的配置模型（三种配置文件的 dataclass 定义）。

配置文件体系（这是插件开发的"契约"，后续按这些字段实现功能）：

1. `plugin_config.json`（全局插件系统配置，放 %LocalAppData%/LyLaunch/）
   —— 插件系统的总开关、安全策略、扫描位置，改它控制整个插件体系。

2. `plugins_installed.json`（已安装插件注册表，放 %LocalAppData%/LyLaunch/）
   —— 记录"装过哪些插件、启没启用、装在哪"，软件内卸载靠它。

3. `<插件目录>/plugin.json`（单个插件清单）
   —— 每个插件自己的身份证：名字、入口、关键词、权限。

字段命名沿用工程习惯：大写开头（与 config.py / C# 序列化风格一致）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ============ ③ 单个插件清单 plugin.json ============
def _pick(d: dict, *names, default=None):
    """按多个候选键名取第一个非空值。

    历史包袱：老 C# 版写 PascalCase（Id / Version / Enabled），Python 版的
    ``to_dict()`` 用 ``asdict`` 写 snake_case（id / version / enabled）。
    只读大写就永远读不回自己写的数据 —— 插件禁用状态因此一直不生效
    （重启就复活）。这里两种都认。
    """
    for n in names:
        if isinstance(d, dict) and n in d and d[n] is not None:
            return d[n]
    return default


@dataclass
class PluginManifest:
    """一个插件的清单（对应插件目录里的 plugin.json）。

    约定：插件目录名 = 插件 Id，两者必须一致，防止同名冲突。
    """
    id: str = ""                          # 唯一标识（= 目录名，小写 + 下划线，如 "hello_plugin"）
    name: str = ""                        # 显示名称（瓦片上看到的名字）
    version: str = "0.1.0"                # 版本号，软件内检测更新用
    author: str = ""                      # 作者
    description: str = ""                 # 一句话介绍（设置页/商店列表显示）
    icon: str = ""                        # 图标文件名（相对插件目录），空则用首字母瓦片
    main: str = "main.py"                 # 入口脚本（相对插件目录）
    entry_class: str = "Plugin"           # 入口脚本里继承 PluginBase 的类名
    keywords: list[str] = field(default_factory=list)   # 触发关键词（中文/英文/拼音别名，进搜索索引）
    permissions: list[str] = field(default_factory=list)  # 申请的系统能力：clipboard / notification / config ...
    hotkey: str = ""                      # 全局快捷键（如 "Ctrl+L"），空 = 不注册
    config: dict[str, Any] = field(default_factory=dict)  # 插件自定义配置（默认值，运行期可改）
    data_dirs: list[str] = field(default_factory=list)    # 卸载时可清理的数据目录（相对 %LocalAppData%/LyLaunch）

    @classmethod
    def from_dict(cls, d: dict) -> "PluginManifest":
        return cls(
            id=str(d.get("Id", "")).strip(),
            name=str(d.get("Name", "")).strip(),
            version=str(d.get("Version", "0.1.0")),
            author=str(d.get("Author", "")),
            description=str(d.get("Description", "")),
            icon=str(d.get("Icon", "")),
            main=str(d.get("Main", "main.py")),
            entry_class=str(d.get("EntryClass", "Plugin")),
            keywords=[str(k) for k in (d.get("Keywords") or [])],
            permissions=[str(p) for p in (d.get("Permissions") or [])],
            hotkey=str(d.get("Hotkey", "")).strip(),
            config=dict(d.get("Config") or {}),
            data_dirs=[str(x) for x in (d.get("Data") or [])],
        )

    def to_dict(self) -> dict:
        return asdict(self)


# ============ ② 已安装插件注册表 plugins_installed.json ============
@dataclass
class InstalledPlugin:
    """已安装插件的一条注册记录（软件内卸载/启用禁用都改这个）。"""
    id: str = ""                          # 插件 Id（对应 manifest.id）
    version: str = ""                     # 安装时的版本（装完后固定，判断有无新版本）
    enabled: bool = True                  # 是否启用（禁用后不加载、不出现在首页）
    source: str = "user"                  # local=内置（随程序带） / user=用户安装
    path: str = ""                        # 插件目录绝对路径
    installed_at: str = ""                # 安装时间（可读字符串，给人看的）

    @classmethod
    def from_dict(cls, d: dict) -> "InstalledPlugin":
        return cls(
            id=str(_pick(d, "id", "Id", default="")),
            version=str(_pick(d, "version", "Version", default="")),
            enabled=bool(_pick(d, "enabled", "Enabled", default=True)),
            source=str(_pick(d, "source", "Source", default="user")),
            path=str(_pick(d, "path", "Path", default="")),
            installed_at=str(_pick(d, "installed_at", "InstalledAt", default="")),
        )

    def to_dict(self) -> dict:
        return asdict(self)


# ============ ① 全局插件系统配置 plugin_config.json ============
@dataclass
class PluginSystemConfig:
    """插件系统全局配置（对应 plugin_config.json）。

    字段含义：
    - version         本配置文件的格式版本（以后字段增删靠它迁移）
    - enabled         插件系统总开关（False 时完全不加载任何插件）
    - auto_load       启动时是否自动加载已启用插件（False 则只扫描不加载）
    - allow_external  是否允许安装外部插件（安全开关，个人自用默认 True）
    - scan_paths      额外扫描目录：除内置目录和用户目录外，再从这些目录找插件
    - disabled_plugins 手动禁用的插件 Id 列表（与 InstalledPlugin.enabled 二选一，
                     这里适合"暂不卸载但不想用"的插件）
    - uninstalled_plugins 用户主动卸载过的插件 Id（启动时默认全装要跳过它们）
    - max_plugins     插件数量上限，防止恶意/误装导致启动变慢
    - hotkey_scope    是否允许插件注册全局热键（后续能力，先留开关）
    - network_allowed 是否允许插件发起网络请求（后续能力，先留开关）
    """
    version: str = "0.1.0"
    enabled: bool = True
    auto_load: bool = True
    allow_external: bool = True
    scan_paths: list[str] = field(default_factory=list)
    disabled_plugins: list[str] = field(default_factory=list)
    # 用户**主动卸载**过的插件 Id：启动时「默认全装包库插件」要跳过它们，
    # 否则卸载完下次启动又装回来。重置数据后本文件被删，名单自然清空。
    uninstalled_plugins: list[str] = field(default_factory=list)
    max_plugins: int = 50
    hotkey_scope: bool = False
    network_allowed: bool = False

    @classmethod
    def default(cls) -> "PluginSystemConfig":
        return cls()

    @classmethod
    def from_dict(cls, d: dict) -> "PluginSystemConfig":
        # 未知字段直接忽略，缺的字段用默认值 → 配置文件损坏/升级都不崩
        return cls(
            version=str(_pick(d, "version", "Version", default="0.1.0")),
            enabled=bool(_pick(d, "enabled", "Enabled", default=True)),
            auto_load=bool(_pick(d, "auto_load", "AutoLoad", default=True)),
            allow_external=bool(_pick(d, "allow_external", "AllowExternal", default=True)),
            scan_paths=[str(x) for x in (_pick(d, "scan_paths", "ScanPaths", default=[]) or [])],
            disabled_plugins=[str(x) for x in
                              (_pick(d, "disabled_plugins", "DisabledPlugins", default=[]) or [])],
            uninstalled_plugins=[str(x) for x in
                                 (_pick(d, "uninstalled_plugins", "UninstalledPlugins",
                                        default=[]) or [])],
            max_plugins=int(_pick(d, "max_plugins", "MaxPlugins", default=50) or 50),
            hotkey_scope=bool(_pick(d, "hotkey_scope", "HotkeyScope", default=False)),
            network_allowed=bool(_pick(d, "network_allowed", "NetworkAllowed", default=False)),
        )

    def to_dict(self) -> dict:
        return asdict(self)
