"""开机自启（HKCU\\Run 注册表项）——集中管理"登录后自动运行 LyLaunch"。

- is_enabled()         读注册表，当前是否已启用
- stored_command()     注册表里实际存着的启动命令行（排查"开关开了但开机没启"用）
- enable() / disable() 写 / 删 Run 项（enable 写完会回读校验，失败抛异常）
- current_command()    当前程序应写入的启动命令行（设置页展示用）

命令分派：
- 打包后（PyInstaller frozen）：直接运行 exe 自身，路径用双引号包住防空格截断。
- 开发期：用 pythonw.exe 启动 main.py（无控制台黑窗）；pythonw 缺失退回 python.exe。

⚠️ 注册表"值名"是大小写不敏感的（`LyLaunch` 与 `Lylaunch` 是同一条）。
   历史事故：更名后把连写旧名 `Lylaunch` 当成"更名前的遗留项"在 enable() 里删除，
   而它和我们新写的 `LyLaunch` 根本是同一条 → **每次开启自启都把自己刚写的值删掉**，
   表现为"设置页开关能打开、提示已开启，但下次进设置又变回关闭、开机也不会自启"。
   因此 `_LEGACY_APP_NAMES` 里只能放与 `_APP_NAME` **不只是大小写差异**的旧名。
"""

from __future__ import annotations

import os
import sys
import winreg

from . import paths

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME = "LyLaunch"
# 更名前注册表里的旧值名：只有名字真正不同（非仅大小写）的才能删，
# 否则会连带删除当前值（见模块 docstring 的历史事故）。
_LEGACY_APP_NAMES = ("LiyTools",)


def _same_value_name(a: str, b: str) -> bool:
    """注册表值名比较：大小写不敏感。"""
    return a.casefold() == b.casefold()


def current_command() -> str:
    """返回应写入 Run 项的命令行。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    main_py = paths.resource_path("main.py")
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return f'"{pythonw}" "{main_py}"'


def stored_command() -> str | None:
    """读注册表里当前实际存着的命令；不存在返回 None。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _APP_NAME)
            return str(value) if value else None
    except FileNotFoundError:
        return None
    except Exception:
        return None


def is_enabled() -> bool:
    return bool(stored_command())


def command_is_current() -> bool:
    """已开启时，注册表里的命令行是否指向"当前这个程序"。

    重装/换目录后旧值会指向不存在的路径 —— 开关看着是开的，开机却不启动。
    设置页据此给出"关闭再打开即可更新"的提示。
    """
    stored = stored_command()
    if not stored:
        return False
    return stored.strip().casefold() == current_command().strip().casefold()


def enable() -> None:
    """写入 Run 项（覆盖旧值）并回读校验；写不进去抛 RuntimeError。"""
    cmd = current_command()
    # CreateKeyEx：Run 键理论上一定存在，但新建用户的首次登录窗口期可能还没建出来
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
        winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
    ) as key:
        # 先清旧名（与当前值名不只是大小写差异的那些），再写当前值：
        # 顺序反了的话，万一将来又把同名旧值加进列表，就会重演"自己删自己"。
        for legacy in _LEGACY_APP_NAMES:
            if _same_value_name(legacy, _APP_NAME):
                continue
            try:
                winreg.DeleteValue(key, legacy)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, cmd)

    # 回读校验：注册表被重定向/无权限时 SetValueEx 也可能"静默成功"，
    # 不校验就会出现"开关开了、实际没写进去"的假象（用户最难查的那种 bug）。
    if stored_command() != cmd:
        raise RuntimeError("注册表写入未生效（可能被安全软件拦截或无权限）")


def disable() -> None:
    """删除 Run 项；值不存在时静默忽略。

    同时清理更名前的旧值（LiyTools）——旧安装可能已被删，但注册表残留会指向
    不存在的路径；以前只在 enable() 时清，用户若一直没重新开启过自启，残留就
    永远留着。注意跳过大写/小写仅不同的名字（它们就是当前值本身）。
    """
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE,
        ) as key:
            for name in (_APP_NAME, *_LEGACY_APP_NAMES):
                if name != _APP_NAME and _same_value_name(name, _APP_NAME):
                    continue
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
    except FileNotFoundError:
        pass
