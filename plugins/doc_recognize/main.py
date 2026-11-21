"""文档识别插件：本地大模型（Xiaomi-OCR-0 via llama.cpp）读表格 / 公式 / 手写。

形态与「图片转文字」刻意区分：那边是"截一段字、复制走"的轻工具（上下一条流），
这边是"把一页纸变成一份文档"的编辑器形态 —— 左原图、右渲染后的文档预览，
结果可以导出成 网页（HTML）/ Markdown / 纯文本。

进程由 doc_engine 管理：打开弹窗自动拉起、关闭后空闲 N 分钟释放，
并绑了 Windows Job Object（主程序被强杀时服务也会被系统清理，不留孤儿进程）。
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QImage
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from lylaunch.plugins.base import PluginBase
from lylaunch.plugins import theme
from lylaunch.plugins.plugin_icons import plugin_icon_svg, render_plugin_svg

import doc_engine as eng
import md_render
import inputs as inp


def _dbg_print(*args, **kwargs):
    """调试输出已停用；保留调用以维持语句结构。"""
    pass


# 提示词不再写死在这里：每个模型有自己训练时用的 prompt，见 eng.MODELS。
MAX_SIDE = 1600
_IDLE_TIMER: QTimer | None = None

_PLACEHOLDER = (
    "<div style='color:#5F6570; font-size:13px; padding:24px 8px; line-height:180%;'>"
    "识别结果会显示在这里。<br><br>"
    "左边放文件，右边看结果；表格会渲染成真正的表格，不再是一堆竖线。<br>"
    "完成后可导出 <b>网页（HTML）</b>、<b>Markdown</b> 或纯文本。"
    "</div>"
)


def _data_dir() -> Path:
    """模型工作目录，必须与「图片转文字」插件一致（见 doc_core.shared_data_dir）。

    0.36 起两个插件共用一份模型，get_engine 的缓存 key 含这个路径 ——
    各传各的目录就会各起一个 llama-server，白占一倍显存。
    """
    return eng.shared_data_dir()


def _cancel_release() -> None:
    if _IDLE_TIMER is not None and _IDLE_TIMER.isActive():
        _IDLE_TIMER.stop()


def _schedule_release(engine: eng.Engine, seconds: int) -> None:
    """关掉弹窗后延迟释放进程：频繁开关不必每次重拉，闲置久了要把 1GB 内存还回去。"""
    global _IDLE_TIMER
    if _IDLE_TIMER is None:
        _IDLE_TIMER = QTimer()
        _IDLE_TIMER.setSingleShot(True)
    try:
        _IDLE_TIMER.timeout.disconnect()
    except Exception:
        pass
    _IDLE_TIMER.timeout.connect(engine.stop)
    _IDLE_TIMER.start(max(30, int(seconds)) * 1000)


# ── 可选识别模型面板 ──────────────────────────────────────────────
# 刻意做成主窗口内的普通 QFrame 子控件，不用 Qt.Popup 顶层窗口：
# Windows 下 Popup 会带系统标题栏、不继承样式，之前还出过黑边和按钮点不动。
# 0.36：私有 QSS 的色值全部收敛到定稿色板（与 lylaunch/plugins/theme.py 同步），
# 主色 #2F6BEE / 次要 #5F6570 / 主文字 #161C28 / 描边 #D8E3F5 / 悬停 #E4EDFC。
_PANEL_QSS = """
QFrame#modelPanel { background:#FFFFFF; border:1px solid #D8E3F5; border-radius:10px; }
QFrame#modelRow { background:#FFFFFF; border:none; border-bottom:1px solid #E8EDF6; }
QLabel#mName { color:#161C28; font-size:13px; font-weight:600; }
QLabel#mNote { color:#5F6570; font-size:11px; }
QLabel#mMeta { color:#5F6570; font-size:10px; }
QLabel#mBadge { color:#2F6BEE; font-size:10px; }
QLabel#mSection { color:#5F6570; font-size:10px; padding-top:6px; }
QPushButton#mBtn { border:1px solid #C3D3EE; border-radius:6px; background:#FFFFFF;
                   color:#2F6BEE; padding:4px 10px; font-size:11px; }
QPushButton#mBtn:hover { background:#E4EDFC; }
QPushButton#mBtn:disabled { color:#5F6570; border-color:#D8E3F5; background:#F2F6FD; }
QPushButton#mUn { border:none; background:transparent; color:#5F6570;
                  padding:4px 2px; font-size:11px; }
QPushButton#mUn:hover { color:#D93A2F; }
QPushButton#mClose { border:none; background:transparent; color:#5F6570; font-size:14px; }
QPushButton#mClose:hover { color:#161C28; }
QFrame#mConfirm { background:#FDECEA; border:1px solid #F5B3AD; border-radius:6px; }
QLabel#mWarn { color:#5F6570; font-size:11px; }
QPushButton#mDanger { border:1px solid #D93A2F; border-radius:6px; background:#D93A2F;
                      color:#FFFFFF; padding:4px 10px; font-size:11px; }
QPushButton#mDanger:hover { background:#B92C24; }
QPushButton#mCancel { border:1px solid #C3D3EE; border-radius:6px; background:#FFFFFF;
                      color:#5F6570; padding:4px 10px; font-size:11px; }
QPushButton#mCancel:hover { background:#E4EDFC; }
"""


_BUBBLE_QSS = """
QFrame#mDetail { background:#FFFFFF; border:1px solid #D8E3F5; border-radius:10px; }
QLabel#dName { color:#161C28; font-size:13px; font-weight:600; }
QLabel#dState { color:#5F6570; font-size:10px; }
QLabel#dStateCur { color:#2F6BEE; background:#E4EDFC; border:1px solid #C3D3EE;
                   border-radius:9px; padding:2px 9px; font-size:10px; }
QLabel#dStateOk { color:#2F6BEE; font-size:10px; }
QLabel#dDesc { color:#161C28; font-size:11px; }
QLabel#dMeta { color:#5F6570; font-size:10px; }
QPushButton#mUn { border:1px solid #C3D3EE; border-radius:6px; background:#FFFFFF;
                  color:#D93A2F; padding:5px 10px; font-size:11px; }
QPushButton#mUn:hover { background:#FDECEA; border-color:#F5B3AD; }
QFrame#mConfirm { background:#FDECEA; border:1px solid #F5B3AD; border-radius:6px; }
QLabel#mWarn { color:#5F6570; font-size:11px; }
QPushButton#mDanger { border:1px solid #D93A2F; border-radius:6px; background:#D93A2F;
                      color:#FFFFFF; padding:4px 10px; font-size:11px; }
QPushButton#mDanger:hover { background:#B92C24; }
QPushButton#mCancel { border:1px solid #C3D3EE; border-radius:6px; background:#FFFFFF;
                      color:#5F6570; padding:4px 10px; font-size:11px; }
QPushButton#mCancel:hover { background:#E4EDFC; }
"""


class ModelDetailBubble(QFrame):
    """模型明细气泡：鼠标停在哪一行，就在面板旁浮出详情卡片。

    对齐本地 AI 插件的 ModelInfoCard 观感。行内只保留「切换 / 下载并启用」，
    **卸载（含确认条）挪到气泡里**，列表更干净、明细和操作也在一处。
    """

    WIDTH = 250
    GAP = 8

    def __init__(self, panel) -> None:
        super().__init__(panel.parent())
        self.panel = panel
        self.setObjectName("mDetail")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(self.WIDTH)
        self.setStyleSheet(_BUBBLE_QSS)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(13, 12, 13, 12)
        lay.setSpacing(7)

        self.d_name = QLabel("—")
        self.d_name.setObjectName("dName")
        self.d_name.setWordWrap(True)
        lay.addWidget(self.d_name)

        # 状态做成小胶囊：套一层 HBox + stretch，QLabel 才不会铺满整行
        # 变成一条浅蓝横幅（0.37 视觉修正）。
        state_row = QHBoxLayout()
        state_row.setSpacing(0)
        self.d_state = QLabel("—")
        self.d_state.setObjectName("dState")
        state_row.addWidget(self.d_state)
        state_row.addStretch(1)
        lay.addLayout(state_row)

        self.d_note = QLabel("")
        self.d_note.setObjectName("dDesc")
        self.d_note.setWordWrap(True)
        lay.addWidget(self.d_note)

        self.d_points = QLabel("")
        self.d_points.setObjectName("dDesc")
        self.d_points.setWordWrap(True)
        self.d_points.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.d_points)

        self.d_meta = QLabel("")
        self.d_meta.setObjectName("dMeta")
        self.d_meta.setWordWrap(True)
        lay.addWidget(self.d_meta)

        lay.addStretch(1)
        hint = QLabel("在左侧列表里切换 / 下载")
        hint.setObjectName("dMeta")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.un_btn = QPushButton("卸载模型")
        self.un_btn.setObjectName("mUn")
        self.un_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.un_btn.clicked.connect(self._ask_remove)
        lay.addWidget(self.un_btn)
        self._confirm = None
        self._spec = None
        self._active = False

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(200)
        self._hide_timer.timeout.connect(self.hide)
        try:
            from PySide6.QtWidgets import QGraphicsDropShadowEffect
            from PySide6.QtGui import QColor
            sh = QGraphicsDropShadowEffect(self)
            sh.setBlurRadius(20)
            sh.setOffset(0, 6)
            sh.setColor(QColor(28, 34, 48, 46))
            self.setGraphicsEffect(sh)
        except Exception:
            pass
        self.hide()

    def show_for(self, spec, installed: bool, active: bool, row) -> None:
        from html import escape as _esc
        self._hide_timer.stop()
        self._spec = spec
        self._active = active
        self.collapse_confirm()
        self.un_btn.setVisible(installed and self.panel._on_remove is not None)
        state = "使用中" if active else ("已安装" if installed else "未安装")
        self.d_name.setText(spec.name)
        self.d_state.setText(state)
        self.d_state.setObjectName("dStateCur" if active else
                                   ("dStateOk" if installed else "dState"))
        st = self.d_state.style()
        if st is not None:
            st.unpolish(self.d_state)
            st.polish(self.d_state)
        self.d_note.setText(spec.note)
        self.d_points.setText("<br>".join(
            f"<span style='color:#2F6BEE;'>·</span> {_esc(p)}"
            for p in spec.points))
        # 只给大小与显存：许可协议 / 「可商用」这类文字不属于用户需要看的信息
        self.d_meta.setText(" · ".join(
            p for p in (f"约 {spec.total_mb}MB", spec.vram) if p))
        self._place(row)
        self.show()
        self.raise_()

    def _place(self, row) -> None:
        self.adjustSize()
        dlg = self.window()
        px, pw = self.panel.x(), self.panel.width()
        cw, ch = self.width(), self.height()
        dw, dh = dlg.width(), dlg.height()
        if px + pw + self.GAP + cw <= dw - 8:
            x = px + pw + self.GAP
        elif px - self.GAP - cw >= 8:
            x = px - self.GAP - cw
        else:
            x = max(8, dw - cw - 12)
        y = row.mapTo(dlg, row.rect().topLeft()).y() - 4 if row else self.panel.y()
        self.move(x, max(8, min(y, dh - ch - 8)))

    # ---- 卸载（从列表行挪进气泡；确认条也在这里展开）
    def _ask_remove(self) -> None:
        if self._spec is None:
            return
        if self._confirm is None:
            self._confirm = self._build_confirm()
            self.layout().addWidget(self._confirm)
        tail = "当前正在使用，会先停掉模型。" if self._active else ""
        self._confirm._msg.setText(
            f"卸载 {self._spec.name}？{tail}将释放约 {self._spec.total_mb}MB，"
            "以后要用得重新下载。")
        self._confirm.setVisible(True)
        self.un_btn.setVisible(False)
        self.adjustSize()

    def _build_confirm(self) -> QFrame:
        box = QFrame()
        box.setObjectName("mConfirm")
        v = QVBoxLayout(box)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)
        msg = QLabel()
        msg.setObjectName("mWarn")
        msg.setWordWrap(True)
        v.addWidget(msg)
        box._msg = msg          # 每次展开时按当前模型刷新文案
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("mCancel")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(lambda: self.collapse_confirm(True))
        ok = QPushButton("确定卸载")
        ok.setObjectName("mDanger")
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.clicked.connect(self._do_remove)
        row.addWidget(cancel)
        row.addWidget(ok)
        v.addLayout(row)
        return box

    def collapse_confirm(self, restore: bool = False) -> None:
        """收起确认条；restore=True 时顺带恢复「卸载模型」按钮。"""
        if self._confirm is not None:
            self._confirm.hide()
        if restore:
            self.un_btn.setVisible(True)
        self.adjustSize()

    def _do_remove(self) -> None:
        spec = self._spec
        self.collapse_confirm()
        self.hide()
        if spec is not None and self.panel._on_remove is not None:
            self.panel._on_remove(spec.id)

    def schedule_hide(self) -> None:
        self._hide_timer.start()

    def enterEvent(self, e) -> None:  # noqa: N802
        self._hide_timer.stop()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self.hide()
        super().leaveEvent(e)


# 队列（文件列表）区域的样式（0.36 定稿色板，同上）
_QUEUE_QSS = """
QFrame#dropZone { border:1px dashed #C3D3EE; border-radius:10px; background:#F8FBFF; }
QFrame#dropZone:hover { border-color:#2F6BEE; background:#F2F6FD; }
QLabel#dzTitle { color:#5F6570; font-size:13px; }
QLabel#dzSub { color:#5F6570; font-size:11px; }
QFrame#queueRow { background:#FFFFFF; border:1px solid #D8E3F5; border-radius:8px; }
QFrame#queueRow:hover { border-color:#C3D3EE; }
QFrame#queueRow[selected="true"] { background:#F2F6FD; border-color:#BFD3FA; }
QLabel#qName { color:#161C28; font-size:12px; font-weight:600; }
QLabel#qMeta { color:#5F6570; font-size:10px; }
QPushButton#qRemove { border:none; background:transparent; color:#7C8798; font-size:15px; font-weight:600; padding:0; margin:0; }
QPushButton#qRemove:hover { color:#D93A2F; }
"""


class QueueItem:
    """队列里的一个文件。

    results[i] 是第 i 页的 Markdown：文本类页（Excel / Word / TXT / PDF 文字层）
    在入队时就已经填好了，只有图片页要等模型识别。

    ⚠️ 这里**故意不用 @dataclass**：插件入口是被
    `lylaunch/plugins/manager.py` 用 `spec_from_file_location` + `exec_module`
    加载的，**没有注册进 sys.modules**；而 Python 3.14 的 dataclasses 解析字段
    类型时会去查 `sys.modules[cls.__module__].__dict__`，拿到 None 直接抛
    `AttributeError: 'NoneType' object has no attribute '__dict__'`，
    插件在加载阶段就炸了（表现为"插件打不开"）。
    插件入口里的类一律用普通类，dataclass 只放在被普通 import 的模块里
    （如 doc_engine.ModelSpec 就没问题）。
    """
    __slots__ = ("loaded", "results", "error", "errors")

    def __init__(self, loaded, results=None, error: str = "") -> None:
        self.loaded = loaded
        self.results = list(results) if results else []
        self.error = error
        self.errors: dict = {}          # 页下标 → 失败原因

    @property
    def name(self) -> str:
        return self.loaded.name

    @property
    def pages(self) -> list:
        return self.loaded.pages

    @property
    def kind(self) -> str:
        return self.loaded.kind

    @property
    def model_pages(self) -> list[int]:
        """需要模型识别的页下标。"""
        return [i for i, p in enumerate(self.pages) if p.image is not None]

    def _page_md(self, i: int) -> str:
        return self.results[i] if 0 <= i < len(self.results) else ""

    @property
    def pending_pages(self) -> list[int]:
        return [i for i in self.model_pages if not self._page_md(i).strip()]

    @property
    def done_pages(self) -> int:
        return sum(1 for i in self.model_pages if self._page_md(i).strip())

    def status_text(self) -> str:
        """页数一律报**文件真实总页数**（len(pages)），不要只报需要模型识别的页数。

        之前写「PDF · 3 页」其实只数了图片页（1/3/5），而一个 5 页 PDF 里
        第 2/4 页有文字层直接解析；下拉却列出全部 5 页 → 看起来像「3 页切成 5 页」。
        """
        if self.error:
            return f"读取失败：{self.error}"
        kind = inp.SUPPORT_HINT.get(self.kind, self.kind)
        total = len(self.pages)
        need = len(self.model_pages)
        if not need:
            return f"{kind} · {total} 页 · 已直接解析"
        done = self.done_pages
        if done >= need:
            return f"{kind} · {total} 页 · 已完成"
        return f"{kind} · {total} 页 · 待识别 {need - done}/{need} 页"


class QueueRow(QFrame):
    """队列里的一行：文件名 / 状态 / 移除。"""
    def __init__(self, index: int, item: QueueItem,
                 on_select, on_remove, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("queueRow")
        self.index = index
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 7, 6, 7)
        lay.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.name = QLabel(item.name)
        self.name.setObjectName("qName")
        # 名字可压缩：否则长文件名把整行撑得比可视区还宽，最右侧的删除按钮会被挤出/裁掉
        self.name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.name.setMinimumWidth(0)
        top.addWidget(self.name, 1)
        self.remove_btn = QPushButton("×")
        self.remove_btn.setObjectName("qRemove")
        self.remove_btn.setFixedSize(24, 24)
        # 必须内联写 padding:0：theme.DIALOG_QSS 的全局规则
        # 「QDialog QPushButton { padding: 7px 14px }」会把 22~24px 的方形按钮
        # 内容区挤成负数，× 直接画不出来（表现为「删除按钮不显示」）。
        # 内联样式优先级最高，能压住所有全局/父级 QSS。
        self.remove_btn.setStyleSheet(
            "QPushButton{border:none;background:transparent;color:#7C8798;"
            "padding:0;margin:0;font-size:16px;font-weight:700;}"
            "QPushButton:hover{color:#D93A2F;background:#FDECEA;border-radius:6px;}"
            "QPushButton:pressed{color:#B92C24;background:#FBDDD9;}")
        self.remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_btn.clicked.connect(
            lambda _c=False, _i=index: on_remove(_i))
        top.addWidget(self.remove_btn)
        lay.addLayout(top)

        self.meta = QLabel(item.status_text())
        self.meta.setObjectName("qMeta")
        self.meta.setWordWrap(True)
        self.meta.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.meta.setMinimumWidth(0)
        lay.addWidget(self.meta)

    def update_item(self, item: QueueItem) -> None:
        self.name.setText(item.name)
        self.meta.setText(item.status_text())

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", "true" if selected else "false")
        st = self.style()
        if st is not None:
            # QSS 属性选择器不会自动重算，必须 unpolish/polish
            st.unpolish(self)
            st.polish(self)
        self.update()


class BatchWorker(QThread):
    """按页串行识别。

    必须串行：一个 llama-server 实例扛不住并发请求，并发只会排队 +
    把上下文顶爆；而且用户要的也是"一份一份来"，能随时停。
    """
    page_done = Signal(int, int, str, float)   # 文件下标, 页下标, 原文, 秒
    page_failed = Signal(int, int, str)
    progress = Signal(int, int)                # 已完成, 总数
    all_done = Signal(bool)                    # True = 全部跑完, False = 被取消

    def __init__(self, engine: eng.Engine, tasks) -> None:
        super().__init__()
        self._engine = engine
        self._tasks = tasks          # [(文件下标, 页下标, png bytes)]
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        total = len(self._tasks)
        done = 0
        for (ii, pi, png) in self._tasks:
            if self._cancelled:
                break
            try:
                text, sec = self._engine.recognize(png, None)
            except eng.EngineError as e:
                self.page_failed.emit(ii, pi, str(e))
            except Exception as e:  # noqa: BLE001
                self.page_failed.emit(ii, pi, f"{type(e).__name__}：{e}")
            else:
                self.page_done.emit(ii, pi, text, sec)
            done += 1
            self.progress.emit(done, total)
        self.all_done.emit(not self._cancelled)


def _png_bytes(img: QImage) -> bytes:
    """QImage → PNG bytes；长边超 MAX_SIDE 就先缩，避免顶爆视觉上下文。

    必须在主线程做：QImage 不是线程安全的。
    """
    from PySide6.QtCore import QBuffer, QIODevice
    send = img
    if max(img.width(), img.height()) > MAX_SIDE:
        send = img.scaled(MAX_SIDE, MAX_SIDE,
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    send.save(buf, "PNG")
    return bytes(buf.data())


class DropZone(QFrame):
    """可点击的拖放区。"""
    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        super().mousePressEvent(event)


class ModelRow(QFrame):
    """一行模型：名称 / 状态 / 简介 / 体积·显存·协议 / 操作。

    行内只放「切换 / 下载并启用」；**卸载（含确认条）挪到悬停气泡里**，
    明细也由气泡承载，列表更干净、明细和操作在一处。
    """
    def __init__(self, spec, installed: bool, active: bool,
                 on_use, on_install, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("modelRow")
        self._spec = spec
        self._active = active
        self._installed = installed
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(8)
        name = QLabel(spec.name)
        name.setObjectName("mName")
        top.addWidget(name)
        badge = QLabel("使用中" if active else ("已安装" if installed else "未安装"))
        badge.setObjectName("mBadge")
        top.addWidget(badge)
        top.addStretch(1)
        if active:
            btn = QPushButton("使用中")
            btn.setEnabled(False)
        elif installed:
            btn = QPushButton("切换")
            # 用默认参数绑住 id：直接在 lambda 里写 spec.id 会闭到循环最后一次的值
            btn.clicked.connect(lambda _checked=False, _id=spec.id: on_use(_id))
        else:
            btn = QPushButton("下载并启用")
            btn.clicked.connect(lambda _checked=False, _id=spec.id: on_install(_id))
        btn.setObjectName("mBtn")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        top.addWidget(btn)

        lay.addLayout(top)

        note = QLabel(spec.note)
        note.setObjectName("mNote")
        note.setWordWrap(True)
        lay.addWidget(note)

        meta = QLabel(" · ".join(p for p in (f"约 {spec.total_mb}MB", spec.vram) if p))
        meta.setObjectName("mMeta")
        meta.setWordWrap(True)
        lay.addWidget(meta)
        # 明细改由悬停气泡承载（不再用系统原生 tooltip）

    def enterEvent(self, e) -> None:  # noqa: N802
        panel = self.parent()
        if isinstance(panel, ModelPanel):
            panel.show_detail(self)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802
        panel = self.parent()
        if isinstance(panel, ModelPanel):
            panel.schedule_hide_detail()
        super().leaveEvent(e)

    # 卸载入口与确认条已移到 ModelDetailBubble（见上方类）


class ModelPanel(QFrame):
    def __init__(self, parent, on_use, on_install, on_remove=None) -> None:
        super().__init__(parent)
        self.setObjectName("modelPanel")
        self.setFrameShape(QFrame.Shape.NoFrame)   # 默认 StyledPanel 会画黑边
        self.setFixedWidth(360)
        self.setStyleSheet(_PANEL_QSS)
        self._on_use = on_use
        self._on_install = on_install
        self._on_remove = on_remove
        self.setVisible(False)
        self.detail = ModelDetailBubble(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 10)
        root.setSpacing(4)
        head = QHBoxLayout()
        title = QLabel("识别模型")
        title.setObjectName("mName")
        head.addWidget(title)
        head.addStretch(1)
        close = QPushButton("×")
        close.setObjectName("mClose")
        close.setFixedSize(22, 22)
        close.clicked.connect(self.hide)
        head.addWidget(close)
        root.addLayout(head)

        self._list = QVBoxLayout()
        self._list.setSpacing(0)
        root.addLayout(self._list)

    def show_detail(self, row) -> None:
        self.detail.show_for(row._spec, row._installed, row._active, row)

    def schedule_hide_detail(self) -> None:
        self.detail.schedule_hide()

    def hide(self) -> None:
        if hasattr(self, "detail"):
            self.detail.hide()
        super().hide()

    def collapse_confirms(self, keep=None) -> None:
        """卸载确认条现在只存在于气泡里，同一时间天然只有一条。"""
        if hasattr(self, "detail"):
            self.detail.collapse_confirm()

    def refresh(self, current_id: str) -> None:
        self.detail.hide()
        # 清旧行用 hide() + deleteLater()，别用 setParent(None)
        # （那会造出游离的顶层小窗口，之前在新会话列表上踩过）。
        while self._list.count():
            item = self._list.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        installed = [m for m in eng.MODELS if eng.is_installed(m.id)]
        missing = [m for m in eng.MODELS if not eng.is_installed(m.id)]
        for spec in installed:
            self._list.addWidget(ModelRow(spec, True, spec.id == current_id,
                                          self._on_use, self._on_install, self))
        if missing:
            tip = QLabel(f"＋ 可选模型下载（{len(missing)}）")
            tip.setObjectName("mSection")
            self._list.addWidget(tip)
            for spec in missing:
                self._list.addWidget(ModelRow(spec, False, False,
                                              self._on_use, self._on_install, self))
        self.adjustSize()


class _ProbeWorker(QThread):
    """后台问一次「模型在跑吗 / 文件在吗」。

    为什么不在打开窗口时同步问：`engine.ready()` 是一次 HTTP 请求（GET /models），
    router 正在加载或测显存时要等好几秒 —— 挡在打开窗口这条路上就是「卡两三秒，
    连拖文件都点不动」。放到线程里，窗口立刻可用。
    """

    done = Signal(bool, bool)      # (已加载, 文件齐)

    def __init__(self, engine, parent=None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            running = bool(self._engine.ready())
            files = bool(self._engine.has_files())
        except Exception:
            running, files = False, False
        if not self._cancel:
            self.done.emit(running, files)


class PrepareWorker(QThread):
    """后台准备模型：检查/下载运行时与模型 → 起进程 → 健康检查 → 预热。"""

    progress = Signal(str, object)
    ready = Signal()
    failed = Signal(str)

    def __init__(self, engine: eng.Engine) -> None:
        super().__init__()
        self._engine = engine
        self._cancelled = False

    def cancel(self) -> None:
        """关窗口时中断下载：模型的进度回调会把这个变成中断异常。"""
        self._cancelled = True

    def _progress(self, doc: str, pct) -> None:
        if self._cancelled:
            raise eng.Cancelled()
        self.progress.emit(doc, pct)

    def run(self) -> None:
        try:
            # cancelled 回调必须传下去：下载循环每个 chunk 都会查它（0.39.1）
            self._engine.start(self._progress, lambda: self._cancelled)
            self.ready.emit()
        except eng.Cancelled:
            pass            # 用户主动取消，不算错误
        except eng.EngineError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"模型准备失败（{type(e).__name__}）：{e}")


class DocDialog(QDialog):
    def __init__(self, api, settings, parent=None, initial_paths=None) -> None:
        super().__init__(parent)
        self._api = api
        self._settings = settings
        self._items: list[QueueItem] = []      # 队列里的文件
        self._rows: list[QueueRow] = []        # 队列行（与 _items 同序）
        self._cur_item = -1                    # 当前查看的文件下标
        self._cur_page = 0                     # 当前查看的页下标
        self._prep: PrepareWorker | None = None
        self._probe: _ProbeWorker | None = None    # 打开时的状态探测（后台线程）
        self._worker: BatchWorker | None = None
        self._prepared = False
        self._panel: ModelPanel | None = None
        self.setWindowTitle("文档识别")
        self.resize(920, 620)
        self.setMinimumSize(760, 540)
        self.setAcceptDrops(True)

        # engine 必须先建好：顶栏按钮要显示当前选中的模型名。
        self._port = int(self._cfg("port", eng.DEFAULT_PORT) or eng.DEFAULT_PORT)
        self._model_id = eng.spec_of(
            self._cfg("model_id", eng.DEFAULT_MODEL_ID)).id
        self.engine = eng.get_engine(_data_dir(), self._port, self._model_id)

        self._build_ui()
        self.setStyleSheet(theme.DIALOG_QSS)
        from lylaunch.plugins.window_chrome import install_chrome
        install_chrome(self, "文档识别", show_minmax=True, resizable=True)
        _cancel_release()
        if self._cfg("auto_start", True):
            QTimer.singleShot(120, self._prepare)

        # 主搜索框粘贴进来的文件：只入队，不自动开始批量识别。
        # _start_batch 要求模型已启动，自动开跑会拉起约 965MB 常驻模型，
        # 应由用户点「开始识别」决定。singleShot(0) 等窗口 show 完再刷队列 UI。
        if initial_paths:
            QTimer.singleShot(0, lambda: self._add_paths(initial_paths))

    def _cfg(self, key: str, default):
        try:
            v = self._api.settings_get(key, None)
            if v is not None:
                return v
        except Exception:
            pass
        return self._settings.get(key, default)

    # ---------- UI ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 16)
        root.setSpacing(12)

        # 图标头部（0.36 通用壳）：40px 线性 SVG 图标块 + 标题
        head = QHBoxLayout()
        head.setSpacing(12)
        icon = QLabel()
        icon.setFixedSize(40, 40)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        icon.setStyleSheet(f"background:{theme.PLUGIN_BG}; border-radius:10px;")
        icon.setPixmap(render_plugin_svg(plugin_icon_svg("doc"), 24))
        head.addWidget(icon)
        title = QLabel("文档识别")
        title.setProperty("role", "title")
        head.addWidget(title)
        head.addStretch(1)
        self.model_btn = QPushButton(f"{self.engine.spec.name}  ▾")
        self.model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.model_btn.setStyleSheet(
            "QPushButton{border:1px solid #C3D3EE;border-radius:6px;background:#FFFFFF;"
            "color:#161C28;padding:5px 10px;font-size:12px;}"
            "QPushButton:hover{background:#E4EDFC;border-color:#BFD3FA;}")
        self.model_btn.clicked.connect(self._toggle_panel)
        head.addWidget(self.model_btn)
        # 手动启停：模型常驻会占几百 MB 内存，用户要能自己关掉。
        # 0.39.1 起这颗按钮常驻，文字随状态变（停止 / 启动 / 下载并启动），
        # 不再另放「模型已启动 · 未启动」徽章。
        self.power_btn = QPushButton("启动模型")
        self.power_btn.setMinimumSize(96, 32)
        self.power_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.power_btn.setVisible(True)
        self.power_btn.clicked.connect(self._toggle_engine)
        head.addWidget(self.power_btn)
        root.addLayout(head)

        self.sub = QLabel("把一页纸变成一份文档：表格还原成真表格，可导出网页 / Markdown / 纯文本")
        self.sub.setProperty("role", "subtitle")
        self.sub.setWordWrap(True)
        root.addWidget(self.sub)

        self.progress_box = QWidget()
        pb = QVBoxLayout(self.progress_box)
        pb.setContentsMargins(0, 0, 0, 0)
        pb.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.prepare_text = QLabel("正在检查识别模型…")
        self.prepare_text.setProperty("role", "status")
        self.prepare_text.setWordWrap(True)
        row.addWidget(self.prepare_text, 1)
        # 下载入口统一收在右上角那颗按钮上（缺模型时显示「下载并启动」），
        # 这里不再重复放一个，避免同一件事出现两个蓝按钮。
        pb.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        pb.addWidget(self.progress)
        root.addWidget(self.progress_box)

        # ── 主体：左队列 / 右预览
        body = QHBoxLayout()
        body.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(8)

        self.drop_zone = DropZone()
        self.drop_zone.setObjectName("dropZone")
        self.drop_zone.setCursor(Qt.CursorShape.PointingHandCursor)
        dz = QVBoxLayout(self.drop_zone)
        dz.setContentsMargins(12, 14, 12, 14)
        dz.setSpacing(3)
        dz_title = QLabel("把文件拖到这里")
        dz_title.setObjectName("dzTitle")
        dz_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dz.addWidget(dz_title)
        dz_sub = QLabel("PDF · 图片 · Excel · Word · TXT")
        dz_sub.setObjectName("dzSub")
        dz_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dz.addWidget(dz_sub)
        self.drop_zone.clicked.connect(self._pick)
        left.addWidget(self.drop_zone)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.add_btn = QPushButton("添加文件")
        self.paste_btn = QPushButton("粘贴截图")
        row1.addWidget(self.add_btn)
        row1.addWidget(self.paste_btn)
        left.addLayout(row1)

        q_head = QHBoxLayout()
        q_head.setSpacing(6)
        self.queue_label = QLabel("队列（0）")
        self.queue_label.setProperty("role", "section")
        q_head.addWidget(self.queue_label)
        q_head.addStretch(1)
        self.clear_btn = QPushButton("清空")
        self.clear_btn.setProperty("role", "ghost")
        self.clear_btn.clicked.connect(self._clear_queue)
        q_head.addWidget(self.clear_btn)
        left.addLayout(q_head)

        self.queue_area = QScrollArea()
        self.queue_area.setWidgetResizable(True)
        self.queue_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.queue_box = QWidget()
        self.queue_box.setStyleSheet(_QUEUE_QSS)
        self.queue_layout = QVBoxLayout(self.queue_box)
        self.queue_layout.setContentsMargins(0, 0, 0, 0)
        self.queue_layout.setSpacing(4)
        self.queue_layout.addStretch(1)
        self.queue_area.setWidget(self.queue_box)
        left.addWidget(self.queue_area, 1)

        left_box = QWidget()
        left_box.setLayout(left)
        left_box.setFixedWidth(320)
        body.addWidget(left_box)

        right = QVBoxLayout()
        right.setSpacing(8)
        r_head = QHBoxLayout()
        r_head.setSpacing(6)
        self.view_combo = QComboBox()
        self.view_combo.setMinimumWidth(190)
        self.view_combo.currentIndexChanged.connect(self._on_view_changed)
        r_head.addWidget(self.view_combo)
        self.doc_label = QLabel("文档预览")
        self.doc_label.setProperty("role", "section")
        r_head.addWidget(self.doc_label)
        r_head.addStretch(1)
        self.html_btn = QPushButton("导出网页")
        self.html_btn.setProperty("role", "ghost")
        self.md_btn = QPushButton("Markdown")
        self.md_btn.setProperty("role", "ghost")
        self.plain_btn = QPushButton("纯文本")
        self.plain_btn.setProperty("role", "ghost")
        self.copy_btn = QPushButton("复制")
        self.copy_btn.setProperty("role", "ghost")
        for b in (self.html_btn, self.md_btn, self.plain_btn, self.copy_btn):
            r_head.addWidget(b)
        self.all_btn = QPushButton("导出全部 ▾")
        self.all_btn.setProperty("role", "ghost")
        _all_menu = QMenu(self.all_btn)
        _all_menu.addAction("网页（HTML）", lambda: self._export("html", True))
        _all_menu.addAction("Markdown", lambda: self._export("md", True))
        _all_menu.addAction("纯文本", lambda: self._export("txt", True))
        self.all_btn.setMenu(_all_menu)
        r_head.addWidget(self.all_btn)
        right.addLayout(r_head)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self._set_doc_html(_PLACEHOLDER)
        right.addWidget(self.view, 1)
        body.addLayout(right, 1)

        root.addLayout(body, 1)

        # ── 底部：状态 + 批量进度 + 开始 / 取消
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.status = QLabel("拖入文件或点「添加文件」，可一次放多个")
        self.status.setProperty("role", "status")
        bottom.addWidget(self.status, 1)
        self.batch_bar = QProgressBar()
        self.batch_bar.setFixedWidth(170)
        self.batch_bar.setTextVisible(False)
        self.batch_bar.setVisible(False)
        bottom.addWidget(self.batch_bar)
        self.run_btn = QPushButton("开始识别")
        self.run_btn.setProperty("role", "primary")
        self.run_btn.setMinimumHeight(32)
        self.run_btn.setEnabled(False)
        bottom.addWidget(self.run_btn)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setMinimumHeight(32)
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self._cancel_batch)
        bottom.addWidget(self.cancel_btn)
        root.addLayout(bottom)

        self.add_btn.clicked.connect(self._pick)
        self.paste_btn.clicked.connect(self._paste)
        self.run_btn.clicked.connect(self._start_batch)
        self.copy_btn.clicked.connect(self._copy)
        self.html_btn.clicked.connect(lambda: self._export("html", False))
        self.md_btn.clicked.connect(lambda: self._export("md", False))
        self.plain_btn.clicked.connect(lambda: self._export("txt", False))

    def _set_status(self, msg: str) -> None:
        self.status.setText(msg)

    def _set_doc_html(self, inner_html: str) -> None:
        self.view.setHtml(
            "<html><head><style>" + md_render._WIDGET_CSS + "</style></head><body>"
            + inner_html + "</body></html>")

    # ---------- 可选模型 ----------
    def _toggle_panel(self) -> None:
        if self._panel is None:
            self._panel = ModelPanel(self, self._on_use_model,
                                     self._on_install_model, self._on_remove_model)
        # 用 isHidden() 而不是 isVisible()：后者依赖父窗口可见性，
        # 窗口还没 show / 被最小化时永远是 False，按钮会变成"只开不收"。
        if not self._panel.isHidden():
            self._panel.hide()
            return
        self._panel.refresh(self._model_id)
        anchor = self.model_btn.mapTo(self, QPoint(0, self.model_btn.height() + 6))
        x = max(8, min(anchor.x(), self.width() - self._panel.width() - 12))
        self._panel.move(x, anchor.y())
        self._panel.raise_()
        self._panel.show()

    def _busy(self) -> bool:
        return ((self._prep is not None and self._prep.isRunning())
                or (self._worker is not None and self._worker.isRunning()))

    def _update_model_label(self) -> None:
        spec = self.engine.spec
        self.model_btn.setText(f"{spec.name}  ▾")

    def _switch_to(self, mid: str, install: bool = False) -> None:
        """切模型 = 停旧进程 → 换 Engine 实例 → 重新准备。

        同一端口挂不了两套权重，必须先停干净；get_engine 的 key 里带 model_id，
        否则拿回来的还是挂着旧模型的那个实例（界面说换了，实际没换）。
        """
        if self._panel is not None:
            self._panel.hide()
        if mid == self._model_id and not install:
            return
        if self._busy():
            self._set_status("正在准备或识别，等这一轮结束再切换模型")
            return
        spec = eng.MODEL_BY_ID.get(mid)
        if spec is None:
            return
        # 0.39.1：不再停旧模型（可能是另一个插件正在用的），交给 Router 的 models-max/LRU
        self._model_id = mid
        try:
            self._api.settings_set("model_id", mid)
        except Exception:
            pass           # 配置写不进去不该挡住本次使用
        self.engine = eng.get_engine(_data_dir(), self._port, mid)
        self._prepared = False
        self._update_model_label()
        # 换了模型，之前识别的结果作废（文本类页不依赖模型，保留）
        for it in self._items:
            it.results = [md if it.pages[i].image is None else ""
                          for i, md in enumerate(it.results)]
            it.errors = {}
        self._refresh_queue()
        self._show_page()
        if install:
            # 0.39.1 修复：模型面板里那颗「下载并启用」点一次就要真开始下载。
            # 旧版这里走 _prepare() → has_files()=False → _show_download_prompt()，
            # 只是把主界面的「下载并启用」按钮亮起来，用户还得再点一次才下载。
            # PrepareWorker → Engine.start() 内部本来就会下载缺失的模型文件，
            # 所以直接起准备线程即可（进度 / 失败后的「重试」都走原有链路）。
            self._prepared = True          # 本次准备已由 _start_engine 接管
            self._start_engine()
        else:
            self._set_status(f"已切换到 {spec.name} · 正在准备模型…")
            self._prepare()

    def _on_use_model(self, mid: str) -> None:
        self._switch_to(mid)

    def _on_install_model(self, mid: str) -> None:
        self._switch_to(mid, install=True)

    def _on_remove_model(self, mid: str) -> None:
        """卸载模型文件。

        顺序很重要：先停模型再删。Windows 下 llama-server 会把 gguf mmap 住，
        进程没退就 unlink 会 PermissionError —— 用户看到的就是"点了没反应"。
        """
        if self._busy():
            self._set_status("正在准备或识别，等这一轮结束再卸载模型")
            return
        spec = eng.MODEL_BY_ID.get(mid)
        if spec is None:
            return
        was_current = mid == self._model_id
        if was_current:
            self._stop_engine(silent=True)      # 放掉文件句柄
        ok, msg = eng.remove_model(mid)

        if was_current and not eng.is_installed(mid):
            others = [m for m in eng.MODELS
                      if m.id != mid and eng.is_installed(m.id)]
            if others:
                self._switch_to(others[0].id)   # 顺带把配置里的 model_id 写回去
                self._set_status(f"{msg} · 已切到 {others[0].name}")
                if self._panel is not None:
                    self._panel.refresh(self._model_id)
            else:
                # 一个模型都不剩：回到"未安装"态，等用户再点下载
                self._prepared = False
                self._update_model_label()
                self._show_download_prompt()
                self._set_status(msg)
        else:
            if self._panel is not None:
                self._panel.refresh(self._model_id)
            self._set_status(msg)

    # ---------- 模型 ----------
    def _prepare(self) -> None:
        """打开弹窗先看状态：已就绪/文件齐→静默起进程；缺模型→等用户点下载。

        状态探测走后台线程：`ready()` 是 HTTP 请求，router 忙时要等几秒。
        同步做的话打开窗口会卡住 —— 拖文件、选文件这些操作也跟着点不动。
        """
        if self._prepared:
            return
        self._prepared = True
        old = self._probe
        if old is not None and old.isRunning():
            old.cancel()
        # 探测期间先摆出「检查中」：只有「开始识别」按钮不可点，
        # 拖拽 / 选择文件 / 切模型都不受影响
        self.prepare_text.setText("正在检查识别模型…")
        self.progress_box.setVisible(True)
        self.progress.setVisible(False)
        self.run_btn.setEnabled(False)
        self._set_status("正在检查识别模型…")
        eng = self.engine
        probe = _ProbeWorker(eng, self)
        probe.done.connect(lambda running, files: self._on_probe_done(running, files, eng))
        self._probe = probe
        probe.start()

    def _on_probe_done(self, running: bool, has_files: bool, probe_engine) -> None:
        if probe_engine is not self.engine:
            return          # 探测期间换了模型：这次结果作废，_switch_to 会重新跑一遍
        if running:
            self._on_ready()
        elif has_files:
            # 文件都在，只是进程没起（约 4~6 秒、不花流量）→ 直接起，用户无感
            self._start_engine()
        else:
            self._show_download_prompt()

    def _show_download_prompt(self) -> None:
        spec = self.engine.spec
        self.progress_box.setVisible(True)
        self.progress.setVisible(False)
        self.prepare_text.setText(
            f"首次使用需要下载 {spec.name}（约 {spec.total_mb}MB，只需一次，之后可完全离线）。\n"
            "点右上角「下载并启动」开始；也可以先不下载，直接关掉这个窗口。")
        self.run_btn.setEnabled(False)
        # 右上角那颗按钮常驻：没下载时就是「下载并启动」，点一下直接开始下。
        self.power_btn.setVisible(True)
        self.power_btn.setEnabled(True)
        self._set_power_role(True)
        self._sync_power_label()
        self._set_status(f"{spec.name} 未下载 · 点「下载并启动」开始")

    def _sync_power_label(self) -> None:
        """右上角启停按钮的文字随真实状态走。

        0.39.1 起「模型已启动 / 未启动」徽章取消，状态全收在这一颗按钮上：
        · 运行中 → 停止模型；· 有文件没起 → 启动模型；· 没下载 → 下载并启动。
        """
        if self.engine.running:
            self.power_btn.setText("停止模型")
        elif self.engine.has_files():
            self.power_btn.setText("启动模型")
        else:
            self.power_btn.setText("下载并启动")

    def _set_power_role(self, primary: bool) -> None:
        """切换按钮权重：启动=主按钮（蓝底），停止=普通按钮（白底描边）。

        Qt 的 QSS 属性选择器不会在 setProperty 后自动重算，必须 unpolish/polish。
        """
        self.power_btn.setProperty("role", "primary" if primary else "")
        st = self.power_btn.style()
        if st is not None:
            st.unpolish(self.power_btn)
            st.polish(self.power_btn)
        self.power_btn.update()

    def _toggle_engine(self) -> None:
        """右上角那颗按钮：就绪时停止，停止后启动。"""
        if self.engine.running:
            self._stop_engine()
        else:
            self._start_engine()

    def _stop_engine(self, silent: bool = False) -> None:
        if self._worker is not None and self._worker.isRunning():
            if not silent:
                self._set_status("正在识别，等这一页完成后再停止")
            return
        if self._probe is not None and self._probe.isRunning():
            self._probe.cancel()
            self._probe.wait(4000)
        if self._prep is not None and self._prep.isRunning():
            self._prep.cancel()
            self._prep.wait(6000)
        mem = self.engine.memory_mb()
        self.engine.stop()
        if silent:
            return                  # 切模型时别闪一帧"已停止"
        self._sync_power_label()
        self._set_power_role(True)
        self.power_btn.setEnabled(True)
        self.run_btn.setEnabled(False)
        freed = f"（释放约 {mem}MB 内存）" if mem else ""
        self._set_status(f"模型已停止{freed} · 模型文件保留，点「启动模型」可再开")

    def _start_engine(self) -> None:
        self.power_btn.setVisible(True)
        self.power_btn.setEnabled(False)
        self._set_power_role(False)
        self.power_btn.setText("启动中…")
        self.prepare_text.setText("正在准备识别模型…")
        self._set_status(
            f"正在准备 {self.engine.spec.name}…"
            f"（首次需要下载约 {self.engine.spec.total_mb}MB，只需一次）")
        self.progress_box.setVisible(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.run_btn.setEnabled(False)
        self._prep = PrepareWorker(self.engine)
        self._prep.progress.connect(self._on_progress)
        self._prep.ready.connect(self._on_ready)
        self._prep.failed.connect(self._on_prep_failed)
        self._prep.start()

    def _on_progress(self, doc: str, pct) -> None:
        self.prepare_text.setText(doc if pct is None else f"{doc}（{int(pct)}%）")
        self.progress.setVisible(True)
        if pct is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(pct))

    def _on_ready(self) -> None:
        self.progress_box.setVisible(False)
        self.progress.setVisible(False)
        self._update_action_state()
        # 启停按钮：就绪时变成「停止模型」
        self.power_btn.setVisible(True)
        self.power_btn.setEnabled(True)
        self._sync_power_label()
        self._set_power_role(False)
        # 展示当前用的是哪个模型（用户最想确认的就是这个）
        info = self.engine.model_info()
        if info:
            self.sub.setText(f"本地模型 {info['name']}")
        self._update_model_label()
        if self._panel is not None and not self._panel.isHidden():
            self._panel.refresh(self._model_id)
        # 区分"沿用本机已有文件"和"本次真下载"，否则用户会以为偷偷下了一堆东西
        hint = {
            "reuse": "模型已启动 · 沿用了本机已有的模型（本次未下载）",
            "download": "模型已启动 · 模型已下载完成",
        }.get(getattr(self.engine, "last_action", ""), "模型已启动 · 可以识别了（有独显约 1 秒一页）")
        self._set_status(hint)
        if not self.isVisible():
            # 后台下载完成时窗口没打开：别让 1GB 模型一直常驻内存，
            # 下次打开 _prepare() 会自动重新起进程（几秒，不再花流量）。
            _schedule_release(self.engine, 60)

    def _on_prep_failed(self, msg: str) -> None:
        self.progress_box.setVisible(True)
        self.progress.setVisible(False)
        self.power_btn.setVisible(True)
        self.power_btn.setEnabled(True)
        self._set_power_role(True)
        self._sync_power_label()
        self.prepare_text.setText(msg)
        self._set_status("准备失败，可点右上角按钮重新开始")

    # ---------- 加入队列 ----------
    def _add_paths(self, paths) -> None:
        """把若干文件加入队列。读取在主线程做（QImage 不是线程安全的）。"""
        added = 0
        last_err = ""
        for p in paths:
            if not p or not os.path.isfile(p):
                continue
            loaded = inp.load(p)
            item = QueueItem(loaded=loaded, error=loaded.error)
            # 文本类页（Excel / Word / TXT / PDF 文字层）本身就是结果，不用模型
            item.results = [pg.text if pg.image is None else ""
                            for pg in loaded.pages]
            self._items.append(item)
            added += 1
            if loaded.error:
                last_err = f"{loaded.name}：{loaded.error}"
        if not added:
            self._set_status("没有可加入的文件")
            return
        if self._cur_item < 0:
            self._cur_item = 0
            self._cur_page = self._default_page(0)
        self._refresh_queue()
        self._show_page()
        self._update_action_state()
        need = sum(len(i.pending_pages) for i in self._items)
        msg = f"已加入 {added} 个文件 · 待识别 {need} 页"
        if need == 0:
            msg += "（文本类文件已直接解析）"
        if last_err:
            msg += f" · {last_err}"
        self._set_status(msg)

    def _add_clipboard_image(self, img: QImage) -> None:
        """剪贴板截图不是文件，单独造一个队列项。"""
        loaded = inp.Loaded(path="", name="剪贴板截图", kind=inp.KIND_IMAGE)
        loaded.pages = [inp.Page(label="整页", image=img, source="模型识别")]
        self._items.append(QueueItem(loaded=loaded, results=[""]))
        if self._cur_item < 0:
            self._cur_item = 0
            self._cur_page = self._default_page(0)
        self._refresh_queue()
        self._show_page()
        self._update_action_state()
        self._set_status("已把剪贴板截图加进队列")

    def _paste(self) -> None:
        img = QApplication.clipboard().image()
        if img.isNull():
            md = QApplication.clipboard().mimeData()
            files = []
            if md and md.hasUrls():
                files = [u.toLocalFile() for u in md.urls()]
                files = [f for f in files if f and os.path.isfile(f)]
            if files:
                self._add_paths(files)
                return
            self._set_status("剪贴板里没有图片或文件（先按 Win+Shift+S 截一张）")
            return
        self._add_clipboard_image(img)

    def _pick(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择文件（可多选）", "",
            "支持的文档 (*.pdf *.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff *.gif "
            "*.xlsx *.xlsm *.docx *.txt *.md *.csv *.log *.json *.yaml *.yml);;"
            "所有文件 (*)")
        if paths:
            self._add_paths(paths)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        try:
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
        except Exception:
            pass

    def dropEvent(self, event) -> None:  # noqa: N802
        files = []
        for u in event.mimeData().urls():
            p = u.toLocalFile()
            if p and os.path.isfile(p):
                files.append(p)
        if files:
            self._add_paths(files)
            event.acceptProposedAction()
            return
        super().dropEvent(event)

    # ---------- 队列 ----------
    def _refresh_queue(self) -> None:
        # 清旧行：hide() + deleteLater()，别用 setParent(None)（会造游离顶层窗口）
        while self.queue_layout.count():
            item = self.queue_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        self._rows = []
        for i, it in enumerate(self._items):
            row = QueueRow(i, it, self._select_item, self._remove_item,
                           self.queue_box)
            row.set_selected(i == self._cur_item)
            self.queue_layout.addWidget(row)
            self._rows.append(row)
        self.queue_layout.addStretch(1)
        self.queue_label.setText(f"队列（{len(self._items)}）")
        self._refresh_combo()

    @staticmethod
    def _has_full(it) -> bool:
        """多页文件才给「全文」项；单页再放一个重复项没意义。"""
        return len(it.pages) > 1

    def _refresh_combo(self) -> None:
        self.view_combo.blockSignals(True)
        self.view_combo.clear()
        for it in self._items:
            if self._has_full(it):
                self.view_combo.addItem(f"{it.name} › 全文（合并 {len(it.pages)} 页）")
            for pg in it.pages:
                self.view_combo.addItem(f"{it.name} › {pg.label}")
        idx = self._combo_index(self._cur_item, self._cur_page)
        if idx >= 0:
            self.view_combo.setCurrentIndex(idx)
        self.view_combo.blockSignals(False)
        self.view_combo.setVisible(bool(self._items))

    def _combo_index(self, ii: int, jj: int) -> int:
        """(文件下标, 页下标) → combo 下标；jj < 0 表示「全文」。"""
        k = 0
        for i, it in enumerate(self._items):
            n = len(it.pages)
            off = 1 if self._has_full(it) else 0
            if i == ii:
                return k if jj < 0 else k + off + jj
            k += off + n
        return -1

    def _index_to_item(self, idx: int) -> tuple[int, int]:
        """combo 下标 → (文件下标, 页下标)，页下标 -1 = 全文。

        不用 combo 的 userData 存 tuple：Qt 存进去会变成 QVariant，
        findData 再拿 tuple 去比经常取不到（实测取不到），干脆按顺序换算。
        """
        k = 0
        for i, it in enumerate(self._items):
            n = len(it.pages)
            off = 1 if self._has_full(it) else 0
            if off and idx == k:
                return i, -1
            if k + off <= idx < k + off + n:
                return i, idx - k - off
            k += off + n
        return -1, 0

    def _on_view_changed(self, idx: int) -> None:
        if idx < 0:
            return
        ii, jj = self._index_to_item(idx)
        if ii < 0:
            return
        self._cur_item, self._cur_page = ii, jj
        # 程序内部改状态时也要把 combo 拉回一致的位置
        if self.view_combo.currentIndex() != idx:
            self.view_combo.blockSignals(True)
            self.view_combo.setCurrentIndex(idx)
            self.view_combo.blockSignals(False)
        self._mark_selected()
        self._show_page()

    def _select_item(self, index: int) -> None:
        if not (0 <= index < len(self._items)):
            return
        self._cur_item = index
        self._cur_page = self._default_page(index)
        self._mark_selected()
        self._refresh_combo()
        self._show_page()

    def _mark_selected(self) -> None:
        for i, row in enumerate(self._rows):
            row.set_selected(i == self._cur_item)

    def _remove_item(self, index: int) -> None:
        if not (0 <= index < len(self._items)):
            return
        self._items.pop(index)
        if self._cur_item >= len(self._items):
            self._cur_item = len(self._items) - 1
            self._cur_page = 0
        self._refresh_queue()
        self._show_page()
        self._update_action_state()

    def _clear_queue(self) -> None:
        if not self._items:
            return
        self._items.clear()
        self._cur_item = -1
        self._cur_page = 0
        self._refresh_queue()
        self._show_page()
        self._update_action_state()
        self._set_status("队列已清空")

    # ---------- 预览 ----------
    def _show_page(self) -> None:
        if not (0 <= self._cur_item < len(self._items)):
            self._set_doc_html(_PLACEHOLDER)
            self.doc_label.setText("文档预览")
            return
        it = self._items[self._cur_item]
        # _cur_page < 0 = 「全文」合并视图（多页文件下拉第一项）
        if self._cur_page < 0:
            md = self._file_md(self._cur_item)
        elif 0 <= self._cur_page < len(it.pages):
            md = it._page_md(self._cur_page)
        else:
            md = ""
        if md.strip():
            self.view.setHtml(md_render.to_html(md))
            chars = len("".join(md_render.to_plain(md).split()))
            self.doc_label.setText(f"{chars} 字")
        else:
            self._set_doc_html(
                "<div style='color:#5F6570;font-size:13px;padding:24px 8px;'>"
                "这一页还没识别。<br>点右下角「开始识别」批量处理队列里的文件。"
                "</div>")
            self.doc_label.setText("尚未识别")

    def _update_action_state(self) -> None:
        need = sum(len(i.pending_pages) for i in self._items)
        running = self._worker is not None and self._worker.isRunning()
        self.run_btn.setEnabled(need > 0 and not running and self.engine.ready())
        self.run_btn.setText("识别中…" if running else "开始识别")
        self.cancel_btn.setVisible(running)

    # ---------- 识别 ----------
    def _start_batch(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._set_status("已经在识别了")
            return
        if not self.engine.ready():
            self._set_status("模型还没启动，等模型起来再识别")
            return
        tasks = []
        for i, it in enumerate(self._items):
            for pi in it.pending_pages:
                img = it.pages[pi].image
                if img is None:
                    continue
                tasks.append((i, pi, _png_bytes(img)))
        if not tasks:
            self._set_status("没有待识别的页（文本类文件已直接解析）")
            return
        self._worker = BatchWorker(self.engine, tasks)
        self._worker.page_done.connect(self._on_page_done)
        self._worker.page_failed.connect(self._on_page_failed)
        self._worker.progress.connect(self._on_batch_progress)
        self._worker.all_done.connect(self._on_batch_done)
        self._worker.start()
        self.batch_bar.setVisible(True)
        self.batch_bar.setRange(0, len(tasks))
        self.batch_bar.setValue(0)
        self._update_action_state()
        self._set_status(
            f"开始识别 {len(tasks)} 页…（有独显约 1 秒一页，纯 CPU 十几秒）")

    def _cancel_batch(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._set_status("正在停止…（当前这一页跑完就停）")

    def _on_page_done(self, ii: int, pi: int, text: str, sec: float) -> None:
        if not (0 <= ii < len(self._items)):
            return
        it = self._items[ii]
        # 先按模型"方言"整理：OvisOCR2 给的是 HTML 表格 + bbox 图片标签，
        # 不预处理的话预览和导出都会剩一屏生标签。
        md = md_render.preprocess(text, self.engine.spec.dialect)
        while len(it.results) <= pi:
            it.results.append("")
        it.results[pi] = md
        it.errors.pop(pi, None)
        if ii < len(self._rows):
            self._rows[ii].update_item(it)
        # 全文视图（_cur_page < 0）下任一页识别完都要重绘合并内容
        if ii == self._cur_item and (self._cur_page < 0 or pi == self._cur_page):
            self._show_page()
        self._set_status(
            f"识别完成 1 页 · {self.engine.spec.name} · {sec:.2f} 秒")

    def _on_page_failed(self, ii: int, pi: int, msg: str) -> None:
        if not (0 <= ii < len(self._items)):
            return
        it = self._items[ii]
        it.errors[pi] = msg
        if ii < len(self._rows):
            self._rows[ii].update_item(it)
        self._set_status(f"有一页识别失败：{msg}")

    def _on_batch_progress(self, done: int, total: int) -> None:
        self.batch_bar.setValue(done)
        self.batch_bar.setFormat(f"{done}/{total}")

    def _on_batch_done(self, completed: bool) -> None:
        self.batch_bar.setVisible(False)
        self._worker = None
        self._refresh_queue()
        self._show_page()
        self._update_action_state()
        need = sum(len(i.pending_pages) for i in self._items)
        if completed:
            total = sum(len(i.model_pages) for i in self._items)
            pages_all = sum(len(i.pages) for i in self._items)
            tail = (f"（文件共 {pages_all} 页，其中 {total} 页为图片需识别）"
                    if pages_all != total else "")
            self._set_status(f"识别完成 · 本次共 {total} 页{tail}")
        else:
            self._set_status(f"已取消 · 还剩 {need} 页没识别")

    # ---------- 导出 / 复制 ----------
    def _file_md(self, ii: int) -> str:
        """某个文件的全部页合并成一份连续 Markdown（「全文」视图用）。

        刻意不加「### 第 N 页」之类的小标题：用户反馈分太多小节读起来碎，
        页与页之间只用一条分隔线断开，整体读起来是连贯的一篇。
        """
        if not (0 <= ii < len(self._items)):
            return ""
        it = self._items[ii]
        parts = [it._page_md(j).strip() for j in range(len(it.pages))]
        return "\n\n---\n\n".join(p for p in parts if p)

    def _default_page(self, ii: int) -> int:
        """多页文件默认看「全文」(-1)，单页文件看第 1 页(0)。"""
        if 0 <= ii < len(self._items) and self._has_full(self._items[ii]):
            return -1
        return 0

    def _current_md(self) -> str:
        if not (0 <= self._cur_item < len(self._items)):
            return ""
        if self._cur_page < 0:
            return self._file_md(self._cur_item)
        return self._items[self._cur_item]._page_md(self._cur_page)

    def _all_md(self) -> str:
        """队列里所有文件的所有页，合并成一份 Markdown。"""
        chunks = []
        for it in self._items:
            parts = []
            for j, pg in enumerate(it.pages):
                md = it._page_md(j)
                if md.strip():
                    parts.append(f"### {pg.label}\n\n{md}")
            if parts:
                chunks.append(f"# {it.name}\n\n" + "\n\n---\n\n".join(parts))
        return "\n\n---\n\n".join(chunks)

    def _default_stem(self) -> str:
        if 0 <= self._cur_item < len(self._items):
            it = self._items[self._cur_item]
            if it.loaded.path:
                return Path(it.loaded.path).stem
            return it.name
        return "识别结果"

    def _default_dir(self) -> Path:
        if 0 <= self._cur_item < len(self._items):
            p = self._items[self._cur_item].loaded.path
            if p:
                return Path(p).parent
        return Path.home()

    def _export(self, kind: str, everything: bool = False) -> None:
        md = self._all_md() if everything else self._current_md()
        if not md.strip():
            self._set_status("还没有内容可以导出")
            return
        stem = "识别结果" if everything else self._default_stem()
        if kind == "html":
            filt, ext = "网页 (*.html)", ".html"
            content = md_render.to_html(md, for_browser=True, title=stem,
                                        model_name=self.engine.spec.name)
        elif kind == "md":
            filt, ext = "Markdown 文件 (*.md)", ".md"
            content = md_render.clean(md) + "\n"
        else:
            filt, ext = "纯文本 (*.txt)", ".txt"
            content = md_render.to_plain(md)
        out, _ = QFileDialog.getSaveFileName(
            self, "导出", str(self._default_dir() / f"{stem}{ext}"), filt)
        if not out:
            return
        try:
            if not out.lower().endswith(ext):
                out += ext
            with open(out, "w", encoding="utf-8") as f:
                f.write(content)
            self._set_status(f"已导出：{out}")
        except Exception as e:  # noqa: BLE001
            self._set_status(f"导出失败：{e}")

    def _copy(self) -> None:
        md = self._current_md()
        if not md.strip():
            self._set_status("还没有内容可以复制")
            return
        QApplication.clipboard().setText(md_render.to_plain(md).strip())
        self._set_status("已复制为纯文本到剪贴板")

    # ---------- 生命周期 ----------
    def _ask_close_while_downloading(self) -> str:
        """下载中关窗口：返回 continue / abort / back。

        以前 closeEvent 无条件 cancel()，用户一按 X 几百 MB 就白下了。
        现在给两个明确动作：「继续下载」= 窗口退到后台、下载继续；
        「取消下载」= 中断并关闭；右上角 X / Esc = 返回，什么都不做。
        """
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setWindowTitle("模型还在下载")
        box.setText(
            "识别模型还没下载完，关闭窗口要怎么处理？\n\n"
            "· 继续下载：窗口退到后台，下载继续；重新打开本插件可看进度。\n"
            "· 取消下载：中断下载并关闭窗口（下次再点会重新下载）。")
        box.setIcon(QMessageBox.Icon.Question)
        # 消息框必须自带完整样式：主窗口 background:transparent 会被子窗口
        # 继承，直接 new QMessageBox 会变黑底黑字（theme.py 里有说明）。
        box.setStyleSheet(theme._MESSAGE_BOX_QSS)
        theme.apply_light_titlebar(box)
        b_continue = box.addButton("继续下载", QMessageBox.ButtonRole.AcceptRole)
        b_abort = box.addButton("取消下载", QMessageBox.ButtonRole.DestructiveRole)
        b_back = box.addButton("返回", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(b_continue)
        box.exec()
        hit = box.clickedButton()
        if hit is b_continue:
            return "continue"
        if hit is b_abort:
            return "abort"
        return "back"

    def closeEvent(self, event: QCloseEvent) -> None:
        # 下载/准备中：别直接掐断，先让用户选「继续下载」还是「取消下载」。
        # 程序整体退出时不弹框（见 _cancel_all_workers），直接走中断。
        prep_running = self._prep is not None and self._prep.isRunning()
        if prep_running and not _APP_QUITTING and not getattr(self, "_close_decided", False):
            choice = self._ask_close_while_downloading()
            if choice == "back":
                event.ignore()
                return
            if choice == "continue":
                # 只隐藏、不销毁：QThread / 进度信号 / _DLG 单例全都还在，
                # 下载在后台继续跑，用户随时能从插件瓦片把窗口叫回来。
                event.ignore()
                self.hide()
                self._set_status("已在后台继续下载 · 重新打开本插件可查看进度")
                return
            self._close_decided = True     # 选了取消：下面照原逻辑中断并关闭
        if self._probe is not None and self._probe.isRunning():
            self._probe.cancel()
            self._probe.wait(4000)
        if self._prep is not None and self._prep.isRunning():
            self._prep.cancel()
            self._prep.wait(6000)
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()      # 批量识别：先让当前这页跑完再停
            self._worker.wait(8000)
        idle = int(self._cfg("idle_release", 300) or 300)
        _schedule_release(self.engine, idle)
        super().closeEvent(event)


_DLG = None   # 非模态单例
_APP_QUITTING = False
_APP_QUIT_HOOKED = False


def _cancel_all_workers() -> None:
    """LyLaunch 真正退出时收掉后台下载线程。

    进程都要结束了，下载不可能在后台继续 —— 此时必须 cancel + wait，
    否则 QThread 会在进程销毁时报 "Destroyed while thread is still running"。
    """
    global _APP_QUITTING
    _APP_QUITTING = True
    dlg = _DLG
    if dlg is None:
        return
    try:
        for name, wait_ms in (("_probe", 3000), ("_prep", 6000), ("_worker", 8000)):
            w = getattr(dlg, name, None)
            if w is not None and w.isRunning():
                w.cancel()
                w.wait(wait_ms)
    except RuntimeError:
        pass          # 窗口的 C++ 对象已销毁


def _hook_app_quit() -> None:
    """插件加载时挂一次 aboutToQuit（重复加载模块也只挂一次）。"""
    global _APP_QUIT_HOOKED
    app = QApplication.instance()
    if app is None or _APP_QUIT_HOOKED:
        return
    app.aboutToQuit.connect(_cancel_all_workers)
    _APP_QUIT_HOOKED = True


def _open_nonmodal(cls, api, settings, **kw) -> None:
    """非模态打开：不再 exec() 阻塞主程序；重复打开只把已有窗口提到前台。"""
    global _DLG
    if _DLG is not None:
        try:
            # 「继续下载」是 hide 不是销毁；raise_() 对隐藏窗口无效，
            # 必须先 show() 才能把后台下载中的窗口叫回来。
            if not _DLG.isVisible():
                _DLG.show()
            _DLG.raise_()
            _DLG.activateWindow()
            # 已开窗口时又粘了一份文件：追加进队列，别静默丢掉。
            paths = kw.get("initial_paths")
            if paths and hasattr(_DLG, "_add_paths"):
                try:
                    _DLG._add_paths(paths)
                except Exception:
                    pass
            return
        except RuntimeError:
            _DLG = None
    dlg = cls(api, settings, **kw)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

    def _cleared(_obj=None) -> None:
        # destroyed 信号发出时底层 C++ 对象已销毁，绝不能再调 _DLG.isVisible()
        # （会抛 RuntimeError: wrapped C/C++ object has been deleted）。
        # 单例模式下同一时刻只会有一个窗口，直接清空引用即可。
        global _DLG
        _DLG = None

    dlg.destroyed.connect(_cleared)
    _DLG = dlg
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()


class Plugin(PluginBase):
    def on_load(self) -> None:
        _hook_app_quit()
        _dbg_print(f"[插件] 已加载: {self.ctx.name} v{self.ctx.manifest.version}", flush=True)

    def on_unload(self) -> None:
        # 0.39.1：不再杀整个 Router（可能其他插件正在用），只卸载本插件的 OCR 模型
        try:
            from lylaunch.engine import EngineManager
            mgr = EngineManager.instance()
            ocr_ids = {m.id for m in eng.MODELS}
            for one in mgr.loaded_ids():
                if one in ocr_ids:
                    mgr.unload(if_model=one)
        except Exception:
            pass

    def on_execute(self, query: str = "") -> None:
        # 主搜索框粘贴文件时把路径列表用换行拼进 query（插件 API 只有 str 通道）。
        # 只保留真实存在的路径，避免用户点插件瓦片时把搜索词当文件名入队。
        paths = [t for t in (p.strip().strip('"') for p in (query or "").splitlines())
                 if t and os.path.isfile(t)]
        _open_nonmodal(DocDialog, self.ctx.api, self.ctx.settings,
                       initial_paths=paths or None)
