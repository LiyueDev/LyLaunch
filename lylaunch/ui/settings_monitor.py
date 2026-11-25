"""运行监控（设置 → 外部集成 → 运行监控）。

展示两套 llama-server 的实时状态：
- 模型 / 上下文占用 / 输出速度（t/s）/ 提示词缓存命中率
- 显存占用、GPU 利用率、温度
- 18090 另有「请求历史」（逐请求 token、耗时、t/s，来自 RouterClient 埋点）
- 18091 是 dsh 直连，只有进程级数据，历史区会写明原因

数据全部来自 `engine/monitor.py`；采样在后台线程跑（本机回环空端口要耗满
超时，2 s 一次若放主线程会闪屏）。
"""
from __future__ import annotations

import time

from PySide6.QtCore import QRectF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)

from ..engine import monitor as _mon
from ..plugins import theme as _t

_ORIGINS = ((18090, "插件引擎"), (18091, "外部引擎"))
_INTERVAL_MS = 2000
_SPARK_POINTS = 60
_MUTED = "#7C8391"
_LINE = "#E3E9F1"


# ---------------------------------------------------------------- 采样线程
class _SampleWorker(QThread):
    done = Signal(object)

    def __init__(self, mon, parent=None) -> None:
        super().__init__(parent)
        self._mon = mon

    def run(self) -> None:  # noqa: D401 - QThread 入口
        try:
            self.done.emit(self._mon.sample(timeout=1.0))
        except Exception:
            self.done.emit(None)


# ---------------------------------------------------------------- 自绘小件
class _Wrap(QLabel):
    """长提示标签：宽度不撑页面（QLabel 的 minimumSizeHint 会按整段文字算宽）。

    不处理就会出现横向滚动条：设置页视口约 640px，而一句 70 字的提示
    minimumSizeHint 能报到 800+，整张卡跟着变宽，右边缘被切掉。
    """

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

    def minimumSizeHint(self):  # noqa: N802
        s = super().minimumSizeHint()
        return QSize(0, s.height())

    def sizeHint(self):  # noqa: N802
        s = super().sizeHint()
        return QSize(min(s.width(), 520), s.height())


class _Bar(QWidget):
    """细进度条（显存 / 上下文），颜色由占比决定：<70% 蓝、<90% 橙、其余红。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._ratio = 0.0
        self.setFixedHeight(6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_ratio(self, ratio: float) -> None:
        self._ratio = max(0.0, min(1.0, ratio))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#EEF2F8"))
        p.drawRoundedRect(r, 3, 3)
        if self._ratio <= 0:
            p.end()
            return
        color = _t.ACCENT if self._ratio < 0.7 else ("#E8A33D" if self._ratio < 0.9 else "#D9534F")
        w = max(3.0, r.width() * self._ratio)
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(0, 0, w, r.height()), 3, 3)
        p.end()


class _Spark(QWidget):
    """60 点折线（t/s 或显存），无坐标轴，只画趋势线 + 渐变填充。"""

    def __init__(self, color: str = _t.ACCENT, empty_text: str = "",
                 parent=None) -> None:
        super().__init__(parent)
        self._values: list[float] = []
        self._color = QColor(color)
        self._empty_text = empty_text
        self.setFixedHeight(48)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_values(self, values: list[float]) -> None:
        self._values = list(values)[-_SPARK_POINTS:]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 6, -1, -6)
        p.setPen(QPen(QColor(_LINE), 1))
        p.drawLine(int(r.left()), int(r.bottom()), int(r.right()), int(r.bottom()))
        vals = self._values
        if len(vals) < 2 or max(vals) <= 0.01:
            # 空闲时给一行灰字，比一条贴着底边的直线好读
            if self._empty_text:
                p.setPen(QPen(QColor("#AEB6C2"), 1))
                p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._empty_text)
            p.end()
            return
        hi = max(vals) or 1.0
        lo = min(min(vals), 0.0)
        span = max(hi - lo, 1e-6)
        # 不满 60 个点时铺满整宽（否则刚打开面板只会看到右边缘一小段线）
        step = r.width() / max(len(vals) - 1, 1)
        xs_start = r.left()
        pts = []
        for i, v in enumerate(vals):
            x = xs_start + step * i
            y = r.bottom() - (v - lo) / span * r.height()
            pts.append((x, y))
        path = QPainterPath()
        path.moveTo(pts[0][0], pts[0][1])
        for x, y in pts[1:]:
            path.lineTo(x, y)
        grad = QLinearGradient(0, r.top(), 0, r.bottom())
        c = QColor(self._color)
        c.setAlpha(38)
        grad.setColorAt(0.0, c)
        c2 = QColor(self._color)
        c2.setAlpha(0)
        grad.setColorAt(1.0, c2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grad)
        p.drawPath(path)
        p.setPen(QPen(self._color, 2))
        for i in range(len(pts) - 1):
            p.drawLine(int(pts[i][0]), int(pts[i][1]), int(pts[i + 1][0]), int(pts[i + 1][1]))
        p.end()


class _Metric(QWidget):
    """一块指标：小标题 + 大字数值 + 副说明。"""

    def __init__(self, title: str, unit: str = "", parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)
        self._title = QLabel(title)
        self._title.setStyleSheet(f"font-size:11px; color:{_MUTED};")
        self._value = QLabel("-")
        self._value.setStyleSheet(
            f"font-size:20px; font-weight:600; color:{_t.TEXT_PRIMARY};")
        self._unit = QLabel(unit)
        self._unit.setStyleSheet(f"font-size:11px; color:{_MUTED}; margin-left:2px;")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        row.addWidget(self._value, 0, Qt.AlignmentFlag.AlignBaseline)
        row.addWidget(self._unit, 0, Qt.AlignmentFlag.AlignBaseline)
        row.addStretch(1)
        self._sub = QLabel("")
        self._sub.setStyleSheet(f"font-size:11px; color:{_MUTED};")
        # 副说明会变长（"9,537 / 114,688 tok"），不参与最小宽度计算
        self._sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self._title)
        lay.addLayout(row)
        lay.addWidget(self._sub)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, value: str, sub: str = "") -> None:
        self._value.setText(value)
        self._sub.setText(sub)


def _fmt_int(n: int | float) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return "-"


def _fmt_time(ts: float) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(ts))
    except Exception:
        return "-"


# ---------------------------------------------------------------- 主块
class _MonitorSection:
    def __init__(self, host, lay: QVBoxLayout) -> None:
        self.host = host
        self._origin = _ORIGINS[0][0]
        self._sample: dict | None = None
        self._worker = None
        self._pill_state: str | None = None
        self._sub_text: str | None = None
        self._hist_sig: tuple | None = None
        self._build(lay)
        self._timer = QTimer()
        self._timer.setInterval(_INTERVAL_MS)
        self._timer.timeout.connect(self.poll)
        self.start()

    # ---------- 装配 ----------
    def _build(self, lay: QVBoxLayout) -> None:
        from .settings_window import _Segmented

        # ── 顶栏：切哪一套 + 状态（不套白卡，避免出现「卡里还有一层卡」）──
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(2, 0, 2, 0)
        bl.setSpacing(5)
        top = QWidget()
        tl = QHBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(8)
        self.seg = _Segmented([(str(p), name) for p, name in _ORIGINS], "18090")
        self.seg.changed.connect(self._on_switch)
        self.seg.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.seg.setToolTip("插件引擎 = 18090（翻译 / 文档识别 / 本地 AI）　"
                            "外部引擎 = 18091（dsh 用的那套）")
        self.pill = QLabel("读取中…")
        self.pill.setStyleSheet(
            f"background:{_t.BG_SUBTLE}; border:1px solid {_t.BORDER}; border-radius:8px;"
            f" padding:4px 10px; font-size:12px; color:{_MUTED};")
        tl.addWidget(self.seg, 0, Qt.AlignmentFlag.AlignVCenter)
        tl.addStretch(1)
        tl.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignVCenter)
        bl.addWidget(top)
        self.sub_hint = _Wrap("")
        self.sub_hint.setStyleSheet(f"font-size:11px; color:{_MUTED};")
        bl.addWidget(self.sub_hint)
        lay.addWidget(box)

        # ── 状态卡 ──
        card = self.host._card()
        body = QVBoxLayout(card)
        body.setContentsMargins(16, 12, 16, 12)
        body.setSpacing(10)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        self.model_label = QLabel("（未加载模型）")
        self.model_label.setStyleSheet(
            f"font-size:15px; font-weight:600; color:{_t.TEXT_PRIMARY};")
        self.gpu_label = QLabel("")
        self.gpu_label.setStyleSheet(f"font-size:11px; color:{_MUTED};")
        head.addWidget(self.model_label, 1)
        head.addWidget(self.gpu_label, 0, Qt.AlignmentFlag.AlignRight)
        body.addLayout(head)

        metrics = QHBoxLayout()
        metrics.setContentsMargins(0, 0, 0, 0)
        metrics.setSpacing(18)
        self.m_tps = _Metric("输出速度", "t/s")
        self.m_ctx = _Metric("上下文占用")
        self.m_prompt = _Metric("提示词 token")
        self.m_cache = _Metric("缓存命中")
        for m in (self.m_tps, self.m_ctx, self.m_prompt, self.m_cache):
            metrics.addWidget(m, 1)
        body.addLayout(metrics)

        self.ctx_bar = _Bar()
        self.vram_bar = _Bar()
        body.addWidget(self.ctx_bar)
        vrow = QHBoxLayout()
        vrow.setContentsMargins(0, 0, 0, 0)
        self.vram_text = QLabel("显存 -")
        self.vram_text.setStyleSheet(f"font-size:11px; color:{_MUTED};")
        self.gpu_text = QLabel("")
        self.gpu_text.setStyleSheet(f"font-size:11px; color:{_MUTED};")
        # 两行短文本（约 360px），别让它们参与撑宽即可
        for lb in (self.vram_text, self.gpu_text):
            lb.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        vrow.addWidget(self.vram_text, 0)
        vrow.addStretch(1)
        vrow.addWidget(self.gpu_text, 0)
        body.addWidget(self.vram_bar)
        body.addLayout(vrow)
        lay.addWidget(card)

        # ── 趋势卡 ──
        self.trend_card = self.host._card()
        tb = QVBoxLayout(self.trend_card)
        tb.setContentsMargins(16, 12, 16, 12)
        tb.setSpacing(6)
        t1 = QLabel("输出速度（最近 2 分钟）")
        t1.setStyleSheet(f"font-size:11px; color:{_MUTED};")
        self.spark_tps = _Spark(_t.ACCENT, "空闲 · 最近 2 分钟没有解码")
        t2 = QLabel("显存占用（MB）")
        t2.setStyleSheet(f"font-size:11px; color:{_MUTED};")
        self.spark_vram = _Spark("#4C9BE8", "正在采集…")
        tb.addWidget(t1)
        tb.addWidget(self.spark_tps)
        tb.addWidget(t2)
        tb.addWidget(self.spark_vram)
        lay.addWidget(self.trend_card)

        # ── 历史卡 ──
        self.hist_card = self.host._card()
        hb = QVBoxLayout(self.hist_card)
        hb.setContentsMargins(16, 12, 16, 12)
        hb.setSpacing(4)
        self.hist_title = QLabel("最近请求")
        self.hist_title.setStyleSheet(f"font-size:13px; font-weight:600; color:{_t.TEXT_PRIMARY};")
        self.hist_hint = _Wrap("")
        self.hist_hint.setStyleSheet(f"font-size:11px; color:{_MUTED};")
        self.hist_box = QVBoxLayout()
        self.hist_box.setContentsMargins(0, 4, 0, 0)
        self.hist_box.setSpacing(0)
        hb.addWidget(self.hist_title)
        hb.addWidget(self.hist_hint)
        hb.addLayout(self.hist_box)
        lay.addWidget(self.hist_card)

        # 指标卡右侧不抢宽度，避免长模型名把布局撑开
        self.model_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Preferred)

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
        self.poll()

    def stop(self) -> None:
        if self._timer.isActive():
            self._timer.stop()

    def _on_switch(self, value: str) -> None:
        try:
            self._origin = int(value)
        except Exception:
            return
        self._sample = None
        self.poll()

    def poll(self) -> None:
        """起一个后台采样；上一次还没跑完就跳过（不排队堆积）。"""
        w = self._worker
        if w is not None:
            try:
                if w.isRunning():
                    return
            except RuntimeError:      # 上一轮的 QThread 已 deleteLater，引用过期
                self._worker = None
        w = _SampleWorker(_mon.instance(self._origin), self.host)
        origin = self._origin
        w.done.connect(lambda s, o=origin: self._on_sample(o, s))
        w.finished.connect(self._on_worker_done)
        w.finished.connect(w.deleteLater)
        self._worker = w
        w.start()

    def _on_sample(self, origin: int, sample) -> None:
        """切端口时上一轮采样可能还在飞，端口对不上就丢掉，别渲染成另一套的数据。"""
        if origin == self._origin:
            self._render(sample)

    def _on_worker_done(self) -> None:
        self._worker = None

    # ---------- 渲染 ----------
    def _render(self, sample) -> None:
        if not isinstance(sample, dict):
            self.pill.setText("读取失败")
            return
        self._sample = sample
        mon = _mon.instance(self._origin)
        running = sample.get("running")
        loaded = sample.get("loaded")
        state = "run" if (running and loaded) else ("idle" if running else "off")
        if state != self._pill_state:      # 2 s 一次重设 QSS 会让标签轻微闪，只在变化时改
            self._pill_state = state
            if state == "run":
                self.pill.setText("运行中")
                self.pill.setStyleSheet(
                    "background:#E8F5EC; border:1px solid #BFE3CB; border-radius:8px;"
                    " padding:4px 10px; font-size:12px; color:#2E7D4F;")
            elif state == "idle":
                self.pill.setText("Router 在跑")
                self.pill.setStyleSheet(
                    "background:#FFF4E5; border:1px solid #F3D6A8; border-radius:8px;"
                    " padding:4px 10px; font-size:12px; color:#9A6212;")
            else:
                self.pill.setText("未运行")
                self.pill.setStyleSheet(
                    f"background:{_t.BG_SUBTLE}; border:1px solid {_t.BORDER};"
                    f" border-radius:8px; padding:4px 10px; font-size:12px;"
                    f" color:{_MUTED};")
        self.pill.setToolTip(
            "Router 进程在跑，但没加载模型：插件发请求时才加载"
            if state == "idle" else "")

        name = sample.get("model") or "（未加载模型）"
        self.model_label.setText(name)
        self.model_label.setToolTip(name)
        ctx = int(sample.get("ctx") or 0)
        prompt = int(sample.get("prompt_tok") or 0)
        cache = int(sample.get("cache_tok") or 0)
        tps = sample.get("tps")

        self.m_tps.set_value("—" if tps is None else f"{tps:.1f}", "每 2 s 差分")
        if ctx:
            ratio = min(1.0, prompt / ctx)
            self.m_ctx.set_value(f"{ratio * 100:.1f}%",
                                 f"{_fmt_int(prompt)} / {_fmt_int(ctx)} tok")
            self.ctx_bar.set_ratio(ratio)
        else:
            self.m_ctx.set_value("—", "模型未加载")
            self.ctx_bar.set_ratio(0.0)
        self.m_prompt.set_value(_fmt_int(prompt) if prompt else "—",
                                "本请求送入的提示词" if prompt else "还没有请求")
        if prompt and cache:
            self.m_cache.set_value(f"{cache / prompt * 100:.0f}%",
                                   f"复用 {_fmt_int(cache)} tok")
        elif prompt:
            self.m_cache.set_value("—", "slot 未报告缓存")
        else:
            self.m_cache.set_value("—", "还没有请求")

        vram_used = int(sample.get("vram_used") or 0)
        vram_total = int(sample.get("vram_total") or 0)
        if vram_total:
            self.vram_text.setText(
                f"显存 {_fmt_int(vram_used)} / {_fmt_int(vram_total)} MB · 整卡")
            self.vram_bar.set_ratio(vram_used / vram_total)
        else:
            self.vram_text.setText("显存 读不到（非 NVIDIA？）")
            self.vram_bar.set_ratio(0.0)
        self.gpu_text.setText(
            f"GPU {int(sample.get('gpu_util') or 0)}% · {int(sample.get('gpu_temp') or 0)}°C")
        self.gpu_label.setText(sample.get("gpu_name") or "")

        self.spark_tps.set_values(mon.series("tps", _SPARK_POINTS))
        self.spark_vram.set_values(mon.series("vram_used", _SPARK_POINTS))

        self._render_history(mon)

        hint = (f"采样间隔 {_INTERVAL_MS // 1000} s · 本地库保留 {_mon.TTL_DAYS} 天 · "
                f"显存是整卡占用（含其它程序）")
        if hint != self._sub_text:      # 每 2 s 重设一次文字会触发整页重排
            self._sub_text = hint
            self.sub_hint.setText(hint)

    def _render_history(self, mon) -> None:
        rows = [] if self._origin == 18091 else mon.history(10)
        sig = (self._origin, len(rows),
               rows[0]["ts"] if rows else 0.0, rows[-1]["ts"] if rows else 0.0)
        if sig == self._hist_sig:      # 没有新请求就不重建行（采样每 2 s 一次）
            return
        self._hist_sig = sig
        while self.hist_box.count():
            item = self.hist_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if self._origin == 18091:
            self.hist_title.setText("外部引擎（dsh 直连）")
            self.hist_hint.setText(
                "dsh 直接请求 18091，不经过 LyLaunch，因此这里只能给模型/上下文/显存这些"
                "进程级数据，没有逐请求明细。需要明细请切到「插件引擎」。")
            return
        self.hist_title.setText("最近请求")
        if not rows:
            self.hist_hint.setText("还没有记录。翻译 / 文档识别 / 本地 AI 跑一次后这里会出现明细。")
            return
        s = mon.summary()
        self.hist_hint.setText(
            f"7 天内共 {_fmt_int(s['count'])} 次 · 送入 {_fmt_int(s['prompt_tok'])} tok / "
            f"输出 {_fmt_int(s['out_tok'])} tok · 平均 {s['avg_tps']} t/s")
        for r in rows:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 4, 0, 4)
            rl.setSpacing(10)
            t = QLabel(_fmt_time(r["ts"]))
            t.setStyleSheet(f"font-size:11px; color:{_MUTED};")
            t.setFixedWidth(56)
            model = QLabel(str(r["model"]))
            model.setStyleSheet(f"font-size:12px; color:{_t.TEXT_PRIMARY};")
            model.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            toks = QLabel(f"{_fmt_int(r['prompt_tok'])} → {_fmt_int(r['out_tok'])} tok")
            toks.setStyleSheet(f"font-size:11px; color:{_MUTED};")
            toks.setFixedWidth(110)
            dur = QLabel(f"{r['dur_ms'] / 1000:.2f} s")
            dur.setStyleSheet(f"font-size:11px; color:{_MUTED};")
            dur.setFixedWidth(52)
            tp = QLabel(f"{r['tps']:.1f} t/s" if r["tps"] else "—")
            tp.setStyleSheet(f"font-size:11px; color:{_MUTED if not r['tps'] else _t.ACCENT};")
            tp.setFixedWidth(60)
            rl.addWidget(t)
            rl.addWidget(model, 1)
            rl.addWidget(toks)
            rl.addWidget(dur)
            rl.addWidget(tp)
            self.hist_box.addWidget(row)


def build_monitor_block(host, lay: QVBoxLayout) -> _MonitorSection:
    """设置窗「外部集成 → 运行监控」页的装配入口。"""
    section = _MonitorSection(host, lay)
    host.monitor_section = section
    return section
