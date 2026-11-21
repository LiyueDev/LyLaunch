"""设置页「数据与存储」分页（P0-2）。

本机数据一直是「看不见的黑盒」：模型 704 MB、插件若干、日志偶尔出现。
这一页把它摊开：

- 占用：模型 / 插件 / 配置与日志各自的体积 + 打开数据目录
- 清理：日志与下载残片、迁移后遗留的旧模型副本

「重置本机数据」这类全量删除的按钮在「开发者」页（settings_dev.py），
不放在日常设置里。

体积统计要遍历几 GB 目录，一律放后台线程，先把「计算中…」显示出来。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QVBoxLayout, QWidget, QPushButton, QHBoxLayout, QLabel,
)

from .. import paths, reset
from ..engine import registry
from ..plugins import theme as _t
from .settings_common import open_dir, size_text
from .settings_window import _GHOST_QSS

_LOG_NAMES = ("crash.log", "plugin_errors.log")


class _ScanWorker(QThread):
    """统计本机数据占用（目录遍历，别放主线程）。"""

    done = Signal(dict)
    failed = Signal(str)

    def run(self) -> None:  # noqa: D401
        try:
            models = reset.size_of(paths.ENGINE_MODELS_DIR)
            plugins = reset.size_of(paths.USER_PLUGINS_DIR)
            packages = reset.size_of(paths.PLUGIN_PACKAGES_DIR)
            logs = 0
            for name in _LOG_NAMES:
                p = paths.DATA_DIR / name
                try:
                    if p.is_file():
                        logs += p.stat().st_size
                except Exception:
                    pass
            parts = 0
            for p in paths.ENGINE_MODELS_DIR.rglob("*.part"):
                try:
                    parts += p.stat().st_size
                except Exception:
                    pass
            total = reset.size_of(paths.DATA_DIR)
            installed = sum(1 for s in registry.MODELS if registry.is_installed(s))
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.done.emit({
            "models": models, "plugins": plugins, "packages": packages,
            "logs": logs, "parts": parts, "total": total,
            "installed": installed,
        })


class _DataSection:
    """由 build_data_page() 装进「数据与存储」分页。"""

    def __init__(self, host, lay: QVBoxLayout) -> None:
        self.host = host
        self._scan: _ScanWorker | None = None
        self._data: dict = {}
        self._build(lay)
        self._start_scan()

    # ---------- 装配
    def _build(self, lay: QVBoxLayout) -> None:
        # 占用
        cv = self.host._group(lay, "占用")
        col = self.host._row(
            cv, "本机数据",
            "配置、插件、模型都放在用户目录下，卸载程序不会自动删",
            self.host._link_button("打开数据目录", self._open_data_dir),
            wide_hint=True)
        self.dir_hint = self.host._hint_label(col, wide=True)
        self.dir_hint.setText(f"数据目录：{paths.DATA_DIR}")

        col = self.host._row(cv, "本地模型", "下载的模型文件，可在「本地模型」页管理",
                             self._value("计算中…"), divider=True)
        self.models_value = self._last_value

        col = self.host._row(cv, "插件", "用户插件目录 + 本地插件包库",
                             self._value("计算中…"), divider=True)
        self.plugins_value = self._last_value

        col = self.host._row(cv, "配置与日志", "热键、最近使用、固定项、崩溃日志等",
                             self._value("计算中…"), divider=True)
        self.cfg_value = self._last_value

        # 清理（旧模型副本已合并到「本地模型」页，这里不重复放）
        cv = self.host._group(lay, "清理")
        col = self.host._row(
            cv, "日志与下载残片",
            "崩溃日志、插件错误日志、下载中断留下的 .part",
            self._ghost("清理", self._clean_junk), wide_hint=True)
        self.junk_hint = self.host._hint_label(col, wide=True)
        self.junk_hint.setText("计算中…")
        self.junk_btn = self._last_btn

        # 危险操作（重置本机数据）已搬到「开发者」页：日常设置里不该出现"删除一切"
        self.junk_btn.setEnabled(False)

    def _value(self, text: str):
        lab = QWidget()
        h = QHBoxLayout(lab)
        h.setContentsMargins(0, 0, 0, 0)
        inner = QLabel(text)
        inner.setStyleSheet(
            f"font-size:13px; font-weight:600; color:{_t.TEXT_PRIMARY};")
        h.addWidget(inner, 0, Qt.AlignmentFlag.AlignVCenter)
        self._last_value = inner
        return lab

    def _ghost(self, text: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setFixedSize(84, 30)
        b.setStyleSheet(_GHOST_QSS)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda _=False: slot())
        self._last_btn = b
        return b

    # ---------- 统计
    def _start_scan(self) -> None:
        self._scan = _ScanWorker()
        self._scan.done.connect(self._on_scanned)
        self._scan.failed.connect(self._on_scan_failed)
        self._scan.start()

    def _on_scan_failed(self, msg: str) -> None:
        w = self._scan
        self._scan = None
        if w is not None:
            w.wait(1500)
        for lab in (self.models_value, self.plugins_value, self.cfg_value):
            lab.setText("读取失败")
        self.junk_hint.setText(f"统计失败：{msg[:120]}")
        self.junk_hint.set_color(_t.DANGER)

    def _on_scanned(self, data: dict) -> None:
        w = self._scan
        self._scan = None
        if w is not None:
            w.wait(1500)     # 排队投递的信号可能早于 run() 返回，别让 QThread 提前析构
        self._data = data
        models = data["models"]
        plugins = data["plugins"]
        packages = data["packages"]
        self.models_value.setText(f"{size_text(models)} · {data['installed']} 个")
        self.plugins_value.setText(
            f"{size_text(plugins + packages)}"
            + (f" · 包库 {size_text(packages)}" if packages else ""))
        others = max(0, data["total"] - models - plugins - packages)
        self.cfg_value.setText(size_text(others))

        junk = data["logs"] + data["parts"]
        if junk > 0:
            self.junk_hint.setText(
                f"可清理 {size_text(junk)}"
                + (f" · 日志 {size_text(data['logs'])}" if data["logs"] else "")
                + (f" · 残片 {size_text(data['parts'])}" if data["parts"] else ""))
            self.junk_hint.set_color(_t.TEXT_SECONDARY)
        else:
            self.junk_hint.setText("没有日志或残片")
            self.junk_hint.set_color(None)
        self.junk_btn.setEnabled(junk > 0)

        # 合计放在路径那行：占用卡里每行只讲自己那一项，不重复总数
        self.dir_hint.setText(
            f"数据目录：{paths.DATA_DIR} · 合计 {size_text(data['total'])}")

    # ---------- 清理
    def _clean_junk(self) -> None:
        targets = []
        for name in _LOG_NAMES:
            p = paths.DATA_DIR / name
            if p.is_file():
                targets.append(p)
        targets += [p for p in paths.ENGINE_MODELS_DIR.rglob("*.part") if p.is_file()]
        total = 0
        for p in targets:
            try:
                total += p.stat().st_size
            except Exception:
                pass
        if not targets:
            _t.show_warning(self.host, "没有可清理的内容", "日志和残片都已经是空的。")
            return
        if not _t.ask_confirm(
                self.host, "清理日志与残片",
                f"将删除 {len(targets)} 个文件，共 {size_text(total)}。\n\n"
                "崩溃日志与下载残片删掉不影响使用（残片是没下完的半截文件，"
                "删掉后下次下载要重新开始）。\n\n确定清理吗？",
                yes_text="清理", no_text="取消"):
            return
        failed = []
        for p in targets:
            try:
                p.unlink()
            except Exception:
                failed.append(p.name)
        n = len(targets) - len(failed)
        self.junk_hint.setText(
            f"已清理 {n} 个文件" if n else "没能删掉，可能正被占用")
        self.junk_hint.set_color(_t.SUCCESS if n else _t.DANGER)
        self._start_scan()

    # ---------- 其它
    def _open_data_dir(self) -> None:
        try:
            paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        if open_dir(paths.DATA_DIR):
            self.dir_hint.setText(f"已在资源管理器打开：{paths.DATA_DIR}")
            self.dir_hint.set_color(None)
        else:
            self.dir_hint.setText(f"打不开数据目录：{paths.DATA_DIR}")
            self.dir_hint.set_color(_t.DANGER)

    def stop(self) -> None:
        if self._scan is not None:
            try:
                self._scan.wait(3000)
            except Exception:
                pass
            self._scan = None


def build_data_page(host, lay: QVBoxLayout) -> None:
    """由 SettingsWindow._page("data") 调用。"""
    section = _DataSection(host, lay)
    host.data_section = section     # done() 里等后台统计线程收尾
