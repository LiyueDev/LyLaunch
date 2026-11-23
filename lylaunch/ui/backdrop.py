"""Windows 背景材质：Acrylic 毛玻璃。

为什么单独成模块：Win32 调用只集中在这里，UI 侧只关心「开 / 关 / 是否成功」。

三条实测结论（2026-11-23，Win11 26300，125% 缩放）：
1. `DwmSetWindowAttribute(DWMWA_SYSTEMBACKDROP_TYPE)` 对**分层透明窗口**
   （`WA_TranslucentBackground` → WS_EX_LAYERED）无效，窗口会变成一块死灰；
   可用的只有 `SetWindowCompositionAttribute` 的 Acrylic。
2. Acrylic 会给**整个窗口矩形**加模糊（含为了投影留出的空白边），
   所以毛玻璃皮肤把窗口缩到面板大小（pad=0），投影另开一个伴生窗口画。
3. DWM 的圆角裁剪（DWMWA_WINDOW_CORNER_PREFERENCE=ROUND）也会裁掉模糊区，
   窗口边缘才不会露出一圈方角磨砂。

调用时机：必须在 show() 之后（原生 HWND 稳定后再改合成属性）。
系统关闭「透明效果」时不启用，由调用方保留原来的不透明渐变。
"""
from __future__ import annotations

import ctypes
import sys


def _dbg_print(*args, **kwargs):
    """调试输出已停用；保留调用以维持语句结构。"""
    pass


ACCENT_DISABLED = 0
ACCENT_ENABLE_BLURBEHIND = 3
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
WCA_ACCENT_POLICY = 19


class _AccentPolicy(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_uint),
        ("AccentFlags", ctypes.c_uint),
        ("GradientColor", ctypes.c_uint),   # AABBGGRR（不是 RGB）
        ("AnimationId", ctypes.c_uint),
    ]


class _WindowCompositionAttribData(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.c_void_p),
        ("SizeOfData", ctypes.c_size_t),
    ]


def _rgba_to_abgr(color: str) -> int:
    """'#RRGGBB' → 0x00BBGGRR；非法输入退回白色。"""
    text = (color or "").strip().lstrip("#")
    if len(text) != 6:
        return 0x00FFFFFF
    try:
        r = int(text[0:2], 16)
        g = int(text[2:4], 16)
        b = int(text[4:6], 16)
    except ValueError:
        return 0x00FFFFFF
    return (b << 16) | (g << 8) | r


def _transparency_enabled() -> bool:
    """系统「透明效果」开关（设置 → 个性化 → 颜色）。

    关掉后 DWM 不会做模糊，Acrylic 会退化成一块实色，不如直接用原来的渐变。
    """
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        try:
            value, _ = winreg.QueryValueEx(key, "EnableTransparency")
            return int(value) != 0
        finally:
            winreg.CloseKey(key)
    except Exception:
        return True


def supported() -> bool:
    """当前系统是否具备启用 Acrylic 的前提。"""
    if sys.platform != "win32":
        return False
    try:
        build = sys.getwindowsversion().build
    except Exception:
        build = 0
    if build < 17134:          # Win10 1803 起才有 Acrylic
        return False
    return _transparency_enabled()


def _set_accent(hwnd: int, state: int, tint: str, tint_alpha: int) -> bool:
    try:
        user32 = ctypes.windll.user32
        user32.SetWindowCompositionAttribute.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_WindowCompositionAttribData)]
        user32.SetWindowCompositionAttribute.restype = ctypes.c_int
        alpha = max(0, min(255, int(tint_alpha))) << 24
        policy = _AccentPolicy(state, 0, alpha | _rgba_to_abgr(tint), 0)
        data = _WindowCompositionAttribData(
            WCA_ACCENT_POLICY,
            ctypes.cast(ctypes.byref(policy), ctypes.c_void_p),
            ctypes.sizeof(policy))
        return bool(user32.SetWindowCompositionAttribute(
            ctypes.c_void_p(hwnd), ctypes.byref(data)))
    except Exception as e:
        _dbg_print(f"[LyLaunch] 背景材质设置失败: {e!r}")
        return False


def _round_window_corners(hwnd: int) -> None:
    """让 DWM 把整个窗口（含 Acrylic 模糊区）裁成圆角，避免外圈露出一圈方角磨砂边。"""
    try:
        dwmapi = ctypes.windll.dwmapi
        pref = ctypes.c_int(2)   # DWMWCP_ROUND
        dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), ctypes.c_uint(33),
                                     ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


def accent_rgb() -> tuple[int, int, int]:
    """系统强调色（个性化 → 颜色）的 RGB；读不到退回 Windows 默认蓝 #0078D4。

    毛玻璃皮肤目前用「原版渐变 + 半透明」着色（用户要求观感别偏离原版太多），
    这个函数留着给以后可能加的"跟随强调色"开关用，当前没有调用方。
    """
    if sys.platform != "win32":
        return (0, 120, 212)
    try:
        dwmapi = ctypes.windll.dwmapi
        color = ctypes.c_ulong(0)
        opaque = ctypes.c_int(0)   # 第二个参数：是否不透明（这里用不到）
        hr = dwmapi.DwmGetColorizationColor(ctypes.byref(color), ctypes.byref(opaque))
        if hr != 0:
            return (0, 120, 212)
        v = color.value
        r, g, b = (v >> 16) & 255, (v >> 8) & 255, v & 255
        if r + g + b < 24:          # 全黑说明没读到，别把玻璃做成黑色
            return (0, 120, 212)
        return (r, g, b)
    except Exception:
        return (0, 120, 212)


def apply_acrylic(widget, tint: str = "#FFFFFF", tint_alpha: int = 0) -> bool:
    """给已经 show() 过的窗口开 Acrylic；返回是否真的开了。

    tint 的 alpha 默认 0：让 DWM 只做模糊、不加色，颜色交给 Qt 自绘的
    半透明白罩层控制 —— 这样透明度、渐变方向都在一处可调。
    """
    if not supported():
        return False
    try:
        hwnd = int(widget.winId())
    except Exception:
        return False
    if hwnd == 0:
        return False
    ok = _set_accent(hwnd, ACCENT_ENABLE_ACRYLICBLURBEHIND, tint, tint_alpha)
    if not ok:
        # 老旧显卡 / 驱动不支持 Acrylic 时退回普通模糊（没有磨砂颗粒）
        ok = _set_accent(hwnd, ACCENT_ENABLE_BLURBEHIND, tint, tint_alpha)
    if ok:
        _round_window_corners(hwnd)
    _dbg_print(f"[LyLaunch] Acrylic 背景: {'on' if ok else 'off'}")
    return ok


def clear_acrylic(widget) -> None:
    """关掉背景材质（窗口回到普通合成），并撤销 Acrylic 时加的窗口圆角。"""
    try:
        hwnd = int(widget.winId())
    except Exception:
        return
    if not hwnd:
        return
    _set_accent(hwnd, ACCENT_DISABLED, "#FFFFFF", 0)
    try:
        dwmapi = ctypes.windll.dwmapi
        pref = ctypes.c_int(0)   # DWMWCP_DEFAULT
        dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), ctypes.c_uint(33),
                                     ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass
