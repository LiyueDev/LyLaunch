"""本机搜索索引（对应 C# 的 BuildIndex / BuildSystemApps / RebuildSearchIndex / RefreshList 的数据层）。

职责：
- 扫描桌面 / 开始菜单的 .lnk（已装软件），以及自定义路径（Steam 等游戏库）的 .lnk + 限定 .exe
- 预置一组系统自带工具（cmd / 设置 / 卸载 / 记事本 …）
- 按"桌面同名优先 → 最近 → 其余应用 → 系统工具"去重排序，构造搜索候选缓存
- 提供 search(query) 做名称/路径/拼音首字母/全拼 四路匹配

图标的"后台加载"由 UI 层负责（见 ui/main_window.py），本模块只产出数据(Item)。
"""

from __future__ import annotations

import os
from pathlib import Path

from . import paths, config
from .item import Item
from .pinyin import get_pinyin
from .search_rank import rank_score

# 应用瓦片配色（图标自己带色，靠哈希稳定分配到同一色）
_APP_PALETTE = [
    "#2F6BEE", "#00B2A9", "#FF8C00", "#7C5CFF",
    "#22C55E", "#0EA5E9", "#EAB308", "#D93A2F",
]


def _hash_color(name: str) -> str:
    s = name or ""
    h = 0
    for c in s:
        h = (h * 31 + ord(c)) & 0x7FFFFFFF
    return _APP_PALETTE[h % len(_APP_PALETTE)]


# .lnk 递归扫描的防护：Windows 开始菜单/桌面常有自引用的 junction/符号链接，
# 一旦成环递归就永不终止（表现为程序卡死、不报错）。这里加深度上限 + 已访问目录去环 + 数量上限。
_LNK_MAX_DEPTH = 6
_LNK_MAX_PER_ROOT = 3000


class SearchIndex:
    def __init__(self) -> None:
        self.index: list[Item] = []            # 应用（来自 .lnk / 自定义 .exe）
        self.system_apps: list[Item] = []      # 系统自带工具
        self.desktop_names: set[str] = set()   # 桌面快捷方式名集合（同名优先）
        self._search_cache: list[dict] = []    # 搜索候选缓存
        self._dirty = True

    # ---------- 构建索引 ----------
    def build(self) -> None:
        items: list[Item] = []
        for d in (paths.desktop(), paths.common_desktop()):
            self._collect_lnk(d, items)
        for d in (paths.start_menu(), paths.common_start_menu()):
            self._collect_lnk(d, items)

        # 同名去重：名字完全相同的只保留第一个（桌面优先），避免重复瓦片
        seen = set()
        items = [it for it in items if it.name and it.name not in seen and not seen.add(it.name)]

        self.index = items

        # 收集桌面快捷方式名
        self.desktop_names.clear()
        for d in (paths.desktop(), paths.common_desktop()):
            if d and os.path.isdir(d):
                try:
                    for f in os.listdir(d):
                        if f.lower().endswith(".lnk"):
                            self.desktop_names.add(Path(f).stem)
                except Exception:
                    pass

        self._build_system_apps()
        self._dirty = True

    def _collect_lnk(self, directory: str | None, target: list[Item],
                     _depth: int = 0, _visited: set[str] | None = None) -> None:
        if _depth > _LNK_MAX_DEPTH or len(target) >= _LNK_MAX_PER_ROOT:
            return
        if not directory or not os.path.isdir(directory):
            return
        # 去环：用真实路径（展开符号链接/junction）记录已访问目录
        try:
            real = os.path.realpath(directory).lower()
        except Exception:
            real = str(directory).lower()
        if _visited is None:
            _visited = set()
        if real in _visited:
            return
        _visited.add(real)
        try:
            for f in os.listdir(directory):
                if not f.lower().endswith(".lnk"):
                    continue
                name = Path(f).stem
                if "卸载" in name or "uninstall" in name.lower():
                    continue
                # 不算拼音：pypinyin 要 49MB 内存，瓦片不显示拼音，搜索时才需要
                target.append(self.make_app_item(name, os.path.join(directory, f), "应用"))
        except Exception:
            pass
        try:
            for sub in os.listdir(directory):
                full = os.path.join(directory, sub)
                if os.path.isdir(full):
                    self._collect_lnk(full, target, _depth + 1, _visited)
        except Exception:
            pass

    # ---------- 系统自带工具 ----------
    def _find_start_menu_shortcut(self, *keywords: str) -> str | None:
        for root in (paths.common_start_menu(), paths.start_menu()):
            if not root or not os.path.isdir(root):
                continue
            try:
                for base, _, files in os.walk(root):
                    for f in files:
                        if not f.lower().endswith(".lnk"):
                            continue
                        name = Path(f).stem
                        if "卸载" in name or "uninstall" in name.lower():
                            continue
                        if any(k.lower() in name.lower() for k in keywords):
                            return os.path.join(base, f)
            except Exception:
                pass
        return None

    def _build_system_apps(self) -> None:
        self.system_apps.clear()
        sys32 = paths.system32()
        win = paths.windows_dir()
        settings_exe = os.path.join(
            win, "SystemApps",
            "Microsoft.Windows.ImmersiveControlPanel_cw5n1h2tyxyewy",
            "SystemSettings.exe",
        )

        def add(name: str, file: str, launch: str | None, args: str | None,
                alias: str, icon_path: str | None = None) -> None:
            # 0.36：系统应用统一挂线性 SVG（和插件区同一套 UI 库），
            # 不再混用 Windows 彩色 exe 图标。局部导入避免包间循环。
            from .plugins.plugin_icons import system_icon_svg
            it = self.make_app_item(
                name, file, "系统应用", launch=launch, args=args,
                icon_path=icon_path, search_alias=alias,
            )
            it.icon_svg = system_icon_svg(name, file)
            self.system_apps.append(it)

        add("命令提示符", os.path.join(sys32, "cmd.exe"), None, None, "cmd 命令行 command")
        add("设置", "ms-settings:", "ms-settings:", None, "设置 系统设置 shezhi set windows", icon_path=settings_exe)
        add("卸载或更改程序", os.path.join(sys32, "appwiz.cpl"), "control.exe", "appwiz.cpl",
            "卸载 更改程序 xiezai uninstall 程序和功能 控制面板程序")
        add("记事本", os.path.join(sys32, "notepad.exe"), None, None, "记事本 jishiben notepad 文本 编辑",
            icon_path=self._find_start_menu_shortcut("记事本", "Notepad") or os.path.join(sys32, "notepad.exe"))
        add("计算器", os.path.join(sys32, "calc.exe"), None, None, "计算器 jisuanqi calc calculator",
            icon_path=self._find_start_menu_shortcut("计算器", "Calculator", "calc") or os.path.join(sys32, "calc.exe"))
        add("任务管理器", os.path.join(sys32, "taskmgr.exe"), None, None, "任务管理器 renwu taskmgr task 进程")
        add("文件资源管理器", os.path.join(win, "explorer.exe"), None, None, "文件 资源管理器 explorer 文件夹 我的电脑 此电脑")
        add("控制面板", os.path.join(sys32, "control.exe"), None, None, "控制面板 kongzhi control panel")
        add("注册表编辑器", os.path.join(sys32, "regedit.exe"), None, None, "注册表 zhuce regedit registry")
        add("画图", os.path.join(sys32, "mspaint.exe"), None, None, "画图 huatu paint 图像")

        # 系统设置子页（ms-settings: URI，可直接搜到并打开对应设置页）
        add("主页", "ms-settings:", "ms-settings:", None, "主页 home 设置 系统设置 shouye")
        add("系统", "ms-settings:system", "ms-settings:system", None, "系统 system 关于 设置 xitong")
        add("蓝牙", "ms-settings:bluetooth", "ms-settings:bluetooth", None, "蓝牙 bluetooth 无线 连接 lanoya")
        add("网络", "ms-settings:network", "ms-settings:network", None, "网络 network 网络与internet wangluo wifi 无线")
        add("更新", "ms-settings:windowsupdate", "ms-settings:windowsupdate", None, "更新 windows update gengxin 升级")
        add("个性化", "ms-settings:personalization", "ms-settings:personalization", None, "个性化 personalization 背景 主题 gexinghua")
        add("显示", "ms-settings:display", "ms-settings:display", None, "显示 display xianshi 亮度 分辨率 缩放")
        add("声音", "ms-settings:sound", "ms-settings:sound", None, "声音 sound shengyin 音量 音频")
        add("电源和睡眠", "ms-settings:powersleep", "ms-settings:powersleep", None, "电源 睡眠 power sleep 关机 休眠 dianyuan")
        add("账户", "ms-settings:accounts", "ms-settings:accounts", None, "账户 accounts 账号 登录 zhanghu user microsoft")
        add("应用", "ms-settings:appsfeatures", "ms-settings:appsfeatures", None, "应用 apps 程序 卸载 功能 yingyong")
        add("存储", "ms-settings:storage", "ms-settings:storage", None, "存储 storage cunchu 磁盘 空间")
        add("时间与语言", "ms-settings:dateandtime", "ms-settings:dateandtime", None, "时间 语言 time language 区域 日期 shijian")
        add("隐私和安全性", "ms-settings:privacy", "ms-settings:privacy", None, "隐私 安全 privacy security yinsi")
        add("关于", "ms-settings:about", "ms-settings:about", None, "关于 about guanyu 设备规格 系统信息")

    # ---------- 构造 Item ----------
    def make_app_item(self, name: str, path: str, group: str,
                      launch: str | None = None, args: str | None = None,
                      icon_path: str | None = None,
                      pinyin: str | None = None, pinyin_full: str | None = None,
                      search_alias: str = "") -> Item:
        # 刻意不在这里算拼音：pypinyin 会加载完整词典（49MB），而首页瓦片根本不用拼音。
        # 只有用户第一次搜索、rebuild_search_cache() 跑起来时才算。
        is_settings = bool(path and path.lower().startswith("ms-settings:"))
        item = Item(
            name=name, path=path, item_type="系统应用" if group == "系统应用" else "应用",
            launch=launch, launch_args=args,
            group_label=group,
            group_order=0 if group == "收藏应用" else (2 if group == "系统应用" else 1),
            pinyin=pinyin or "", pinyin_full=pinyin_full or "", search_alias=search_alias,
            icon_path=icon_path or path,
        )
        if is_settings:
            item.icon_text = "⚙"
            item.icon_fg = "#FFFFFF"
            item.tile_color = "#2F6BEE"
            item.icon_size = 24
        else:
            item.icon_text = (name[0].upper() if name else "?")
            item.icon_fg = "#FFFFFF"
            item.tile_color = _hash_color(name)
        return item

    # ---------- 搜索候选缓存 ----------
    def rebuild_search_cache(self) -> None:
        seen = set()
        cache: list[dict] = []

        def add(name: str, path: str, launch: str | None = None,
                args: str | None = None, alias: str = "", is_sys: bool = False) -> None:
            if not name or not path or name in seen:
                return
            seen.add(name)
            # 拼音在这里才算（首次搜索时触发，pypinyin 词典此时才加载）
            pin, pin_full = get_pinyin(f"{name} {alias}" if alias else name)
            cache.append({"name": name, "path": path, "pin": pin,
                          "pin_full": pin_full, "launch": launch, "args": args,
                          "sys": is_sys})

        # 1) 桌面同名应用优先
        for a in self.index:
            if a.name and a.name in self.desktop_names:
                add(a.name, a.path)
        # 2) 其余应用
        for a in self.index:
            add(a.name, a.path)
        # 3) 系统工具（别名参与拼音，如"设置"能靠 set / shezhi 命中）
        for s in self.system_apps:
            add(s.name, s.path, s.launch, s.launch_args, s.search_alias, True)
        self._search_cache = cache
        self._dirty = False

    def search(self, query: str) -> list[Item]:
        return [it for _, it in self.search_scored(query)]

    def search_scored(self, query: str) -> list[tuple[int, Item]]:
        """搜索并按相关度降序返回 (分数, Item)。

        打分走 search_rank.rank_score —— 与插件的 plugin_match_score 同一套阶梯，
        这样首页能把"应用 + 插件"混在一列里统一排序（不需要插件单独占一行）。
        名称完全相等 100 / 名称前缀 92 / 名称拼音首字母 74 / 名称子串 72 / 全拼 68 …
        只命中路径（如某目录下的 exe）给最低档 40，排在任何名称/拼音命中之后。
        同分保持候选缓存的原顺序（桌面同名优先 → 其余应用 → 系统工具）。
        """
        q = (query or "").strip().lower()
        if self._dirty:
            self.rebuild_search_cache()
        if not q:
            return []

        scored: list[tuple[int, int, Item]] = []
        for order, t in enumerate(self._search_cache):
            score = rank_score(q, t["name"], t["pin"], t["pin_full"])
            if not score and q in t["path"].lower():
                score = 40
            if score:
                it = self.make_app_item(
                    t["name"], t["path"], "应用",
                    launch=t["launch"], args=t["args"],
                    pinyin=t["pin"], pinyin_full=t["pin_full"],
                )
                if t.get("sys"):
                    # 系统应用在首页"系统应用"区块挂的是线性 SVG；搜索路径以前重建 Item
                    # 时把它丢了，于是退回旧版「彩色方块 + ⚙ 字母」占位（0.37 修）。
                    # 局部导入：避免 search_index ↔ plugins 包间循环。
                    from .plugins.plugin_icons import system_icon_svg
                    it.icon_svg = system_icon_svg(t["name"], t["path"])
                scored.append((score, order, it))
        scored.sort(key=lambda p: (-p[0], p[1]))
        return [(s, it) for s, _, it in scored[:90]]

    def mark_dirty(self) -> None:
        self._dirty = True
