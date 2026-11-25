"""设置页「本地模型」的模型管理区（P0-1）。

职责边界：只做模型文件的可见与可管理（占多大、装没装、能不能下、能不能删）。
推理与加载全部交给 `engine.manager`，界面不碰推理逻辑。

两个必须记住的坑：
1. **不能复用 `doc_core.doc_engine.remove_model`** —— 它的 `spec_of()` 只在 OCR
   两个模型里查表，传 `qwen3-4b` 会**静默回退**到 `MODELS[0]`，一删就把 OCR 模型删了。
   卸载一律自己 `unload(if_model=…)` + `registry.remove_files(spec)`。
2. **下载 / 卸载都必须跑在后台线程**：`ensure_model_files` 是几 GB 的阻塞 IO，
   `unload()` 内部还会等 router 最多 25 秒，放主线程界面会直接假死。
"""
from __future__ import annotations

import shutil
import threading
import time

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QLabel, QVBoxLayout, QHBoxLayout, QWidget, QPushButton, QProgressBar,
)

from .. import reset
from ..engine import downloads, migrate, registry
from ..plugins import theme as _t
from .settings_common import mb_text, open_dir, size_text
from .settings_window import (
    _GHOST_QSS, _LINK_QSS, _LINE, _PRIMARY_QSS, _HintLabel,
)

_BTN_W = 84
_BTN_H = 30
_BAR_W = 84      # 与按钮同宽，两件控件在右列里天然对齐
_CAP_TEXT = {
    "vision": "视觉", "ocr": "OCR", "text": "文本",
    "translate": "翻译", "chat": "对话", "thinking": "思考",
}
# 分页里的能力分组：顺序 = 界面上从上到下
_GROUPS = (("doc", "文本识别专精模型"), ("mt", "多语言翻译专精模型"),
           ("llm", "本地多模态模型"))


def _cap_text(spec) -> str:
    return " · ".join(_CAP_TEXT.get(c, c) for c in spec.caps)


def _group_key(spec) -> str:
    if "ocr" in spec.caps:
        return "doc"
    if "translate" in spec.caps:
        return "mt"
    return "llm"


# ---------------------------------------------------------------- 后台线程
class _DownloadWorker(QThread):
    """一个模型的后台下载；暂停/续传/取消全部交给 downloads 层处理。"""

    pct = Signal(str, int)        # model_id, 0-100
    chunk = Signal(str, int, int)  # model_id, 已下字节, 总字节
    ok = Signal(str)
    cancelled = Signal(str)
    failed = Signal(str, str)

    def __init__(self, spec) -> None:
        super().__init__()
        self._spec = spec
        self._paused = threading.Event()
        self._cancel = False

    def pause(self, on: bool) -> None:
        if on:
            self._paused.set()
        else:
            self._paused.clear()

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:  # noqa: D401 - QThread 入口
        mid = self._spec.id
        try:
            downloads.ensure_model_files(
                self._spec,
                progress=lambda _doc, p: self.pct.emit(mid, int(p or 0)),
                cancelled=lambda: self._cancel,
                paused=lambda: self._paused.is_set(),
                on_bytes=lambda _name, got, total: self.chunk.emit(mid, got, total),
            )
        except downloads.DownloadCancelled:
            self.cancelled.emit(mid)     # 用户的取消：.part 保留，下次续传
        except Exception as exc:
            self.failed.emit(mid, str(exc))
        else:
            self.ok.emit(mid)


class _RemoveWorker(QThread):
    """卸载：先让 router 松手，再删文件，最后重写 preset（否则引擎还“看得见”它）。"""

    done = Signal(str, bool, str)   # model_id, 成功?, 提示

    def __init__(self, spec) -> None:
        super().__init__()
        self._spec = spec

    def run(self) -> None:  # noqa: D401
        mid = self._spec.id
        try:
            from ..engine.manager import EngineManager
            EngineManager.instance().unload(if_model=mid)
        except Exception:
            pass
        ok, msg = registry.remove_files(self._spec)
        try:
            registry.write_preset()
        except Exception:
            pass
        self.done.emit(mid, ok, msg)


class _CleanWorker(QThread):
    """清理旧目录里迁移后多余的副本（可能涉及几十 GB，放后台）。"""

    done = Signal(int, int)   # 释放字节, 删除文件数
    failed = Signal(str)

    def run(self) -> None:  # noqa: D401
        try:
            _removed, freed, _notes = migrate.cleanup_legacy(dry_run=False)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.done.emit(int(freed), int(_removed))


# ---------------------------------------------------------------- 页面区块
class _ModelsSection:
    """「模型文件」「已安装 / 可下载」这些分组，由 build_models_page() 装进分页。"""

    def __init__(self, host, lay: QVBoxLayout) -> None:
        self.host = host
        self._rows: dict[str, dict] = {}
        self._dl: _DownloadWorker | None = None
        self._rm: _RemoveWorker | None = None
        self._clean: _CleanWorker | None = None
        self._mid_active: str | None = None
        self._paused = False
        self._got = 0
        self._total = 0
        self._started = 0.0
        self._dl_text = ""
        self._build(lay)
        self.refresh()

    # ---------- 装配
    def _build(self, lay: QVBoxLayout) -> None:
        cv = self.host._group(lay, "模型文件")
        col = self.host._row(
            cv, "本地模型",
            "文本识别、多语言翻译、本地多模态共用一套引擎；装好的模型直接可用",
            self.host._link_button("打开模型目录", self._open_models_dir),
            wide_hint=True)
        self.summary_hint = self.host._hint_label(col, wide=True)

        col = self.host._row(
            cv, "旧副本",
            "迁移后遗留的重复文件，删掉不影响使用",
            self.host._link_button("清理旧副本", self._clean_legacy),
            divider=True, wide_hint=True)
        self.legacy_hint = self.host._hint_label(col, wide=True)

        for key, label in _GROUPS:
            specs = [s for s in registry.MODELS if _group_key(s) == key]
            if not specs:
                continue
            cv = self.host._group(lay, label)
            for i, spec in enumerate(specs):
                self._build_row(cv, spec, divider=(i > 0))

    def _build_row(self, cv: QVBoxLayout, spec, divider: bool) -> None:
        if divider:
            line = QWidget()
            line.setFixedHeight(1)
            line.setStyleSheet(f"background:{_LINE};")
            cv.addWidget(line)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(2, 11, 2, 11)
        h.setSpacing(14)

        col = QVBoxLayout()
        col.setSpacing(2)
        title = QLabel(spec.display)
        title.setStyleSheet(f"font-size:13px; font-weight:600; color:{_t.TEXT_PRIMARY};")
        desc = QLabel(f"{_cap_text(spec)} · {mb_text(spec.total_mb)}")
        desc.setStyleSheet(f"font-size:11px; color:#7C8391;")
        status = _HintLabel("")
        col.addWidget(title)
        col.addWidget(desc)
        col.addWidget(status)
        h.addLayout(col, 1)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(3)
        bar = QProgressBar()
        bar.setFixedSize(_BAR_W, 4)
        bar.setRange(0, 100)
        bar.setTextVisible(False)
        bar.setStyleSheet(
            "QProgressBar{background:#E9EEF7; border:none; border-radius:2px;}"
            f" QProgressBar::chunk{{background:{_t.ACCENT}; border-radius:2px;}}")
        bar.hide()
        btn = QPushButton("下载")
        btn.setFixedSize(_BTN_W, _BTN_H)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(f"{spec.note}\n{spec.vram}")
        cancel = QPushButton("取消")
        cancel.setFixedHeight(20)
        cancel.setStyleSheet(_LINK_QSS)
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.hide()
        rv.addWidget(bar, 0, Qt.AlignmentFlag.AlignHCenter)
        rv.addWidget(btn, 0, Qt.AlignmentFlag.AlignHCenter)
        rv.addWidget(cancel, 0, Qt.AlignmentFlag.AlignHCenter)
        h.addWidget(right, 0, Qt.AlignmentFlag.AlignVCenter)
        cv.addWidget(row)

        self._rows[spec.id] = {"spec": spec, "status": status, "btn": btn,
                               "bar": bar, "cancel": cancel}
        btn.clicked.connect(lambda _=False, s=spec: self._on_action(s))
        cancel.clicked.connect(lambda _=False: self._on_cancel())

    # ---------- 刷新
    def refresh(self) -> None:
        self._refresh_summary()
        for spec in registry.MODELS:
            self._refresh_row(spec)

    def _refresh_summary(self) -> None:
        specs = list(registry.MODELS)
        installed = [s for s in specs if registry.is_installed(s)]
        broken = [s for s in installed if registry.model_files_problem(s)]
        try:
            used = reset.size_of(registry.models_dir())
        except Exception:
            used = 0
        try:
            anchor = registry.models_dir()
            if not anchor.is_dir():
                anchor = anchor.parent
            free = shutil.disk_usage(str(anchor)).free
        except Exception:
            free = 0
        text = (f"已安装 {len(installed)} / {len(specs)} · "
                f"占用 {size_text(used)}"
                + (f" · 磁盘剩余 {size_text(free)}" if free else ""))
        if broken:
            text += f"\n有 {len(broken)} 个模型文件不完整，点右边「重新下载」修复"
        self.summary_hint.setText(text)
        self.summary_hint.set_color(_t.WARNING if broken else None)
        self._refresh_legacy()

    def _refresh_legacy(self) -> None:
        try:
            _removed, dup_bytes, _notes = migrate.cleanup_legacy(dry_run=True)
        except Exception:
            dup_bytes = 0
        if dup_bytes > 0:
            self.legacy_hint.setText(f"可清理约 {size_text(dup_bytes)}")
            self.legacy_hint.set_color(_t.TEXT_SECONDARY)
        else:
            self.legacy_hint.setText("未发现重复副本")
            self.legacy_hint.set_color(None)

    def _refresh_row(self, spec) -> None:
        row = self._rows.get(spec.id)
        if row is None:
            return
        btn, status, bar, cancel = row["btn"], row["status"], row["bar"], row["cancel"]
        mid = spec.id
        busy = self._dl is not None

        if busy and mid == self._mid_active:
            bar.show()
            cancel.show()
            btn.setEnabled(True)
            btn.setStyleSheet(_GHOST_QSS)
            btn.setText("继续" if self._paused else "暂停")
            status.set_color(_t.TEXT_SECONDARY)
            status.setText(self._dl_text)
            return

        bar.hide()
        cancel.hide()
        problem = registry.model_files_problem(spec)
        installed = registry.is_installed(spec)
        if installed and not problem:
            btn.setText("卸载")
            btn.setStyleSheet(_GHOST_QSS)
            btn.setEnabled(True)
            status.set_color(None)
            status.setText("已安装 · 可直接使用")
        elif problem:
            if "缺少" in problem:
                # 从没下过 = 正常状态，不要用警告色；只有半截文件才是真问题
                btn.setText("下载")
                btn.setStyleSheet(_PRIMARY_QSS)
                btn.setEnabled(not busy)
                status.set_color(None)
                status.setText("未安装")
            else:
                btn.setText("重新下载")
                btn.setStyleSheet(_PRIMARY_QSS)
                btn.setEnabled(not busy)
                status.set_color(_t.WARNING)
                status.setText(problem)
        else:
            btn.setText("下载")
            btn.setStyleSheet(_PRIMARY_QSS)
            btn.setEnabled(not busy)
            status.set_color(None)
            status.setText("未安装")

    # ---------- 动作
    def _on_action(self, spec) -> None:
        if self._dl is not None:
            if spec.id == self._mid_active:
                self._toggle_pause()
            return
        problem = registry.model_files_problem(spec)
        if registry.is_installed(spec) and not problem:
            self._uninstall(spec)
        else:
            self._download(spec)

    def _download(self, spec) -> None:
        need = spec.total_mb * 1024 * 1024
        try:
            anchor = registry.models_dir()
            anchor.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(str(anchor)).free
        except Exception:
            free = 0
        if free and free < need * 1.12:
            _t.show_warning(
                self.host, "磁盘空间不足",
                f"下载 {spec.display} 需要约 {size_text(need)}，"
                f"当前磁盘只剩 {size_text(free)}。\n\n"
                "先清理一下再试，或换个更大的盘装。")
            return

        self._dl = _DownloadWorker(spec)
        self._mid_active = spec.id
        self._paused = False
        self._got = self._total = 0
        self._started = time.monotonic()
        self._dl_text = "正在连接模型源…"
        self._dl.pct.connect(self._on_pct)
        self._dl.chunk.connect(self._on_chunk)
        self._dl.ok.connect(self._on_done)
        self._dl.cancelled.connect(self._on_cancelled)
        self._dl.failed.connect(self._on_failed)
        self._dl.start()
        self.refresh()

    def _toggle_pause(self) -> None:
        if self._dl is None:
            return
        self._paused = not self._paused
        self._dl.pause(self._paused)     # 暂停 = 断开连接，.part 留着续传
        if self._paused:
            self._dl_text = f"已暂停 · 已下载 {size_text(self._got)}"
        else:
            self._dl_text = "继续下载…"
        self.refresh()

    def _on_cancel(self) -> None:
        if self._dl is not None:
            self._dl.cancel()

    def _on_pct(self, _mid: str, pct: int) -> None:
        row = self._rows.get(self._mid_active or "")
        if row is not None:
            row["bar"].setValue(max(0, min(100, pct)))

    def _on_chunk(self, _mid: str, got: int, total: int) -> None:
        self._got, self._total = got, total
        if self._paused:
            # 暂停瞬间还会收到一批在途分片，别把「已暂停」覆盖成「下载中」
            return
        elapsed = max(0.4, time.monotonic() - self._started)
        speed = got / elapsed
        text = f"下载中 · {size_text(got)}"
        if total:
            text += f" / {size_text(total)}"
        if speed > 0:
            text += f" · {speed / 1048576:.1f} MB/s"
        self._dl_text = text
        row = self._rows.get(self._mid_active or "")
        if row is not None:
            row["status"].setText(text)

    def _on_done(self, mid: str) -> None:
        try:
            registry.write_preset()   # 让引擎下次启动时看得见新模型
        except Exception:
            pass
        self._finish_download()
        self.refresh()                 # 先按真实状态重排行，再补一句结果提示
        row = self._rows.get(mid)
        if row is not None:
            row["status"].set_color(_t.SUCCESS)
            row["status"].setText("已安装 · 重启对应插件后生效")

    def _on_cancelled(self, mid: str) -> None:
        got = self._got
        self._finish_download()
        self.refresh()
        row = self._rows.get(mid)
        if row is not None:
            row["status"].set_color(None)
            row["status"].setText(f"已取消 · 已下载 {size_text(got)} 保留，可续传")

    def _on_failed(self, mid: str, msg: str) -> None:
        self._finish_download()
        self.refresh()
        row = self._rows.get(mid)
        if row is not None:
            row["status"].set_color(_t.DANGER)
            row["status"].setText(f"下载失败：{msg[:120]}")

    def _finish_download(self) -> None:
        w = self._dl
        self._dl = None
        self._mid_active = None
        self._paused = False
        self._dl_text = ""
        if w is not None:
            try:
                w.wait(1500)
            except Exception:
                pass

    def _uninstall(self, spec) -> None:
        size = 0
        for p in (registry.model_file_path(spec), registry.mmproj_path(spec)):
            try:
                if p is not None:
                    size += p.stat().st_size
            except Exception:
                pass
        text = (f"卸载 {spec.display}（{size_text(size or spec.total_mb * 1024 * 1024)}）？\n\n"
                "文件会从本机删除，以后要用需要重新下载。\n"
                "其它模型与插件不受影响。\n\n确定卸载吗？")
        if not _t.ask_confirm(self.host, "卸载模型", text,
                              yes_text="卸载", no_text="取消", default_yes=False):
            return
        row = self._rows.get(spec.id)
        if row is not None:
            row["btn"].setEnabled(False)
            row["btn"].setText("卸载中…")
        self._rm = _RemoveWorker(spec)
        self._rm.done.connect(self._on_removed)
        self._rm.start()

    def _on_removed(self, mid: str, ok: bool, msg: str) -> None:
        w = self._rm
        self._rm = None
        if w is not None:
            # 信号是排队投递的，run() 可能还差最后一行没返回；
            # 直接丢掉最后一个引用会让 Qt 析构仍在运行的 QThread，进程会直接崩。
            w.wait(1500)
        self.refresh()                 # 状态行会跟着安装态重排，所以先刷新再写结果
        row = self._rows.get(mid)
        if row is not None:
            row["status"].set_color(_t.SUCCESS if ok else _t.DANGER)
            row["status"].setText(msg)

    def _open_models_dir(self) -> None:
        target = registry.models_dir()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        if not open_dir(target):
            self.summary_hint.setText(f"打不开模型目录：{target}")
            self.summary_hint.set_color(_t.DANGER)

    def _clean_legacy(self) -> None:
        try:
            _removed, dup_bytes, _notes = migrate.cleanup_legacy(dry_run=True)
        except Exception as exc:
            self.legacy_hint.setText(f"检查失败：{exc}")
            self.legacy_hint.set_color(_t.DANGER)
            return
        if dup_bytes <= 0:
            _t.show_warning(self.host, "没有可清理的副本",
                            "当前没有旧的重复模型文件。")
            return
        if not _t.ask_confirm(
                self.host, "清理旧副本",
                f"将从旧目录删除重复的模型文件，约 {size_text(dup_bytes)}。\n\n"
                "只删与 engine/models 同一份文件（硬链接或同内容复制品），"
                "正在使用的模型不会受影响。\n\n确定清理吗？",
                yes_text="清理", no_text="取消"):
            return
        self.legacy_hint.setText("正在清理…")
        self.legacy_hint.set_color(None)
        self._clean = _CleanWorker()
        self._clean.done.connect(self._on_cleaned)
        self._clean.failed.connect(self._on_clean_failed)
        self._clean.start()

    def _on_cleaned(self, freed: int, removed: int) -> None:
        w = self._clean
        self._clean = None
        if w is not None:
            w.wait(1500)
        self.legacy_hint.setText(
            f"已清理 {removed} 个旧文件，释放 {size_text(freed)}，建议重启 LyLaunch")
        self.legacy_hint.set_color(_t.SUCCESS)
        self._refresh_summary()

    def _on_clean_failed(self, msg: str) -> None:
        w = self._clean
        self._clean = None
        if w is not None:
            w.wait(1500)
        self.legacy_hint.setText(f"清理失败：{msg[:120]}")
        self.legacy_hint.set_color(_t.DANGER)

    # ---------- 收尾
    def stop(self) -> None:
        """窗口关闭时调用：取消在跑的下载、等线程收尾，避免信号打到已销毁的控件。"""
        if self._dl is not None:
            self._dl.cancel()
            try:
                self._dl.wait(3000)
            except Exception:
                pass
            self._dl = None
        for w in (self._rm, self._clean):
            if w is not None:
                try:
                    w.wait(3000)
                except Exception:
                    pass


def build_models_page(host, lay: QVBoxLayout) -> None:
    """由 SettingsWindow._page("models") 调用：在「运行」组之后追加模型管理区。"""
    section = _ModelsSection(host, lay)
    host.models_section = section    # 窗口关闭时 done() 会调 stop()
