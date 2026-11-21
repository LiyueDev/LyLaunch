"""插件管理器：插件体系的"总管"。

职责：
- 扫描所有插件目录（内置 plugins/ + 用户 %LocalAppData%/LyLaunch/plugins/ + 额外路径），
  解析每个插件的 plugin.json 生成 PluginInfo
- 依据注册表 plugins_installed.json 判断启用/禁用
- 加载启用插件：importlib 动态导入入口脚本，实例化 entry_class，调 on_load()
- 提供 enable / disable / uninstall 给设置页和将来的插件安装器调用
- 把插件清单转成 Item 交给主窗口渲染"插件"区块

隔离约定：
- 主窗口只通过 PluginManager 拿数据，不直接 import 任何插件代码；
- 插件代码也只通过 PluginAPI 碰系统，不 import 主窗口/基础模块。
- 插件目录名 = 插件 Id，两者必须一致，否则忽略该插件。

v1 骨架说明：扫描/加载/启用/禁用/卸载已可跑通；
"插件搜索结果接管""插件常驻界面"按 config.py 里的契约后续补。
"""

from __future__ import annotations

import os
import sys
import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import paths
from ..item import Item
from ..pinyin import get_pinyin
from ..search_rank import rank_score
from .config import PluginManifest, PluginSystemConfig, InstalledPlugin
from . import registry
from .base import PluginBase, PluginContext
from .api import PluginAPI

# 插件区块在首页的专用配色（与基础功能三区块区分）
PLUGIN_TILE_COLOR = "#7C5CFF"   # 紫色，一眼能分出"这是插件"


@dataclass
class PluginInfo:
    """一次扫描得到的插件元信息（给 UI / 管理器用，不含运行时实例）。"""
    manifest: PluginManifest
    directory: str
    source: str = "user"          # local=内置 / user=用户安装 / extra=额外扫描路径
    enabled: bool = True          # 是否启用（来自注册表/全局禁用列表）
    loaded: bool = False          # 是否已加载成实例
    error: str = ""               # 加载失败时的原因（设置页展示）

    @property
    def id(self) -> str:
        return self.manifest.id


class PluginManager:
    """插件管理器（单例，全程序共享一份）。"""

    _instance: Optional["PluginManager"] = None

    @classmethod
    def instance(cls) -> "PluginManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._cfg: PluginSystemConfig = registry.load_system_config()
        self._plugins: dict[str, PluginInfo] = {}
        self._loaded: dict[str, PluginBase] = {}
        self._clipboard = None
        # 先扫一次才知道哪些已经装了；装完再扫一次，新插件才能进清单被 load_all 加载
        self.scan()
        try:
            self.auto_install_packages()
        except Exception:
            pass
        self.scan()

    # ---------- 扫描 ----------
    def scan(self) -> None:
        """扫描全部插件目录，重建插件清单。任何目录异常都不影响其它目录。"""
        self._plugins.clear()
        roots: list[tuple[str, Path]] = []
        # 扫描顺序决定同 id 冲突时谁胜出（下面 self._plugins[info.id] = info 后者覆盖前者）。
        # 内置(local)必须最后扫：随程序分发的插件是权威版本，用户数据目录里可能残留
        # 旧版同名插件（如 1.0 之前手动装的 doc_recognize/llm_chat，内含已废弃的
        # liytools 包名），若让 user 覆盖 local，内置的新版会被旧版遮蔽而加载失败。
        roots.append(("user", Path(paths.USER_PLUGINS_DIR)))
        for extra in self._cfg.scan_paths:
            p = Path(extra)
            if p.is_dir():
                roots.append(("extra", p))
        if Path(paths.BUILTIN_PLUGINS_DIR).is_dir():
            roots.append(("local", Path(paths.BUILTIN_PLUGINS_DIR)))

        for source, root in roots:
            try:
                children = sorted(root.iterdir(), key=lambda x: x.name.lower())
            except Exception:
                continue
            for child in children:
                if not child.is_dir():
                    continue
                info = self._read_manifest(source, child)
                if info is not None:
                    self._plugins[info.id] = info

    def _read_manifest(self, source: str, plugin_dir: Path) -> Optional[PluginInfo]:
        """读一个插件目录的 plugin.json，做基本校验。返回 None 表示不是合法插件。"""
        try:
            manifest_file = plugin_dir / "plugin.json"
            if not manifest_file.exists():
                return None
            with open(manifest_file, "r", encoding="utf-8") as f:
                d = __import__("json").load(f)
            manifest = PluginManifest.from_dict(d if isinstance(d, dict) else {})
        except Exception:
            return None
        # 校验：目录名 = 插件 Id（防重名、防大小写混乱）
        if not manifest.id or plugin_dir.name.lower() != manifest.id.lower():
            return None
        if not manifest.name:
            return None
        # 启用状态：注册表记录优先；全局禁用列表其次；默认启用
        installed = registry.get_installed(manifest.id)
        enabled = installed.enabled if installed is not None else True
        if manifest.id in self._cfg.disabled_plugins:
            enabled = False
        return PluginInfo(
            manifest=manifest,
            directory=str(plugin_dir),
            source=source,
            enabled=enabled,
        )

    # ---------- 只读查询 ----------
    @property
    def config(self) -> PluginSystemConfig:
        return self._cfg

    def all_plugins(self) -> list[PluginInfo]:
        """所有已识别的插件（含禁用）。"""
        return list(self._plugins.values())

    def enabled_plugins(self) -> list[PluginInfo]:
        """已启用插件（首页/搜索只用这些）。"""
        return [p for p in self._plugins.values() if p.enabled]

    def get(self, plugin_id: str) -> Optional[PluginInfo]:
        return self._plugins.get(plugin_id)

    def is_running(self) -> bool:
        return self._cfg.enabled

    # ---------- 加载 / 卸载 ----------
    def load_all(self, clipboard=None) -> int:
        """加载所有启用插件，返回成功数。clipboard 传主程序的 ClipboardWatcher，
        让插件写入剪贴板时不污染历史；不传则用系统剪贴板。"""
        self._clipboard = clipboard
        count = 0
        if not self._cfg.enabled:
            return 0
        for info in self.enabled_plugins():
            if self._load_plugin(info):
                count += 1
        return count

    def _load_plugin(self, info: PluginInfo) -> bool:
        """动态导入单个插件：加载 main.py，实例化 entry_class，调 on_load。"""
        if info.id in self._loaded:
            return True
        try:
            main_path = os.path.join(info.directory, info.manifest.main)
            if not os.path.exists(main_path):
                info.error = f"入口文件不存在: {info.manifest.main}"
                return False
            # 把插件目录临时加入 sys.path，允许插件内 import 同目录模块
            if info.directory not in sys.path:
                sys.path.insert(0, info.directory)
            spec = importlib.util.spec_from_file_location(
                f"lylaunch_plugin_{info.id}", main_path)
            if spec is None or spec.loader is None:
                info.error = "入口脚本解析失败"
                return False
            module = importlib.util.module_from_spec(spec)
            # ⚠️ 必须注册进 sys.modules：插件入口里若用了 @dataclass，
            # Python 3.14 的 dataclasses 解析字段类型时会查
            # sys.modules[cls.__module__].__dict__；没注册就拿到 None →
            # AttributeError: 'NoneType' object has no attribute '__dict__'，
            # 插件在**加载阶段**就炸，用户看到的是"插件打不开"（2026-11-05 踩过）。
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            cls = getattr(module, info.manifest.entry_class)
            api = PluginAPI(info.id, self._clipboard)
            api._plugin_router = self.execute  # 注入跨插件调用路由（供 api.invoke_plugin 用）
            api.settings_load()
            ctx = PluginContext(info.manifest, info.directory, api)
            plugin = cls(ctx)
            plugin.on_load()
            self._loaded[info.id] = plugin
            info.loaded = True
            info.error = ""
            return True
        except Exception as e:
            info.loaded = False
            info.error = f"{type(e).__name__}: {e}"
            # 加载失败别把半成品模块留在 sys.modules 里
            sys.modules.pop(f"lylaunch_plugin_{info.id}", None)
            return False

    def unload_plugin(self, info: PluginInfo) -> None:
        """卸载单个插件实例（禁用它之前调用，确保 on_unload 被执行）。"""
        plugin = self._loaded.pop(info.id, None)
        if plugin is not None:
            try:
                plugin.on_unload()
            except Exception:
                pass
        info.loaded = False

    def execute(self, plugin_id: str, query: str = "") -> bool:
        """用户选中插件时执行（主窗口点中 plugin:// 瓦片会调这里）。"""
        if not self._cfg.enabled:
            return False
        plugin = self._loaded.get(plugin_id)
        if plugin is None:
            info = self._plugins.get(plugin_id)
            if info is not None and info.enabled:
                self._load_plugin(info)
                plugin = self._loaded.get(plugin_id)
        if plugin is None:
            return False
        try:
            plugin.on_execute(query)
            return True
        except BaseException as e:
            # 用 BaseException 而非 Exception：捕获 SystemExit/KeyboardInterrupt，
            # 避免异常逃逸到 Qt 事件循环触发 Qt abort（表现为"软件直接消失"无报错）。
            info = self._plugins.get(plugin_id)
            if info is not None:
                info.error = f"{type(e).__name__}: {e}"
            self._log_error(plugin_id, e)
            return False

    @staticmethod
    def _log_error(plugin_id: str, exc: BaseException) -> None:
        """把插件执行异常写进 %LocalAppData%/LyLaunch/plugin_errors.log，便于排查。"""
        import datetime
        import traceback
        try:
            from .. import paths
            paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(paths.DATA_DIR / "plugin_errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n==== {datetime.datetime.now().isoformat(timespec='seconds')} "
                        f"plugin={plugin_id} ====\n")
                f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        except Exception:
            pass

    # ---------- 默认全装（包库里未安装的插件）
    def auto_install_packages(self) -> list[str]:
        """把包库里还没装的插件全部装上（首次启动 / 重置后即等于「全部安装」）。

        用户**主动卸载**过的插件记在 ``PluginSystemConfig.uninstalled_plugins``，
        这里跳过 —— 否则「卸载」会变成「下次启动又回来」，那就没法真卸了。
        重置数据后 plugin_config.json 一起被删，名单清空，于是又回到默认全装。
        """
        done: list[str] = []
        skip = set(self._cfg.uninstalled_plugins or [])
        try:
            pkgs = self.list_packages()
        except Exception:
            return done
        for pkg in pkgs:
            pid = str(pkg.get("id") or "")
            if not pid or pkg.get("installed") or pid in skip:
                continue
            # 已有内置同 id 插件时跳过：包库里可能残留旧版 zip（如 1.0 之前的
            # doc_recognize/llm_chat，内含废弃的 liytools 包名），一旦装到用户目录
            # 就会遮蔽内置新版。既然程序已随包分发该插件，就没有再装的必要。
            current = self._plugins.get(pid)
            if current is not None and current.source == "local":
                continue
            try:
                ok, _msg = self.install_from_zip(pkg["path"])
            except Exception:
                continue
            if ok:
                done.append(pid)
        return done

    def _remember_uninstalled(self, plugin_id: str) -> None:
        """记下「用户主动卸载过」，让默认全装别再把它装回来。"""
        lst = list(self._cfg.uninstalled_plugins or [])
        if plugin_id in lst:
            return
        lst.append(plugin_id)
        self._cfg.uninstalled_plugins = lst
        registry.save_system_config(self._cfg)

    def _forget_uninstalled(self, plugin_id: str) -> None:
        """用户手动装回来了 → 从卸载名单里去掉。"""
        lst = list(self._cfg.uninstalled_plugins or [])
        if plugin_id not in lst:
            return
        self._cfg.uninstalled_plugins = [x for x in lst if x != plugin_id]
        registry.save_system_config(self._cfg)

    # ---------- 启用 / 禁用 / 卸载 ----------
    def enable(self, plugin_id: str) -> bool:
        info = self._plugins.get(plugin_id)
        if info is None:
            return False
        registry.upsert_installed(InstalledPlugin(
            id=info.id, version=info.manifest.version, enabled=True,
            source=info.source, path=info.directory))
        info.enabled = True
        return True

    def disable(self, plugin_id: str) -> bool:
        info = self._plugins.get(plugin_id)
        if info is None:
            return False
        self.unload_plugin(info)  # 先卸载，确保 on_unload 执行
        registry.upsert_installed(InstalledPlugin(
            id=info.id, version=info.manifest.version, enabled=False,
            source=info.source, path=info.directory))
        info.enabled = False
        return True

    # ---------- 安装 / 卸载 ----------
    def install_from_dir(self, src_dir: str) -> tuple[bool, str]:
        """从本地目录安装一个插件到用户插件目录。

        返回 (成功, 错误信息 / 成功时返回插件 Id)。
        插件必须是 <id>/plugin.json + 代码 的结构。
        """
        import json as _json
        src = Path(src_dir)
        if not src.is_dir():
            return False, "目录不存在"
        manifest_file = src / "plugin.json"
        if not manifest_file.exists():
            return False, "目录里没有 plugin.json，不是合法插件"
        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                manifest = PluginManifest.from_dict(_json.load(f))
        except Exception:
            return False, "plugin.json 解析失败"
        if not manifest.id:
            return False, "plugin.json 缺少 Id 字段"
        existing = self._plugins.get(manifest.id)
        if existing is not None and existing.source == "local":
            return False, f"「{existing.manifest.name}」是内置插件，不能覆盖安装"
        target = Path(paths.USER_PLUGINS_DIR) / manifest.id
        import shutil
        # 已装过同 id → 视为升级：先卸掉旧目录（插件配置存在 DATA_DIR，不受影响）
        if existing is not None:
            self.unload_plugin(existing)
            shutil.rmtree(existing.directory, ignore_errors=True)
        try:
            shutil.copytree(src, target, dirs_exist_ok=True)
        except Exception as e:
            return False, f"复制插件目录失败：{e}"
        self.scan()
        info = self.get(manifest.id)
        if info is not None:
            self.enable(manifest.id)
        self._forget_uninstalled(manifest.id)      # 手动装回来 → 从卸载名单移除
        return True, manifest.id

    def install_from_zip(self, zip_path: str) -> tuple[bool, str]:
        """从 zip 包安装插件。

        支持两种包内结构：
        1. zip 根目录就是插件（里面有 plugin.json）
        2. zip 第一层是个目录，里面才是插件（常见压缩工具打包样式）
        """
        import zipfile
        import shutil
        import tempfile
        if not os.path.isfile(zip_path):
            return False, "zip 文件不存在"
        try:
            zf = zipfile.ZipFile(zip_path)
        except Exception:
            return False, "zip 文件无法打开"
        tmp = Path(tempfile.mkdtemp(prefix="lylaunch_plugin_"))
        try:
            zf.extractall(tmp)
        except Exception as e:
            shutil.rmtree(tmp, ignore_errors=True)
            return False, f"解压失败：{e}"

        # 找含 plugin.json 的插件根目录
        candidates = []
        if (tmp / "plugin.json").exists():
            candidates.append(tmp)
        for child in tmp.iterdir():
            if child.is_dir() and (child / "plugin.json").exists():
                candidates.append(child)
        if not candidates:
            shutil.rmtree(tmp, ignore_errors=True)
            return False, "zip 里没找到 plugin.json，不是合法插件包"

        # 用第一个候选（结构简单，v1 不做多选）
        ok, msg = self.install_from_dir(str(candidates[0]))
        shutil.rmtree(tmp, ignore_errors=True)
        if ok:
            _keep_package(Path(zip_path))
        return ok, msg

    # ---------- 插件包库（卸载后还能装回来） ----------
    def list_packages(self) -> list[dict]:
        """列出本地插件包库里的 zip（不安装），给设置页"从本地包安装"用。"""
        out: list[dict] = []
        try:
            files = sorted(paths.PLUGIN_PACKAGES_DIR.glob("*.zip"))
        except Exception:
            return out
        for f in files:
            manifest = _read_zip_manifest(f)
            if not manifest:
                continue
            pid = str(manifest.get("Id", "")).strip()
            if not pid:
                continue
            installed = self._plugins.get(pid)
            out.append({
                "path": str(f),
                "id": pid,
                "name": str(manifest.get("Name") or pid),
                "version": str(manifest.get("Version", "")),
                "installed": installed is not None,
                "installed_version": installed.manifest.version if installed else "",
            })
        return out

    def uninstall(self, plugin_id: str, purge_data: bool = False) -> bool:
        """卸载插件：删目录 + 清注册表。内置插件(local)不允许删除，只可禁用。

        purge_data=True 时，连同清单里 `Data` 声明的数据目录一起删
        （典型是模型/引擎这种几百 MB 的东西），默认保留，重装就不用重下。
        """
        info = self._plugins.get(plugin_id)
        if info is None:
            return False
        if info.source == "local":
            return False  # 内置插件随程序走，不提供物理卸载
        self.unload_plugin(info)
        try:
            import shutil
            shutil.rmtree(info.directory, ignore_errors=True)
        except Exception:
            pass
        registry.remove_installed(plugin_id)
        self._remember_uninstalled(plugin_id)   # 默认全装要跳过它，否则卸载=下次复活
        self._plugins.pop(plugin_id, None)
        if purge_data:
            self.purge_data(info)
        return True

    # ---------- 插件数据（模型/缓存）清理 ----------
    def _data_paths(self, info: PluginInfo) -> list[Path]:
        """把清单里声明的相对路径解析成绝对路径，并挡掉越界（../ 之类）。"""
        root = Path(paths.DATA_DIR).resolve()
        out: list[Path] = []
        for rel in info.manifest.data_dirs:
            try:
                p = (root / rel).resolve()
                p.relative_to(root)      # 不在 LyLaunch 目录里的一律忽略
            except Exception:
                continue
            out.append(p)
        return out

    def plugin_data_size(self, plugin_id: str) -> int:
        """插件声明的数据占用（字节），用于卸载时提示"要不要一起删"。"""
        info = self._plugins.get(plugin_id)
        if info is None:
            return 0
        total = 0
        for p in self._data_paths(info):
            if p.is_dir():
                for root, _dirs, files in os.walk(p):
                    for name in files:
                        try:
                            total += os.path.getsize(os.path.join(root, name))
                        except Exception:
                            pass
        return total

    def purge_data(self, info: PluginInfo) -> int:
        """删除插件声明的数据目录，返回释放的字节数。"""
        import shutil
        freed = 0
        for p in self._data_paths(info):
            if not p.is_dir():
                continue
            for root, _dirs, files in os.walk(p):
                for name in files:
                    try:
                        freed += os.path.getsize(os.path.join(root, name))
                    except Exception:
                        pass
            shutil.rmtree(p, ignore_errors=True)
        return freed

    # ---------- 给 UI 的数据 ----------
    def get_plugin_items(self, query: str = "") -> list[Item]:
        """把已启用插件转成首页瓦片用的 Item（path 用 plugin:// 前缀标记）。

        不传 query：返回全部已启用插件（首页态）。
        传 query：只返回命中的插件，按相关度降序（见 scored_plugin_items）。
        """
        if not (query or "").strip():
            return [make_plugin_item(i) for i in self.enabled_plugins()]
        return [it for _, it in self.scored_plugin_items(query)]

    def scored_plugin_items(self, query: str) -> list[tuple[int, Item]]:
        """搜索态：返回 (相关度, 插件瓦片) 列表，按相关度降序。

        打分与应用的 search_rank.rank_score 是同一套阶梯，所以 UI 能把插件和应用
        混在一个列表里统一排序（不需要插件单独占一行）。同分时保持插件的扫描顺序。
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        scored: list[tuple[int, int, Item]] = []
        for order, info in enumerate(self.enabled_plugins()):
            score = plugin_match_score(info.manifest, q)
            if score > 0:
                scored.append((score, order, make_plugin_item(info)))
        scored.sort(key=lambda p: (-p[0], p[1]))
        return [(s, it) for s, _, it in scored]


def make_plugin_item(info: "PluginInfo") -> Item:
    """插件 → 首页瓦片 Item。

    刻意不填拼音：pypinyin 会加载完整词典（49MB），而瓦片不显示拼音；
    插件搜索走 plugin_match_score()，那里会现算。
    """
    m = info.manifest
    from lylaunch.plugins.plugin_icons import plugin_icon_svg  # 局部导入，避免任何包间循环

    return Item(
        name=m.name,
        path=f"plugin://{m.id}",
        item_type="插件",
        group_label="插件",
        group_order=3,
        icon_text=(m.name[0].upper() if m.name else "P"),
        icon_fg="#FFFFFF",
        tile_color=PLUGIN_TILE_COLOR,
        icon_size=17,
        icon_svg=plugin_icon_svg(m.id),  # 0.36「图标·线性」：插件瓦片用线性 SVG
    )


def plugin_match_score(manifest: PluginManifest, q: str) -> int:
    """插件相关度打分（0 = 不命中）。阶梯与应用的 search_rank.rank_score 完全一致。

    插件把 Keywords 和 Id 作为"附加词"参与打分，所以 `jt` 能命中截图（名拼音首字母）、
    `screenshot` / `capture` 能命中同名关键词、`fy` 能命中翻译插件的关键词。
    """
    name = manifest.name or ""
    pin, full = get_pinyin(name) if name else ("", "")
    return rank_score(q, name, pin, full, [*manifest.keywords, manifest.id])


# ---------- 插件包库辅助 ----------
def _keep_package(zip_path) -> None:
    """安装成功后把 zip 存进包库：以后卸载了还能一键装回来。"""
    try:
        from pathlib import Path as _P
        src = _P(zip_path)
        dst = paths.PLUGIN_PACKAGES_DIR / src.name
        if src.resolve() != dst.resolve():
            import shutil as _sh
            _sh.copy2(src, dst)
    except Exception:
        pass


def _read_zip_manifest(zip_path):
    """从插件 zip 里读 plugin.json（zip 根目录或第一层目录），读不到返回 None。"""
    import json as _json
    import zipfile as _zip
    try:
        with _zip.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                if name.endswith("plugin.json") and name.count("/") <= 1:
                    return _json.loads(zf.read(name).decode("utf-8"))
    except Exception:
        return None
    return None
