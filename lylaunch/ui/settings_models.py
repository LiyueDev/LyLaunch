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
from ..engine import downloads, registry
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


# 模型小标签：文字 → (底色, 字色)。注册表 ModelDef.tags 只用这几类词，
# 没登记的走中性灰；加新词在这里补一行配色即可（界面不写死具体模型）。
_TAG_STYLE = {
    "推荐":     ("#E9F0FF", "#2F6BEE"),
    "低配友好": ("#E7F7EE", "#1F8A55"),
    "CPU 可用": ("#E7F7EE", "#1F8A55"),
    "轻量":     ("#E7F7EE", "#1F8A55"),
    "省显存":   ("#E7F7EE", "#1F8A55"),
    "能看图":   ("#E9F0FF", "#2F6BEE"),
    "会思考":   ("#F1ECFE", "#6B4FE0"),
    "大显存":   ("#FFF2E4", "#B0650F"),
}
_TAG_NEUTRAL = ("#EEF1F6", "#6E7683")


def _clear_layout(lay: QVBoxLayout) -> None:
    """清空一个布局（含嵌套布局）：详情页每次重绘都整体重建，不做增量替换。"""
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        sub = item.layout()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        elif sub is not None:
            _clear_layout(sub)
            sub.deleteLater()


class _RowCard(QWidget):
    """模型行：整行可点，进入该模型的「模型详细设置」页（行内按钮不受影响）。"""

    clicked = Signal()

    def mouseReleaseEvent(self, ev) -> None:  # noqa: D102
        if (ev.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(ev.position().toPoint())):
            self.clicked.emit()
        super().mouseReleaseEvent(ev)


def _tag_label(text: str) -> QLabel:
    """模型名后面的小胶囊：10px 字、圆角浅底，不抢主文字的颜色。"""
    bg, fg = _TAG_STYLE.get(text, _TAG_NEUTRAL)
    lab = QLabel(text)
    lab.setStyleSheet(
        f"background:{bg}; color:{fg}; font-size:10px;"
        "padding:1px 6px; border-radius:8px;")
    lab.setFixedHeight(17)
    return lab


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


# ---------------------------------------------------------------- 页面区块
class _ModelsSection:
    """「模型文件」「已安装 / 可下载」这些分组，由 build_models_page() 装进分页。"""

    def __init__(self, host, lay: QVBoxLayout) -> None:
        self.host = host
        self._rows: dict[str, dict] = {}
        self._groups: dict[str, dict] = {}
        self._expanded: set[str] = set()   # 用户手动展开「未下载」的组
        self._dl: _DownloadWorker | None = None
        self._rm: _RemoveWorker | None = None
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
        self.tip_hint = self.host._hint_label(col, wide=True)
        self.tip_hint.setText("点任意模型进入「模型详细设置」：大小、显存、上下文、文件位置都在那一页")
        self.tip_hint.set_color(_t.TEXT_SECONDARY)

        for key, label in _GROUPS:
            specs = [s for s in registry.MODELS if _group_key(s) == key]
            if not specs:
                continue
            cv = self.host._group(lay, label)
            self._build_group(cv, key, specs)

    def _build_group(self, cv: QVBoxLayout, key: str, specs: list) -> None:
        """一组模型：已安装的常显，没下载的折进底部一行「未下载（N）」。

        模型多了以后逐条铺开太长；默认只给已安装的，其余折叠——
        但「下了一半 / 正在下载」的必须露出来，否则用户看不到自己刚点的进度。
        """
        box = QWidget()
        rows_lay = QVBoxLayout(box)
        rows_lay.setContentsMargins(0, 0, 0, 0)
        rows_lay.setSpacing(0)

        entries: list[dict] = []
        for spec in specs:
            entries.append(self._build_row(rows_lay, spec))

        fold_line = QWidget()
        fold_line.setFixedHeight(1)
        fold_line.setStyleSheet(f"background:{_LINE};")
        fold_row = QWidget()
        fh = QHBoxLayout(fold_row)
        fh.setContentsMargins(2, 9, 2, 9)
        fh.setSpacing(8)
        btn = QPushButton("")
        btn.setStyleSheet(_LINK_QSS)
        btn.setFixedHeight(26)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip("这里只列已安装的模型；没下载的折叠在这里")
        btn.clicked.connect(lambda _=False, k=key: self._toggle_group(k))
        fh.addWidget(btn, 0)
        fh.addStretch(1)

        rows_lay.addWidget(fold_line)
        rows_lay.addWidget(fold_row)
        cv.addWidget(box)

        self._groups[key] = {"entries": entries, "fold_row": fold_row,
                             "fold_line": fold_line, "btn": btn, "lay": rows_lay}

    def _build_row(self, cv: QVBoxLayout, spec) -> dict:
        # 每条都带一根分割线：顺序会随安装态重排，谁能露出来由 _apply_group 决定，
        # 否则「原来排第一、后来排第二」的那行会少一条线。
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{_LINE};")
        cv.addWidget(line)

        row = _RowCard()
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        row.setToolTip(f"进入「模型详细设置」查看 {spec.display}")
        h = QHBoxLayout(row)
        h.setContentsMargins(2, 11, 2, 11)
        h.setSpacing(14)

        col = QVBoxLayout()
        col.setSpacing(2)
        trow = QHBoxLayout()
        trow.setSpacing(8)
        title = QLabel(spec.display)
        title.setStyleSheet(f"font-size:13px; font-weight:600; color:{_t.TEXT_PRIMARY};")
        trow.addWidget(title, 0, Qt.AlignmentFlag.AlignVCenter)
        # 「能力 · 大小」贴着名字走一行灰字：比单独一行短，也省得扫两遍
        meta = QLabel(f"{_cap_text(spec)} · {mb_text(spec.total_mb)}")
        meta.setStyleSheet("font-size:11px; color:#8A90A0;")
        trow.addWidget(meta, 0, Qt.AlignmentFlag.AlignVCenter)
        trow.addStretch(1)
        status = _HintLabel("")
        col.addLayout(trow)
        col.addWidget(status)
        h.addLayout(col, 1)

        # 标签放右侧按钮之前：名字那一行留给「名字 + 能力 + 大小」
        for tag in getattr(spec, "tags", ()) or ():
            h.addWidget(_tag_label(tag), 0, Qt.AlignmentFlag.AlignVCenter)

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
        btn.setToolTip(f"下载 / 卸载 {spec.display}")
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
                               "bar": bar, "cancel": cancel, "row": row}
        btn.clicked.connect(lambda _=False, s=spec: self._on_action(s))
        cancel.clicked.connect(lambda _=False: self._on_cancel())
        row.clicked.connect(lambda s=spec: self._open_detail(s))
        return {"spec": spec, "row": row, "line": line}

    # ---------- 刷新
    def refresh(self) -> None:
        self._refresh_summary()
        for spec in registry.MODELS:
            self._refresh_row(spec)
        for key in self._groups:
            self._apply_group(key)
        # 详情页和列表共用同一份状态：列表刷新完让它重绘一次，别出现「列表说已装、详情说没装」
        self._sync_detail()

    def _sync_detail(self) -> None:
        detail = getattr(self.host, "models_detail", None)
        if detail is not None:
            detail.sync()

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

    # ---------- 折叠：只显示已安装 / 需要处理的
    def _spec_visible(self, spec) -> bool:
        """这条是否属于「默认显示」：已装好、有残留文件要重下、或正在下载。

        从未下载过的模型（主文件都没有）才折进「未下载」。
        """
        if spec.id == self._mid_active:
            return True
        if registry.is_installed(spec) and not registry.model_files_problem(spec):
            return True
        return registry.model_file_path(spec) is not None

    def _toggle_group(self, key: str) -> None:
        if key in self._expanded:
            self._expanded.discard(key)
        else:
            self._expanded.add(key)
        self._apply_group(key)

    def _apply_group(self, key: str) -> None:
        """按「已安装在前、未下载在后」重排这一组，并同步可见性。

        顺序要跟着安装状态变（刚下载完的模型应该往上跳到已安装区），
        所以整段重排一次，而不是逐条算下标去 insert。
        """
        g = self._groups.get(key)
        if g is None:
            return
        expanded = key in self._expanded
        entries = g["entries"]
        visible = [e for e in entries if self._spec_visible(e["spec"])]
        hidden = [e for e in entries if not self._spec_visible(e["spec"])]
        order = visible + hidden

        lay = g["lay"]
        for e in entries:
            lay.removeWidget(e["row"])
            if e["line"] is not None:
                lay.removeWidget(e["line"])
        lay.removeWidget(g["fold_line"])
        lay.removeWidget(g["fold_row"])

        prev = False
        for e in order:
            vis = expanded or self._spec_visible(e["spec"])
            if e["line"] is not None:
                # 分割线画在行的上方：只有「本行可见且上方已有可见行」才显示，
                # 否则卡片顶上会多出一条悬空的细线
                e["line"].setVisible(vis and prev)
                lay.addWidget(e["line"])
            e["row"].setVisible(vis)
            lay.addWidget(e["row"])
            prev = prev or vis

        n = len(hidden)
        g["fold_row"].setVisible(n > 0)
        g["fold_line"].setVisible(n > 0 and prev)
        lay.addWidget(g["fold_line"])
        lay.addWidget(g["fold_row"])
        g["btn"].setText(f"↑ 收起（{n}）" if expanded
                         else f"↓ 未下载的模型（{n}）")

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
        """取消 = 不再下载这个模型：断开连接后由 _on_cancelled 清掉文件与目录。"""
        if self._dl is not None:
            self._dl.cancel()
            row = self._rows.get(self._mid_active or "")
            if row is not None:
                row["cancel"].setEnabled(False)
                row["cancel"].setText("取消中…")

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
        detail = getattr(self.host, "models_detail", None)
        if detail is not None:
            detail.on_chunk(self._mid_active or "", text, got, total)

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
        self._sync_detail()

    def _on_cancelled(self, mid: str) -> None:
        self._finish_download()
        spec = registry.MODEL_BY_ID.get(mid)
        removed, err = (0, "")
        if spec is not None:
            # 「取消」和「暂停」不一样：暂停留 .part 等续传，取消就把这次下的东西全删掉
            removed, err = downloads.cleanup_model_dir(spec)
        self.refresh()
        row = self._rows.get(mid)
        if row is not None:
            if err:
                row["status"].set_color(_t.WARNING)
                row["status"].setText(f"已取消，但文件没删干净：{err[:80]}")
            else:
                row["status"].set_color(None)
                row["status"].setText(f"已取消 · 已删除下载的文件（{removed} 个）")
        self._sync_detail()

    def _on_failed(self, mid: str, msg: str) -> None:
        self._finish_download()
        self.refresh()
        row = self._rows.get(mid)
        if row is not None:
            row["status"].set_color(_t.DANGER)
            row["status"].setText(f"下载失败：{msg[:120]}")
        self._sync_detail()

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
        self._sync_detail()

    # ---------- 详情页
    def _open_detail(self, spec) -> None:
        """点模型行：切到「模型详细设置」整页（不在导航里，带返回）。

        详情页由 settings_window 装配（它得往自己的页面栈里加一页），
        这里只负责把「点哪个模型」传过去；状态仍然共用本类的 worker。
        """
        opener = getattr(self.host, "open_models_detail", None)
        if opener is not None:
            opener(spec)

    def _open_models_dir(self) -> None:
        target = registry.models_dir()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        if not open_dir(target):
            self.summary_hint.setText(f"打不开模型目录：{target}")
            self.summary_hint.set_color(_t.DANGER)

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
        if self._rm is not None:
            try:
                self._rm.wait(3000)
            except Exception:
                pass


def build_models_page(host, lay: QVBoxLayout) -> None:
    """由 SettingsWindow._page("models") 调用：在「运行」组之后追加模型管理区。"""
    section = _ModelsSection(host, lay)
    host.models_section = section    # 窗口关闭时 done() 会调 stop()


class _ModelDetailPage:
    """「模型详细设置」：一个模型一页，从模型列表点进来，左上角返回。

    单独占设置页面栈里的一页（但不在左侧导航里），所以不用挤在列表底部；
    安装态一律现读 registry 和 _ModelsSection，不另存一份，避免两处说法不一致。
    """

    def __init__(self, host, lay: QVBoxLayout, section) -> None:
        self.host = host
        self.section = section
        self._spec = None
        self._bar: QProgressBar | None = None
        self._status: _HintLabel | None = None
        self._build(lay)

    # ---------- 装配
    def _build(self, lay: QVBoxLayout) -> None:
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        back = QPushButton("‹  返回模型列表")
        back.setStyleSheet(_LINK_QSS)
        back.setFixedHeight(28)
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.setToolTip("回到「本地模型」，列表与下载进度都还在原处")
        back.clicked.connect(lambda _=False: self.host.back_to_models())
        top.addWidget(back, 0)
        top.addStretch(1)
        lay.addLayout(top)
        self.host._page_title(lay, "模型详细设置",
                              "大小、显存、上下文与文件位置；下载 / 卸载也都在这一页")
        self._body = QWidget()
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(0, 0, 0, 0)
        self._body_lay.setSpacing(14)
        lay.addWidget(self._body)

    # ---------- 给 _ModelsSection / SettingsWindow 调
    def show_model(self, spec) -> None:
        self._spec = spec
        self.render()

    def sync(self) -> None:
        if self._spec is not None:
            self.render()

    def on_chunk(self, mid: str, text: str, got: int, total: int) -> None:
        """下载进度直达这一页：不整页重绘，只改百分比和那行字（否则滚动条会跳）。"""
        if self._spec is None or self._spec.id != mid:
            return
        if self._status is not None and self._status.text():
            self._status.setText(text)
        if self._bar is not None and total > 0:
            self._bar.setValue(max(0, min(100, int(got * 100 / total))))

    # ---------- 渲染
    def render(self) -> None:
        spec = self._spec
        if spec is None:
            return
        sec = self.section
        problem = registry.model_files_problem(spec)
        installed = registry.is_installed(spec)
        busy = (sec is not None and sec._dl is not None
                and sec._mid_active == spec.id)
        row = sec._rows.get(spec.id) if sec is not None else None

        _clear_layout(self._body_lay)
        self._bar = None
        self._status = None

        # 标题行：名字 + 标签 + 状态
        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(2, 0, 2, 0)
        hl.setSpacing(7)
        name = QLabel(spec.display)
        name.setStyleSheet(f"font-size:16px; font-weight:700; color:{_t.TEXT_PRIMARY};")
        hl.addWidget(name, 0, Qt.AlignmentFlag.AlignVCenter)
        for tag in spec.tags or ():
            hl.addWidget(_tag_label(tag), 0, Qt.AlignmentFlag.AlignVCenter)
        hl.addStretch(1)
        if busy:
            state_txt, state_color = "下载中", _t.ACCENT
        elif installed and not problem:
            state_txt, state_color = "已安装", _t.SUCCESS
        elif problem and "缺少" not in problem:
            state_txt, state_color = "文件不完整", _t.WARNING
        else:
            state_txt, state_color = "未安装", _t.TEXT_SECONDARY
        state = QLabel(state_txt)
        state.setStyleSheet(f"font-size:11px; color:{state_color};")
        hl.addWidget(state, 0, Qt.AlignmentFlag.AlignVCenter)
        self._body_lay.addWidget(head)

        note = QLabel(spec.note or "—")
        note.setWordWrap(True)
        note.setStyleSheet(f"font-size:12px; color:{_t.TEXT_SECONDARY};")
        self._body_lay.addWidget(note)

        # 参数：一行一项，右侧是值；比原来塞在一张卡里的小表格更好读
        cv = self.host._group(self._body_lay, "概览")
        size_txt = size_text(spec.size + spec.mmproj_size)
        if spec.mmproj:
            size_txt += (f"（主模型 {size_text(spec.size)}"
                         f" + 视觉投影 {size_text(spec.mmproj_size)}）")
        ctx_txt = f"{registry.effective_ctx(spec):,} tokens"
        if spec.ctx_max:
            ctx_txt += f" · 外部集成长上下文上限 {int(spec.ctx_max):,}"
        items = (("开发者", spec.org or "未标注"), ("大小", size_txt),
                 ("显存", spec.vram or "未标注"), ("内存", spec.ram or "未标注"),
                 ("上下文", ctx_txt), ("能力", _cap_text(spec) or "未标注"),
                 ("许可", spec.license or "未标注"))
        for i, (k, v) in enumerate(items):
            self.host._row(cv, k, v, divider=i > 0)

        if spec.points:
            cvs = self.host._group(self._body_lay, "说明")
            pts = QLabel("\n".join("·  " + p for p in spec.points))
            pts.setWordWrap(True)
            pts.setStyleSheet("font-size:11px; color:#4A5160;")
            cvs.setContentsMargins(16, 12, 16, 12)
            cvs.addWidget(pts)

        # 文件：主文件与视觉投影分开列，落盘路径用整行提示，别挤在行右侧
        cvf = self.host._group(self._body_lay, "文件")
        mf = registry.model_file_path(spec)
        col = self.host._row(cvf, "主模型文件", spec.file, wide_hint=True)
        fh = self.host._hint_label(col, wide=True)
        if mf is not None:
            fh.setText(f"当前文件：{mf}")
        else:
            fh.setText("尚未下载 · 下载后会放在：" +
                       str(registry.models_dir() / spec.id / spec.file))
        if spec.mmproj:
            self.host._row(cvf, "视觉投影", spec.mmproj, divider=True)
        self.host._row(cvf, "模型目录", "所有模型的落盘位置",
                       self.host._link_button("打开目录", self._open_dir),
                       divider=True)

        # 下载 / 卸载：状态行 + 进度条 + 主动作，和列表行共用同一套文案
        cva = self.host._group(self._body_lay, "下载与卸载")
        act = QWidget()
        al = QHBoxLayout(act)
        al.setContentsMargins(2, 12, 2, 12)
        al.setSpacing(10)
        left = QVBoxLayout()
        left.setSpacing(6)
        status = _HintLabel("")
        if sec is None:
            status.setText("模型管理区不可用，关掉设置重开一次")
            status.set_color(_t.DANGER)
        elif row is not None and row["status"].text():
            status.setText(row["status"].text())
        left.addWidget(status)
        bar = QProgressBar()
        bar.setFixedHeight(6)
        bar.setRange(0, 100)
        bar.setTextVisible(False)
        bar.setStyleSheet(
            "QProgressBar{background:#E9EEF7; border:none; border-radius:3px;}"
            f" QProgressBar::chunk{{background:{_t.ACCENT}; border-radius:3px;}}")
        if busy and row is not None:
            bar.setValue(row["bar"].value())
        bar.setVisible(busy)
        left.addWidget(bar)
        al.addLayout(left, 1)

        if busy:
            cancel = QPushButton("取消")
            cancel.setStyleSheet(_LINK_QSS)
            cancel.setFixedHeight(26)
            cancel.setCursor(Qt.CursorShape.PointingHandCursor)
            cancel.setToolTip("取消下载；已下载的部分保留，下次可续传")
            cancel.clicked.connect(lambda _=False: sec._on_cancel())
            al.addWidget(cancel, 0, Qt.AlignmentFlag.AlignVCenter)

        btn = QPushButton("")
        btn.setFixedSize(_BTN_W + 16, _BTN_H)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        if sec is None:
            btn.setText("不可用")
            btn.setEnabled(False)
            btn.setStyleSheet(_GHOST_QSS)
        elif busy:
            btn.setText("继续" if sec._paused else "暂停")
            btn.setStyleSheet(_GHOST_QSS)
            btn.clicked.connect(lambda _=False: sec._toggle_pause())
        elif installed and not problem:
            btn.setText("卸载")
            btn.setStyleSheet(_GHOST_QSS)
            btn.clicked.connect(lambda _=False, s=spec: sec._uninstall(s))
        else:
            btn.setText("重新下载" if (problem and "缺少" not in problem) else "下载并安装")
            btn.setStyleSheet(_PRIMARY_QSS)
            btn.setEnabled(sec._dl is None)
            btn.clicked.connect(lambda _=False, s=spec: sec._download(s))
        al.addWidget(btn, 0, Qt.AlignmentFlag.AlignVCenter)
        cva.addWidget(act)

        self._bar = bar
        self._status = status

    def _open_dir(self) -> None:
        """这一页只负责打开目录；失败提示借列表页那行字（它一直存在）。"""
        sec = self.section
        if sec is not None:
            sec._open_models_dir()


def build_models_detail_page(host, lay: QVBoxLayout):
    """由 SettingsWindow._setup_ui 调用：建「模型详细设置」这一页并返回它。"""
    section = getattr(host, "models_section", None)
    page = _ModelDetailPage(host, lay, section)
    host.models_detail = page
    return page
