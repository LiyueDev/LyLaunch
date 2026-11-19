"""插件 API：插件能调用的"系统能力"。

每个插件在 PluginContext.api 上拿到本类的实例。
这里集中暴露 LyLaunch 允许插件触碰的能力，是插件与基础功能之间的**唯一边界**。
后续加能力都在这一个类里扩展（改这里 = 给所有插件加权限）。

能力清单（v1 骨架）：
- clipboard      读/写剪贴板（复用主程序 ClipboardWatcher，写入不污染历史）
- settings       读/写插件自己的持久化配置（存 %LocalAppData%/LyLaunch/plugins_<id>.json）
- system_config  读全局插件系统配置（plugin_config.json）
- launch         启动应用 / 文件 / URL
- reveal         在资源管理器里定位文件
- notify         托盘气泡通知
"""

from __future__ import annotations

import os
import json
import subprocess
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..clipboard import ClipboardWatcher
    from .. import paths as _paths_mod


class PluginAPI:
    def __init__(self, plugin_id: str, clipboard: "ClipboardWatcher | None" = None) -> None:
        self._plugin_id = plugin_id
        self._clipboard = clipboard
        # 跨插件调用路由（由 PluginManager 注入）：plugin_id + query → 执行另一个插件。
        # 插件之间不直接 import 彼此，一律经此路由，保持隔离边界。
        self._plugin_router = None
        # 插件自身配置的持久化文件（路径在构造时按插件 id 解析）
        from .. import paths
        self._settings_file = paths.DATA_DIR / f"plugins_{plugin_id}.json"
        self._settings: dict[str, Any] = {}

    def invoke_plugin(self, plugin_id: str, query: str = "") -> bool:
        """调用另一个插件（如 OCR 识别完把文本交给翻译插件）。

        经 PluginManager 路由到目标插件的 on_execute(query)，成功返回 True。
        目标插件未安装/被禁用/抛异常时返回 False，不会让当前插件崩掉。
        """
        if self._plugin_router is None:
            return False
        try:
            return bool(self._plugin_router(plugin_id, query))
        except Exception:
            return False

    # ---------- 系统 ----------
    def refresh_index(self) -> bool:
        """让主窗口重建搜索索引。

        插件改动桌面结构后（比如把快捷方式收进文件夹），主程序手里的索引路径
        就失效了 —— 图标会退化成字母瓦片。调用本方法重新扫描桌面（含子目录）
        并刷新首屏。成功返回 True。
        """
        try:
            from PySide6.QtWidgets import QApplication
        except Exception:
            return False
        for widget in QApplication.topLevelWidgets():
            index = getattr(widget, "search_index", None)
            if index is None:
                continue
            refresh = getattr(widget, "refresh_list", None)
            try:
                index.build()
                if callable(refresh):
                    refresh()
                return True
            except Exception:
                return False
        return False

    # ---------- 剪贴板 ----------
    def clipboard_read(self) -> str:
        """读取当前剪贴板文本。"""
        try:
            from PySide6.QtWidgets import QApplication
            return QApplication.clipboard().text() or ""
        except Exception:
            return ""

    def clipboard_write(self, text: str) -> None:
        """把文本写入剪贴板（经过主程序 Watcher，不污染剪贴板历史）。"""
        if self._clipboard is not None:
            self._clipboard.write(text)
        else:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(text)

    def clipboard_write_image(self, image) -> None:
        """把图片（QPixmap / QImage）写入剪贴板。截图类插件用它。"""
        try:
            from PySide6.QtWidgets import QApplication
            cb = QApplication.clipboard()
            from PySide6.QtGui import QPixmap
            if isinstance(image, QPixmap):
                cb.setPixmap(image)
            else:
                cb.setImage(image)
        except Exception:
            pass

    # ---------- 插件自身配置 ----------
    def settings_get(self, key: str, default: Any = None) -> Any:
        """读插件自己的配置项（持久化在 %LocalAppData%/LyLaunch/plugins_<id>.json）。"""
        return self._settings.get(key, default)

    def settings_set(self, key: str, value: Any) -> None:
        """写插件自己的配置项并立即落盘。"""
        self._settings[key] = value
        self.settings_save()

    def settings_save(self) -> None:
        try:
            self._settings_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._settings_file, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def settings_load(self) -> dict[str, Any]:
        try:
            if self._settings_file.exists():
                with open(self._settings_file, "r", encoding="utf-8") as f:
                    self._settings = json.load(f)
        except Exception:
            self._settings = {}
        return self._settings

    # ---------- 启动 / 打开 ----------
    @staticmethod
    def launch(path_or_url: str, args: str | None = None) -> bool:
        """启动应用 / 文件 / URL。args 给系统应用用（如 ms-settings: 配合参数）。"""
        try:
            if args:
                subprocess.Popen([path_or_url, args], shell=True)
            else:
                os.startfile(path_or_url)  # type: ignore[attr-defined]
            return True
        except Exception:
            return False

    @staticmethod
    def reveal(path: str) -> bool:
        """在资源管理器里定位文件/目录。"""
        if path and os.path.exists(path):
            try:
                subprocess.Popen(f'explorer.exe /select,"{path}"', shell=True)
                return True
            except Exception:
                return False
        return False

    # ---------- 通知 ----------
    def notify(self, title: str, message: str = "") -> None:
        """托盘气泡通知。"""
        try:
            from PySide6.QtWidgets import QSystemTrayIcon
            if QSystemTrayIcon.isSystemTrayAvailable():
                QSystemTrayIcon.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 3000)
        except Exception:
            pass

    # ---------- 全局推理引擎（llama.cpp Router） ----------
    # 插件通过这几个方法查询/驱动统一 Router，而不是自己起 llama-server。
    @staticmethod
    def _engine():
        from liytools.engine import EngineManager
        return EngineManager.instance()

    def engine_status(self) -> dict:
        """{'running': bool, 'active': model_id|None, 'port': int, 'memory_mb': int}"""
        try:
            return self._engine().status()
        except Exception:
            return {"running": False, "active": None, "port": 0, "memory_mb": 0}

    def engine_active(self) -> str | None:
        """当前活跃（已加载）的模型 id。"""
        try:
            return self._engine().active()
        except Exception:
            return None

    def engine_ensure(self, model_id: str) -> bool:
        """确保 model_id 就绪（Router 负责卸载/加载调度）；成功 True。"""
        try:
            self._engine().ensure_model(model_id)
            return True
        except Exception:
            return False

    def engine_unload(self, model_id: str | None = None,
                      if_model: str | None = None) -> None:
        """卸载模型；if_model 用于防止误卸别的插件正在用的模型。"""
        try:
            self._engine().unload(model_id, if_model=if_model)
        except Exception:
            pass

    def engine_release_all(self) -> None:
        """卸载所有已加载模型（不杀 Router）。"""
        try:
            self._engine().unload()
        except Exception:
            pass

    def engine_touch(self, model_id: str, idle_seconds: int = 300) -> None:
        """记录一次使用时间，配合空闲释放。"""
        try:
            self._engine().touch(model_id, idle_seconds)
        except Exception:
            pass

    def engine_is_installed(self, model_id: str) -> bool:
        try:
            from liytools.engine import is_installed, spec_of
            return is_installed(spec_of(model_id))
        except Exception:
            return False

    def engine_specs(self) -> list:
        """全部模型规格（插件做模型列表用）。"""
        try:
            from liytools.engine import MODELS
            return list(MODELS)
        except Exception:
            return []

    def engine_remove_model(self, model_id: str) -> bool:
        """卸载并删除模型文件（先卸载规避 mmap 占用）。

        必须走 registry.remove_files：迁移用硬链接把旧目录的模型链进 engine/models，
        同一份文件有两个名字，只删 engine 那份会让 is_installed() 回退旧目录仍判"已装"。
        """
        try:
            from liytools.engine import registry
            ok, _msg = registry.remove_files(model_id)
            return ok
        except Exception:
            return False
