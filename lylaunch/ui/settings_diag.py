"""「开发者」页的「诊断」分组（P1-2，原在关于页）：日志入口 + 一键导出诊断包。

出问题时用户只有两种选择：截图聊天，或者自己翻 %LocalAppData%。这里把
「版本 / 配置 / 日志 / 插件清单 / 模型状态」打成一个 zip，用户点一下就得到
可以直接发出来的文件。

只读不写：不修改任何配置，也不联网。
"""
from __future__ import annotations

import platform
import sys
import time
import zipfile
from pathlib import Path

from .. import build_info, config, paths
from ..engine import registry
from ..plugins import theme as _t
from ..plugins.manager import PluginManager
from .settings_common import open_dir, size_text

_LOG_FILES = (paths.DATA_DIR / "crash.log",
              paths.DATA_DIR / "plugin_errors.log",
              paths.ENGINE_LOG_FILE)
_JSON_NAMES = ("config.json", "hotkey.json", "screenshot_hotkey.json",
               "pinned.json", "recents.json", "engine.json",
               "plugin_config.json", "plugins_installed.json")


def _logs_summary() -> str:
    rows = []
    for p in _LOG_FILES:
        try:
            if p.is_file():
                rows.append(f"{p.name} {size_text(p.stat().st_size)}")
        except Exception:
            pass
    return ("有日志：" + " · ".join(rows)) if rows else "还没有日志；一切正常时不会生成"


def _info_text() -> str:
    lines = list(build_info.about_lines(config.VERSION))
    lines += [
        "",
        f"数据目录：{paths.DATA_DIR}",
        f"系统：{platform.platform()}",
        f"Python：{sys.version.split()[0]}",
        f"打包运行：{bool(getattr(sys, 'frozen', False))}",
        f"导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    return "\n".join(lines)


def _plugins_text() -> str:
    try:
        plugins = PluginManager.instance().all_plugins()
    except Exception as exc:
        return f"读取插件清单失败：{exc}"
    if not plugins:
        return "没有已安装的插件"
    out = []
    for p in plugins:
        try:
            version = p.manifest.version
        except Exception:
            version = "?"
        out.append(f"{'启用' if p.enabled else '禁用'}  {p.id}  v{version}  "
                   f"{getattr(p, 'source', '')}  {p.directory}")
    return "\n".join(out)


def _models_text() -> str:
    out = []
    for spec in registry.MODELS:
        installed = registry.is_installed(spec)
        problem = registry.model_files_problem(spec)
        state = "已安装" if installed and not problem else ("不完整" if problem else "未安装")
        out.append(f"{spec.display:22} {spec.total_mb:6d} MB  {state}"
                   + (f"  （{problem}）" if problem else ""))
    try:
        out.append(f"\n模型目录：{registry.models_dir()}")
    except Exception:
        pass
    return "\n".join(out)


def build_bundle(dest_dir: Path | None = None) -> tuple[bool, str]:
    """导出诊断包，返回 (是否成功, 结果说明)。成功时说明里带完整路径。"""
    target_dir = Path(dest_dir) if dest_dir else Path(paths.desktop() or paths.DATA_DIR)
    name = time.strftime("LyLaunch-diag-%Y%m%d-%H%M") + ".zip"
    out = target_dir / name
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("info.txt", _info_text())
            z.writestr("plugins.txt", _plugins_text())
            z.writestr("models.txt", _models_text())
            for n in _JSON_NAMES:
                p = paths.DATA_DIR / n
                if p.is_file():
                    z.write(p, arcname=f"config/{n}")
            for p in _LOG_FILES:
                try:
                    if p.is_file():
                        z.write(p, arcname=f"logs/{p.name}")
                except Exception:
                    pass
    except Exception as exc:
        return False, f"导出失败：{exc}"
    return True, f"已导出 {out}（{size_text(out.stat().st_size)}）"


def build_diag_group(host, lay) -> None:
    """往「开发者」页追加「诊断」分组（只读：不写配置、不联网）。"""
    cv = host._group(lay, "诊断")

    col = host._row(cv, "日志", "崩溃日志与插件错误日志，出问题时先看这里",
                    host._link_button("打开日志目录", _open_logs), wide_hint=True)
    hint = host._hint_label(col, wide=True)
    hint.setText(_logs_summary())

    col = host._row(cv, "导出诊断包",
                    "版本、配置、日志、插件与模型清单打成一个 zip",
                    host._ghost_button("导出", lambda: _export(hint)),
                    divider=True, wide_hint=True)
    tip = host._hint_label(col, wide=True)
    tip.setText("包内含本机路径与用户名，不含任何密钥")


def _open_logs() -> None:
    open_dir(paths.DATA_DIR)


def _export(hint) -> None:
    ok, msg = build_bundle()
    hint.setText(msg)
    hint.set_color(_t.SUCCESS if ok else _t.DANGER)
    # 包里有本机路径，导出后让用户知道去哪找
    if ok:
        hint.setText(msg + "\n可直接把这个 zip 发出去")
