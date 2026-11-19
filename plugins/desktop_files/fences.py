"""Fences 式收纳：把桌面上的真实文件按大类移动到桌面上的分类文件夹。

设计要点
--------
- **只移动文件，不移动文件夹**（桌面上的目录保持原位，不做嵌套）。
- 快捷方式（``.lnk`` / ``.url``）永远不移动 —— 它们是常用入口。
- 收纳夹建在**桌面根目录**下，名字就是分类名（``文档`` / ``图片`` / ``压缩包`` …），
  视觉上接近 Stardock Fences 的"围栏"。
- 每次移动都返回 ``{"from": 原路径, "to": 新路径, "category": 分类}``，
  由调用方写入状态文件；恢复时按这份记录**精确移回原位置**。
- 同名冲突自动加 ``(2)``、``(3)``，绝不覆盖。
- 单个文件失败（被占用/无权限）只记录、不中断整批。

本模块零 Qt 依赖，可被 CLI 复用。
"""

from __future__ import annotations

import os
import shutil

# 大类 -> 扩展名集合（小写，含点）
CATEGORIES: dict[str, set[str]] = {
    "文档": {
        ".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".odt", ".wps",
        ".xls", ".xlsx", ".csv", ".et", ".ppt", ".pptx", ".dps", ".odp", ".ods",
        ".html", ".htm", ".mht", ".mhtml",
    },
    "图片": {
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico",
        ".tif", ".tiff", ".psd", ".ai", ".heic", ".raw", ".cr2", ".nef",
    },
    "压缩包": {
        ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso", ".cab",
    },
    "音视频": {
        ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma",
        ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ts",
    },
    "程序脚本": {
        ".exe", ".msi", ".bat", ".cmd", ".ps1", ".py", ".pyw", ".vbs",
        ".js", ".jar", ".ahk", ".reg",
    },
}
FALLBACK = "其他"
SHORTCUT_GROUPS = ("游戏", "应用")   # 快捷方式的二次分类
SHORTCUT_SUFFIXES = (".lnk", ".url")
SKIP_NAMES = {"desktop.ini", "thumbs.db"}

# 统一容器：桌面上只建这一个文件夹，所有分类夹、文件、快捷方式都塞进去，
# 收纳时**只隐藏这一个容器**。这样既不会碰到个别有保护的文件，
# 也只需要一次属性操作。
CONTAINER_NAME = "桌面净化"


def container_dir(root: str) -> str:
    return os.path.join(root, CONTAINER_NAME)


def is_container(path: str) -> bool:
    return os.path.basename(path).casefold() == CONTAINER_NAME.casefold()

# 扩展名 -> 大类（反查表，启动时建一次）
_EXT_TO_CATEGORY: dict[str, str] = {}
for _name, _exts in CATEGORIES.items():
    for _ext in _exts:
        _EXT_TO_CATEGORY[_ext] = _name


def category_of(path: str) -> str:
    """返回该文件应归入的大类名（无扩展名或未知后缀归「其他」）。"""
    ext = os.path.splitext(path)[1].lower()
    return _EXT_TO_CATEGORY.get(ext, FALLBACK)


def category_names() -> list[str]:
    """所有会出现的分类名（快捷方式两类 + 文件夹 + 文件大类 + 其他）。"""
    return [*SHORTCUT_GROUPS, "文件夹", *CATEGORIES.keys(), FALLBACK]


def display_order() -> list[str]:
    """视图里的分区顺序：游戏、应用在前，然后是文件夹与文件大类。"""
    return [*SHORTCUT_GROUPS, "文件夹", *CATEGORIES.keys(), FALLBACK]


def is_shortcut(name: str) -> bool:
    return name.casefold().endswith(SHORTCUT_SUFFIXES)


def is_skippable(name: str) -> bool:
    return name.casefold() in SKIP_NAMES


def unique_path(path: str) -> str:
    """目标已存在时追加 (2)/(3)…，绝不覆盖已有文件。"""
    if not os.path.exists(path):
        return path
    folder, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    index = 2
    while True:
        candidate = os.path.join(folder, f"{stem} ({index}){ext}")
        if not os.path.exists(candidate):
            return candidate
        index += 1


def folder_path(root: str, category: str) -> str:
    return os.path.join(root, category)


def collect_targets(root: str) -> list[str]:
    """列出桌面根目录下「应该被收纳」的文件（跳过文件夹、快捷方式和系统文件）。"""
    targets: list[str] = []
    try:
        with os.scandir(root) as iterator:
            for item in iterator:
                if is_skippable(item.name) or is_shortcut(item.name):
                    continue
                try:
                    if item.is_dir(follow_symlinks=False):
                        continue        # 文件夹不移动
                except OSError:
                    continue
                targets.append(item.path)
    except OSError:
        pass
    return targets


def move_files(paths, root: str) -> tuple[list[dict], list[dict], list[str]]:
    """把 files 按大类移入 root 下的分类夹。

    快捷方式（.lnk/.url）不按扩展名归类，而是按「游戏 / 应用」分
    （公共桌面的快捷方式只能靠移动来隐藏，正好复用这条路径）。

    返回 ``(moved, failed, folders)``：
    - moved: ``[{"from":…, "to":…, "category":…}, …]``
    - failed: ``[{"path":…, "error":…}, …]``
    - folders: 本次实际创建/使用到的分类夹路径
    """
    moved: list[dict] = []
    failed: list[dict] = []
    folders: list[str] = []

    items = [p for p in (paths or []) if p and os.path.exists(p)]
    links = [p for p in items
             if os.path.isfile(p) and p.casefold().endswith(SHORTCUT_SUFFIXES)]
    targets = resolve_targets(links) if links else {}

    for src in items:
        if os.path.isdir(src):
            category = "文件夹"          # 整个目录搬进容器，不做嵌套拆分
        elif src.casefold().endswith(SHORTCUT_SUFFIXES):
            name = os.path.splitext(os.path.basename(src))[0]
            category = classify_shortcut(name, targets.get(src, ""))
        else:
            category = category_of(src)

        folder = folder_path(root, category)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            failed.append({"path": src, "error": f"无法创建分类夹：{exc}"})
            continue
        destination = unique_path(os.path.join(folder, os.path.basename(src)))
        try:
            shutil.move(src, destination)
        except Exception as exc:      # 占用 / 无权限 / 跨卷失败
            failed.append({"path": src, "error": str(exc)})
            continue
        moved.append({"from": src, "to": destination, "category": category})
        if folder not in folders:
            folders.append(folder)

    return moved, failed, folders


def restore(moved) -> tuple[list[str], list[dict], list[str]]:
    """按记录把文件移回原位。

    返回 ``(restored, failed, missing)``：
    - restored: 成功移回的文件名
    - failed: ``[{"path":…, "error":…}]``
    - missing: 记录里存在、但目标文件已经不在（用户手动挪走了）的路径
    """
    restored: list[str] = []
    failed: list[dict] = []
    missing: list[str] = []

    for item in moved or []:
        src = str(item.get("from") or "")
        dst = str(item.get("to") or "")
        if not src or not dst:
            continue
        if not os.path.exists(dst):
            missing.append(dst)
            continue
        target = unique_path(src) if os.path.exists(src) else src
        try:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.move(dst, target)
        except Exception as exc:
            failed.append({"path": dst, "error": str(exc)})
            continue
        restored.append(os.path.basename(target))

    return restored, failed, missing


def cleanup_empty_folders(folders) -> None:
    """恢复后顺手删掉空掉的分类夹（非空或不存在时静默跳过）。"""
    for folder in folders or []:
        try:
            if os.path.isdir(folder) and not os.listdir(folder):
                os.rmdir(folder)
        except OSError:
            pass


def managed_folders(root: str) -> list[str]:
    """列出全部分组夹：容器内**任意**子目录都算 —— 用户自建分组自动生效。

    固定分类（游戏/应用/文档…）只是预置名字，不再是白名单；
    用户在容器里手动建文件夹、或通过「新建分组」创建的，都会被识别。
    另兼容旧布局（分类夹直接放在桌面根）。
    """
    found: list[str] = []
    container = container_dir(root)
    if os.path.isdir(container):
        try:
            for entry in os.scandir(container):
                if entry.is_dir(follow_symlinks=False):
                    found.append(entry.path)
        except OSError:
            pass
    for name in category_names():     # 兼容还没迁移进容器的旧布局
        path = os.path.join(root, name)
        if os.path.isdir(path) and path not in found:
            found.append(path)
    return found


def cleanup_container(root: str) -> None:
    """恢复后顺手删掉空掉的分类夹和空容器。"""
    container = container_dir(root)
    if not os.path.isdir(container):
        return
    for name in category_names():
        path = os.path.join(container, name)
        try:
            if os.path.isdir(path) and not os.listdir(path):
                os.rmdir(path)
        except OSError:
            pass
    try:
        if not os.listdir(container):
            os.rmdir(container)
    except OSError:
        pass


# ---------------------------------------------------------------- 快捷方式分类
# 目标路径里出现这些片段 → 判定为「游戏」
_GAME_PATH_HINTS = (
    "steamapps", "steam://", "epic games", "wegame", "wegameapps",
    "battle.net", "uplay", "ubisoft", "gog galaxy", "\\games\\", "\\game\\",
    "riot games", "minecraft", "genshin", "hoyoplay",
)
# 文件名里出现这些 → 判定为「游戏」
_GAME_NAME_HINTS = ("游戏", "online", "launcher 游戏")
# 目标路径里出现这些 → 判定为「应用」
_APP_PATH_HINTS = (
    "program files", "appdata\\local", "appdata\\roaming",
    "\\microsoft\\", "\\windows\\", "\\bin\\",
)

_TARGET_CACHE: dict[str, str] = {}


def resolve_targets(paths) -> dict[str, str]:
    """批量解析快捷方式目标（.lnk 用 WScript.Shell，.url 直接读 ini）。

    批量做只初始化一次 COM —— 逐个解析 40+ 个 lnk 会慢到几秒。
    结果按路径缓存，第二次打开分区视图几乎无开销。
    """
    out: dict[str, str] = {}
    todo: list[str] = []
    for path in paths or []:
        cached = _TARGET_CACHE.get(path.casefold())
        if cached is None:
            todo.append(path)
        else:
            out[path] = cached
    if not todo:
        return out

    urls = [p for p in todo if p.casefold().endswith(".url")]
    links = [p for p in todo if not p.casefold().endswith(".url")]

    for path in urls:
        target = ""
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    text = line.strip()
                    if text[:4].casefold() == "url=":
                        target = text[4:].strip()
                        break
        except OSError:
            target = ""
        _TARGET_CACHE[path.casefold()] = target
        out[path] = target

    if links:
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            shell = None
            try:
                shell = win32com.client.Dispatch("WScript.Shell")
                for path in links:
                    target = ""
                    shortcut = None
                    try:
                        shortcut = shell.CreateShortCut(path)
                        target = str(shortcut.TargetPath or "")
                    except Exception:
                        target = ""
                    finally:
                        del shortcut          # 及时释放，否则 CoUninitialize 会报 IUnknown
                    _TARGET_CACHE[path.casefold()] = target
                    out[path] = target
            finally:
                del shell
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
        except Exception:
            for path in links:
                _TARGET_CACHE.setdefault(path.casefold(), "")
                out[path] = ""
    return out


def classify_shortcut(name: str, target: str = "") -> str:
    """把快捷方式分成「游戏」或「应用」（target 由 resolve_targets 批量给出）。"""
    low_name = (name or "").casefold()
    low_target = (target or "").casefold()

    if any(hint in low_target for hint in _GAME_PATH_HINTS):
        return "游戏"
    if any(hint in low_name for hint in _GAME_NAME_HINTS):
        return "游戏"
    if any(hint in low_target for hint in _APP_PATH_HINTS):
        return "应用"
    return "应用"
