"""JSON 持久化：热键 / 自定义扫描路径 / 收藏 / 最近使用。

C# 里是分散的 hotkey.json / recents.json / pinned.json / custompaths.json，
这里用统一的小工具函数读写，并对损坏/缺失做容错（读不到就返回默认）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import paths

VERSION = "1.1.0"  # LyLaunch 版本号

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

# 出厂默认全局唤起热键：Alt+空格（0.39.1 起，原为 Alt+R）
# 0x20 = VK_SPACE。display 与 hotkey.format_hotkey() 的输出保持一致（Alt+Space），
# 跟 Alt+A 等其它组合的英文风格统一；设置页捕获到空格后也是这个文案。
DEFAULT_HOTKEY_MODS = MOD_ALT
DEFAULT_HOTKEY_VK = 0x20
DEFAULT_HOTKEY_DISPLAY = "Alt+Space"


@dataclass
class HotkeyConfig:
    modifiers: int = DEFAULT_HOTKEY_MODS   # fsModifiers 位组合
    vk: int = DEFAULT_HOTKEY_VK            # VK_SPACE
    display: str = DEFAULT_HOTKEY_DISPLAY

    def to_dict(self) -> dict:
        return {"Modifiers": self.modifiers, "Vk": self.vk, "Display": self.display}

    @classmethod
    def from_dict(cls, d: dict) -> "HotkeyConfig":
        return cls(
            modifiers=int(d.get("Modifiers", DEFAULT_HOTKEY_MODS)),
            vk=int(d.get("Vk", DEFAULT_HOTKEY_VK)),
            display=str(d.get("Display", DEFAULT_HOTKEY_DISPLAY)),
        )


def _read_json(path: Path):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _write_json(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------- 热键 ----------
def load_hotkey() -> HotkeyConfig:
    # 只有「从没保存过热键」的用户才吃到出厂默认值；已经存过 hotkey.json 的
    # 老用户保持自己选的组合，不因为默认改了就被悄悄覆盖。
    d = _read_json(paths.HOTKEY_FILE)
    if isinstance(d, dict) and d.get("Vk") and d.get("Modifiers"):
        return HotkeyConfig.from_dict(d)
    return HotkeyConfig()


def save_hotkey(cfg: HotkeyConfig) -> None:
    _write_json(paths.HOTKEY_FILE, cfg.to_dict())


# ---------- 截图快捷键 ----------
# 出厂默认 Alt+A（0.39.5 起，原为 Ctrl+Alt+S）：三个键按起来别扭，Alt+A 单手就能到。
# 0x41 = VK_A；display 与 hotkey.format_hotkey() 输出一致。
DEFAULT_SCREENSHOT_HOTKEY = HotkeyConfig(
    modifiers=MOD_ALT, vk=0x41, display="Alt+A")


def load_screenshot_hotkey() -> HotkeyConfig:
    d = _read_json(paths.SCREENSHOT_HOTKEY_FILE)
    if isinstance(d, dict) and d.get("Vk") and d.get("Modifiers"):
        return HotkeyConfig.from_dict(d)
    return DEFAULT_SCREENSHOT_HOTKEY


def save_screenshot_hotkey(cfg: HotkeyConfig) -> None:
    _write_json(paths.SCREENSHOT_HOTKEY_FILE, cfg.to_dict())


# ---------- 收藏（固定）----------
@dataclass
class PinnedEntry:
    path: str
    name: str
    launch: Optional[str] = None
    args: Optional[str] = None


def load_pinned() -> list[PinnedEntry]:
    d = _read_json(paths.PINNED_FILE)
    result = []
    if isinstance(d, list):
        for e in d:
            p = (e or {}).get("Path", "")
            # 不再因为路径暂时不存在就丢弃：文件可能只是被挪进了桌面上的收纳文件夹，
            # 真实路径由主窗口按名字在索引里重新解析（见 main_window 收藏段）。
            if p:
                result.append(PinnedEntry(
                    path=p,
                    name=str(e.get("Name", Path(p).stem)),
                    launch=e.get("Launch"),
                    args=e.get("Args"),
                ))
    return result


def save_pinned(entries: list[PinnedEntry]) -> None:
    _write_json(paths.PINNED_FILE, [
        {"Path": e.path, "Name": e.name, "Launch": e.launch, "Args": e.args}
        for e in entries
    ])


# ---------- 首页插件顺序（拖拽排序） ----------
def load_plugin_order() -> list[str]:
    """读首页插件的自定义顺序（插件 id 列表）；没有/损坏返回空列表。"""
    d = _read_json(paths.PLUGIN_ORDER_FILE)
    if isinstance(d, list):
        return [str(x) for x in d if isinstance(x, str) and x]
    if isinstance(d, dict):          # 兼容以后可能改成 {"Order": [...]}
        order = d.get("Order")
        if isinstance(order, list):
            return [str(x) for x in order if isinstance(x, str) and x]
    return []


def save_plugin_order(ids: list[str]) -> None:
    _write_json(paths.PLUGIN_ORDER_FILE, list(ids))


# ---------- 最近使用 ----------
@dataclass
class RecentEntry:
    path: str
    name: str
    launch: Optional[str] = None
    args: Optional[str] = None
    last_launch: int = 0   # Unix 毫秒


def load_recents() -> list[RecentEntry]:
    d = _read_json(paths.RECENTS_FILE)
    result = []
    if isinstance(d, list):
        for e in d:
            p = (e or {}).get("Path", "")
            if p and Path(p).exists():
                result.append(RecentEntry(
                    path=p,
                    name=str(e.get("Name", Path(p).stem)),
                    launch=e.get("Launch"),
                    args=e.get("Args"),
                    last_launch=int(e.get("LastLaunch", 0)),
                ))
    result.sort(key=lambda r: r.last_launch, reverse=True)
    return result


def save_recents(entries: list[RecentEntry]) -> None:
    _write_json(paths.RECENTS_FILE, [
        {"Path": r.path, "Name": r.name, "Launch": r.launch,
         "Args": r.args, "LastLaunch": r.last_launch}
        for r in entries
    ])


# ---------- 本地引擎（llama.cpp Router）----------
# 同时驻留几个模型（--models-max）。键存 engine.json，和模型/运行时放一起。
#   "auto" = 按显存自动（≥8GB 放 2 个，否则 1 个）
#   "1"    = 只驻留一个（低显存 / 核显最稳，切模型要重新加载）
#   "2"    = 允许两个同时驻留（切模型不用等，但显存吃紧时可能爆）
ENGINE_MODELS_MAX_CHOICES = ("auto", "1", "2")
DEFAULT_ENGINE_MODELS_MAX = "auto"


def load_engine_cfg() -> dict:
    d = _read_json(paths.ENGINE_CFG_FILE)
    return d if isinstance(d, dict) else {}


def save_engine_cfg(data: dict) -> None:
    old = load_engine_cfg()
    old.update(data or {})
    _write_json(paths.ENGINE_CFG_FILE, old)


def load_models_max() -> str:
    """读"同时驻留几个模型"的设置，脏值一律回退 auto。"""
    v = str(load_engine_cfg().get("models_max", DEFAULT_ENGINE_MODELS_MAX)).lower()
    return v if v in ENGINE_MODELS_MAX_CHOICES else DEFAULT_ENGINE_MODELS_MAX


def save_models_max(value: str) -> None:
    v = str(value).lower()
    save_engine_cfg({"models_max": v if v in ENGINE_MODELS_MAX_CHOICES
                     else DEFAULT_ENGINE_MODELS_MAX})
