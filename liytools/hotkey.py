"""全局热键（对应 C# 的 RegisterHotKey + HwndSource Hook）。

用 ctypes 直接调 user32.RegisterHotKey，参数是窗口句柄(HWND)。
收到 WM_HOTKEY 由主窗口重写 nativeEvent 处理（见 ui/main_window.py）。

另外提供"从原生按键消息里取 (修饰键, 虚拟键码)"的工具（设置页捕获热键用）：
Qt 的 keyPressEvent 拿不到某些组合（Alt+空格 会被 Windows 当成"打开窗口系统菜单"），
而且 Qt 的 key 值对 F1-F12 / 符号键不等于 Win32 虚拟键码，必须从 MSG.wParam 取真实 VK。
"""

from __future__ import annotations

import ctypes
from ctypes import c_int
from ctypes.wintypes import HWND, UINT, BOOL

from . import config

user32 = ctypes.windll.user32
user32.RegisterHotKey.argtypes = [HWND, c_int, UINT, UINT]
user32.RegisterHotKey.restype = BOOL
user32.UnregisterHotKey.argtypes = [HWND, c_int]
user32.UnregisterHotKey.restype = BOOL
user32.GetAsyncKeyState.argtypes = [c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

HOTKEY_ID = 9000              # 全局唤起
SCREENSHOT_HOTKEY_ID = 9001   # 全局截图
PLUGIN_HOTKEY_BASE = 9100     # 插件热键从 9100 起（按插件清单的 Hotkey 字段注册）

# 原生键盘消息（捕获热键时要处理"系统键"，Alt 组合走的是 SYSKEY 系列）
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_KEYUP = 0x0101
WM_SYSKEYUP = 0x0105

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12                # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_ESCAPE = 0x1B

# 纯修饰键：捕获时要忽略（只按 Ctrl/Alt 不算一个热键）
_MODIFIER_VKS = {
    0x10, 0x11, 0x12,        # Shift / Ctrl / Alt
    0xA0, 0xA1,              # LShift / RShift
    0xA2, 0xA3,              # LCtrl / RCtrl
    0xA4, 0xA5,              # LAlt / RAlt
    VK_LWIN, VK_RWIN,
}


def register(hwnd: int, mods: int, vk: int, hotkey_id: int = HOTKEY_ID) -> bool:
    return bool(user32.RegisterHotKey(HWND(hwnd), hotkey_id, UINT(mods), UINT(vk)))


def unregister(hwnd: int, hotkey_id: int = HOTKEY_ID) -> None:
    user32.UnregisterHotKey(HWND(hwnd), hotkey_id)


def _is_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def is_modifier_vk(vk: int) -> bool:
    return vk in _MODIFIER_VKS


def pressed_modifiers(context_alt: bool = False) -> int:
    """取当前按住的修饰键位组合（RegisterHotKey 的 fsModifiers 格式）。

    `context_alt`：MSG.lParam 的 bit29「上下文代码」——Alt 组合键在系统键消息里
    会置这一位。真实按键时 GetAsyncKeyState 已经能读到 Alt，这一位只是保险
    （注入式/特殊键盘布局下异步键状态可能抓不到）。
    """
    mods = 0
    if _is_down(VK_CONTROL):
        mods |= config.MOD_CONTROL
    if _is_down(VK_SHIFT):
        mods |= config.MOD_SHIFT
    if _is_down(VK_MENU) or context_alt:
        mods |= config.MOD_ALT
    if _is_down(VK_LWIN) or _is_down(VK_RWIN):
        mods |= config.MOD_WIN
    return mods


# Qt 特殊键 -> Win32 虚拟键码（Qt 的 F 键/编辑键 key 值是 0x010000xx，直接当 VK 用会注册到不存在的键）
_QT_TO_VK = {
    0x01000000: 0x1B,   # Escape
    0x01000001: 0x09,   # Tab
    0x01000003: 0x08,   # Backspace
    0x01000004: 0x0D,   # Return
    0x01000005: 0x0D,   # Enter
    0x01000006: 0x2D,   # Insert
    0x01000007: 0x2E,   # Delete
    0x01000010: 0x24,   # Home
    0x01000011: 0x23,   # End
    0x01000012: 0x25,   # Left
    0x01000013: 0x26,   # Up
    0x01000014: 0x27,   # Right
    0x01000015: 0x28,   # Down
    0x01000016: 0x21,   # PageUp
    0x01000017: 0x22,   # PageDown
    0x01000020: 0x10,   # Shift
    0x01000021: 0x11,   # Control
    0x01000022: 0x5B,   # Meta -> Win 键
    0x01000023: 0x12,   # AltGr
    0x01000024: 0x2C,   # Print
    0x01000025: 0x13,   # Pause
}
# Qt 符号键（Qt 值 = ASCII）-> VK_OEM_* 区段
_QT_PUNCT_TO_VK = {
    0x3B: 0xBA,  # ;
    0x3D: 0xBB,  # =
    0x2C: 0xBC,  # ,
    0x2D: 0xBD,  # -
    0x2E: 0xBE,  # .
    0x2F: 0xBF,  # /
    0x60: 0xC0,  # `
    0x5B: 0xDB,  # [
    0x5C: 0xDC,  # \
    0x5D: 0xDD,  # ]
    0x27: 0xDE,  # '
}


def vk_from_qt_key(key: int) -> int:
    """Qt 的 event.key() -> Win32 虚拟键码；无法映射返回 0。

    只作为原生消息过滤器的兜底（正常路径直接读 MSG.wParam，天然是真 VK）。
    """
    if 0x01000030 <= key <= 0x0100003B:          # F1..F12
        return 0x70 + (key - 0x01000030)
    if key in _QT_TO_VK:
        return _QT_TO_VK[key]
    if key in _QT_PUNCT_TO_VK:
        return _QT_PUNCT_TO_VK[key]
    if 0x30 <= key <= 0x39 or 0x41 <= key <= 0x5A:   # 0-9 / A-Z
        return key
    if key == 0x20:                               # 空格
        return 0x20
    return 0


# 虚拟键码 -> 可读名（覆盖常见键，其余用 hex 兜底）
_VK_NAMES = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x10: "Shift", 0x11: "Ctrl",
    0x12: "Alt", 0x13: "Pause", 0x1B: "Esc", 0x20: "Space", 0x21: "PageUp",
    0x22: "PageDown", 0x23: "End", 0x24: "Home", 0x25: "←", 0x26: "↑",
    0x27: "→", 0x28: "↓", 0x2D: "Ins", 0x2E: "Del", 0x30: "0", 0x31: "1",
    0x32: "2", 0x33: "3", 0x34: "4", 0x35: "5", 0x36: "6", 0x37: "7", 0x38: "8",
    0x39: "9", 0x41: "A", 0x42: "B", 0x43: "C", 0x44: "D", 0x45: "E", 0x46: "F",
    0x47: "G", 0x48: "H", 0x49: "I", 0x4A: "J", 0x4B: "K", 0x4C: "L", 0x4D: "M",
    0x4E: "N", 0x4F: "O", 0x50: "P", 0x51: "Q", 0x52: "R", 0x53: "S", 0x54: "T",
    0x55: "U", 0x56: "V", 0x57: "W", 0x58: "X", 0x59: "Y", 0x5A: "Z",
    0x70: "F1", 0x71: "F2", 0x72: "F3", 0x73: "F4", 0x74: "F5", 0x75: "F6",
    0x76: "F7", 0x77: "F8", 0x78: "F9", 0x79: "F10", 0x7A: "F11", 0x7B: "F12",
    0xBA: ";", 0xBB: "+", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
    0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
}


def vk_name(vk: int) -> str:
    return _VK_NAMES.get(vk, f"0x{vk:02X}")


def format_hotkey(mods: int, vk: int) -> str:
    parts = []
    if mods & config.MOD_CONTROL:
        parts.append("Ctrl")
    if mods & config.MOD_ALT:
        parts.append("Alt")
    if mods & config.MOD_SHIFT:
        parts.append("Shift")
    if mods & config.MOD_WIN:
        parts.append("Win")
    parts.append(vk_name(vk))
    return "+".join(parts)


# 反查表：可读名 -> VK（给插件清单里的 "Ctrl+L" 这类字符串用）
_NAME_TO_VK: dict[str, int] = {}
for _vk, _name in _VK_NAMES.items():
    _NAME_TO_VK.setdefault(_name.casefold(), _vk)

_MOD_TOKENS = {
    "ctrl": config.MOD_CONTROL,
    "control": config.MOD_CONTROL,
    "alt": config.MOD_ALT,
    "shift": config.MOD_SHIFT,
    "win": config.MOD_WIN,
    "meta": config.MOD_WIN,
}


def parse_hotkey(text: str) -> tuple[int, int] | None:
    """把 "Ctrl+L" / "Alt+Space" / "Ctrl+Shift+F1" 解析成 (mods, vk)。

    解析失败（空串、出现不认识的键）返回 None —— 调用方据此跳过注册，
    不要静默注册成别的组合（那会让用户按了没反应还查不出原因）。
    """
    if not text or not isinstance(text, str):
        return None
    mods = 0
    vk = 0
    for raw in text.replace(" ", "").split("+"):
        token = raw.strip()
        if not token:
            continue
        lowered = token.casefold()
        if lowered in _MOD_TOKENS:
            mods |= _MOD_TOKENS[lowered]
            continue
        found = _NAME_TO_VK.get(lowered)
        if found is None and len(token) == 1 and token.isascii() and token.isalnum():
            found = ord(token.upper())
        if found is None:
            return None
        vk = found
    if not vk:
        return None
    return mods, vk
