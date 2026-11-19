"""桌面图标隐藏/显示 —— 纯 Windows 层（零 Qt 依赖）。

设计原则
--------
1. **绝不移动/删除文件**。两种隐藏模式：

   - ``files_only``（默认）：给「真实文件/文件夹」加 ``FILE_ATTRIBUTE_HIDDEN``
     —— 只让它们的桌面图标消失，路径不变、``.lnk``/``.url`` 快捷方式照常显示；
     恢复时只清 HIDDEN 位（只读/存档等其它属性原样保留）。
   - ``all``：写注册表 ``HideIcons``（就是系统「桌面右键 → 查看 → 显示桌面图标」），
     把所有图标一起隐藏。

2. **幂等 set-to-value**，连调 10 次结果一致。
3. ``all`` 模式的即时刷新靠给 ``SHELLDLL_DefView`` 发 ``WM_COMMAND / 0x7402``，
   但**该命令本身是翻转（toggle）**，所以必须先判断当前状态再决定发不发，发完回读校验。
4. 所有跨进程消息一律 ``SendMessageTimeoutW`` 带超时，**绝不用裸 ``SendMessage``**
   （explorer 卡死会连带卡死 LyLaunch）。
5. 只用 ctypes 调 user32/kernel32/shell32，**不用 win32gui**（打包 spec 未收录）。

命令行兜底（GUI 挂了也能救回来）::

    python plugins/desktop_files/desktop_icons.py status     # 看当前状态
    python plugins/desktop_files/desktop_icons.py hide       # 只隐藏真实文件（默认模式）
    python plugins/desktop_files/desktop_icons.py hide-all   # 连快捷方式一起隐藏
    python plugins/desktop_files/desktop_icons.py show       # 恢复显示（两种模式都清）
    python plugins/desktop_files/desktop_icons.py restore    # 按上次标记恢复
"""

from __future__ import annotations

import ctypes
import json
import os
import time
import winreg
from ctypes import wintypes

# ---------------------------------------------------------------- ctypes 声明
_u32 = ctypes.WinDLL("user32", use_last_error=True)

WM_COMMAND = 0x0111
WM_SETTINGCHANGE = 0x001A
CMD_TOGGLE_DESKTOP_ICONS = 0x7402
SMTO_ABORTIFHUNG = 0x0002
HWND_BROADCAST = 0xFFFF
GW_CHILD = 5
GWL_STYLE = -16
WS_VISIBLE = 0x10000000
SW_HIDE = 0
SW_SHOW = 5

# 文件属性（只隐藏真实文件时用）
FILE_ATTRIBUTE_READONLY = 0x0001
FILE_ATTRIBUTE_HIDDEN = 0x0002
FILE_ATTRIBUTE_SYSTEM = 0x0004
FILE_ATTRIBUTE_NORMAL = 0x0080
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

# SHChangeNotify：改完属性后让桌面立即刷新该项（不用等 F5）
SHCNE_UPDATEITEM = 0x00002000
SHCNE_UPDATEDIR = 0x00001000
SHCNE_ASSOCCHANGED = 0x08000000
SHCNF_PATHW = 0x0005
SHCNF_IDLIST = 0x0000

_u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_u32.FindWindowW.restype = wintypes.HWND

_u32.FindWindowExW.argtypes = [
    wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
]
_u32.FindWindowExW.restype = wintypes.HWND

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_u32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_u32.EnumWindows.restype = wintypes.BOOL

_u32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_u32.GetWindow.restype = wintypes.HWND

_u32.IsWindowVisible.argtypes = [wintypes.HWND]
_u32.IsWindowVisible.restype = wintypes.BOOL

# 64 位必须显式声明 argtypes，否则 HWND 会被截成 int32 改到别的窗口上（项目红线）
_u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_u32.GetWindowLongW.restype = ctypes.c_long

_u32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
]
_u32.SendMessageTimeoutW.restype = ctypes.c_ssize_t

_u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_u32.ShowWindow.restype = wintypes.BOOL

# 文件属性 / 桌面刷新
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
_k32.GetFileAttributesW.restype = wintypes.DWORD
_k32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_k32.SetFileAttributesW.restype = wintypes.BOOL

_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_shell32.SHChangeNotify.argtypes = [
    ctypes.c_long, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
]
_shell32.SHChangeNotify.restype = None

# ---------------------------------------------------------------- 注册表
_ADV_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
_VALUE = "HideIcons"


def registry_hidden() -> int | None:
    """1=隐藏、0=显示（值不存在按 0，Windows 默认显示）；读失败返回 None。"""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _ADV_KEY, 0, winreg.KEY_QUERY_VALUE
        ) as key:
            value, _type = winreg.QueryValueEx(key, _VALUE)
            return 1 if int(value) else 0
    except FileNotFoundError:
        return 0
    except OSError:
        return None


def write_hidden(value: bool | int) -> bool:
    """写 HideIcons（幂等），写完回读校验；不假定 SetValueEx 一定成功。"""
    want = 1 if value else 0
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _ADV_KEY, 0,
            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
        ) as key:
            winreg.SetValueEx(key, _VALUE, 0, winreg.REG_DWORD, want)
    except OSError:
        return False
    return registry_hidden() == want


# ---------------------------------------------------------------- 桌面窗口
def find_defview() -> int:
    """找桌面图标宿主 SHELLDLL_DefView 的 HWND；找不到返回 0。

    正常情况下它挂在 Progman 下；启用壁纸轮播 / 多显示器时可能挂到某个 WorkerW 下，
    所以 Progman 里找不到就全量枚举。**不要缓存句柄**——explorer 重启后会失效。
    """
    progman = _u32.FindWindowW("Progman", None)
    if progman:
        view = _u32.FindWindowExW(progman, None, "SHELLDLL_DefView", None)
        if view:
            return int(view)

    hits: list[int] = []

    def _callback(hwnd, _lparam):
        view = _u32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None)
        if view:
            hits.append(int(view))
            return False  # 找到一个就停
        return True

    callback = _WNDENUMPROC(_callback)  # 必须保持引用到调用结束
    _u32.EnumWindows(callback, 0)
    return hits[0] if hits else 0


def desktop_listview() -> int:
    """桌面图标列表 SysListView32 的 HWND；拿不到返回 0。"""
    view = find_defview()
    if not view:
        return 0
    listview = _u32.FindWindowExW(view, None, "SysListView32", None)
    if not listview:
        # 某些系统中间多一层 FolderView，退回取第一个子窗口
        listview = _u32.GetWindow(view, GW_CHILD)
    return int(listview) if listview else 0


def icons_visible() -> bool | None:
    """桌面图标当前是否可见；结构异常/拿不到返回 None（调用方不得当作「可见」）。

    只看 ListView 自身的 WS_VISIBLE：Win+D 会隐藏祖先窗口，但不会清这个位，
    所以比 IsWindowVisible 更能反映「显示桌面图标」这个开关本身的状态。
    """
    listview = desktop_listview()
    if not listview:
        return None
    style = _u32.GetWindowLongW(listview, GWL_STYLE)
    if not style:
        return None
    return bool(style & WS_VISIBLE)


def _send_toggle() -> bool:
    """给 DefView 发「显示/隐藏桌面图标」命令（注意：这是翻转，不是设为某值）。"""
    view = find_defview()
    if not view:
        return False
    result = ctypes.c_size_t(0)
    sent = _u32.SendMessageTimeoutW(
        view, WM_COMMAND, CMD_TOGGLE_DESKTOP_ICONS, 0,
        SMTO_ABORTIFHUNG, 300, ctypes.byref(result),
    )
    return bool(sent)


def broadcast_settingchange() -> None:
    """广播 WM_SETTINGCHANGE，让 explorer 重新读取外观设置（兜底刷新）。"""
    result = ctypes.c_size_t(0)
    text = ctypes.c_wchar_p(_ADV_KEY)
    _u32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0,
        ctypes.cast(text, ctypes.c_void_p).value or 0,
        SMTO_ABORTIFHUNG, 300, ctypes.byref(result),
    )


def force_showwindow(visible: bool) -> bool:
    """硬切 ListView 可见性（仅在 0x7402 完全失效时由配置开启）。

    ⚠️ 会绕过 explorer 的内部状态，导致桌面右键的勾选态与实际不符，默认不用。
    """
    listview = desktop_listview()
    if not listview:
        return False
    return bool(_u32.ShowWindow(listview, SW_SHOW if visible else SW_HIDE))


# ---------------------------------------------------------------- 文件级隐藏
# 用途：只隐藏「真实文件/文件夹」的桌面图标，保留 .lnk/.url 快捷方式可见。
# 做法：给文件加 FILE_ATTRIBUTE_HIDDEN（不移动、不删除、路径不变），恢复时只清这一位。
def notify_item_changed(path: str) -> None:
    """告诉资源管理器「这一项变了」，桌面图标立即响应（不用等 F5）。"""
    try:
        text = ctypes.c_wchar_p(path)
        _shell32.SHChangeNotify(
            SHCNE_UPDATEITEM, SHCNF_PATHW, ctypes.cast(text, ctypes.c_void_p), None
        )
    except Exception:
        pass


def refresh_shell(directory: str = "", deep: bool = False) -> None:
    """通知 shell「这个目录的内容变了」（大量移动/恢复文件后必须调用）。

    只发 SHCNE_UPDATEDIR 有时不够：桌面图标缓存会残留**空白条目**，
    表现为「快捷方式还在、图标变成白纸」，主程序却能正常显示。
    ``deep=True`` 再补一次 ASSOCCHANGED，让资源管理器重建图标缓存。
    这是一次性通知，不会持续占用资源。
    """
    if directory:
        try:
            text = ctypes.c_wchar_p(str(directory))
            _shell32.SHChangeNotify(
                SHCNE_UPDATEDIR, SHCNF_PATHW,
                ctypes.cast(text, ctypes.c_void_p), None,
            )
        except Exception:
            pass
    if deep:
        try:
            _shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
        except Exception:
            pass


def set_path_hidden(path: str, hidden: bool) -> bool:
    """只改 HIDDEN 位，保留 READONLY / SYSTEM / ARCHIVE 等其它属性。

    ⚠️ SetFileAttributesW 是**整值覆盖**，直接传 FILE_ATTRIBUTE_HIDDEN 会把只读/存档
    属性一起抹掉，所以必须「先读 → 位运算 → 写回」。
    """
    attrs = _k32.GetFileAttributesW(path)
    if attrs == INVALID_FILE_ATTRIBUTES:
        return False

    target = (attrs | FILE_ATTRIBUTE_HIDDEN) if hidden else (attrs & ~FILE_ATTRIBUTE_HIDDEN)
    if target == attrs:
        return True  # 已经是目标状态

    # 只读文件可能拒绝改属性：临时去掉 READONLY 再设，目标值里已包含 READONLY
    readonly = bool(attrs & FILE_ATTRIBUTE_READONLY)
    if readonly:
        _k32.SetFileAttributesW(path, attrs & ~FILE_ATTRIBUTE_READONLY)
    ok = bool(_k32.SetFileAttributesW(path, target))
    if not ok and readonly:
        _k32.SetFileAttributesW(path, attrs)  # 失败也要把只读还原
    if ok:
        notify_item_changed(path)
    return ok


def set_paths_hidden(paths, hidden: bool) -> tuple[list[str], list[str]]:
    """批量设置/清除隐藏属性，返回 (成功列表, 失败列表)。"""
    done: list[str] = []
    failed: list[str] = []
    for path in paths or []:
        if not path:
            continue
        if set_path_hidden(str(path), hidden):
            done.append(str(path))
        else:
            failed.append(str(path))
    return done, failed


def hide_paths(paths) -> list[dict]:
    """真正「看不见」的隐藏：同时加 HIDDEN + SYSTEM 属性。

    为什么要 SYSTEM：只加 HIDDEN 的话，用户只要开着资源管理器的
    「显示隐藏的项目」，桌面图标就只是变半透明 —— 那不算隐藏。
    而 SYSTEM 属性归「隐藏受保护的操作系统文件」管（默认勾选），
    两者独立，所以开着「显示隐藏的项目」也依然看不见。

    返回 ``[{"path": ..., "attrs": 原始属性}]``，恢复时按原始属性**精确还原**，
    不会误删文件本来就有的 SYSTEM / 只读等属性。
    """
    records: list[dict] = []
    for path in paths or []:
        if not path:
            continue
        attrs = _k32.GetFileAttributesW(str(path))
        if attrs == INVALID_FILE_ATTRIBUTES:
            continue
        target = attrs | FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
        if target != attrs and not _k32.SetFileAttributesW(str(path), target):
            continue
        notify_item_changed(str(path))
        records.append({"path": str(path), "attrs": int(attrs)})
    return records


def show_paths(records) -> tuple[list[str], list[str]]:
    """按 hide_paths 的记录还原原始属性。返回 (成功, 失败)。"""
    done: list[str] = []
    failed: list[str] = []
    for item in records or []:
        path = str(item.get("path") or "")
        if not path:
            continue
        original = item.get("attrs")
        if original is None:
            # 兼容旧记录（只有路径）：只清 HIDDEN，不动 SYSTEM
            current = _k32.GetFileAttributesW(path)
            if current == INVALID_FILE_ATTRIBUTES:
                failed.append(path)
                continue
            ok = bool(_k32.SetFileAttributesW(path, current & ~FILE_ATTRIBUTE_HIDDEN))
        else:
            ok = bool(_k32.SetFileAttributesW(path, int(original)))
        if ok:
            notify_item_changed(path)
            done.append(path)
        else:
            failed.append(path)
    return done, failed


def is_hidden(path: str) -> bool:
    """文件当前是否带 HIDDEN 属性。"""
    attrs = _k32.GetFileAttributesW(str(path))
    if attrs == INVALID_FILE_ATTRIBUTES:
        return False
    return bool(attrs & FILE_ATTRIBUTE_HIDDEN)


def clear_hidden(path: str) -> bool:
    """强制去掉 HIDDEN 与 SYSTEM（用于「只移动不隐藏」方案里清掉历史属性）。"""
    target = str(path)
    attrs = _k32.GetFileAttributesW(target)
    if attrs == INVALID_FILE_ATTRIBUTES:
        return False
    new_attrs = attrs & ~FILE_ATTRIBUTE_HIDDEN & ~FILE_ATTRIBUTE_SYSTEM
    if new_attrs == attrs:
        return True
    ok = bool(_k32.SetFileAttributesW(target, new_attrs))
    if ok:
        notify_item_changed(target)
    return ok


def windows_shows_hidden() -> bool:
    """资源管理器是否开了「显示隐藏的项目」——开着时设 HIDDEN 属性对用户无效。"""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _ADV_KEY, 0, winreg.KEY_QUERY_VALUE
        ) as key:
            value, _type = winreg.QueryValueEx(key, "Hidden")
            return int(value) == 1
    except Exception:
        return False


# ---------------------------------------------------------------- 图标重排
LVM_ARRANGE = 0x1022
LVA_DEFAULT = 0x0000
LVA_SNAPTOGRID = 0x0005


def arrange_desktop_icons() -> bool:
    """让桌面图标重新排列，补上文件被移走留下的空位。

    桌面若开着「自动排列图标」，``LVA_DEFAULT`` 会立刻重排；
    关着的话 Windows 会忽略它，再补一次 ``LVA_SNAPTOGRID`` 至少让图标对齐网格。
    """
    listview = desktop_listview()
    if not listview:
        return False
    result = ctypes.c_size_t(0)
    ok_default = bool(_u32.SendMessageTimeoutW(
        listview, LVM_ARRANGE, LVA_DEFAULT, 0,
        SMTO_ABORTIFHUNG, 800, ctypes.byref(result),
    ))
    ok_snap = bool(_u32.SendMessageTimeoutW(
        listview, LVM_ARRANGE, LVA_SNAPTOGRID, 0,
        SMTO_ABORTIFHUNG, 800, ctypes.byref(result),
    ))
    return ok_default or ok_snap


def _desktop_roots(include_public: bool = True) -> list[str]:
    roots: list[str] = []
    try:
        from win32com.shell import shell, shellcon

        roots.append(shell.SHGetKnownFolderPath(shellcon.FOLDERID_Desktop))
        if include_public:
            roots.append(shell.SHGetKnownFolderPath(shellcon.FOLDERID_PublicDesktop))
    except Exception:
        pass
    return [r for r in roots if r]


def list_desktop_files(include_public: bool = True) -> list[str]:
    """列出桌面上「非快捷方式」的真实文件/文件夹路径（CLI 兜底用，不依赖 Qt）。"""
    skip = {"desktop.ini", "thumbs.db"}
    shortcuts = (".lnk", ".url")
    found: list[str] = []
    seen: set[str] = set()
    for root in _desktop_roots(include_public):
        try:
            with os.scandir(root) as iterator:
                for item in iterator:
                    low = item.name.casefold()
                    if low in skip or low.endswith(shortcuts):
                        continue
                    key = low
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(item.path)
        except OSError:
            continue
    return found


# ---------------------------------------------------------------- 核心入口
def set_hidden(desired: bool, allow_showwindow_fallback: bool = False) -> tuple[bool, bool, str]:
    """把桌面图标设成 desired。返回 ``(成功, 是否即时生效, 说明文案)``。

    顺序很重要（0x7402 是翻转命令，顺序反了结果会正好相反）：

    1. 视觉上已经到位 → 只把注册表补成目标值，不发命令；
    2. 注册表已到位但视觉没跟上（explorer 没应用 / 被 Win+D 干扰）→ 先广播重读，
       仍然不一致才发一次翻转；
    3. 正常情况（注册表与目标不符）→ 发翻转，最多两次，每次回读校验；
    4. 全部失败 → 降级为「写注册表 + 广播」，如实报告未即时生效。
    """
    want = 1 if desired else 0
    visible = icons_visible()
    registered = registry_hidden()

    # ① 已经到位：只需要让注册表与之一致
    if visible is not None and visible == (not desired):
        if registered != want and not write_hidden(want):
            return True, True, "桌面已切换，但注册表写入失败（重启后会复原）"
        return True, True, ""

    # ② 注册表已是目标值、但视觉没跟上：先广播，避免误翻转
    if registered == want and visible is not None:
        broadcast_settingchange()
        time.sleep(0.08)
        if icons_visible() == (not desired):
            return True, True, ""

    # ③ 发翻转（最多两次，防 explorer 竞态）
    for _ in range(2):
        if not _send_toggle():
            break
        for _ in range(8):  # 8 × 50ms = 400ms
            if icons_visible() == (not desired):
                if registry_hidden() != want:
                    write_hidden(want)  # explorer 有时不落盘，补一刀
                return True, True, ""
            time.sleep(0.05)

    # ④ 降级：写注册表 + 广播，如实告知
    if write_hidden(want):
        broadcast_settingchange()
        if allow_showwindow_fallback and force_showwindow(not desired):
            return True, True, "已用兼容方式切换（桌面右键的勾选状态可能不同步）"
        return True, False, "已写入注册表，但桌面没即时刷新（按 F5 或重启资源管理器后生效）"
    return False, False, "切换失败：请右键桌面 → 查看 → 显示桌面图标"


# ---------------------------------------------------------------- 状态标记
def state_path() -> str:
    """标记文件路径。故意不放在 plugins_<id>.json 里——重置数据会把它删掉。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "LyLaunch", "desktop_files_state.json")


def read_mark() -> dict:
    try:
        with open(state_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_mark(hidden_by_us: bool, note: str = "", mode: str = "all",
               paths=None, moved=None, folders=None, hidden_paths=None,
               hidden_items=None) -> None:
    """原子写标记（先写临时文件再 replace，断电不会留下半个文件）。

    mode:
      - "files_only"：只给真实文件设 HIDDEN（paths 记录路径）
      - "all"：系统级隐藏全部图标
      - "fences"：Fences 式收纳（moved 记录 原路径->新路径，folders 记录分类夹）
    hidden_paths: fences 模式下**用隐藏属性藏起来**的项
                  （分类夹本身、桌面文件夹、桌面快捷方式）。
    """
    try:
        path = state_path()
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "hidden_by_us": bool(hidden_by_us),
                    "mode": str(mode or "all"),
                    "paths": [str(p) for p in (paths or [])],
                    "moved": list(moved or []),
                    "folders": [str(p) for p in (folders or [])],
                    "hidden_paths": [str(p) for p in (hidden_paths or [])],
                    "hidden_items": list(hidden_items or []),
                    "since": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "pid": os.getpid(),
                    "note": note,
                },
                handle,
                ensure_ascii=False,
            )
        os.replace(tmp, path)
    except Exception:
        pass


def clear_mark() -> None:
    try:
        os.remove(state_path())
    except Exception:
        pass


def restore_if_marked() -> bool:
    """若「上次是我们动过的」就恢复。返回是否真的做了恢复。"""
    mark = read_mark()
    if not mark.get("hidden_by_us"):
        return False

    mode = str(mark.get("mode") or "all")
    if mode in ("fences", "files_only"):
        # 交给统一的兜底恢复，避免两套逻辑各写一遍
        ok, _message = force_show_all()
        return ok

    if registry_hidden() != 1 and icons_visible() is not False:
        write_mark(False)
        return False
    ok, _immediate, _msg = set_hidden(False)
    if ok:
        write_mark(False)
    return ok


def status() -> dict:
    """汇总当前状态，给 UI / CLI 用。"""
    registered = registry_hidden()
    visible = icons_visible()
    mark = read_mark()
    return {
        "registry": registered,                 # 1 隐藏 / 0 显示 / None 读失败
        "visible": visible,                     # True 可见 / False 不可见 / None 未知
        "defview": find_defview(),
        "listview": desktop_listview(),
        "mark": mark,
        "mark_mode": str(mark.get("mode") or ""),
        "mark_paths": len(mark.get("paths") or []),
        "shows_hidden": windows_shows_hidden(),  # 资源管理器是否开着「显示隐藏的项目」
        "state_path": state_path(),
    }


def force_show_all() -> tuple[bool, str]:
    """兜底恢复：把「我们动过的东西」全部还原（三种模式都清）。

    1) fences：按记录把文件移回原位，并清理空掉的分类夹；
    2) files_only：清掉记录文件的 HIDDEN 位；
    3) all：若系统级全隐藏还开着，关掉；
    最后清标记。返回 (是否全部成功, 说明)。
    """
    messages: list[str] = []
    ok = True

    mark = read_mark()
    mode = str(mark.get("mode") or "")
    moved = mark.get("moved") or []
    folders = [str(p) for p in (mark.get("folders") or [])]
    hidden_paths = [str(p) for p in (mark.get("hidden_paths") or [])]
    hidden_items = mark.get("hidden_items") or []
    paths = [str(p) for p in (mark.get("paths") or [])]

    # ① Fences 收纳：先让隐藏的快捷方式/文件夹/分类夹回到桌面，再把文件移回
    if mode == "fences":
        if hidden_items:
            _done, failed = show_paths(hidden_items)     # 按原始属性精确还原
            if failed:
                ok = False
                messages.append(f"{len(failed)} 个图标未能恢复显示")
        elif hidden_paths:
            _done, failed = set_paths_hidden(hidden_paths, False)
            if failed:
                ok = False
                messages.append(f"{len(failed)} 个图标未能恢复显示")
        if moved:
            try:
                import fences  # 同目录模块

                _restored, failed, missing = fences.restore(moved)
                if failed:
                    ok = False
                    messages.append(f"{len(failed)} 个文件未能移回原位")
                if missing:
                    messages.append(f"{len(missing)} 个文件已不在分类夹里（可能被手动移动过）")
            except Exception as exc:
                ok = False
                messages.append(f"收纳恢复失败：{exc}")
        if folders:
            try:
                import fences

                fences.cleanup_empty_folders(folders)
            except Exception:
                pass
        # 容器方案：把空掉的容器（桌面净化）也删掉
        try:
            import fences

            for root in _desktop_roots(include_public=False):
                fences.cleanup_container(root)
        except Exception:
            pass
        write_mark(False)
        arrange_desktop_icons()
        return ok, "；".join(messages)

    # ② 只隐藏真实文件：清 HIDDEN 位
    if paths:
        _done, failed = set_paths_hidden(paths, False)
        if failed:
            ok = False
            messages.append(f"{len(failed)} 个文件未能恢复隐藏属性")

    # ③ 系统级全隐藏：关掉
    if registry_hidden() == 1 or icons_visible() is False:
        switched, _immediate, msg = set_hidden(False)
        if not switched:
            ok = False
            messages.append(msg or "桌面图标未能恢复显示")

    write_mark(False)
    return ok, "；".join(messages)


# ---------------------------------------------------------------- CLI
def _main(argv: list[str]) -> int:
    try:
        sys_stdout = __import__("sys").stdout
        if hasattr(sys_stdout, "reconfigure"):
            sys_stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    action = (argv[1] if len(argv) > 1 else "status").strip().lower()

    if action == "status":
        info = status()
        reg = {1: "隐藏", 0: "显示", None: "读取失败"}[info["registry"]]
        vis = {True: "可见", False: "不可见", None: "未知"}[info["visible"]]
        print(f"注册表 HideIcons : {reg}")
        print(f"桌面图标视觉状态 : {vis}")
        print(f"SHELLDLL_DefView : {info['defview'] or '未找到'}")
        print(f"SysListView32    : {info['listview'] or '未找到'}")
        print(f"资源管理器显示隐藏项: {'是（设隐藏属性对你在桌面上无效）' if info['shows_hidden'] else '否'}")
        print(f"标记模式         : {info['mark_mode'] or '（无）'}")
        print(f"标记记录的文件数 : {info['mark_paths']}")
        print(f"标记文件         : {info['state_path']}")
        print(f"标记内容         : {info['mark'] or '（无）'}")
        return 0

    if action == "hide":
        # 默认只隐藏真实文件（快捷方式保持可见）
        paths = list_desktop_files()
        write_mark(True, mode="files_only", paths=paths)  # 先写标记，再改属性
        done, failed = set_paths_hidden(paths, True)
        print(f"已隐藏 {len(done)} 个真实文件/文件夹（快捷方式保持显示）")
        if failed:
            print(f"有 {len(failed)} 个未能隐藏（可能无权限）：")
            for path in failed[:10]:
                print("  -", path)
        if windows_shows_hidden():
            print("提醒：资源管理器开着「显示隐藏的项目」，桌面上看不到隐藏效果")
        return 0 if not failed else 1

    if action == "hide-all":
        write_mark(True, mode="all", paths=[])
        ok, immediate, message = set_hidden(True)
        print(("已隐藏全部桌面图标" if ok else "失败")
              + ("" if immediate else "（未即时生效）")
              + (f"：{message}" if message else ""))
        return 0 if ok else 1

    if action == "show":
        ok, message = force_show_all()
        print("已恢复桌面图标显示" + (f"：{message}" if message else ""))
        return 0 if ok else 1

    if action == "restore":
        if restore_if_marked():
            print("已从「上次隐藏」状态恢复")
        else:
            ok, message = force_show_all()
            print("没有待恢复标记，已做兜底清理" + (f"：{message}" if message else ""))
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
