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

VERSION = "1.2.2"  # LyLaunch 版本号

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


# 单个模型的上下文窗口（--ctx-size，也同步给 dsh 的 contextWindow）。
# 8K 跑不动 dsh：它的系统提示 + 工具描述约 9.4K tokens，光提示就超了。
# 档位偏保守：9B 模型 16K 约 7.5GB 显存，再往上 KV cache 是翻倍的。
ENGINE_CONTEXT_STEPS = (8192, 12288, 16384, 24576, 32768, 49152, 65536)
DEFAULT_ENGINE_CONTEXT = 16384
ENGINE_CONTEXT_MIN = ENGINE_CONTEXT_STEPS[0]
ENGINE_CONTEXT_MAX = ENGINE_CONTEXT_STEPS[-1]


def engine_context_index(value: int | None = None) -> int:
    """把上下文值夹到最近的档位上，返回档位序号（给滑块的 value 用）。"""
    v = DEFAULT_ENGINE_CONTEXT if value is None else int(value)
    best = min(range(len(ENGINE_CONTEXT_STEPS)),
               key=lambda i: abs(ENGINE_CONTEXT_STEPS[i] - v))
    return best


def engine_context_value(index: int) -> int:
    """档位序号 → 实际上下文长度。"""
    i = max(0, min(len(ENGINE_CONTEXT_STEPS) - 1, int(index)))
    return ENGINE_CONTEXT_STEPS[i]


def load_engine_context() -> int:
    """读上下文窗口；没设置过或脏值，一律按默认档。"""
    raw = load_engine_cfg().get("ctx_size")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_ENGINE_CONTEXT
    i = engine_context_index(v)
    return ENGINE_CONTEXT_STEPS[i]


def save_engine_context(value: int) -> None:
    save_engine_cfg({"ctx_size": engine_context_value(engine_context_index(value))})


# 外部引擎（给 dsh 这类外部工具用的第二套 Router，端口 18091）。
# 默认关闭：平时只有插件那套 18090 在跑。打开后插件那一套的模型会被卸载并暂用
# 不了（显存要让给外部引擎），所以必须显式开启、写盘记状态。
DEFAULT_EXT_ENGINE = False


def load_ext_engine() -> bool:
    return bool(load_engine_cfg().get("ext_engine", DEFAULT_EXT_ENGINE))


def save_ext_engine(on: bool) -> None:
    save_engine_cfg({"ext_engine": bool(on)})



# ---------- 首页瓦片手动顺序（应用 / 系统应用） ----------
def load_tile_order(group: str) -> list[str]:
    """某个首页区块被拖出来的顺序（存 path 列表）；没排过返回空表。"""
    data = _read_json(paths.TILE_ORDER_FILE)
    if not isinstance(data, dict):
        return []
    value = data.get(group)
    return [str(x) for x in value] if isinstance(value, list) else []


def save_tile_order(group: str, ordered_paths: list[str]) -> None:
    """只覆盖传入的那个区块，另一区块的顺序原样保留。"""
    data = _read_json(paths.TILE_ORDER_FILE)
    if not isinstance(data, dict):
        data = {}
    data[group] = [str(p) for p in ordered_paths]
    _write_json(paths.TILE_ORDER_FILE, data)


# ---------- 窗口关闭行为（config.json） ----------
# 老用户没有 config.json → 读默认 quit，行为与以前完全一致，不改变既有习惯。
CLOSE_ACTIONS = ("quit", "tray")
CLOSE_ACTION_LABELS = {"quit": "退出程序", "tray": "最小化到托盘"}
DEFAULT_CLOSE_ACTION = "quit"


def load_close_action() -> str:
    """关闭主面板时的行为：quit=彻底退出 / tray=最小化到托盘。"""
    data = _read_json(paths.CONFIG_FILE)
    value = str((data or {}).get("CloseAction", DEFAULT_CLOSE_ACTION)).lower()
    return value if value in CLOSE_ACTIONS else DEFAULT_CLOSE_ACTION


def save_close_action(value: str) -> None:
    v = str(value).lower()
    if v not in CLOSE_ACTIONS:
        return
    data = _read_json(paths.CONFIG_FILE)
    if not isinstance(data, dict):
        data = {}
    data["CloseAction"] = v
    data.setdefault("Version", VERSION)
    _write_json(paths.CONFIG_FILE, data)


# ---------- 主窗口皮肤（config.json） ----------
# solid = 0.36 起的白→淡蓝渐变（默认，老用户不受影响）
# glass = 系统 Acrylic 毛玻璃（跟随壁纸模糊，需要系统开启透明效果）
WIN_SKINS = ("solid", "glass")
WIN_SKIN_LABELS = {"solid": "原版", "glass": "毛玻璃"}
DEFAULT_WIN_SKIN = "solid"


def load_win_skin() -> str:
    """主窗口皮肤：solid=原版渐变 / glass=Acrylic 毛玻璃。"""
    data = _read_json(paths.CONFIG_FILE)
    value = str((data or {}).get("WinSkin", DEFAULT_WIN_SKIN)).lower()
    return value if value in WIN_SKINS else DEFAULT_WIN_SKIN


def save_win_skin(value: str) -> None:
    v = str(value).lower()
    if v not in WIN_SKINS:
        return
    data = _read_json(paths.CONFIG_FILE)
    if not isinstance(data, dict):
        data = {}
    data["WinSkin"] = v
    data.setdefault("Version", VERSION)
    _write_json(paths.CONFIG_FILE, data)


# ---------- 常驻本地引擎（config.json） ----------
# 开给「外部工具」（目前是 dsh）用：dsh 直接打 127.0.0.1:18090，
# 引擎不在就没有服务。常驻的只是 Router 空壳（模型仍按需加载，平时不占显存）。
DEFAULT_KEEP_ENGINE = False


def load_keep_engine() -> bool:
    data = _read_json(paths.CONFIG_FILE)
    return bool((data or {}).get("KeepEngine", DEFAULT_KEEP_ENGINE))


def save_keep_engine(value: bool) -> None:
    data = _read_json(paths.CONFIG_FILE)
    if not isinstance(data, dict):
        data = {}
    data["KeepEngine"] = bool(value)
    data.setdefault("Version", VERSION)
    _write_json(paths.CONFIG_FILE, data)
