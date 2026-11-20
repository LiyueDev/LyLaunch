"""把桌面上的「桌面净化」容器文件夹换成插件瓦片同款图标（0.43）。

原理
----
Windows 文件夹图标由文件夹内的 ``desktop.ini`` 决定：
    [.ShellClassInfo]
    IconResource=<ico 绝对路径>,0
写完还要给文件夹加 **system + hidden** 属性（desktop.ini 才会被 Explorer 读取），
最后 SHChangeNotify 通知桌面刷新，不用用户按 F5。

只改这一个容器文件夹，不碰其它任何东西；``restore`` 会把 desktop.ini 删掉、
清掉 system/hidden 属性（保留原文件夹和里面的文件），恢复成系统黄色文件夹。
"""
from __future__ import annotations

import os
import ctypes
import shutil
from ctypes import wintypes

try:
    import fences
except ImportError:  # pragma: no cover
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fences

# ico 生成目标：用户数据目录（%LocalAppData%\LyLaunch\icons\desktop_folder_tile.ico）。
# ⚠️ 曾经写在插件目录（自己 __file__ 旁边）——开发期能用，**安装版必失败**：
#   插件在 C:\Program Files\LyLaunch\_internal\plugins\ 下，普通用户只有 RX 权限，
#   save() 抛 PermissionError → 图标永远换不上（0.43 安装版「图标功能不生效」的真因）。
#   放用户目录还顺带解决两件事：升级重装不丢（desktop.ini 里记的是这个绝对路径）、
#   不需要 UAC 提权。
_ICON_NAME = "desktop_folder_tile.ico"
_INI_NAME = "desktop.ini"
# 随插件分发的同款预生成 ico（只读兜底：缺 Pillow / 渲染异常时直接拷这份，功能不哑）
_BUNDLED_ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), _ICON_NAME)

# Win32：desktop.ini 生效需要文件夹带 SYSTEM 属性；HIDDEN 让 ini 自身不显示
_FILE_ATTRIBUTE_SYSTEM = 0x4
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_DIRECTORY = 0x10

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_k32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_k32.SetFileAttributesW.restype = wintypes.BOOL
_k32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
_k32.GetFileAttributesW.restype = wintypes.DWORD
_shell32.SHChangeNotify.argtypes = [
    wintypes.LONG, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p]
_shell32.SHChangeNotify.restype = None

_SHCNE_UPDATEITEM = 0x00002000
_SHCNE_ASSOCCHANGED = 0x08000000
_SHCNF_IDLIST = 0x0000


def _refresh(folder: str) -> None:
    _shell32.SHChangeNotify(_SHCNE_UPDATEITEM, _SHCNF_IDLIST,
                            ctypes.c_wchar_p(folder), None)
    _shell32.SHChangeNotify(_SHCNE_ASSOCCHANGED, _SHCNF_IDLIST, None, None)


def _icon_dir() -> str:
    """ico 落盘目录：优先程序数据目录，取不到时退回 %LocalAppData%\\LyLaunch。"""
    try:
        from lylaunch.paths import DATA_DIR
        return str(DATA_DIR / "icons")
    except Exception:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "LyLaunch", "icons")


def _ico_path() -> str:
    return os.path.join(_icon_dir(), _ICON_NAME)


def make_tile_ico() -> tuple[bool, str]:
    """生成浅蓝圆角底 + 蓝色文件夹线性图标的多尺寸 ico（返回 ico 路径）。"""
    ico = _ico_path()
    try:
        os.makedirs(os.path.dirname(ico), exist_ok=True)
    except OSError as exc:
        return False, f"无法创建图标目录（{os.path.dirname(ico)}）：{exc}"
    try:
        from PySide6.QtWidgets import QApplication
        from lylaunch.plugins.plugin_icons import plugin_icon_svg, render_plugin_svg
        from PIL import Image, ImageDraw

        app = QApplication.instance()
        if app is None:
            app = QApplication([])          # CLI 单独跑时兜底

        big = render_plugin_svg(plugin_icon_svg("desktop"), 256).toImage()
        # QImage -> Pillow RGBA
        rgba = big.convertToFormat(big.Format.Format_ARGB32)
        w, h = rgba.width(), rgba.height()
        # Qt6 的 constBits() 是 memoryview；bytes() 直接拿到原始 BGRA 像素
        raw = bytes(rgba.constBits())
        fg = Image.frombytes("RGBA", (w, h), raw, "raw", "BGRA")

        # 浅蓝圆角底（和插件瓦片 #F2F6FD 一致）
        bg = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(bg)
        radius = int(w * 0.22)
        d.rounded_rectangle((6, 6, w - 7, h - 7), radius=radius,
                            fill=(242, 246, 253, 255),
                            outline=(195, 211, 238, 255), width=max(2, w // 96))
        # 线性图标居中，留 22% 内边距
        pad = int(w * 0.22)
        fg = fg.resize((w - 2 * pad, h - 2 * pad), Image.Resampling.LANCZOS)
        bg.alpha_composite(fg, (pad, pad))

        sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
        bg.save(ico, format="ICO", sizes=sizes)
        return True, ico
    except Exception as exc:
        # 渲染链任一环掉链子（缺 Pillow、QtSvg 异常、像素拷贝失败…）都别让功能变哑：
        # 拷随插件分发的预生成 ico 顶上，效果和现场生成的一模一样。
        if os.path.isfile(_BUNDLED_ICON):
            try:
                shutil.copyfile(_BUNDLED_ICON, ico)
                return True, ico
            except Exception:
                pass
        return False, f"生成图标失败：{exc}"


def set_folder_icon() -> tuple[bool, str]:
    """给桌面净化容器换上瓦片图标（重复调用幂等）。"""
    ok, ico = make_tile_ico()
    if not ok:
        return False, ico
    try:
        import ops
        root = ops.desktop_dir()
    except Exception as exc:
        return False, f"找不到桌面目录：{exc}"

    folder = fences.container_dir(root)
    if not os.path.isdir(folder):
        return False, "桌面上还没有「桌面净化」文件夹，先收纳一次再换图标"

    ini = os.path.join(folder, _INI_NAME)
    # IconResource 用正斜杠，Windows 两种都吃；绝对路径最稳
    icon_ref = ico.replace("\\", "/")
    # ⚠️ 路径含中文（C:\Users\<用户名>\...）：desktop.ini 必须用 UTF-16 LE + BOM，
    # UTF-8 无 BOM 会被 Explorer 按 ANSI 解析成乱码 → 图标死活不生效。
    # 只写 IconResource 一行，别同时写 IconFile/IconIndex（重复项会互相干扰）。
    content = ("[.ShellClassInfo]\r\n"
               f"IconResource={icon_ref},0\r\n")
    try:
        old = os.path.exists(ini)
        # desktop.ini 自身要可写，先去掉只读
        if old:
            attrs = _k32.GetFileAttributesW(ini)
            if attrs != 0xFFFFFFFF and attrs & _FILE_ATTRIBUTE_HIDDEN:
                _k32.SetFileAttributesW(ini, attrs & ~_FILE_ATTRIBUTE_HIDDEN)
        with open(ini, "w", encoding="utf-16") as fh:   # utf-16 = LE + BOM
            fh.write(content)
        # desktop.ini 生效的必要条件：目录有 SYSTEM 属性（HIDDEN 由标志位控制可见）
        attrs = _k32.GetFileAttributesW(folder)
        if attrs != 0xFFFFFFFF:
            _k32.SetFileAttributesW(
                folder,
                (attrs | _FILE_ATTRIBUTE_SYSTEM | _FILE_ATTRIBUTE_DIRECTORY)
                & ~_FILE_ATTRIBUTE_HIDDEN)
        _refresh(folder)
        return True, "桌面文件夹图标已换成瓦片样式"
    except Exception as exc:
        return False, f"写入文件夹图标配置失败：{exc}"


def restore_folder_icon() -> tuple[bool, str]:
    """还原成系统黄色文件夹：删掉 desktop.ini、清掉 system/hidden 属性。"""
    try:
        import ops
        root = ops.desktop_dir()
    except Exception as exc:
        return False, f"找不到桌面目录：{exc}"
    folder = fences.container_dir(root)
    if not os.path.isdir(folder):
        return True, "容器文件夹不存在，无需还原"
    ini = os.path.join(folder, _INI_NAME)
    try:
        if os.path.exists(ini):
            attrs = _k32.GetFileAttributesW(ini)
            if attrs != 0xFFFFFFFF:
                _k32.SetFileAttributesW(ini, attrs & ~_FILE_ATTRIBUTE_HIDDEN)
            os.remove(ini)
        attrs = _k32.GetFileAttributesW(folder)
        if attrs != 0xFFFFFFFF:
            _k32.SetFileAttributesW(
                folder, attrs & ~_FILE_ATTRIBUTE_SYSTEM & ~_FILE_ATTRIBUTE_HIDDEN)
        _refresh(folder)
        return True, "桌面文件夹图标已还原为系统默认"
    except Exception as exc:
        return False, f"还原失败：{exc}"
