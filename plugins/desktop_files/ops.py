"""收纳 / 恢复的核心操作 —— 插件窗口与「桌面净化」主窗口共用。

只负责文件移动与标记，不碰任何 UI；调用方拿到 (ok, message) 后
各自决定怎么展示。
"""

from __future__ import annotations

import json
import os
import shutil

try:
    import fences
    import desktop_icons as di
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fences
    import desktop_icons as di

_SKIP_NAMES = {"desktop.ini", "thumbs.db"}
_SHORTCUT_SUFFIXES = (".lnk", ".url")


def desktop_dir() -> str:
    """当前用户的桌面目录。"""
    from win32com.shell import shell, shellcon

    return shell.SHGetKnownFolderPath(shellcon.FOLDERID_Desktop)


def public_desktop_dir() -> str:
    """所有用户的公共桌面目录。"""
    from win32com.shell import shell, shellcon

    return shell.SHGetKnownFolderPath(shellcon.FOLDERID_PublicDesktop)


def collect(root: str) -> tuple[bool, str]:
    """把桌面上的所有项收进「桌面净化」容器（只移动，不隐藏）。"""
    container = fences.container_dir(root)
    try:
        os.makedirs(container, exist_ok=True)
    except OSError as exc:
        return False, f"无法创建收纳容器：{exc}"

    mark = di.read_mark()
    moved_records = list(mark.get("moved") or [])

    # 旧布局迁移：桌面根上的分类夹整体搬进容器，并修正已记录的 to 路径
    for folder in fences.managed_folders(root):
        if os.path.dirname(folder).casefold() != root.casefold():
            continue                      # 已经在容器里
        dest = os.path.join(container, os.path.basename(folder))
        if os.path.exists(dest):
            continue
        try:
            shutil.move(folder, dest)
        except Exception:
            continue
        prefix = (folder + os.sep).casefold()
        for item in moved_records:
            to = str(item.get("to") or "")
            if to.casefold().startswith(prefix):
                item["to"] = dest + to[len(folder):]

    # 桌面根上除容器以外的一切（文件 / 文件夹 / 快捷方式）；白名单内的跳过
    whitelist = load_whitelist()
    targets: list[str] = []
    try:
        with os.scandir(root) as iterator:
            for entry in iterator:
                low = entry.name.casefold()
                if low in _SKIP_NAMES or low == fences.CONTAINER_NAME.casefold():
                    continue
                if entry.name in whitelist:
                    continue                    # 用户主动固定的，留在桌面
                targets.append(entry.path)
    except OSError:
        pass

    # 公共桌面的快捷方式：普通用户没有写属性权限，但能删除/移动，一并收进来
    try:
        pub = public_desktop_dir()
    except Exception:
        pub = ""
    if pub and os.path.isdir(pub) and pub.casefold() != root.casefold():
        try:
            with os.scandir(pub) as iterator:
                for entry in iterator:
                    low = entry.name.casefold()
                    if low in _SKIP_NAMES:
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if low.endswith(_SHORTCUT_SUFFIXES):
                        targets.append(entry.path)
        except OSError:
            pass

    if not targets:
        di.write_mark(True, mode="fences", moved=moved_records,
                      folders=fences.managed_folders(root))
        return True, "桌面上没有需要收纳的内容"

    moved, failed, _used = fences.move_files(targets, container)
    moved_records.extend(moved)
    folders_now = fences.managed_folders(root)
    di.write_mark(True, mode="fences", moved=moved_records, folders=folders_now)

    di.arrange_desktop_icons()
    di.refresh_shell(root, deep=True)

    if failed:
        return False, f"{len(failed)} 项未能移动（可能被占用或没有权限）"
    categories = sorted({str(item.get("category") or "") for item in moved})
    return True, (
        f"已收纳 {len(moved)} 项到「{fences.CONTAINER_NAME}」，"
        f"分类：{'、'.join(c for c in categories if c)}"
    )


def restore(root: str) -> tuple[bool, str]:
    """把容器里的所有项移回桌面原位，清理空容器。"""
    container = fences.container_dir(root)
    mark = di.read_mark()
    moved = mark.get("moved") or []
    di.clear_hidden(container)          # 历史版本可能给容器加过隐藏属性，先清掉
    if not moved:
        di.write_mark(False)
        return True, "没有需要移回的内容"

    restored, failed, missing = fences.restore(moved) if moved else ([], [], [])
    fences.cleanup_container(root)
    fences.cleanup_empty_folders(
        [fences.folder_path(root, name) for name in fences.category_names()]
    )
    di.write_mark(False)
    di.arrange_desktop_icons()
    di.refresh_shell(root, deep=True)

    if failed:
        return False, f"{len(failed)} 项未能移回原位（可能被占用）"
    note = f"，另有 {len(missing)} 项已被手动移走" if missing else ""
    return True, f"已移回 {len(restored)} 项{note}"


def collected_count(root: str) -> int:
    """当前已收纳的项数（用于按钮状态）。"""
    mark = di.read_mark()
    if not mark.get("hidden_by_us"):
        return 0
    return len(mark.get("moved") or [])


# ---------------------------------------------------------------- 桌面图标隐藏 / 显示
# 和收纳标记（desktop_files_state.json）**分开存**：
# 收纳标记里的 moved 是「文件搬哪去了」的账本，藏图标时绝不能覆盖它，
# 否则「恢复原状」会失去记录。这里只记一个布尔：桌面图标是不是我们藏的。
ICONS_FLAG_NAME = "desktop_files_icons.json"


def icons_flag_path() -> str:
    return os.path.join(os.path.dirname(di.state_path()), ICONS_FLAG_NAME)


def read_icons_flag() -> bool:
    try:
        with open(icons_flag_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return bool(data.get("hidden")) if isinstance(data, dict) else False
    except Exception:
        return False


def write_icons_flag(hidden: bool) -> None:
    """原子写标记（先写临时文件再 replace，断电不会留下半个文件）。"""
    try:
        path = icons_flag_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"hidden": bool(hidden)}, handle)
        os.replace(tmp, path)
    except Exception:
        pass


def set_desktop_icons_hidden(desired: bool, allow_fallback: bool = False) -> tuple[bool, str]:
    """系统级隐藏 / 显示全部桌面图标（右键菜单那两项）。

    返回 ``(ok, message)``。藏图标只改注册表 / 发翻转命令，不动任何文件，
    也**不写**收纳标记 —— 两套状态互不干扰。
    """
    ok, immediate, message = di.set_hidden(
        bool(desired), allow_showwindow_fallback=bool(allow_fallback))
    if not ok:
        return False, message or "切换失败：请右键桌面 → 查看 → 显示桌面图标"
    write_icons_flag(bool(desired))
    if not immediate:
        return True, message or "已记录，桌面未即时刷新（按 F5 试试）"
    if desired:
        return True, "全部桌面图标已隐藏（退出程序会自动恢复）"
    return True, "桌面图标已恢复显示"


def restore_icons_on_exit() -> bool:
    """退出兜底：上次是我们藏的才放回来；返回是否真的做了恢复。"""
    if not read_icons_flag():
        return False
    ok, _message = set_desktop_icons_hidden(False)
    return ok


# ---------------------------------------------------------------- 插件数据 / 白名单
def plugin_data_file() -> str:
    from lylaunch import paths

    return paths.DATA_DIR / "plugins_desktop_files.json"


def load_plugin_data() -> dict:
    """读插件持久化数据（瓦片顺序 / 桌面白名单都在这里）。"""
    import json

    try:
        data = json.loads(plugin_data_file().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_plugin_data(data: dict) -> None:
    import json

    file = plugin_data_file()
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_whitelist() -> set[str]:
    """桌面白名单（按文件名匹配）：用户主动放在桌面上、拒绝被收纳的项。"""
    wl = load_plugin_data().get("desktop_whitelist")
    return {str(x) for x in wl} if isinstance(wl, list) else set()


def save_whitelist(names) -> None:
    data = load_plugin_data()
    data["desktop_whitelist"] = sorted(names)
    save_plugin_data(data)
