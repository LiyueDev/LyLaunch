"""Windows 特殊目录 + 本程序数据目录（%LocalAppData%/LyLaunch）。

对应 C# 里 Environment.GetFolderPath(...) 与各 Json 文件路径。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from win32com.shell import shell, shellcon  # pywin32 提供的 Shell 特殊目录


def _known_folder(folder_id: str) -> str:
    """用 pywin32 取 Windows 已知文件夹（比 ctypes 拼 CSIDL 更稳）。"""
    try:
        return shell.SHGetKnownFolderPath(getattr(shellcon, folder_id))
    except Exception:
        return ""


def desktop() -> str:
    return _known_folder("FOLDERID_Desktop")


def common_desktop() -> str:
    return _known_folder("FOLDERID_PublicDesktop")


def start_menu() -> str:
    return _known_folder("FOLDERID_StartMenu")


def common_start_menu() -> str:
    return _known_folder("FOLDERID_CommonStartMenu")


def system32() -> str:
    return _known_folder("FOLDERID_System")


def windows_dir() -> str:
    return _known_folder("FOLDERID_Windows")


def local_app_data() -> str:
    return _known_folder("FOLDERID_LocalAppData")


# 本程序数据目录：%LocalAppData%/LyLaunch
DATA_DIR = Path(local_app_data()) / "LyLaunch"

# 一次性数据迁移（更名链：LiyTools → Lylaunch → LyLaunch）：
# 旧品牌目录里可能有收藏/最近使用/插件配置/离线翻译模型（约140MB）。
# 首次启动自动把能找到的旧目录整体搬到新目录，用户无感；失败也不阻塞启动。
_LEGACY_DATA_DIRS = [
    Path(local_app_data()) / "Lylaunch",  # 2026-10-23 白天的连写版
    Path(local_app_data()) / "LiyTools",  # 最初的旧名
]
for _legacy in _LEGACY_DATA_DIRS:
    if _legacy.exists() and not DATA_DIR.exists():
        try:
            shutil.move(str(_legacy), str(DATA_DIR))
        except Exception:
            pass
        break

DATA_DIR.mkdir(parents=True, exist_ok=True)

# ========== 全局推理引擎（llama.cpp Router）路径 ==========
# 与 lylaunch/engine/registry.py 的 engine_dir()/models_dir() 指向同一处；
# 这里集中定义，供主程序 / 打包 / 卸载向导使用。
ENGINE_DIR = DATA_DIR / "engine"
ENGINE_MODELS_DIR = ENGINE_DIR / "models"
ENGINE_RUNTIME_DIR = ENGINE_DIR / "runtime"      # 仅内置运行时缺失时下载兜底
ENGINE_PRESET_FILE = ENGINE_DIR / "models.ini"
ENGINE_LOG_FILE = ENGINE_DIR / "engine.log"
ENGINE_PID_FILE = ENGINE_DIR / "engine.pid"
ENGINE_CFG_FILE = DATA_DIR / "engine.json"


def resource_path(*parts: str) -> str:
    """解析资源文件路径，开发期与 PyInstaller 打包期都能用。

    - 打包后（frozen）：优先取 sys._MEIPASS 下的资源（需用
      --add-data "assets;assets" 把资源打进 exe）。
    - 开发期：取项目根目录（lylaunch 的上一级）。
    """
    import sys
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent.parent
    return str(base.joinpath(*parts))


def bundled_runtime_dir() -> str | None:
    """随包内置的 llama-server 运行时目录（打包后 sys._MEIPASS/runtime，开发期 ./runtime）。"""
    p = Path(resource_path("runtime"))
    return str(p) if (p / "llama-server.exe").is_file() else None

# 各类持久化文件路径（与 C# 版文件名保持一致，方便无缝衔接旧数据）
HOTKEY_FILE = DATA_DIR / "hotkey.json"
SCREENSHOT_HOTKEY_FILE = DATA_DIR / "screenshot_hotkey.json"
RECENTS_FILE = DATA_DIR / "recents.json"
PINNED_FILE = DATA_DIR / "pinned.json"
# 首页插件的自定义顺序（拖拽排序后写入的 id 列表；列表外的插件排在后面）
PLUGIN_ORDER_FILE = DATA_DIR / "plugin_order.json"
CONFIG_FILE = DATA_DIR / "config.json"   # 打包时记录版本等（可选）

# ========== 插件系统相关路径（与基础功能隔离） ==========
# 插件系统配置：所有插件相关全局配置集中在这一个文件里
PLUGIN_CONFIG_FILE = DATA_DIR / "plugin_config.json"
# 已安装插件注册表：记录安装过哪些插件、启用/禁用状态、安装来源
PLUGIN_INSTALLED_FILE = DATA_DIR / "plugins_installed.json"
# 用户插件安装目录：每个插件一个子目录（<id>/plugin.json + 代码），打包后也可写
USER_PLUGINS_DIR = DATA_DIR / "plugins"
# 内置插件目录：随程序发布（开发期取仓库 plugins/，打包后取 exe 内 sys._MEIPASS/plugins）
BUILTIN_PLUGINS_DIR = Path(resource_path("plugins"))
USER_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)

# 插件包库：存放可安装的 .zip（卸载插件后仍能从这里一键装回来）。
# 安装包可自带一批可选插件包（随 exe 放在 plugin_packages/），首次启动同步到这里。
PLUGIN_PACKAGES_DIR = DATA_DIR / "plugin_packages"
PLUGIN_PACKAGES_DIR.mkdir(parents=True, exist_ok=True)


def _sync_bundled_packages() -> None:
    """把随程序分发的可选插件包同步进用户库（只补缺，不覆盖用户自己换过的版本）。"""
    try:
        src = Path(resource_path("plugin_packages"))
        if not src.is_dir():
            # 开发模式没有 plugin_packages/：可选插件包放在 dist_plugins/
            # （PyInstaller 的 spec 收的就是它，打进包里时目录名才改成 plugin_packages）。
            # 不回退的话，开发模式跑过一次「重置」就再也装不回插件了 —— 包库是空的。
            alt = Path(resource_path("dist_plugins"))
            if alt.is_dir():
                src = alt
        if not src.is_dir():
            return
        for f in src.glob("*.zip"):
            dst = PLUGIN_PACKAGES_DIR / f.name
            if not dst.exists():
                shutil.copy2(f, dst)
    except Exception:
        pass


_sync_bundled_packages()
