"""插件配置文件的读写（plugin_config.json + plugins_installed.json）。

只负责"读出来 / 写回去"，字段模型见 config.py。
损坏 / 缺失的配置文件一律静默回退默认值——插件系统坏了不能拖垮主程序。

后续开发提示：
- 加"插件市场"时，从这里读已安装列表做"已安装/可更新"比对。
- 加"插件设置页"时，用 upsert_installed 改 enabled 即可控制启用/禁用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .. import paths
from .config import PluginSystemConfig, InstalledPlugin


# ---------- plugin_config.json ----------
def load_system_config() -> PluginSystemConfig:
    """读全局插件系统配置；文件缺失/损坏时返回默认配置。"""
    try:
        if paths.PLUGIN_CONFIG_FILE.exists():
            with open(paths.PLUGIN_CONFIG_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return PluginSystemConfig.from_dict(d)
    except Exception:
        pass
    return PluginSystemConfig.default()


def save_system_config(cfg: PluginSystemConfig) -> None:
    try:
        paths.PLUGIN_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(paths.PLUGIN_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg.to_dict(), f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------- plugins_installed.json ----------
def load_installed() -> list[InstalledPlugin]:
    """读已安装插件注册表；缺失/损坏返回空列表。

    防御：过滤掉没有 Id 的脏记录（防止异常写入污染判断）。
    """
    try:
        if paths.PLUGIN_INSTALLED_FILE.exists():
            with open(paths.PLUGIN_INSTALLED_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("Plugins"), list):
                result = []
                for e in d["Plugins"]:
                    if not isinstance(e, dict):
                        continue
                    # 键名兼容：asdict 写的是小写 id，旧数据是 Id
                    if not (e.get("id") or e.get("Id")):
                        continue
                    result.append(InstalledPlugin.from_dict(e))
                return result
    except Exception:
        pass
    return []


def save_installed(plugins: list[InstalledPlugin]) -> None:
    try:
        paths.PLUGIN_INSTALLED_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(paths.PLUGIN_INSTALLED_FILE, "w", encoding="utf-8") as f:
            json.dump({"Plugins": [p.to_dict() for p in plugins]},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_installed(plugin_id: str) -> Optional[InstalledPlugin]:
    """按 id 查注册表记录，没有返回 None。"""
    for p in load_installed():
        if p.id == plugin_id:
            return p
    return None


def upsert_installed(plugin: InstalledPlugin) -> None:
    """新增或更新一条安装记录（id 相同则覆盖）。"""
    plugins = load_installed()
    plugins = [p for p in plugins if p.id != plugin.id]
    plugins.append(plugin)
    save_installed(plugins)


def remove_installed(plugin_id: str) -> None:
    plugins = [p for p in load_installed() if p.id != plugin_id]
    save_installed(plugins)
