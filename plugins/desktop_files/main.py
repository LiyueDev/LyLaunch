"""桌面净化 —— 插件入口：热键、窗口复用、退出兜底。

窗口结构（只有一套）
--------------------
    Alt+D / 插件入口 → Plugin.on_execute → _open_partition → PartitionView
    已存在就 show/hide 复用（close 会杀掉后台图标线程，重开要重建整个 UI）。

为什么删掉了旧的列表窗口
------------------------
0.42 把「收纳容器 + 分区视图」合并成一个主界面后，旧的 DesktopFilesDialog
不再有任何入口，却还在维护第二套扫描 / 排序 / 渲染 / 状态判断。现已删掉，
配置项与状态只剩分区视图这一份；它独有的「隐藏全部桌面图标」收进分区视图
右键菜单，能力不丢。

五层恢复（隐藏图标后一定能回来）
--------------------------------
    L1 分区视图右键 →「恢复桌面图标」
    L2 QApplication.aboutToQuit（正常退出）
    L3 on_unload（设置页禁用 / 卸载插件）
    L4 下次 on_load 读标记文件自动恢复（崩溃 / 断电）
    L5 桌面右键 → 查看 → 显示桌面图标（系统原生兜底）
    另有 CLI 兜底：python desktop_icons.py show

⚠️ 入口禁用 @dataclass（插件动态加载不注册 sys.modules，Python 3.14 会加载失败）。
"""

from __future__ import annotations

import json
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from lylaunch.plugins.base import PluginBase

# 插件目录已被 manager 插入 sys.path；单独跑本文件时补一次
try:
    import desktop_icons as di
    import ops
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import desktop_icons as di
    import ops


# ---------------------------------------------------------------- 单例
_PARTITION = None      # 分区视图单例（partition_view.PartitionView）
_SETTINGS = None       # 插件内设置页单例（settings_dialog.SettingsWindow）
_QUIT_HOOKED = False


def _restore_on_app_quit() -> None:
    """L2：退出程序时恢复。

    只做文件属性 / 注册表 + 文件 IO，绝不碰任何控件。

    注意：fences（收纳）模式**不**在退出时移回 —— 文件就摆在桌面的分类夹里，
    随时可见可访问，属于"常驻整理"而不是"隐藏"，移回反而让收纳白做。
    """
    try:
        ops.restore_icons_on_exit()
    except Exception:
        pass
    try:
        mark = di.read_mark()
        if not mark.get("hidden_by_us"):
            return
        if str(mark.get("mode") or "") == "fences":
            return
        di.force_show_all()
    except Exception:
        pass


def _make_hotkey_setter(ctx):
    """返回一个 spec -> (ok, msg) 的回调，供窗口里那颗「快捷键」按钮使用。

    写回 plugin.json 的 Hotkey、同步内存 manifest，再让主窗口按新清单重注册
    （立即生效，不用重启）。
    """
    def _set(spec: str):
        from lylaunch.hotkey import parse_hotkey
        if parse_hotkey(spec) is None:
            return False, "无效的按键组合（至少带一个 Ctrl / Alt / Shift / Win）"
        path = os.path.join(ctx.plugin_dir, "plugin.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data["Hotkey"] = spec
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            return False, f"保存失败：{exc}"
        ctx.manifest.hotkey = spec
        main = None
        for w in QApplication.topLevelWidgets():
            if callable(getattr(w, "refresh_plugin_hotkeys", None)):
                main = w
                break
        refresh = getattr(main, "refresh_plugin_hotkeys", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                pass
        return True, f"快捷键已改为 {spec}，立即生效"

    return _set


def _open_partition(ctx) -> None:
    """开关「桌面净化」主窗口（已存在就 hide/show 复用，快捷键再按一次 = 关闭）。

    不用 close 销毁：close 会杀掉后台图标线程，下次打开要重建整个 UI（卡几秒）；
    hide 只藏窗口，二次打开零成本 —— 真正退出程序时随进程一起销毁。
    reload 放到 show 之前：先显示上次的内容，再刷新数据，呼出不阻塞。
    """
    global _PARTITION
    # 设置页里刚切了皮肤：磨砂/模糊参数只能在窗口构造期定，直接整窗重建
    if _PARTITION is not None:
        try:
            desired = str(ctx.api.settings_get("skin", "") or "")
            current = getattr(getattr(_PARTITION, "_skin", None), "name", "")
            if desired and current and desired != current:
                _close_partition()
        except Exception:
            pass
    if _PARTITION is not None:
        try:
            if _PARTITION.isVisible():
                _PARTITION.hide()
                return
            from partition_view import desktop_signature
            # 内容没变 → 直接 show（零卡顿零闪烁）；变了 → 隐藏状态下
            # 重建完再显示（setUpdatesEnabled 兜住中间态，不会闪出错乱）。
            # 刷新/取签名失败也必须把窗口显出来：不能因为一次扫描异常，
            # 让 Alt+D 看起来「按了没反应」。
            try:
                if getattr(_PARTITION, "_last_signature", None) != desktop_signature():
                    _PARTITION.setUpdatesEnabled(False)
                    try:
                        _PARTITION.reload()
                    finally:
                        _PARTITION.setUpdatesEnabled(True)
            except Exception:
                pass
            _PARTITION.show()
            _PARTITION.raise_()
            _PARTITION.activateWindow()
            return
        except RuntimeError:
            _PARTITION = None
    try:
        from partition_view import PartitionView
    except Exception:
        return
    _PARTITION = PartitionView(ctx.api, hotkey_getter=lambda: ctx.manifest.hotkey or "",
                               hotkey_setter=_make_hotkey_setter(ctx))
    _PARTITION._on_open_settings = lambda: _open_settings(ctx)
    _PARTITION.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    _PARTITION.destroyed.connect(
        lambda *_, panel=_PARTITION: _forget_partition(panel))
    _PARTITION.show()
    _PARTITION.raise_()
    _PARTITION.activateWindow()


def _close_partition() -> None:
    """关掉当前面板并清空引用（切皮肤时用，几何/收纳状态都已在磁盘上）。"""
    global _PARTITION
    if _PARTITION is not None:
        try:
            _PARTITION.close()
        except Exception:
            pass
    _PARTITION = None


def _apply_skin(ctx, name: str) -> None:
    """设置页切换皮肤：写配置 → 关旧面板 → 按新皮肤重建并显示。"""
    try:
        ctx.api.settings_set("skin", name)
    except Exception:
        pass
    _close_partition()
    _open_partition(ctx)


def _open_settings(ctx) -> None:
    """插件内设置页（非模态，可和面板同时开着）。"""
    global _SETTINGS
    if _SETTINGS is not None:
        try:
            _SETTINGS.show()
            _SETTINGS.raise_()
            _SETTINGS.activateWindow()
            return
        except RuntimeError:
            _SETTINGS = None
    try:
        from settings_dialog import SettingsWindow
    except Exception:
        return
    _SETTINGS = SettingsWindow(lambda: _PARTITION, ctx.api,
                               on_switch_skin=lambda name: _apply_skin(ctx, name))
    _SETTINGS.show()
    # 面板是独立顶层窗口，谁最后 show 谁在上面：设置页新建后要显式抬到前面，
    # 否则它可能被还开着的面板挡住，看起来像"设置打不开 / 点不动"。
    _SETTINGS.raise_()
    _SETTINGS.activateWindow()


def _forget_partition(panel) -> None:
    """旧面板被销毁时才清空引用。

    切皮肤时是「先 close 旧面板、再建新面板」，而 WA_DeleteOnClose 的销毁事件
    是延后到事件循环里的 —— 若无条件 `_PARTITION = None`，就会把刚建好的新面板
    一起抹掉（表现是设置页切完皮肤后 Alt+D 没反应）。
    """
    global _PARTITION
    if _PARTITION is panel:
        _PARTITION = None


class Plugin(PluginBase):
    def on_load(self) -> None:
        global _QUIT_HOOKED
        app = QApplication.instance()
        if app is not None and not _QUIT_HOOKED:
            app.aboutToQuit.connect(_restore_on_app_quit)
            _QUIT_HOOKED = True
        # L4：异常退出后的处理
        try:
            if ops.read_icons_flag():
                # 上次退出时桌面图标是我们藏起来的 → 现在补回
                ok, _message = ops.set_desktop_icons_hidden(False)
                if ok:
                    self.ctx.api.notify("桌面净化", "上次退出时桌面图标处于隐藏状态，已自动恢复显示")
            mark = di.read_mark()
            if mark.get("hidden_by_us"):
                if str(mark.get("mode") or "") == "fences":
                    # 收纳是常驻语义：重启后保持不动（不自动弹窗，避免打扰）。
                    # 只有当分类夹已经被用户删掉时，标记才作废。
                    folders = [str(p) for p in (mark.get("folders") or [])]
                    if not any(os.path.isdir(f) for f in folders):
                        di.write_mark(False)
                elif bool(self.ctx.settings.get("auto_restore_on_exit", True)):
                    if di.restore_if_marked():
                        self.ctx.api.notify("桌面净化", "上次退出时桌面图标处于隐藏状态，已自动恢复显示")
        except Exception:
            pass
        # 图标缓存预热：插件常驻加载时后台把桌面图标烤进缓存，
        # 第一次按 Alt+D 打开面板也是现成图标（否则要 0.4~1.2s 才补满）。
        try:
            from partition_view import prewarm_icon_cache
            prewarm_icon_cache()
        except Exception:
            pass

    def on_unload(self) -> None:
        # L3：设置页禁用 / 卸载插件时恢复
        global _QUIT_HOOKED
        app = QApplication.instance()
        if app is not None and _QUIT_HOOKED:
            try:
                app.aboutToQuit.disconnect(_restore_on_app_quit)
            except Exception:
                pass
            _QUIT_HOOKED = False
        try:
            ops.set_desktop_icons_hidden(False)
        except Exception:
            pass
        try:
            if di.read_mark().get("hidden_by_us"):
                di.force_show_all()
        except Exception:
            pass

    def on_execute(self, query: str = "") -> None:
        # 桌面净化只有一套界面：主入口直接打开分区视图
        _open_partition(self.ctx)
