"""开发模式专用：一键重置 —— 删除本程序的**全部**本地数据。

只在开发模式（未打包）开放入口。打包版误点一下就是几十 GB 模型 + 全部设置
全没，所以 `is_dev_mode()` 是硬门槛，设置页据此决定要不要显示这张卡片。

清理范围 = ``%LocalAppData%\\LyLaunch`` 下的所有一级条目：
配置（热键 / 固定 / 最近 / 引擎档位）、插件（用户副本 + 包库 + 各自数据目录）、
已下载模型与推理日志、崩溃日志、粘贴临时文件。

删完本进程必须退出：配置在内存里，继续跑会把刚删的东西又写回去。
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
import time
from pathlib import Path

from . import paths

# 一级条目 → 给人看的说明（确认框里逐条列）。没列到的一律按「插件配置」处理。
_LABELS: dict[str, str] = {
    "engine": "已下载的全部本地模型 + 推理日志",
    "models": "旧版模型目录残留",
    "runtime": "推理运行时缓存",
    "plugins": "已安装插件（用户副本 + 备份）",
    "plugin_packages": "插件包库（可安装的 .zip）",
    "translator_models": "离线翻译模型",
    "doc_core": "文档识别数据",
    "llm_chat": "本地 AI 会话与附件",
    "ocr": "图片转文字数据",
    "paste_tmp": "粘贴临时文件",
    "hotkey.json": "唤起热键",
    "screenshot_hotkey.json": "截图快捷键",
    "recents.json": "最近使用",
    "pinned.json": "固定项",
    "engine.json": "本地模型档位（auto / 1 / 2）",
    "config.json": "程序配置",
    "plugin_config.json": "插件全局配置",
    "plugins_installed.json": "插件安装记录",
    "crash.log": "崩溃日志",
    "plugin_errors.log": "插件错误日志",
}


def is_dev_mode() -> bool:
    """开发模式 = 没被 PyInstaller 打包（python main.py 直接跑）。"""
    return not getattr(sys, "frozen", False)


def data_dir() -> Path:
    return paths.DATA_DIR


def label_of(path: Path) -> str:
    name = path.name
    if name in _LABELS:
        return _LABELS[name]
    if name.startswith("plugins_") and name.endswith(".json"):
        return f"插件配置（{name[8:-5]}）"
    return "数据目录" if path.is_dir() else "配置文件"


def size_of(path: Path) -> int:
    """递归统计大小；符号/硬链接可能重复计入，只用于展示量级。"""
    try:
        if path.is_file():
            return path.stat().st_size
    except Exception:
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except Exception:
                pass
    return total


def plan() -> list[dict]:
    """要删的条目清单（按占用从大到小）。"""
    root = data_dir()
    if not root.is_dir():
        return []
    items: list[dict] = []
    try:
        entries = list(root.iterdir())
    except Exception:
        return []
    for p in entries:
        items.append({"path": p, "name": p.name, "label": label_of(p),
                      "size": size_of(p), "is_dir": p.is_dir()})
    items.sort(key=lambda it: it["size"], reverse=True)
    return items


def total_size() -> int:
    return sum(it["size"] for it in plan())


def _force_remove(func, path, exc) -> None:
    """rmtree 的错误回调：只读属性导致删不掉时先去掉只读位再试一次。"""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def _remove(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, onexc=_force_remove)
    else:
        try:
            p.unlink()
        except PermissionError:
            os.chmod(p, stat.S_IWRITE)
            p.unlink()


def shutdown_engine() -> None:
    """先把 Router 整棵树杀掉：模型文件被它 mmap 着，不停根本删不掉。"""
    try:
        from .engine import EngineManager
        EngineManager.instance().shutdown()
    except Exception:
        pass


def reset_all() -> tuple[bool, list[str]]:
    """删除 DATA_DIR 下全部条目（保留目录本身）。返回 (是否全成功, 失败说明)。"""
    shutdown_engine()
    failed: list[str] = []
    for item in plan():
        p: Path = item["path"]
        last_err: Exception | None = None
        for _ in range(3):
            try:
                _remove(p)
                last_err = None
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(0.4)      # 文件刚被关闭时常有几百毫秒的占用窗口
        if last_err is not None:
            failed.append(f"{p.name}：{last_err}")
    return not failed, failed
