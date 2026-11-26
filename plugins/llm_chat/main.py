"""LyLaunch 本地模型插件：在软件内和本地大模型对话（llama.cpp）。

视觉方向：精致极简（不套用通用对话框主题）
- 背景是极淡的竖向渐变，窗口本身即对话页
- 顶栏是渐变圆标 + 名称，右侧模型胶囊与运行状态点
- AI 回答带头像与元信息，用户消息是浅蓝渐变气泡
- 输入区是一张悬浮卡片（工具栏收在卡片内部右下）
- **回答逐字打出来**（打字机），而不是按网络块闪出

引擎层在 chat_engine.py（本文件不碰）。
已具备：多会话历史、模型切换、参数设置、Markdown/公式渲染。

上下文约定：完整对话历史只保存在各自的会话 JSON 里（永不裁剪），
发给模型前才由 session_store.build_context() 按预算裁剪出一份副本；
生成中的回调带会话令牌，切会话后一律丢弃，绝不把 A 会话的回答写进 B 会话。
"""

from __future__ import annotations

import copy
import os
import re
import subprocess
import time
import ctypes
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPointF,
    QRect,
    QSize,
    QObject,
    QPropertyAnimation,
    Qt,
    QThread,
    QTimer,
    Property,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from lylaunch.plugins.base import PluginBase
try:
    # 0.36 线性图标是后加的模块；旧版打包 exe 里可能没有，不能让插件因此打不开
    from lylaunch.plugins.plugin_icons import plugin_icon_svg, render_plugin_svg
except Exception:      # noqa: BLE001
    plugin_icon_svg = None
    render_plugin_svg = None

import chat_engine as eng
import session_store as ss
import md_chat
import doc_inputs   # PDF 文字层 / Excel / Word 的零模型解析（本插件自包含）


def _dbg_print(*args, **kwargs):
    """调试输出已停用；保留调用以维持语句结构。"""
    pass


# ---------- hover 气泡：本插件全部禁用（0.39.1 设计要求）----------
# 所有 setToolTip 已从本文件移除；这里再兜一层，防止以后新增控件或父控件
# 意外带了 tooltip，又冒出「鼠标停一两秒弹黄框」。只拦「本地 AI」窗口树
# （objectName = lylaunchChatDialog）里的 QEvent.ToolTip —— 主程序和其它
# 插件的 tooltip 完全不受影响。
_NO_TIP_PROP = "lylaunchLlmChatNoTooltipFilter"


class _NoTooltipFilter(QObject):
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.ToolTip and isinstance(obj, QWidget):
            w = obj
            while w is not None:
                if w.objectName() == "lylaunchChatDialog":
                    return True
                w = w.parentWidget()
        return False


def _install_no_tooltip_filter() -> None:
    """在 QApplication 上装一次（插件模块可能被重复加载，用动态属性去重）。"""
    app = QApplication.instance()
    if app is None or app.property(_NO_TIP_PROP) is not None:
        return
    f = _NoTooltipFilter()
    f.setParent(app)                    # 生命周期跟随 app，不会悬空
    app.setProperty(_NO_TIP_PROP, f)
    app.installEventFilter(f)


def _remove_no_tooltip_filter() -> None:
    app = QApplication.instance()
    if app is None:
        return
    f = app.property(_NO_TIP_PROP)
    if f is not None:
        app.removeEventFilter(f)
        app.setProperty(_NO_TIP_PROP, None)


# 完整对话历史不再裁剪（以前在这里按 MAX_TURNS 砍 _messages，结果连带把
# 会话 JSON 里的历史也删了）。发给模型时才由 build_context() 按预算挑轮次。
BUBBLE_MAX_W = 500
CONTENT_MAX_W = 720        # 消息列最大宽度（AI 正文与用户消息共用）
COMPOSER_SCALE = 1.10      # 输入卡 = 消息列实宽 × 110%（设计说明：+10px 太小，改加 10%）
                           # 不再用固定宽度 —— 最大化/缩放时固定值会和消息列错位
INPUT_MIN_H = 62           # 输入区最小高度：设计要求「高度高一点」（原 46，动态下限 52）
_IDLE_TIMER: QTimer | None = None

TEXT_EXTS = {".txt", ".md", ".markdown", ".py", ".js", ".ts", ".json", ".csv", ".log",
             ".yaml", ".yml", ".ini", ".cfg", ".conf", ".html", ".css", ".xml", ".sql",
             ".java", ".go", ".rs", ".c", ".cpp", ".h", ".bat", ".ps1", ".sh", ".toml"}
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
MAX_ATTACH_BYTES = 512 * 1024
# 模型上下文 8192 token ≈ 1 万汉字；附件太大必然 400。
# 单文件按"字"限 3000（约 4500 token），再留空间给历史与回答。
MAX_ATTACH_CHARS = 3000
# 一次请求最多带几张图（PDF 页面图也算）：视觉 token 很贵，超了会 400
MAX_SEND_IMAGES = 6

# ⚠️ 这里曾注入过一段 system 提示词（要求 Markdown/公式格式，并写了「简洁、
# 简单问题直接说清楚」「不要硬凑结构」「直接给答案」）。实测副作用：9B 级小模型
# 对这类措辞极其敏感，会把原本分小节展开的回答压成「一句话 + 几个 bullet」，
# 用户反馈「模型突然变懒」。而且模型本来就会输出 Markdown（标题/列表），当时
# 真正的问题只是 LaTeX 公式没被渲染 —— 已由 md_chat.py 解决。
# 故不再注入任何 system 消息，让请求体保持「本会话历史 + 用户消息」的最简形态。
# 若将来确需引导格式，只能加「只管符号、不约束内容详略」的提示，且必须可开关。

SUGGESTIONS = [
    "介绍一下你自己，可以做什么。",
    "详细列出5个常用的git命令",
    "请用 Python 写个冒泡排序。",
]


def _data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "LyLaunch" / "llm_chat"


def _schedule_release(engine: eng.Engine, seconds: int, on_stopped=None) -> None:
    """空闲若干秒后停掉引擎；on_stopped 用于同步 UI（关窗后可传 None）。"""
    global _IDLE_TIMER
    if _IDLE_TIMER is None:
        _IDLE_TIMER = QTimer()
        _IDLE_TIMER.setSingleShot(True)
    try:
        _IDLE_TIMER.timeout.disconnect()
    except Exception:
        pass

    def _do_stop() -> None:
        engine.stop()
        if on_stopped is not None:
            try:
                on_stopped()
            except Exception:
                pass

    _IDLE_TIMER.timeout.connect(_do_stop)
    _IDLE_TIMER.start(max(30, int(seconds)) * 1000)


def _cancel_release() -> None:
    if _IDLE_TIMER is not None and _IDLE_TIMER.isActive():
        _IDLE_TIMER.stop()


def _norm(text: str) -> str:
    """统一换行符并折叠多余空行（模型输出常带 \\r\\n，只处理 \\n 会漏）。"""
    t = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return re.sub(r"\n{2,}", "\n", t)


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def _html(text: str, color: str = "#161C28", size: int = 14) -> str:
    body = md_chat.inline_html(_norm(text)).replace("\n", "<br>")
    return f'<div style="line-height:186%; font-size:{size}px; color:{color};">{body}</div>'


# 思考内容的字数上限（**深度思考也适用**）：带思维链的模型偶尔会一直自言自语
# 不进入正文（实测 9B 蒸馏版吐了 5591 字思考、正文「（没有内容）」）。
# 3000 字按中文约 1 字 1 token 估，给 4096 的配额留出正文空间。
# 超限不是简单丢掉，而是自动关思考重发一次（见 _retry_think_overflow）。
_THINK_CHAR_LIMIT = 3000


def _multi_select_icon(on: bool):
    """自绘「多选列表」图标：一个带勾的小方框 + 三条列表线。

    刻意不用文字、也不带按钮底 —— 它应该是分组标题旁边的一枚小图标，
    不是一颗挤在标题行里的按钮（设计说明）。开启多选时换主色。
    """
    from PySide6.QtGui import QGuiApplication, QIcon, QPixmap
    # ⚠️ 按屏幕实际 DPR 生成，让 pixmap 的**逻辑尺寸正好是 20**：
    # 之前固定 40×40/DPR2（逻辑 20）却被请求 18px，Qt 缩了 0.9 倍，
    # 1px 细线被抗锯齿抹成半透明，界面上只剩勾框看得见 → "显示不完全"。
    # 现在请求 20 时精确匹配，不做任何缩放，细线完整保留。
    scr = QGuiApplication.primaryScreen()
    dpr = float(scr.devicePixelRatio()) if scr is not None else 1.0
    side = int(round(20 * dpr))
    pix = QPixmap(side, side)
    pix.setDevicePixelRatio(dpr)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.scale(dpr, dpr)     # 下面仍按 20×20 的逻辑坐标画
    pen = QPen(QColor("#4C7CE0" if on else "#6B7482"))
    pen.setWidthF(1.2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    # 一个带勾的小方框 + 三条列表线：比"两个方框 + 两条线"轻得多
    p.drawRoundedRect(2.5, 6.5, 6.0, 6.0, 1.2, 1.2)
    p.drawLine(3.6, 9.5, 4.6, 10.5)
    p.drawLine(4.6, 10.5, 7.0, 7.9)
    for y in (4.0, 9.5, 15.0):
        p.drawLine(11.0, y, 17.5, y)
    p.end()
    return QIcon(pix)


def _append_request_hint(msgs: list[dict], hint: str) -> None:
    """在**请求副本**最后一条 user 消息末尾追加一句引导。

    整条 dict 复制后再替换，绝不改到 self._messages / 会话 JSON。
    项目约定不注入 system 消息，所以引导只能附在用户消息里，且可用配置关闭。
    """
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user":
            m = dict(msgs[i])
            m["content"] = (str(m.get("content") or "") + f"\n（{hint}）")
            msgs[i] = m
            return


# ────────────────────────── 打字机 ──────────────────────────
class Typer(QObject):
    """把流式片段按字吐出，避免"一大块闪出来"。

    默认 22ms 一个字；模型积压越多，每拍吐得越多（避免落后太多）。
    """

    def __init__(self, render, on_idle, parent=None) -> None:
        super().__init__(parent)
        self._render = render
        self._on_idle = on_idle
        self._queue = ""
        self._shown = ""
        self._timer = QTimer(self)
        self._timer.setInterval(22)
        self._timer.timeout.connect(self._tick)

    @property
    def idle(self) -> bool:
        return not self._timer.isActive() and not self._queue

    def feed(self, piece: str) -> None:
        self._queue += piece
        if not self._timer.isActive():
            self._timer.start()

    def reset(self) -> None:
        self._timer.stop()
        self._queue = ""
        self._shown = ""

    def finish_now(self) -> None:
        self._timer.stop()
        if self._queue:
            self._shown += self._queue
            self._queue = ""
            self._render(self._shown, False)

    def _tick(self) -> None:
        if not self._queue:
            self._timer.stop()
            self._on_idle()
            return
        # 0.3.7：整体提速（设计说明：打字机太慢）。尾巴原来是 38ms/字
        # （≈26 字/秒），比模型出字还慢，只能眼睁睁等；现在 20ms/字（50 字/秒），
        # 既有逐字感又不会拖在模型后面。积压时更激进地把队列追平。
        backlog = len(self._queue)
        if backlog > 60:
            self._timer.setInterval(6)
            n = 4
        elif backlog > 30:
            self._timer.setInterval(9)
            n = 2
        elif backlog > 10:
            self._timer.setInterval(14)
            n = 1
        else:
            self._timer.setInterval(20)
            n = 1
        self._shown += self._queue[:n]
        self._queue = self._queue[n:]
        self._render(self._shown, bool(self._queue))


def _size_text(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB"


def _mem_text(mb: int) -> str:
    """显存/内存统一口径：≥1GB 报 GB（一位小数），小值回落 MB。"""
    try:
        mb = int(mb)
    except Exception:
        return "—"
    return f"{mb / 1024:.1f}GB" if mb >= 1024 else f"{mb}MB"


class FadeOverlay(QWidget):
    """消息区底部渐隐：透明 → 窗口白底，让滚动内容柔和淡出而非被硬切。

    贴在 QScrollArea 的 viewport 底部（绝对定位，不占布局空间）；
    鼠标事件全穿透，不影响滚动和点击。
    """

    H = 30

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedHeight(self.H)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, float(self.height()))
        g.setColorAt(0.0, QColor(255, 255, 255, 0))
        g.setColorAt(0.55, QColor(255, 255, 255, 140))
        g.setColorAt(1.0, QColor(255, 255, 255, 255))
        p.fillRect(self.rect(), g)
        p.end()


def _small_thumb_b64(b64: str) -> str:
    """把发给模型的大图压成 96×96 JPEG 小图 b64，供气泡缩略图与会话持久化。

    原图 b64 动辄几百 KB，直接存进会话 JSON 会让历史文件膨胀；小图只有几 KB。
    """
    import base64
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImage
    try:
        img = QImage.fromData(base64.b64decode(b64))
        if img.isNull():
            return ""
        img = img.scaled(96, 96, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        if not img.save(buf, "JPEG", 78):
            return ""
        return base64.b64encode(bytes(buf.data())).decode()
    except Exception:
        return ""


def _thumb_pixmap(b64: str, size: int = 46, radius: int = 8) -> "QPixmap":
    """小图 b64 → 圆角缩略图 QPixmap；读不出来返回空。"""
    import base64
    from PySide6.QtGui import QImage, QPixmap
    try:
        img = QImage.fromData(base64.b64decode(b64))
        if img.isNull():
            return QPixmap()
        scaled = img.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
        out = QPixmap(size, size)
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(0, 0, size, size, radius, radius)
        p.setClipPath(clip)
        p.drawPixmap((size - scaled.width()) // 2, (size - scaled.height()) // 2,
                     QPixmap.fromImage(scaled))
        p.end()
        return out
    except Exception:
        return QPixmap()


def _file_badge_pixmap(name: str, size: int = 46) -> "QPixmap":
    """文档附件的扩展名徽章（圆角色块 + 短扩展名），与主窗口文件徽章同风格。"""
    from PySide6.QtGui import QPixmap
    ext = os.path.splitext(name)[1].lstrip(".").upper()[:3] or "FILE"
    palette = {"PDF": "#D93A2F", "MD": "#7C5CFF", "TXT": "#2F6BEE",
               "CSV": "#00B2A9", "JSO": "#EAB308", "LOG": "#64748B"}
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(palette.get(ext, "#64748B")))
    p.drawRoundedRect(0, 0, size, size, 8, 8)
    p.setPen(QColor("#FFFFFF"))
    p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
    p.drawText(out.rect(), Qt.AlignmentFlag.AlignCenter, ext)
    p.end()
    return out


# 大图预览缓存：图片本体存成独立文件，会话 JSON 里只留一个文件名。
# 目录位于 Data=["llm_chat", ...] 之下，卸载插件时可一并清理。
_PREVIEW_DIR = (Path(os.environ.get("LOCALAPPDATA") or str(Path.home()))
                / "LyLaunch" / "llm_chat" / "images")


def _save_preview_file(b64: str) -> str:
    """把发给模型的原图落成预览文件，返回文件名；失败返回空串。"""
    import base64
    import uuid
    try:
        _PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        name = f"p_{uuid.uuid4().hex[:12]}.jpg"
        (_PREVIEW_DIR / name).write_bytes(base64.b64decode(b64))
        return name
    except Exception:
        return ""


def _preview_pixmap(card: dict) -> "QPixmap":
    """优先读大图文件；文件丢失或旧会话没存大图时退回缩略图（兼容不崩）。"""
    import base64
    from PySide6.QtGui import QImage, QPixmap
    fname = card.get("preview") or ""
    if fname:
        try:
            # 只取文件名，防会话 JSON 里的目录穿越
            p = _PREVIEW_DIR / Path(fname).name
            if p.is_file():
                img = QImage(str(p))
                if not img.isNull():
                    return QPixmap.fromImage(img)
        except Exception:
            pass
    thumb = card.get("thumb") or ""
    if thumb:
        try:
            img = QImage.fromData(base64.b64decode(thumb))
            if not img.isNull():
                return QPixmap.fromImage(img)
        except Exception:
            pass
    return QPixmap()


class ImagePreviewDialog(QDialog):
    """点击附件卡片打开的大图预览：非模态、← → 切图、Esc 关闭、可另存。"""

    def __init__(self, cards: list[dict], index: int = 0) -> None:
        super().__init__()
        self.setObjectName("imagePreview")
        self.setWindowTitle("图片预览")
        self._cards = [c for c in cards if c.get("kind") == "image"]
        self._index = max(0, min(index, max(0, len(self._cards) - 1)))
        self._pix = None
        self.resize(960, 680)
        self.setStyleSheet(
            "QDialog#imagePreview { background: #1B1D21; }"
            "QLabel#previewTitle { color: #E8EAED; font-size: 13px; background: transparent; }"
            "QLabel#previewImage { background: transparent; color: #5F6570; }"
            "QPushButton#previewBtn { background: #2E3239; color: #E8EAED; border: none;"
            " border-radius: 6px; padding: 6px 14px; font-size: 12px; }"
            "QPushButton#previewBtn:hover { background: #2F6BEE; }")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)
        top = QHBoxLayout()
        self.title = QLabel("")
        self.title.setObjectName("previewTitle")
        top.addWidget(self.title)
        top.addStretch(1)
        for label, slot in (("另存为…", self._save_as), ("关闭", self.close)):
            btn = QPushButton(label)
            btn.setObjectName("previewBtn")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(slot)
            top.addWidget(btn)
        lay.addLayout(top)

        self.image_label = QLabel()
        self.image_label.setObjectName("previewImage")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setSizePolicy(QSizePolicy.Policy.Expanding,
                                       QSizePolicy.Policy.Expanding)
        lay.addWidget(self.image_label, stretch=1)
        self._show_current()

    def _show_current(self) -> None:
        if not self._cards:
            return
        card = self._cards[self._index]
        self._pix = _preview_pixmap(card)
        name = card.get("name", "图片")
        if len(self._cards) > 1:
            head = f"{name}   ·   {self._index + 1}/{len(self._cards)}   （← → 切换）"
        else:
            head = name
        if not self._pix.isNull():
            head += f"   ·   {self._pix.width()}×{self._pix.height()}"
        self.title.setText(head)
        self._fit()

    def _fit(self) -> None:
        if self._pix is None or self._pix.isNull():
            self.image_label.setText("图片文件已丢失，且没有可用缩略图")
            return
        w = max(1, self.image_label.width() - 8)
        h = max(1, self.image_label.height() - 8)
        scaled = self._pix.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
        self.image_label.setPixmap(scaled)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        k = event.key()
        if k == Qt.Key.Key_Escape:
            self.close()
            return
        if len(self._cards) > 1 and k in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step = -1 if k == Qt.Key.Key_Left else 1
            self._index = (self._index + step) % len(self._cards)
            self._show_current()
            return
        super().keyPressEvent(event)

    def _save_as(self) -> None:
        if self._pix is None or self._pix.isNull():
            return
        from PySide6.QtWidgets import QFileDialog
        name = (self._cards[self._index].get("name") or "图片").strip() or "图片"
        if not name.lower().endswith((".png", ".jpg", ".jpeg")):
            name += ".png"
        path, _ = QFileDialog.getSaveFileName(
            self, "保存图片", name, "图片 (*.png *.jpg *.jpeg)")
        if path:
            self._pix.save(path)


_PREVIEW_DLG = None


def _clear_preview_ref() -> None:
    global _PREVIEW_DLG
    _PREVIEW_DLG = None


def _open_image_preview(cards: list[dict], index: int) -> None:
    """非模态单例：重复点只换图，不叠窗口（插件 UI 红线：show() 不用 exec()）。"""
    global _PREVIEW_DLG
    img_cards = [c for c in cards if c.get("kind") == "image"]
    if not img_cards:
        return
    if _PREVIEW_DLG is not None:
        try:
            _PREVIEW_DLG._cards = img_cards
            _PREVIEW_DLG._index = max(0, min(index, len(img_cards) - 1))
            _PREVIEW_DLG._show_current()
            _PREVIEW_DLG.raise_()
            _PREVIEW_DLG.activateWindow()
            return
        except RuntimeError:
            _PREVIEW_DLG = None
    dlg = ImagePreviewDialog(img_cards, index)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    dlg.destroyed.connect(lambda *_: _clear_preview_ref())
    _PREVIEW_DLG = dlg
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    QTimer.singleShot(0, dlg._fit)


class TextPreviewDialog(QDialog):
    """文档附件预览：显示解析后实际发给模型的文本 / Markdown。"""

    def __init__(self, name: str, text: str) -> None:
        super().__init__()
        self.setObjectName("textPreview")
        self.setWindowTitle(f"文档预览 · {name}")
        self._name = name
        self._text = text
        self.resize(760, 560)
        self.setStyleSheet(
            "QDialog#textPreview { background: #FFFFFF; }"
            "QLabel#previewTitle { color: #161C28; font-size: 13px; }"
            "QPlainTextEdit#previewText { border: 1px solid #D8E3F5; border-radius: 8px;"
            " padding: 10px; font-size: 12.5px; color: #161C28; background: #FFFFFF; }"
            "QPushButton#previewBtn { background: #F2F6FD; color: #161C28; border: none;"
            " border-radius: 6px; padding: 6px 14px; font-size: 12px; }"
            "QPushButton#previewBtn:hover { background: #E4EAF7; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 14)
        lay.setSpacing(8)
        top = QHBoxLayout()
        self.title = QLabel(f"{name}   ·   {len(text)} 字（这是发给模型的内容）")
        self.title.setObjectName("previewTitle")
        top.addWidget(self.title)
        top.addStretch(1)
        for label, slot in (("另存为…", self._save_as), ("关闭", self.close)):
            btn = QPushButton(label)
            btn.setObjectName("previewBtn")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(slot)
            top.addWidget(btn)
        lay.addLayout(top)
        view = QPlainTextEdit(text)
        view.setObjectName("previewText")
        view.setReadOnly(True)
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        lay.addWidget(view, stretch=1)
        self.view = view

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def _save_as(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        name = self._name or "文档"
        if not name.lower().endswith((".txt", ".md")):
            name += ".md"
        path, _ = QFileDialog.getSaveFileName(
            self, "保存解析内容", name, "文本 (*.md *.txt)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self._text)
            except Exception:
                pass


_TEXT_PREVIEW_DLG = None


def _clear_text_preview_ref() -> None:
    global _TEXT_PREVIEW_DLG
    _TEXT_PREVIEW_DLG = None


def _open_text_preview(name: str, text: str) -> None:
    """非模态单例：重复点只换内容，不叠窗口。"""
    global _TEXT_PREVIEW_DLG
    if _TEXT_PREVIEW_DLG is not None:
        try:
            _TEXT_PREVIEW_DLG._name = name
            _TEXT_PREVIEW_DLG._text = text
            _TEXT_PREVIEW_DLG.setWindowTitle(f"文档预览 · {name}")
            _TEXT_PREVIEW_DLG.title.setText(
                f"{name}   ·   {len(text)} 字（这是发给模型的内容）")
            _TEXT_PREVIEW_DLG.view.setPlainText(text)
            _TEXT_PREVIEW_DLG.raise_()
            _TEXT_PREVIEW_DLG.activateWindow()
            return
        except RuntimeError:
            _TEXT_PREVIEW_DLG = None
    dlg = TextPreviewDialog(name, text)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    dlg.destroyed.connect(lambda *_: _clear_text_preview_ref())
    _TEXT_PREVIEW_DLG = dlg
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()


class UserAttachCard(QFrame):
    """用户消息里的附件卡片：图片显示缩略图，文档显示类型徽章（对话式）。"""

    W, H, THUMB = 196, 60, 46

    def __init__(self, name: str, kind: str, thumb_b64: str = "",
                 size_txt: str = "", on_open=None) -> None:
        super().__init__()
        self.setObjectName("userAttachCard")
        self.setFixedSize(self.W, self.H)
        self._on_open = on_open
        if on_open is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(7, 6, 9, 6)
        lay.setSpacing(9)

        thumb = QLabel()
        thumb.setFixedSize(self.THUMB, self.THUMB)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if kind == "image" and thumb_b64:
            pix = _thumb_pixmap(thumb_b64, self.THUMB)
            if pix.isNull():
                pix = _file_badge_pixmap(name, self.THUMB)
            thumb.setPixmap(pix)
            meta = f"图片 · {size_txt}" if size_txt else "图片"
        else:
            thumb.setPixmap(_file_badge_pixmap(name, self.THUMB))
            meta = f"文档 · {size_txt}" if size_txt else "文档"
        lay.addWidget(thumb)

        col = QVBoxLayout()
        col.setSpacing(2)
        nm = QLabel(name)
        nm.setObjectName("userAttachName")
        nm.setWordWrap(False)
        nm.setText(QFontMetrics(nm.font()).elidedText(
            name, Qt.TextElideMode.ElideMiddle, self.W - self.THUMB - 26))
        sub = QLabel(meta)
        sub.setObjectName("userAttachMeta")
        col.addStretch(1)
        col.addWidget(nm)
        col.addWidget(sub)
        col.addStretch(1)
        lay.addLayout(col)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # 左键点图片卡片 → 打开大图预览；文档卡片 on_open 为 None，走父类默认行为。
        if (self._on_open is not None
                and event.button() == Qt.MouseButton.LeftButton):
            self._on_open()
            return
        super().mouseReleaseEvent(event)


class UserBubble(QWidget):
    """用户消息：纯文字靠右显示，图片/文档附件用缩略图卡片摆在文字下方。"""

    def __init__(self, text: str, cards: list[dict] | None = None) -> None:
        super().__init__()
        self.setObjectName("userBubble")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # 原型 Claude 档 .bubble.me：max-width 520、padding 12/16、radius 12、13.5px
        self.setStyleSheet("QWidget#userBubble{background:#EDF3FF;border-radius:12px;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lab = QLabel(text)
        lab.setTextFormat(Qt.TextFormat.PlainText)
        lab.setObjectName("userText")
        lab.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        lay.addWidget(lab)

        # 用户消息正文按原型 Claude 档 13.5px（QSS #userText）。量宽度仍用 14px
        # 保守估计：宁可气泡略宽，也不能把文字压窄折行。单行不限制宽度，
        # 只有贴到气泡上限（520）才 wordWrap 折行，否则整行显示。
        # 不能用 lab.font().family()：QSS 生效前它可能是 "MS Sans Serif" 这类
        # Win11 已删除的旧字体，构造 QFont 时就会触发 DirectWrite 报错刷屏。
        _f = QFont(QApplication.font())
        _f.setPixelSize(14)
        fm = QFontMetrics(_f)
        longest = max((fm.horizontalAdvance(line) for line in text.splitlines()),
                      default=fm.horizontalAdvance(text))
        self._label = lab
        self._longest = longest
        if longest < 520:
            lab.setWordWrap(False)              # 短文本：单行，不折
            lab.setMinimumWidth(longest + 4)
            lab.setMaximumWidth(520)
            self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum)
            self.setMaximumWidth(520)
        else:
            lab.setWordWrap(True)               # 超长：贴到气泡宽度才折
            lab.setMinimumWidth(520)
            lab.setMaximumWidth(520)
            self.setFixedWidth(520)             # 撑满气泡，自然折行

        if cards:
            box = QWidget()
            gl = QGridLayout(box)
            gl.setContentsMargins(0, 6, 0, 0)
            gl.setSpacing(8)
            gl.setAlignment(Qt.AlignmentFlag.AlignRight)
            cols = min(3, len(cards))   # 最多 3 列，多了自动换行
            img_i = 0
            for i, c in enumerate(cards):
                opener = None
                if c.get("kind") == "image":
                    this_idx = img_i
                    img_i += 1
                    # 绑定整条消息的 cards：预览窗里可 ← → 切换同消息的其它图片
                    opener = lambda _cs=cards, _i=this_idx: _open_image_preview(_cs, _i)
                elif c.get("kind") == "file" and c.get("text"):
                    opener = lambda _n=c.get("name", ""), _t=c.get("text", ""): _open_text_preview(_n, _t)
                gl.addWidget(
                    UserAttachCard(c.get("name", ""), c.get("kind", "file"),
                                   c.get("thumb", ""), c.get("size", ""),
                                   on_open=opener),
                    i // cols, i % cols)
            lay.addWidget(box, alignment=Qt.AlignmentFlag.AlignRight)


class AiBlock(QWidget):
    """AI 回答（对话式排版）。

    Claude 版式：无头像、无气泡、纯文本流顶到左边缘；状态行保留名字（深色加粗）
    + 元信息（次要灰字）→ 正文（Markdown 分块）→ 工具行（复制/重新生成）+ 模型名。
    流式期间用单块富文本，打完后按块重排（代码块单独成框）。
    模型会思考时，正文上方还有一块可折叠的「思考过程」，出结果后自动收起。
    """

    THINK_MAX_H = 240      # 思考区最大高度（像素），超出就内部滚动

    def __init__(self, on_copy, model_name: str = "", on_regen=None) -> None:
        super().__init__()
        self._on_copy = on_copy
        self._on_regen = on_regen
        self._model = model_name
        self._full = ""
        self._parts: list[QWidget] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(7)

        # AI 回复：无头像纯文本流（Claude 版式），正文直接顶到左边缘

        # 状态行：原型 Claude 档只去头像，名字仍在（深色加粗）+ 次要灰字元信息
        self.meta = QLabel()
        self.meta.setObjectName("meta")
        self.meta.setTextFormat(Qt.TextFormat.RichText)
        self.meta.setContentsMargins(0, 0, 0, 0)
        self.set_meta("正在生成…")
        outer.addWidget(self.meta)

        # 思考过程（可折叠）：流式时展开、实时滚动，出结果后自动收成一行
        self._think = ""            # 完整思考文本
        self._think_shown = ""      # 已渲染到界面的部分（节流用）
        self._think_open = True
        self._think_done = False
        self.think_box = QWidget()
        tb = QVBoxLayout(self.think_box)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setSpacing(4)

        # 箭头用 ▼/▶（U+25B6/25BC）：▾/▸ 在微软雅黑里缺字形，会显示成方框
        self.think_btn = QPushButton("▼ 正在思考…")
        self.think_btn.setObjectName("thinkBtn")
        self.think_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.think_btn.clicked.connect(self._toggle_think)
        tb.addWidget(self.think_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        # 用 QTextEdit 而不是 QLabel+QScrollArea：QLabel 在滚动区里 sizeHint 算不准，
        # 思考稍长就只显示前两三行。QTextEdit 自带滚动，且能按文档实际高度自适应。
        self.think_body = QTextEdit()
        self.think_body.setObjectName("thinkText")
        self.think_body.setReadOnly(True)
        self.think_body.setFrameShape(QFrame.Shape.NoFrame)
        self.think_body.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.think_body.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        tb.addWidget(self.think_body)
        self.think_box.setVisible(False)
        outer.addWidget(self.think_box)

        # 思考文本按 60ms 节流刷新：模型每个 token 都来一次，直接 setText 会卡
        self._think_timer = QTimer(self)
        self._think_timer.setInterval(60)
        self._think_timer.timeout.connect(self._flush_think)

        # 正文
        self.body = QWidget()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)

        self.typing = WrapLabel()
        self.typing.setObjectName("aiText")
        self.typing.setTextFormat(Qt.TextFormat.RichText)
        self.typing.setWordWrap(True)
        self.typing.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.typing.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.typing.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        bl.addWidget(self.typing)

        self.parts_box = QWidget()
        self.parts = QVBoxLayout(self.parts_box)
        self.parts.setContentsMargins(0, 0, 0, 0)
        self.parts.setSpacing(6)
        self.parts_box.setVisible(False)
        bl.addWidget(self.parts_box)
        outer.addWidget(self.body)

        # 工具行：复制 / 重新生成 …… 模型名
        ftr = QHBoxLayout()
        ftr.setContentsMargins(0, 2, 0, 0)
        ftr.setSpacing(14)
        self.copy_btn = QPushButton("复制")
        self.copy_btn.setObjectName("actBtn")
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setVisible(False)
        self.copy_btn.clicked.connect(self._copy)
        ftr.addWidget(self.copy_btn)

        self.regen_btn = QPushButton("重新生成")
        self.regen_btn.setObjectName("actBtn")
        self.regen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.regen_btn.setVisible(False)
        self.regen_btn.clicked.connect(self._regen)
        ftr.addWidget(self.regen_btn)
        ftr.addStretch(1)

        self.model_lab = QLabel(model_name)
        self.model_lab.setObjectName("modelFoot")
        ftr.addWidget(self.model_lab)
        outer.addLayout(ftr)

    def set_meta(self, state: str, *extra: str) -> None:
        """状态行：名字深色加粗 + 元信息次要灰字（Claude 档只去头像，名字仍在）。"""
        bits = [state] + [b for b in extra if b]
        self.meta.setText(
            '<span style="color:#5F6570;font-weight:600;">LyLaunch</span>'
            f'<span style="color:#5F6570;"> {" · ".join(bits)}  ›</span>')

    # ---- 思考过程
    def append_think(self, piece: str) -> None:
        """思考增量：只累加，交给定时器节流刷新（每 token 都 setText 会卡）。"""
        if not piece:
            return
        self._think += piece
        if not self.think_box.isVisible():
            self.think_box.setVisible(True)
        if not self._think_timer.isActive():
            self._think_timer.start()

    def _flush_think(self) -> None:
        if self._think == self._think_shown:
            self._think_timer.stop()
            return
        self._think_shown = self._think
        body = md_chat.inline_html(_norm(self._think)).replace("\n", "<br>")
        self.think_body.setHtml(
            f'<div style="line-height:190%; font-size:12.5px; color:#5F6570;">{body}</div>')
        self._fit_think_height()
        self._sync_think_head()
        sb = self.think_body.verticalScrollBar()
        sb.setValue(sb.maximum())            # 跟着最新思路滚

    def _fit_think_height(self) -> None:
        """按文档实际高度自适应；超过上限就内部滚动，别把正文挤到看不见。"""
        doc = self.think_body.document()
        w = self.think_body.viewport().width()
        if w > 40:
            doc.setTextWidth(w)
        h = int(doc.size().height()) + 6
        self.think_body.setFixedHeight(max(34, min(self.THINK_MAX_H, h)))

    def _sync_think_head(self) -> None:
        # 只用 ▼/▲：微软雅黑有这两个几何符号，▸/▶ 会缺字形显示成方框
        arrow = "▼" if self._think_open else "▲"
        n = len(self._think)
        if self._think_done:
            text = f"{arrow} 思考过程 · {n} 字"
        else:
            text = f"{arrow} 正在思考…" + (f" · {n} 字" if n else "")
        self.think_btn.setText(text)

    def _toggle_think(self) -> None:
        self._think_open = not self._think_open
        self.think_body.setVisible(self._think_open)
        self._sync_think_head()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._think:                 # 宽度变了，思考区高度要跟着重算
            self._fit_think_height()

    # ---- 流式
    def render_typing(self, shown: str, more: bool) -> None:
        # 同步纯文本快照：切会话/关窗时靠 _full 把没写完的回答落盘
        # （旧实现只在 set_final 里赋值，流式期间 _full 一直是空的）
        self._full = shown
        caret = '<span style="color:#2F6BEE;">▍</span>' if more else ""
        # typing_html 会藏掉流式过程中「还没写完」的半截公式，避免闪出 \( \fra
        body = (md_chat.typing_html(_norm(shown))
                .replace("\n\n", '<br><span style="font-size:7px;">&nbsp;</span><br>')
                .replace("\n", "<br>"))
        self.typing.setText(
            f'<div style="line-height:190%; font-size:14px; color:#161C28;">{body}{caret}</div>')

    def set_placeholder(self, text: str) -> None:
        self.typing.setText(_html(text, "#5F6570"))

    # ---- 完成：按块重排
    def set_final(self, text: str, elapsed: float, tokens: int,
                  reasoning: str = "") -> None:
        self._full = text
        # 历史重放时思考是一次性给的；实时生成时已经边流边渲染过了
        if reasoning and not self._think:
            self._think = reasoning
            self.think_box.setVisible(True)
        self._think_done = True
        if self._think:
            self._think_timer.stop()
            self._flush_think()
            # 出结果后自动折叠成一行（点开头仍可展开看全过程）
            self._think_open = False
            self.think_body.setVisible(False)
            self._sync_think_head()
        self._render_parts(text)
        self.typing.setVisible(False)
        self.parts_box.setVisible(True)
        self.set_meta("已生成",
                      f"{elapsed:.1f}s" if elapsed > 0 else "",
                      f"{tokens} tok" if tokens else "")
        if self._model:
            self.model_lab.setText(f"{tokens} tok · {self._model}" if tokens else self._model)
        self.copy_btn.setVisible(True)
        self.regen_btn.setVisible(self._on_regen is not None)

    def _make_part(self, block) -> QWidget:
        if block.kind == "code":
            lab = WrapLabel()
            lab.setObjectName("codeBox")
            lab.setTextFormat(Qt.TextFormat.RichText)
            lab.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            lab.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            return lab
        lab = WrapLabel()
        lab.setTextFormat(Qt.TextFormat.RichText)
        lab.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        lab.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        lab.setOpenExternalLinks(True)
        # TextBrowserInteraction = 鼠标/键盘可选中 + 链接可点击（两者可共存）
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        return lab

    def _fill_part(self, w: QWidget, block) -> None:
        if block.kind == "code":
            w.setStyleSheet(
                "QLabel#codeBox{background:#F2F6FD; border:1px solid #E8EDF6;"
                " border-radius:9px; padding:9px 11px;}")
            w.setText(md_chat.code_html(block.content))
        else:
            w.setText(md_chat.block_html(block))  # type: ignore[attr-defined]

    def _render_parts(self, md_text: str) -> None:
        blocks = md_chat.parse_blocks(md_text)
        while len(self._parts) < len(blocks):
            b = blocks[len(self._parts)]
            w = self._make_part(b)
            self._parts.append(w)
            self.parts.addWidget(w)
        while len(self._parts) > len(blocks):
            w = self._parts.pop()
            self.parts.removeWidget(w)
            w.hide()

            w.deleteLater()
        for w, b in zip(self._parts, blocks):
            self._fill_part(w, b)

    def set_error(self, msg: str) -> None:
        self.set_meta("出错了")
        self.typing.setText(_html(msg, "#D93A2F"))
        self.typing.setVisible(True)
        self.parts_box.setVisible(False)

    def _copy(self) -> None:
        if self._full:
            self._on_copy(self._full)
            self.copy_btn.setText("已复制")
            QTimer.singleShot(1400, lambda: self.copy_btn.setText("复制"))

    def _regen(self) -> None:
        if self._on_regen is not None:
            self._on_regen()


class WrapLabel(QLabel):
    """wordWrap 的 QLabel，sizeHint 按"实际可用宽度"算高度。

    原生 QLabel 在 wordWrap + 富文本时，sizeHint 会用很窄的默认宽度计算，
    几十个字会被当成 3~4 行 → 布局按这个虚高值分配高度 → 回答下方一大片空白。
    这里用 heightForWidth(当前宽度) 重算，让布局拿到的就是真实高度。
    """

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)

    def sizeHint(self):  # noqa: N802
        w = self.width() if self.width() > 40 else 640
        h = self.heightForWidth(w)
        if h <= 0:
            return super().sizeHint()
        return QSize(w, h)

    def minimumSizeHint(self):  # noqa: N802
        return QSize(60, self.sizeHint().height())


class AttachmentTextEdit(QTextEdit):
    """输入框：把"粘贴/拖入的图片与文件"交给附件系统，纯文本仍走默认行为。

    原生 QTextEdit 只认文本：粘贴截图会没反应，拖文件会插入路径字符串。
    """

    def __init__(self, on_images, on_files, parent=None) -> None:
        super().__init__(parent)
        self._on_images = on_images      # 回调：接收 QImage
        self._on_files = on_files        # 回调：接收本地路径列表

    def canInsertFromMimeData(self, source) -> bool:  # noqa: N802
        if source.hasImage() or source.hasUrls():
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source) -> None:  # noqa: N802
        if source.hasImage():
            img = source.imageData()
            if img is not None:
                self._on_images(img)
                return
        if source.hasUrls():
            paths = [u.toLocalFile() for u in source.urls() if u.isLocalFile()]
            if paths:
                self._on_files(paths)
                return
        # 只插纯文本：从网页 / Word / 聊天软件复制过来的内容同时带 text 和 html，
        # QTextEdit 默认优先用 html —— 字体、颜色、字号、加粗斜体全被带进来，
        # 输入框立刻变得花花绿绿（用户反馈截图）。这里只取纯文本。
        if source.hasText():
            self.insertPlainText(source.text())
            return
        super().insertFromMimeData(source)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        md = event.mimeData()
        if md.hasUrls() or md.hasImage():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls() or event.mimeData().hasImage():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        md = event.mimeData()
        if md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            if paths:
                self._on_files(paths)
                event.acceptProposedAction()
                return
        if md.hasImage():
            img = md.imageData()
            if img is not None:
                self._on_images(img)
                event.acceptProposedAction()
                return
        super().dropEvent(event)


class ConfirmDialog(QDialog):
    """项目风格的确认框（QMessageBox 的系统样式太朴素）。"""

    def __init__(self, parent, title: str, message: str,
                 ok_text: str = "确定", danger: bool = False,
                 show_cancel: bool = True) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setFixedWidth(396)
        self._result = False

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 16)
        root.setSpacing(12)

        # 标题行：色块图标 + 标题
        head = QHBoxLayout()
        head.setSpacing(10)
        icon = QLabel("!" if danger else "i")
        icon.setObjectName("cfmIconDanger" if danger else "cfmIconInfo")
        icon.setFixedSize(26, 26)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.addWidget(icon)
        lab = QLabel(title)
        lab.setObjectName("cfmTitle")
        head.addWidget(lab)
        head.addStretch(1)
        root.addLayout(head)

        body = QLabel(message)
        body.setObjectName("cfmBody")
        body.setWordWrap(True)
        root.addWidget(body)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("ghostBtn")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        cancel.setVisible(show_cancel)
        row.addWidget(cancel)
        ok = QPushButton(ok_text)
        ok.setObjectName("dangerBtn" if danger else "primaryBtn")
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setMinimumWidth(84)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        root.addLayout(row)

        self.setStyleSheet(QSS + """
QDialog { background: #FFFFFF; }
QLabel#cfmTitle { font-size: 14px; font-weight: 600; color: #161C28; }
QLabel#cfmBody { font-size: 12.5px; color: #5F6570; line-height: 170%; }
QLabel#cfmIconDanger {
    background: #FDECEA; color: #EC5F53; border-radius: 13px;
    font-size: 13px; font-weight: 700;
}
QLabel#cfmIconInfo {
    background: #E4EDFC; color: #2F6BEE; border-radius: 13px;
    font-size: 13px; font-weight: 700;
}
QPushButton#primaryBtn { padding: 8px 16px; }
QPushButton#dangerBtn { padding: 8px 16px; }
QPushButton#ghostBtn { padding: 8px 14px; font-size: 12px; }
""")

    @classmethod
    def ask(cls, parent, title: str, message: str, ok_text: str = "确定",
            danger: bool = False, show_cancel: bool = True) -> bool:
        dlg = cls(parent, title, message, ok_text, danger, show_cancel)
        return dlg.exec() == QDialog.DialogCode.Accepted

    @classmethod
    def info(cls, parent, title: str, message: str) -> None:
        cls(parent, title, message, "知道了", False, False).exec()


class NewChatButton(QPushButton):
    """侧栏「新会话」胶囊（原型 `html[data-aichat] .ai-new-chat`）。

    40px 胶囊、SimHei 700、文字居中、与侧栏同底 `#F8FBFF`，靠左上白高光 +
    右下主色淡影浮起；hover 收紧阴影、pressed 去阴影像按下去。

    Qt 里一个 QWidget 只能挂一个 QGraphicsDropShadowEffect → 双向阴影必须在
    paintEvent 里自绘；外层留 PAD 给阴影，否则画不到控件矩形外面。
    QSS 的 transform / transition / box-shadow 都不支持，故全部自绘。
    """

    PAD = 14          # 阴影外扩空间（四边各留）
    H = 40            # 胶囊可见高度

    def __init__(self, text: str = "新会话") -> None:
        super().__init__(text)
        self.setObjectName("newBtn")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hover = False
        self._pressed = False

    def _font(self) -> QFont:
        f = QFont("SimHei")
        f.setPixelSize(13)
        f.setBold(True)      # 700
        return f

    def sizeHint(self) -> QSize:  # noqa: N802
        tw = QFontMetrics(self._font()).horizontalAdvance(self.text())
        return QSize(tw + 40 + 2 * self.PAD, self.H + 2 * self.PAD)

    def enterEvent(self, e) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        self._pressed = True
        self.update()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        self._pressed = False
        self.update()
        super().mouseReleaseEvent(e)

    @staticmethod
    def _glow(p: QPainter, cap, ox: float, oy: float,
              blur: float, color: QColor) -> None:
        """多层半透明圆角矩形 ≈ 柔和外发光。

        从外到内画，中心随后被不透明胶囊盖住，只留边缘的渐变晕影。
        """
        layers = 10
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(layers, 0, -1):
            t = i / layers
            grow = blur * t
            c = QColor(color)
            c.setAlpha(int(color.alpha() * (1 - t) ** 2 / layers * 3))
            if c.alpha() <= 0:
                continue
            # PySide6 对 float→int 参数不隐式转换，四个几何值必须显式 int()
            x = int(cap.x() - grow + ox * t)
            y = int(cap.y() - grow + oy * t)
            w = int(cap.width() + 2 * grow)
            h = int(cap.height() + 2 * grow)
            p.setBrush(c)
            p.drawRoundedRect(x, y, w, h, h / 2, h / 2)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        cap = self.rect().adjusted(self.PAD, self.PAD, -self.PAD, -self.PAD)
        r = cap.height() / 2
        x, y, w, h = cap.x(), cap.y(), cap.width(), cap.height()

        if not self._pressed:
            tight = self._hover
            self._glow(p, cap, -4 if not tight else -2, -2 if not tight else -1,
                       16 if not tight else 8, QColor(255, 255, 255, 242))
            self._glow(p, cap, 4 if not tight else 2, 2 if not tight else 1,
                       16 if not tight else 8, QColor(47, 107, 238, 51))

        if self._pressed:
            bg = QColor("#D6E2F7")     # --bg-pressed
        elif self._hover:
            bg = QColor("#E4EDFC")     # --bg-tile-hover
        else:
            bg = QColor("#F8FBFF")     # --side-bg：与侧栏同底
        path = QPainterPath()
        path.addRoundedRect(x, y, w, h, r, r)
        p.fillPath(path, QBrush(bg))
        p.setPen(QPen(QColor(255, 255, 255, 178), 2))   # --neu-edge 2px
        p.drawPath(path)

        # 原型按钮是「14px 线性 + 图标 + 文字」整体居中，不是只有文字。
        # 之前误把「去模板空格」理解成「去掉整个图标」，这里补回来。
        fm = QFontMetrics(self._font())
        tw = fm.horizontalAdvance(self.text())
        icon_w, gap = 14, 6
        total = icon_w + gap + tw
        left = int(x + (w - total) / 2)
        cy = cap.center().y()
        ink = QColor("#2359D6")
        p.setPen(QPen(ink, 1.6))
        p.drawLine(QPointF(left, cy), QPointF(left + icon_w, cy))
        p.drawLine(QPointF(left + icon_w / 2, cy - icon_w / 2),
                   QPointF(left + icon_w / 2, cy + icon_w / 2))
        p.setPen(ink)
        p.setFont(self._font())
        p.drawText(QRect(left + icon_w + gap, y, tw + 4, h),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   self.text())
        p.end()


class DeepThinkButton(QPushButton):
    """「深度思考」开关：hover 时主色实心圆从中心扩散铺满（600ms ease-out）。

    常态白底细描边灰字；开启态直接实心主色并停用扩散；文字/图标在圆覆盖到
    时反白。QSS 不支持 ::before / transition，所以圆用 Property 半径 +
    QPropertyAnimation 驱动 paintEvent 自绘，圆心固定在按钮中心，不用 transform。
    """

    def __init__(self, text: str = "深度思考") -> None:
        super().__init__(text)
        self.setObjectName("deepBtn")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._radius = 0.0
        self._hover = False
        self._anim = QPropertyAnimation(self, b"fillRadius", self)
        self._anim.setDuration(600)                       # 设计说明：420 → 600ms
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    # ---------- 可动画属性
    def _get_radius(self) -> float:
        return self._radius

    def _set_radius(self, v: float) -> None:
        self._radius = float(v)
        self.update()

    fillRadius = Property(float, _get_radius, _set_radius)

    def _animate_to(self, target: float) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._radius)
        self._anim.setEndValue(target)
        self._anim.start()

    def setChecked(self, v: bool) -> None:  # noqa: N802
        super().setChecked(v)
        self.update()

    def enterEvent(self, e) -> None:  # noqa: N802
        self._hover = True
        if not self.isChecked() and self.isEnabled():
            self._animate_to(90.0)     # 直径 180px，铺满整个按钮
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._hover = False
        self._animate_to(0.0)
        self.update()
        super().leaveEvent(e)

    def sizeHint(self) -> QSize:  # noqa: N802
        f = QFont(self.font())
        f.setPixelSize(11)
        tw = QFontMetrics(f).horizontalAdvance(self.text())
        return QSize(tw + 26, 30)      # padding 6px 13px、圆角 16

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(0, 0, -1, -1)
        x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        on = self.isChecked()
        enabled = self.isEnabled()

        path = QPainterPath()
        path.addRoundedRect(x, y, w, h, 16, 16)
        if on:
            bg, border = QColor("#2F6BEE"), QColor("#2F6BEE")
        else:
            bg = QColor("#FFFFFF")
            border = QColor("#2F6BEE") if self._hover else QColor("#D8E3F5")
        p.fillPath(path, QBrush(bg))

        # 圆扩散：只在未开启、可用、且半径 > 0 时画，裁剪在圆角内
        if not on and enabled and self._radius > 0:
            p.save()
            p.setClipPath(path)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#2F6BEE"))
            cx, cy = rect.center().x(), rect.center().y()
            p.drawEllipse(QPointF(cx, cy), self._radius, self._radius)
            p.restore()

        p.setPen(QPen(border, 1))
        p.drawPath(path)

        # 圆覆盖到文字中心后才反白，否则白字在白底上看不见
        covered = self._radius > max(28.0, w * 0.35)
        if not enabled:
            ink = QColor("#BFC5D0")
        elif on or covered:
            ink = QColor("#FFFFFF")
        else:
            ink = QColor("#5F6570")
        f = QFont(self.font())
        f.setPixelSize(11)
        f.setBold(on)
        p.setPen(ink)
        p.setFont(f)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.text())
        p.end()


class ModelRow(QWidget):
    """模型列表里的一行：左侧列表项，悬停时把详情推到右栏。"""

    hovered = Signal(str)      # model_id
    left = Signal(str)         # 鼠标离开这一行（用来延迟收起介绍气泡）
    clicked = Signal(str)

    def __init__(self, spec, installed: bool, current: bool,
                 state: str = "", downloading: bool = False) -> None:
        super().__init__()
        self.spec = spec
        self.installed = installed
        self.current = current
        # 显式状态徽章：当前 / 已下载 / 未下载 / 下载中（原型要求一眼看得出是否已下载）
        self.state = state or ("当前" if current else ("已下载" if installed else "未下载"))
        self.downloading = downloading
        self.setObjectName("modelRowCur" if current else
                           ("modelRowDl" if downloading else
                            ("modelRowMiss" if not installed else "modelRow")))
        # QWidget 子类要靠这个属性才会用 QSS 画自身背景 / 虚线描边
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(6)

        name = QLabel(spec.name)
        name.setObjectName("rowNameDim" if not installed else "rowName")
        # 允许名字被压窄并打省略号：否则「Qwen3.5-9B-DeepSeek-V4-Flash」这种长名字
        # 会把整行的最小宽度顶到 300+，面板就再也窄不下来（窗口小的时候只能贴满左边）
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        name.setMinimumWidth(0)
        self._name_lab = name
        self._name_text = spec.name
        lay.addWidget(name, 1)

        state_lab = QLabel(self.state)
        state_lab.setObjectName({"当前": "mStateCur", "已下载": "mStateOk",
                                 "下载中": "mStateDl"}.get(self.state, "mStateMiss"))
        lay.addWidget(state_lab)

        if spec.vision:
            tag = QLabel("图")
            tag.setObjectName("rowTag")
            lay.addWidget(tag)

        vram = QLabel(f"{ModelPanel.vram_hint(spec)}")
        vram.setObjectName("rowVram")
        lay.addWidget(vram)

    def enterEvent(self, _e) -> None:  # noqa: N802
        self.hovered.emit(self.spec.id)

    def leaveEvent(self, _e) -> None:  # noqa: N802
        self.left.emit(self.spec.id)

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        avail = self._name_lab.width()
        if avail > 12:
            fm = QFontMetrics(self._name_lab.font())
            txt = fm.elidedText(self._name_text, Qt.TextElideMode.ElideRight, avail)
            if txt != self._name_lab.text():
                self._name_lab.setText(txt)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.spec.id)


def _place_popup(w: QWidget, anchor: QWidget) -> None:
    """把气泡放在主窗口内部（子控件坐标），夹在窗口可见范围内。"""
    w.adjustSize()
    parent = w.parentWidget()
    if parent is None:
        return
    try:
        bottom = anchor.mapToGlobal(anchor.rect().bottomLeft())
        top_pt = anchor.mapToGlobal(anchor.rect().topLeft())
    except RuntimeError:
        # 锚点控件已被销毁（典型场景：列表刷新后旧会话行被删，但气泡还要弹）
        # → 回退到父窗口上部居中，绝不因为一个悬空引用把程序带崩
        w.move(max(8, (parent.width() - w.width()) // 2),
               max(8, min(parent.height() // 6, parent.height() - w.height() - 8)))
        return
    local = parent.mapFromGlobal(bottom)
    x, y = local.x(), local.y() + 6
    if y + w.height() > parent.height() - 8:
        y = parent.mapFromGlobal(top_pt).y() - w.height() - 6
    x = max(8, min(x, parent.width() - w.width() - 8))
    y = max(8, min(y, parent.height() - w.height() - 8))
    w.move(x, y)


# 气泡（PopCard / RowMenu / PopConfirm / RenamePopup）专用样式。
# 它们都是对话框的**子控件**且各自 setStyleSheet，Qt 只匹配最近的祖先样式表，
# 所以这套规则必须独立存在、不能并进主 QSS；主 QSS 里若再写一份永远不生效
# （2026-11-10 清理掉了一整份重复）。色板与主 QSS 保持一致：
# 主色 #2F6BEE / 次要 #5F6570 / 主文字 #161C28 / 描边 #D8E3F5 / 危险 #EC5F53。
_POPUP_QSS = """
QFrame#popCard { background: #FFFFFF; border: 1px solid #D8E3F5; border-radius: 10px; }
QLabel#popTitle { font-size: 12.5px; font-weight: 600; color: #161C28; }
QLabel#popMsg { font-size: 11px; color: #5F6570; }
QLineEdit#popEdit {
    font-size: 12px; padding: 6px 8px; border: 1px solid #D8E3F5;
    border-radius: 7px; background: #FFFFFF; color: #161C28;
}
QLineEdit#popEdit:focus { border-color: #2F6BEE; }
QPushButton#popGhost {
    background: transparent; border: none; color: #5F6570;
    font-size: 11.5px; padding: 5px 8px;
}
QPushButton#popGhost:hover { color: #2F6BEE; }
QPushButton#popDanger {
    background: #FDECEA; border: none; color: #EC5F53;
    font-size: 11.5px; padding: 5px 12px; border-radius: 7px;
}
QPushButton#popDanger:hover { background: #FBDDD9; }
QPushButton#popPrimary {
    background: #2F6BEE; border: none; color: #FFFFFF;
    font-size: 11.5px; padding: 5px 12px; border-radius: 7px;
}
QPushButton#popPrimary:hover { background: #2359D6; }
"""


class PopCard(QFrame):
    """气泡基类：作为主窗口的子控件（不用 Qt.Popup 顶层窗口，避免渲染异常）。

    自带 stylesheet；点击气泡外部自动关闭。
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("popCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(_POPUP_QSS)
        self.hide()

    def popup_at(self, anchor: QWidget) -> None:
        _place_popup(self, anchor)
        self.show()
        self.raise_()
        QApplication.instance().installEventFilter(self)

    def hide(self) -> None:  # noqa: N802
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().hide()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.MouseButtonPress and self.isVisible():
            w = obj if isinstance(obj, QWidget) else None
            if w is not None and w is not self and not self.isAncestorOf(w):
                self.hide()
        return super().eventFilter(obj, event)


class RowMenu(PopCard):
    """会话行的「···」操作列表：置顶 / 重命名 / 删除。"""

    rename = Signal(str)
    delete = Signal(str, object)
    pin = Signal(str)
    open_folder = Signal(str)     # 打开这条会话所在的文件夹
    backup = Signal(str)          # 单独导出这条会话
    multi = Signal(str)           # 以这条为起点进入多选

    def __init__(self, parent, sid: str, pinned: bool = False) -> None:
        super().__init__(parent)
        self._sid = sid
        self._anchor = None      # 打开菜单时的原始锚点（more_btn / 会话行）
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(2)

        p = QPushButton("取消置顶" if pinned else "置顶")
        p.setObjectName("rowMenuItem")
        p.setCursor(Qt.CursorShape.PointingHandCursor)
        p.clicked.connect(self._on_pin)
        lay.addWidget(p)

        r = QPushButton("重命名")
        r.setObjectName("rowMenuItem")
        r.setCursor(Qt.CursorShape.PointingHandCursor)
        r.clicked.connect(self._on_rename)
        lay.addWidget(r)

        op = QPushButton("打开所在文件夹")
        op.setObjectName("rowMenuItem")
        op.setCursor(Qt.CursorShape.PointingHandCursor)
        op.clicked.connect(self._on_open_folder)
        lay.addWidget(op)

        bk = QPushButton("备份这条会话")
        bk.setObjectName("rowMenuItem")
        bk.setCursor(Qt.CursorShape.PointingHandCursor)
        bk.clicked.connect(self._on_backup)
        lay.addWidget(bk)

        mu = QPushButton("批量选择")
        mu.setObjectName("rowMenuItem")
        mu.setCursor(Qt.CursorShape.PointingHandCursor)
        mu.clicked.connect(self._on_multi)
        lay.addWidget(mu)

        line = QFrame()
        line.setObjectName("rowMenuSep")
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFixedHeight(1)
        lay.addWidget(line)

        d = QPushButton("删除")
        d.setObjectName("rowMenuItemDanger")
        d.setCursor(Qt.CursorShape.PointingHandCursor)
        d.clicked.connect(self._on_delete)
        lay.addWidget(d)
        self.setFixedWidth(132)

    def popup_at(self, anchor) -> None:
        self._anchor = anchor          # 记下来，删除确认气泡要用它定位
        super().popup_at(anchor)

    def _on_pin(self) -> None:
        self.hide()
        self.pin.emit(self._sid)

    def _on_rename(self) -> None:
        self.hide()
        self.rename.emit(self._sid)

    def _on_open_folder(self) -> None:
        self.hide()
        self.open_folder.emit(self._sid)

    def _on_backup(self) -> None:
        self.hide()
        self.backup.emit(self._sid)

    def _on_multi(self) -> None:
        self.hide()
        self.multi.emit(self._sid)

    def _on_delete(self) -> None:
        # 关键：不能拿自己当锚点 —— 这行菜单马上就要 hide()，隐藏控件的坐标
        # 不可靠，删除确认气泡会飘到别处（甚至跑到侧边栏底部）。用打开菜单时
        # 记下的原始锚点（会话行 / 「···」按钮），气泡就贴着那一行出现。
        anchor = self._anchor
        self.hide()
        self.delete.emit(self._sid, anchor)


class PopConfirm(PopCard):
    """轻量气泡确认：点删除按钮时在会话行旁边弹出，不打断整页。"""

    confirmed = Signal()

    def __init__(self, parent, title: str, message: str,
                 ok_text: str = "删除") -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 11)
        lay.setSpacing(9)
        t = QLabel(title)
        t.setObjectName("popTitle")
        lay.addWidget(t)
        m = QLabel(message)
        m.setObjectName("popMsg")
        m.setWordWrap(True)
        lay.addWidget(m)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("popGhost")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.hide)
        row.addWidget(cancel)
        ok = QPushButton(ok_text)
        ok.setObjectName("popDanger")
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.clicked.connect(self._confirm)
        row.addWidget(ok)
        lay.addLayout(row)
        self.setFixedWidth(232)
        # 焦点留给气泡本身：两个按钮不抢键盘焦点，这样 Enter/Esc 一定落到这里。
        # 否则焦点还停在输入框上，回车会变成"发送消息"，用户就会觉得确认不了。
        for b in (cancel, ok):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def popup_at(self, anchor) -> None:
        super().popup_at(anchor)
        self.setFocus()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        k = event.key()
        if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._confirm()          # 回车 = 确认删除
            return
        if k == Qt.Key.Key_Escape:
            self.hide()              # Esc = 取消
            return
        super().keyPressEvent(event)

    def _confirm(self) -> None:
        self.hide()
        self.confirmed.emit()


class RenamePopup(PopCard):
    """右键会话 → 就地改名字的小气泡。"""

    def __init__(self, parent, sid: str, current: str, on_save) -> None:
        super().__init__(parent)
        self._sid = sid
        self._on_save = on_save
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 11)
        lay.setSpacing(9)
        t = QLabel("重命名会话")
        t.setObjectName("popTitle")
        lay.addWidget(t)
        self.edit = QLineEdit(current)
        self.edit.setObjectName("popEdit")
        self.edit.setMaxLength(40)
        self.edit.selectAll()
        self.edit.returnPressed.connect(self._save)
        lay.addWidget(self.edit)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("popGhost")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.hide)
        row.addWidget(cancel)
        ok = QPushButton("保存")
        ok.setObjectName("popPrimary")
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.clicked.connect(self._save)
        row.addWidget(ok)
        lay.addLayout(row)
        self.setFixedWidth(250)

    def _save(self) -> None:
        text = self.edit.text().strip()
        self.hide()
        if text:
            self._on_save(self._sid, text)

    def popup_at(self, anchor: QWidget) -> None:
        super().popup_at(anchor)
        self.edit.setFocus()
        self.edit.selectAll()


class SmallCheckBox(QCheckBox):
    """13px 小复选框：未选灰边白底，选中蓝边白底 + **蓝色对勾**。

    系统默认 indicator 在 Windows 上是深/白勾，且 16~18px 太笨重；
    QSS 又没法给勾单独上色（只能塞静态图片），所以直接自绘。
    """

    def paintEvent(self, _e) -> None:  # noqa: N802
        from PySide6.QtCore import QRect
        side = 13
        r = QRect(0, max(0, (self.height() - side) // 2), side, side)
        checked = self.isChecked()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(QPen(QColor("#2F6BEE" if checked else "#C3CBD9"), 1.0))
        p.setBrush(QColor("#FFFFFF"))
        p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 3, 3)
        if checked:
            pen = QPen(QColor("#2F6BEE"), 1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawLine(3.5, 7.0, 5.6, 9.1)
            p.drawLine(5.6, 9.1, 9.6, 4.5)
        p.end()


class SessionRow(QWidget):
    """侧边栏的一条会话：标题 + 日期，整行可点。"""

    clicked = Signal(str)
    checked_changed = Signal(str, bool)
    delete_requested = Signal(str, object)
    rename_requested = Signal(str, object)
    menu_requested = Signal(str, object)

    def __init__(self, sid: str, title: str, date_str: str,
                 current: bool) -> None:
        super().__init__()
        self.sid = sid
        self.setObjectName("sessRowCur" if current else "sessRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 5, 5, 6)
        lay.setSpacing(2)

        self._multi = False
        self.chk = SmallCheckBox()
        self.chk.setVisible(False)
        self.chk.setFixedSize(14, 14)
        self.chk.setStyleSheet(
            "QCheckBox{spacing:0; margin:0; padding:0; background:transparent;}")
        self.chk.toggled.connect(lambda v: self.checked_changed.emit(self.sid, v))
        lay.insertWidget(0, self.chk)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        lab = QLabel(title)
        lab.setObjectName("sessTitleCur" if current else "sessTitle")
        # 关键：标题必须可任意压缩。QLabel 不换行时 minimumSizeHint 等于整段
        # 文字的宽度，会把 QScrollArea 的内容撑得比侧边栏还宽 —— 结果是标题被
        # 硬裁（连省略号都没有）、右侧「···」按钮直接被挤出可视区。
        lab.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lab.setMinimumWidth(0)
        col.addWidget(lab)
        self._lab = lab
        self._title = title
        date = QLabel(date_str)
        date.setObjectName("sessDate")
        col.addWidget(date)
        lay.addLayout(col, 1)

        more_btn = QPushButton("···")
        more_btn.setObjectName("sessMore")
        more_btn.setFixedSize(22, 22)
        more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        more_btn.clicked.connect(lambda: self.menu_requested.emit(sid, more_btn))
        self.more_btn = more_btn
        lay.addWidget(more_btn, 0, Qt.AlignmentFlag.AlignVCenter)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._elide_title()

    def _elide_title(self) -> None:
        """按实际可用宽度加省略号（QLabel 自己不会 elide，只会硬裁文字）。"""
        avail = self._lab.width()
        if avail <= 0:
            return
        fm = QFontMetrics(self._lab.font())
        txt = fm.elidedText(self._title, Qt.TextElideMode.ElideRight, avail)
        if txt != self._lab.text():
            self._lab.setText(txt)

    def set_multi(self, on: bool, checked: bool = False) -> None:
        """切换多选态：显示复选框、藏掉「···」，整行点击变成勾选/取消。"""
        self._multi = on
        self.chk.blockSignals(True)          # 重建列表时别把已选集合洗掉
        self.chk.setChecked(checked)
        self.chk.blockSignals(False)
        self.chk.setVisible(on)
        self.more_btn.setVisible(not on)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self._multi:
            self.chk.toggle()
            return
        self.clicked.emit(self.sid)

    def contextMenuEvent(self, e) -> None:  # noqa: N802
        if self._multi:
            return       # 多选模式下右键菜单只会碍事
        self.menu_requested.emit(self.sid, self)


class OtherModelRow(QWidget):
    """别的插件装的模型（如文档识别的 xiaomi-ocr-0）：只读展示。"""

    hovered = Signal(str)
    left = Signal(str)

    def __init__(self, name: str, size_mb: int) -> None:
        super().__init__()
        self.name = name
        self.size_mb = size_mb
        self.setObjectName("modelRow")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(6)
        lab = QLabel(f"   {name}")
        lab.setObjectName("rowNameDim")
        lay.addWidget(lab, 1)
        vram = QLabel(f"{size_mb / 1024:.1f}GB")
        vram.setObjectName("rowVram")
        lay.addWidget(vram)

    def enterEvent(self, _e) -> None:  # noqa: N802
        self.hovered.emit(self.name)

    def leaveEvent(self, _e) -> None:  # noqa: N802
        self.left.emit(self.name)


class ModelInfoCard(QFrame):
    """模型介绍气泡：鼠标停在哪一行，就在面板旁边浮出来。

    为什么拆出来：原来是面板里固定的一栏，把面板撑到 580 宽，窗口一窄就顶到
    最左边。拆成独立气泡后面板只管列表（更窄、更像下拉），介绍按需出现。

    两个要点：
    - 它是**对话框**的子控件（不是面板的子控件），否则画不到面板外面；
    - 气泡里有「上下文」下拉和「安装 / 卸载」按钮，所以鼠标从行移到气泡上时
      不能立刻消失 —— 用 200ms 的延迟隐藏 + 进入气泡即取消。
    """

    WIDTH = 258
    GAP = 8

    def __init__(self, panel: "ModelPanel") -> None:
        super().__init__(panel.dlg)
        self.panel = panel
        self.setObjectName("detailBox")
        # 子控件要靠这个属性才会用 QSS 画自身背景
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(self.WIDTH)
        self._mid = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(13, 12, 13, 12)
        lay.setSpacing(7)

        self.d_name = QLabel("—")
        self.d_name.setObjectName("dName")
        self.d_name.setWordWrap(True)
        lay.addWidget(self.d_name)

        self.d_state = QLabel("—")
        self.d_state.setObjectName("dStateMiss")
        lay.addWidget(self.d_state)

        self.d_desc = QLabel("")
        self.d_desc.setObjectName("dDesc")
        self.d_desc.setWordWrap(True)
        lay.addWidget(self.d_desc)

        self.d_meta = QLabel("")
        self.d_meta.setObjectName("dMeta")
        self.d_meta.setWordWrap(True)
        lay.addWidget(self.d_meta)

        # 上下文
        ctx_row = QHBoxLayout()
        ctx_row.setSpacing(6)
        ctx_lab = QLabel("上下文")
        ctx_lab.setObjectName("dMeta")
        ctx_row.addWidget(ctx_lab)
        self.ctx_combo = QComboBox()
        self.ctx_combo.setObjectName("ctxCombo")
        for v in (2048, 4096, 8192, 16384):
            self.ctx_combo.addItem(f"{v}", v)
        self.ctx_combo.currentIndexChanged.connect(self._on_ctx_changed)
        ctx_row.addWidget(self.ctx_combo, 1)
        lay.addLayout(ctx_row)

        self.ctx_hint = QLabel("")
        self.ctx_hint.setObjectName("ctxHint")
        self.ctx_hint.setWordWrap(True)
        lay.addWidget(self.ctx_hint)

        lay.addStretch(1)

        self.act_btn = QPushButton("安装")
        self.act_btn.setObjectName("primaryBtn")
        self.act_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.act_btn.clicked.connect(self._on_action)
        lay.addWidget(self.act_btn)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(200)      # 够从列表移到气泡上
        self._hide_timer.timeout.connect(self._maybe_hide)
        # 一层柔和投影：气泡会盖在聊天区 / 侧边栏上，有影子才读得出是「浮层」
        try:
            sh = QGraphicsDropShadowEffect(self)
            sh.setBlurRadius(22)
            sh.setOffset(0, 6)
            sh.setColor(QColor(28, 34, 48, 46))
            self.setGraphicsEffect(sh)
        except Exception:
            pass
        self.hide()

    # ---------- 弹出 / 收起
    def show_for(self, mid: str, row: QWidget | None = None) -> None:
        spec = eng.MODEL_BY_ID.get(mid)
        if not spec:
            return
        self._hide_timer.stop()
        self._mid = mid
        dlg = self.panel.dlg
        installed = dlg.engine.is_installed(mid)
        current = mid == dlg.engine.model_id
        downloading = mid == self.panel._downloading_id()
        state = ("下载中" if downloading else
                 ("当前" if current else ("已下载" if installed else "未下载")))
        self.d_name.setText(spec.name)
        self.d_state.setText(state)
        self.d_state.setObjectName({"当前": "dStateCur", "已下载": "dStateOk",
                                    "下载中": "dStateDl"}.get(state, "dStateMiss"))
        st0 = self.d_state.style()
        if st0 is not None:
            st0.unpolish(self.d_state)
            st0.polish(self.d_state)
        self.d_desc.setText(spec.note or "—")
        bits = [f"{spec.size_mb / 1024:.1f}GB 权重"]
        if spec.mmproj:
            bits.append(f"+{spec.mmproj_size_mb / 1024:.1f}GB 视觉投影")
        bits.append(f"显存约 {ModelPanel.vram_hint(spec)}")
        if spec.reasoning:
            bits.append("会先思考再回答")
        self.d_meta.setText(" · ".join(bits))

        # 上下文：优先用户设置，其次模型默认
        ctx = self.panel.dlg.model_ctx(mid)
        idx = self.ctx_combo.findData(ctx)
        if idx >= 0:
            self.ctx_combo.blockSignals(True)
            self.ctx_combo.setCurrentIndex(idx)
            self.ctx_combo.blockSignals(False)
        self.ctx_combo.setEnabled(installed)

        self.update_ctx_hint()
        # 下载 / 卸载 / 取消只放这个独立侧边气泡里，不塞进模型行内联
        self.act_btn.setVisible(True)
        self.act_btn.setEnabled(True)
        if downloading:
            self.act_btn.setText("查看下载进度")
            self.act_btn.setObjectName("primaryBtn")
        elif installed and current:
            # 0.37 修复：只有一个已下载模型时也必须能卸载，不再禁用按钮。
            self.act_btn.setText("卸载当前模型")
            self.act_btn.setObjectName("dangerBtn")
        elif installed:
            self.act_btn.setText("卸载模型")
            self.act_btn.setObjectName("dangerBtn")
        else:
            self.act_btn.setText("下载并安装")
            self.act_btn.setObjectName("primaryBtn")
        st = self.act_btn.style()
        if st is not None:
            st.unpolish(self.act_btn)
            st.polish(self.act_btn)
        self._place(row)

    def show_other(self, name: str, row: QWidget | None = None) -> None:
        """只读详情：别的插件用的模型。"""
        self._hide_timer.stop()
        self._mid = ""
        owner = "文档识别" if name.startswith("xiaomi-ocr") else "其它插件"
        size_mb = next((s for n, s in self.panel._other_models() if n == name), 0)
        self.d_name.setText(name)
        self.d_state.setText("只读")
        self.d_state.setObjectName("dStateMiss")
        st0 = self.d_state.style()
        if st0 is not None:
            st0.unpolish(self.d_state)
            st0.polish(self.d_state)
        self.d_meta.setText(f"{size_mb / 1024:.1f}GB")
        self.d_desc.setText(f"由「{owner}」插件使用，在这里只读展示。卸载请到对应插件的设置里操作。")
        self.ctx_combo.setEnabled(False)
        self.ctx_hint.setText("")
        self.act_btn.setVisible(False)
        self._place(row)

    def _place(self, row: QWidget | None) -> None:
        self.adjustSize()
        cw, ch = self.width(), self.height()
        dl = self.panel.dlg
        dw, dh = dl.width(), dl.height()
        px, pw = self.panel.x(), self.panel.width()
        # 面板已在 popup_at 里为气泡腾出右侧 266px → 气泡固定贴窗口右侧（原型 right:24）
        if getattr(self.panel, "_bubble_side", "auto") == "right":
            x = max(8, dw - cw - 24)
        elif px + pw + self.GAP + cw <= dw - 8:
            x = px + pw + self.GAP
        elif px - self.GAP - cw >= 8:
            x = px - self.GAP - cw
        else:
            x = max(8, dw - cw - 8)
        if row is not None:
            y = row.mapTo(dl, row.rect().topLeft()).y() - 4
        else:
            y = self.panel.y()
        y = max(8, min(y, dh - ch - 8))
        self.move(x, y)
        self.show()
        self.raise_()

    def schedule_hide(self) -> None:
        if self.isVisible():
            self._hide_timer.start()

    def _maybe_hide(self) -> None:
        if self.underMouse():      # 鼠标还停在气泡上（正要点按钮）
            return
        # 点「上下文」会把下拉弹成独立 Popup 窗口，此时鼠标既不在气泡上、
        # 气泡也收到 leaveEvent —— 不特判的话刚点开下拉气泡就没了。
        # 两道判断：活动 Popup + 下拉列表自身可见（后者更直接、也没有时序问题）
        if QApplication.activePopupWidget() is not None:
            return
        view = self.ctx_combo.view()
        if view is not None and view.isVisible():
            return
        self.hide()

    def enterEvent(self, e) -> None:  # noqa: N802
        self._hide_timer.stop()

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._hide_timer.start()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        e.accept()          # 别冒泡给对话框（会被当成「点了面板外面」）

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        e.accept()

    # ---------- 交互
    def update_ctx_hint(self) -> None:
        """上下文选择下方的小字：能放多少字 + 对显存的影响。"""
        val = int(self.ctx_combo.currentData() or 0)
        if val <= 0:
            self.ctx_hint.setText("")
            return
        tips = {
            2048: "只够短问答，最省显存",
            4096: "日常对话够用，显存占用小",
            8192: "能放中等文档；比 4096 多吃约 0.5GB",
            16384: "长文档；8B 以上模型在 12GB 卡上会紧",
        }
        words = val * 1.5 / 1000
        self.ctx_hint.setText(f"约 {words:.0f} 千字上下文 · {tips.get(val, '')}")

    def _on_ctx_changed(self, _i: int) -> None:
        if not self._mid:
            return
        val = int(self.ctx_combo.currentData())
        self.panel.dlg.model_ctx_set(self._mid, val)
        self.update_ctx_hint()      # 就地更新提示，不重渲染（重渲染会把值读回配置）

    def _on_action(self) -> None:
        mid = self._mid
        if not mid:
            return
        dlg = self.panel.dlg
        if mid == self.panel._downloading_id():
            # 0.39.5：下载中的「取消」搬进独立下载页（那里才有暂停/取消的完整语义），
            # 这里只负责把页面叫回前台。
            self.panel.hide()
            dlg._show_download_page()
            return
        if dlg.engine.is_installed(mid):
            self.panel.hide()
            dlg.model_uninstall(mid)
        else:
            dlg._choose_model(mid)
            self.panel.hide()


class ModelPanel(QFrame):
    """模型选择面板：左边列表（已安装 / 可选下载折叠），右边悬停详情 + 操作。

    和 PopCard 一样做成**对话框的子控件**，不用 Qt.Popup 顶层窗口：
    Popup 会抢激活，一旦显示那一瞬间被判成"失活"就立刻自毁 —— 表现就是
    「点一下闪一下」。子控件没有这套窗口语义，点击外部靠事件过滤器自己关。
    """

    # 面板收起时通知外面把模型按钮的箭头翻回朝下
    closed = Signal()

    def __init__(self, dialog) -> None:
        super().__init__(dialog)
        self.dlg = dialog
        self.setObjectName("panel")
        # 子控件要靠这个属性才会用 QSS 画自身背景
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._rows: dict[str, ModelRow] = {}
        self._detail_id: str = ""
        self._optional_open = False
        # 本次打开面板期间用户是否手动收/开过「可选模型下载」：
        # 没有它的话「一个模型都没装就自动展开」会和「用户点收起」打架 ——
        # 收起会触发 _rebuild，_rebuild 又把它展开，按钮看起来点不动。
        self._optional_touched = False
        self._anchor = None          # 打开它的那颗按钮，点它不关面板（交给 toggle）
        self._bubble_side = "auto"   # "right" = 面板已左移，气泡固定贴窗口右侧

        # 竖排：标题行固定，下面列表可滚 —— 模型装多了或展开「可选下载」时滚动，
        # 不会像以前那样把行挤扁、文字被裁成半截
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        head_row = QHBoxLayout()
        head_row.setSpacing(8)
        head = QLabel("选择模型")
        head.setObjectName("panelTitle")
        head_row.addWidget(head)
        head_row.addStretch(1)
        self.open_btn = QPushButton("文件夹")
        self.open_btn.setObjectName("ghostBtn")
        self.open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_btn.clicked.connect(self._open_folder)
        head_row.addWidget(self.open_btn)
        outer.addLayout(head_row)
        self._head_row = head_row

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        self.scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            # 只把这一处滚动条收细调淡：全局那份 8px 太抢眼
            "QScrollBar:vertical { background: transparent; width: 5px; margin: 2px 0; }"
            "QScrollBar::handle:vertical {"
            " background: rgba(150,158,172,0.28); border-radius: 2px; min-height: 22px; }"
            "QScrollBar::handle:vertical:hover { background: rgba(150,158,172,0.5); }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {"
            " background: transparent; }")
        holder = QWidget()
        holder.setObjectName("panelList")
        holder.setStyleSheet("QWidget#panelList { background: transparent; }")
        self.list_box = QVBoxLayout(holder)
        self.list_box.setContentsMargins(0, 0, 0, 0)
        self.list_box.setSpacing(2)
        self.scroll.setWidget(holder)
        outer.addWidget(self.scroll, 1)

        # 介绍不放在面板里了：改成独立气泡，鼠标停在哪一行就浮在面板旁边
        self.info = ModelInfoCard(self)
        self.resize(320, 300)
        self._rebuild()

    # ---------- 工具
    @staticmethod
    def vram_hint(spec) -> str:
        """粗略显存估计：权重 + 视觉投影 + 一点 KV cache 余量。"""
        mb = spec.size_mb + spec.mmproj_size_mb + 800
        return f"~{mb / 1024:.1f}GB"

    def _downloading_id(self) -> str:
        """正在下载的模型 id；没在下载（或已取消）就返回空串。"""
        sw = getattr(self.dlg, "_switcher", None)
        if (sw is not None and sw.isRunning()
                and getattr(sw, "_dl", False)
                and not getattr(sw, "_cancelled", False)):
            return str(getattr(sw, "_mid", "") or "")
        return ""

    def _rebuild(self) -> None:
        while self.list_box.count():
            it = self.list_box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        self._rows.clear()

        eng_ = self.dlg.engine
        installed = [m for m in eng.MODELS if eng_.is_installed(m.id)]
        missing = [m for m in eng.MODELS if not eng_.is_installed(m.id)]
        dl_id = self._downloading_id()

        # 一个模型都没下载时默认展开「可选模型下载」：否则整个面板只有
        # 「已下载（0）」+ 一个折叠标题，用户不知道下一步该点哪里（重置后首次进来就这样）。
        if not installed and not self._optional_touched:
            self._optional_open = True

        # 数据驱动分组：已下载 / 可选模型下载（N），每行都带显式状态徽章
        sec = QLabel(f"已下载（{len(installed)}）")
        sec.setObjectName("panelSection")
        self.list_box.addWidget(sec)
        for spec in installed:
            cur = spec.id == eng_.model_id
            self._add_row(spec, True, "当前" if cur else "已下载")

        if missing:
            sign = "−" if self._optional_open else "+"
            toggle = QPushButton(f"{sign} 可选模型下载（{len(missing)}）")
            toggle.setObjectName("foldBtn")
            toggle.setCursor(Qt.CursorShape.PointingHandCursor)
            toggle.setCheckable(True)
            toggle.setChecked(self._optional_open)
            toggle.clicked.connect(lambda v: self._toggle_optional(v, toggle))
            self.list_box.addWidget(toggle)
            # 正在下载的模型即使折叠也要露出来，否则看不到进度状态
            if self._optional_open or dl_id:
                for spec in missing:
                    is_dl = spec.id == dl_id
                    self._add_row(spec, False, "下载中" if is_dl else "未下载", is_dl)

        others = self._other_models()
        if others:
            sec2 = QLabel("其他插件的模型")
            sec2.setObjectName("panelSection")
            self.list_box.addSpacing(6)
            self.list_box.addWidget(sec2)
            for name, size_mb in others:
                row = OtherModelRow(name, size_mb)
                row.hovered.connect(self._show_other)
                row.left.connect(lambda _n: self.info.schedule_hide())
                self.list_box.addWidget(row)

        # 末尾放一根弹簧：面板高度有下限（300），模型少的时候（比如只有 1 个已下载）
        # 多余空间会被 QVBoxLayout 平分给每一行 —— 行被撑成一坨、文字浮在中间，
        # 看起来像 UI 崩了。有弹簧就把空白全收到下面。
        self.list_box.addStretch(1)

        # 介绍气泡是「悬停才出」，这里不再自动弹（否则一打开就挡住列表）
        self._refit()       # 行数变了（展开/收起可选下载）→ 高度跟着调

    def _fit_height(self, dh: int) -> int:
        """按内容算面板高度，但不超过对话框；超出的部分交给滚动区。"""
        holder = self.scroll.widget()
        content = 200
        if holder is not None:
            # 先让刚加/删的行参与布局，否则读到的是上一轮的 sizeHint，
            # 展开「可选下载」时高度就不会跟着长（内容被裁成半行）
            self.list_box.activate()
            holder.adjustSize()
            content = holder.sizeHint().height()
        chrome = 12 + self._head_row.sizeHint().height() + 8 + 12
        # 下限 300：默认留一点空白，面板看起来更从容（用户要求）
        return max(300, min(chrome + content, dh - 16))

    def _refit(self) -> None:
        """重建列表后重新贴合高度。

        必须延到下一轮事件循环：`_rebuild` 里旧行只是 `deleteLater()`，此刻它们
        还挂在控件树上，尺寸读到的是上一轮的，直接调会得出「不用变」而跳过。
        """
        if not self.isVisible():
            return
        QTimer.singleShot(0, self._apply_fit)

    def _apply_fit(self) -> None:
        """真正改尺寸。面板底边贴着输入框，所以长高要往上长。"""
        if not self.isVisible():
            return
        h = self._fit_height(self.dlg.height())
        if h == self.height():
            return
        bottom = self.y() + self.height()
        self.resize(self.width(), h)
        self.move(self.x(), max(8, bottom - h))

    def _other_models(self) -> list[tuple[str, int]]:
        """models/ 下不属于本插件的模型目录（文档识别的 xiaomi-ocr-0 等）。"""
        known = {m.id for m in eng.MODELS}
        out: list[tuple[str, int]] = []
        root = eng.models_root()
        if not root.is_dir():
            return out
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name in known:
                continue
            try:
                size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            except Exception:
                size = 0
            if size > 0:
                out.append((d.name, size // 1048576))
        return out

    def _show_other(self, name: str) -> None:
        """只读详情：别的插件用的模型（也走同一个气泡）。"""
        self._detail_id = ""
        sender = self.sender()
        row = sender if isinstance(sender, QWidget) else None
        self.info.show_other(name, row)

    def _add_row(self, spec, installed: bool, state: str = "",
                 downloading: bool = False) -> None:
        row = ModelRow(spec, installed, spec.id == self.dlg.engine.model_id,
                       state, downloading)
        row.hovered.connect(self._show_detail)
        row.left.connect(lambda _mid: self.info.schedule_hide())   # 移开后延迟收气泡
        row.clicked.connect(self._on_row_clicked)
        self.list_box.addWidget(row)
        self._rows[spec.id] = row

    def _open_folder(self) -> None:
        self.hide()
        self.dlg._open_models_dir()

    def _toggle_optional(self, checked: bool, btn: QPushButton) -> None:
        # 不再往 btn 上写字：重建会把它销毁，新按钮会按 _optional_open 自己起符号
        self._optional_open = checked
        self._optional_touched = True        # 用户自己收/开过，别再自动展开
        self._rebuild()

    # ---------- 详情（实际内容都在独立气泡里）
    def _show_detail(self, mid: str) -> None:
        self._detail_id = mid
        self.info.show_for(mid, self._rows.get(mid))

    def _on_row_clicked(self, mid: str) -> None:
        if self.dlg.engine.is_installed(mid):
            self.hide()
            self.dlg._choose_model(mid)
        else:
            self._show_detail(mid)      # 未安装：不切换，只展示详情（右侧可安装）

    def popup_at(self, anchor) -> None:
        self._anchor = anchor
        self._rebuild()
        self.adjustSize()
        dw, dh = self.dlg.width(), self.dlg.height()
        # 介绍拆成独立气泡后，面板只剩列表，宽度按列表自然宽度来（约 340），
        # 这样它只占窗口一小块，右边缘仍对齐模型按钮。
        avail = max(240, dw - 16)
        w = min(320, avail)
        w = max(w, min(self.minimumSizeHint().width(), avail))
        h = self._fit_height(dh)
        self.resize(w, h)
        w, h = self.width(), self.height()   # 布局可能把宽度抬高，以实际为准
        # 位置一律用对话框内部坐标（不再是屏幕坐标）
        p = anchor.mapTo(self.dlg, anchor.rect().topLeft())
        # 默认贴着按钮右边缘；原型要求气泡固定右侧，面板整体左移腾出 258+8。
        # 左移幅度 >120px 会让下拉离按钮太远（踩过），那种情况退回「气泡自己找空位」。
        x = p.x() + anchor.width() - w
        reserve = ModelInfoCard.WIDTH + ModelInfoCard.GAP + 8
        x_pref = dw - reserve - w - 8
        self._bubble_side = "right" if 0 <= x - x_pref <= 120 else "auto"
        if self._bubble_side == "right":
            x = max(8, x_pref)

        y = p.y() - h - 6                       # 默认放按钮上方
        if y < 8:                               # 上方放不下 → 放下面
            y = p.y() + anchor.height() + 6
        x = max(8, min(x, dw - w - 8))
        y = max(8, min(y, dh - h - 8))
        self.move(x, y)
        self.show()
        self.raise_()
        QApplication.instance().installEventFilter(self)

    def hide(self) -> None:  # noqa: N802
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        # 面板收起时介绍气泡一起收，别让它在窗口上孤零零地留着
        info = getattr(self, "info", None)
        if info is not None:
            info._hide_timer.stop()
            info.hide()
        # 这次打开期间的「用户已介入」不作数了：下次打开还是按
        # 「一个模型都没装就自动展开」来
        self._optional_touched = False
        super().hide()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        # 必须吃掉：QFrame 默认「不处理」鼠标按下，事件会冒泡到对话框，
        # 被下面那个 eventFilter 判成「点了面板外面」→ 面板自己关掉。
        e.accept()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        e.accept()          # 同上，别让点击冒泡出去

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """点面板外面就收起（QFrame 子控件没有 Popup 那套自动关闭）。"""
        if event.type() == QEvent.Type.MouseButtonPress and self.isVisible():
            w = obj if isinstance(obj, QWidget) else None
            if w is not None and w is not self and not self.isAncestorOf(w):
                # 点的是那颗开关按钮时不要关：交给它的 clicked 去 toggle。
                # 否则会变成「按下关掉 → 松开又打开」，正是那个闪一下。
                a = getattr(self, "_anchor", None)
                if a is not None and (w is a or a.isAncestorOf(w)):
                    return super().eventFilter(obj, event)
                # 点介绍气泡里的「卸载 / 上下文」也不能关面板（气泡是兄弟控件，
                # 不在面板的子树里，不特判就会被当成「点了外面」）
                info = getattr(self, "info", None)
                if info is not None and (w is info or info.isAncestorOf(w)):
                    return super().eventFilter(obj, event)
                self.hide()
        return super().eventFilter(obj, event)

    def hideEvent(self, event) -> None:  # noqa: N802
        """任何方式收起时都通知外面把箭头翻回去。"""
        super().hideEvent(event)
        # 主窗口正在关闭时不要再回调：那时 model_btn 可能已经销毁，
        # 槽里碰它就是 RuntimeError（虽然不会崩，但控制台会喷一屏）
        if not self.dlg.isVisible():
            return
        self.closed.emit()


class AttachChip(QFrame):
    """输入框上方的附件小卡片：文件名 + 大小 + 删除。

    ``key`` 是回调时回传的标识，默认等于 ``name``。图片附件要单独传：
    它的显示名是「图 · xxx」（带类型前缀），而列表里存的是原始名 ——
    用显示名去匹配永远匹配不上，点了 × 没反应（0.3.3 修的就是这个）。
    """

    def __init__(self, name: str, size_txt: str, on_remove,
                 key: str | None = None) -> None:
        super().__init__()
        self.setObjectName("attachChip")
        _key = name if key is None else key
        lay = QHBoxLayout(self)
        lay.setContentsMargins(9, 4, 5, 4)
        lay.setSpacing(6)
        lab = QLabel(f"{name}  ·  {size_txt}")
        lab.setObjectName("attachName")
        lay.addWidget(lab)
        btn = QPushButton("×")
        btn.setObjectName("attachDel")
        btn.setFixedSize(16, 16)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda _checked=False, k=_key: on_remove(k))
        lay.addWidget(btn)


class SuggestChip(QPushButton):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setObjectName("chip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # 不吃键盘焦点：QSS 里没有 :focus 规则，Qt 会画系统焦点框，
        # 表现为「第一个建议按钮带个蓝框、其它没有」，一排看着参差不齐。
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)


# ────────────────────────── 后台线程 ──────────────────────────
class PrepareWorker(QThread):
    progress = Signal(str, object)
    ready = Signal()
    failed = Signal(str)
    model_missing = Signal()          # 模型没下载：安静提示，不算故障

    def __init__(self, engine: eng.Engine) -> None:
        super().__init__()
        self._engine = engine
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def _report(self, doc: str, pct) -> None:
        if self._cancelled:
            raise eng.Cancelled()
        self.progress.emit(doc, pct)

    def run(self) -> None:
        try:
            self._engine.start(self._report)
            self.ready.emit()
        except eng.Cancelled:
            pass
        except eng.ModelNotInstalled:
            self.model_missing.emit()
        except eng.EngineError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"模型启动失败（{type(e).__name__}）：{e}")


class SwitchWorker(QThread):
    """切换模型：需要时先下载，然后启动模型。

    0.39.5：下载阶段可暂停 / 恢复（断点续传由 downloads 层用 Range 完成），
    并把「阶段 / 真实字节」单独发出来给独立下载页用。
    """

    progress = Signal(str, object)
    stage = Signal(str)               # download / load
    # 字节数用 object 传 Python int：`int` 在 Qt 里是 32 位有符号，
    # Qwen3-VL-8B 的 5GB 会超 2^31 → libshiboken 每帧刷 Overflow 警告且事件丢失。
    nbytes = Signal(str, object, object)   # 文件名, 已下载字节, 总字节（0 = 未知）
    ready = Signal(str)
    failed = Signal(str)
    cancelled = Signal()              # 用户取消并已真正停下来

    def __init__(self, engine: eng.Engine, model_id: str, need_download: bool) -> None:
        super().__init__()
        self._engine = engine
        self._mid = model_id
        self._dl = need_download
        self._cancelled = False
        self._paused = False
        self._stage = "download"      # 页面被关掉又打开时用它还原阶段
        self._last_bytes_at = 0.0

    def cancel(self) -> None:
        self._cancelled = True
        self._paused = False          # 暂停中取消：先解除暂停，让循环跑到取消判断

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    def _report(self, doc, pct) -> None:
        if self._cancelled:
            raise eng.Cancelled()
        self.progress.emit(doc, pct)

    def _on_bytes(self, name: str, got: int, total: int) -> None:
        # 256KB 一块、几 MB/s 时每秒能回调几十次；UI 只按时间戳节流到 ~8 次/秒
        now = time.monotonic()
        if now - self._last_bytes_at < 0.12:
            return
        self._last_bytes_at = now
        self.nbytes.emit(name, int(got), int(total))

    def run(self) -> None:
        try:
            if self._dl:
                spec = eng.MODEL_BY_ID[self._mid]
                self._stage = "download"
                self.stage.emit("download")
                eng.download_model(spec, self._report, lambda: self._cancelled,
                                   paused=lambda: self._paused,
                                   on_bytes=self._on_bytes)
            # 0.39.1：不再 stop（会误杀另一插件正在用的模型），直接切 model_id 后 ensure
            self._stage = "load"
            self.stage.emit("load")
            self._engine.model_id = self._mid
            self._engine.start(self._report)
            self.ready.emit(self._mid)
        except eng.Cancelled:
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


def _gb_text(n: float) -> str:
    """字节 → 人类可读（二进制口径，和模型清单里的 GB 保持一致）。"""
    try:
        n = float(n)
    except Exception:
        return "—"
    if n >= (1 << 30):
        return f"{n / (1 << 30):.1f} GB"
    if n >= (1 << 20):
        return f"{n / (1 << 20):.0f} MB"
    return f"{max(0.0, n) / 1024:.0f} KB"


def _eta_text(sec: float) -> str:
    if sec is None or sec <= 0 or sec > 86400 * 3:
        return ""
    s = int(sec)
    if s < 60:
        return f"剩余 {max(1, s)} 秒"
    if s < 3600:
        return f"剩余 {s // 60} 分"
    return f"剩余 {s // 3600} 小时 {s % 3600 // 60} 分"


class ModelDownloadDialog(QDialog):
    """模型下载独立页（0.39.5）。

    以前下载进度只挤在底部状态栏那根 2px 细线里：看不出速度、估不出还要多久，
    中途想停只能按对话区那颗「停止」。这里给它一个真正的页面 —— 大进度条 +
    百分比 + 已下载/总量 + 速度 + 剩余时间，以及「暂停 / 继续 / 取消 / 后台运行」。

    无边框 + 自绘圆角阴影（照抄主窗口的画法），非模态，可以退到后台继续下。
    """

    PAD = 14                 # 四周阴影留白，真正的卡片画在内侧
    WIDTH = 476

    background = Signal()          # 退到后台（下载继续）
    retry_requested = Signal(str)  # 点「重试」
    dismissed = Signal()           # 页面已关闭

    def __init__(self, parent, mid: str, need_download: bool = True) -> None:
        super().__init__(parent)
        self.mid = mid
        self.spec = eng.MODEL_BY_ID.get(mid)
        self._worker: SwitchWorker | None = None
        self._state = "download" if need_download else "load"
        self._need_download = bool(need_download)   # 取消时只有下载路径才清文件
        self._paused = False
        self._speed = 0.0
        self._t_last = 0.0
        self._cur_last = 0.0
        self._total_all = 0
        if self.spec is not None:
            self._total_all = (self.spec.size_mb
                               + (self.spec.mmproj_size_mb if self.spec.mmproj else 0)
                               ) * 1048576

        self.setWindowTitle("下载模型")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        # 圆角外要透明，所以窗口本身不能铺底色（白卡在 paintEvent 里画）
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(self.WIDTH)

        root = QVBoxLayout(self)
        m = self.PAD
        root.setContentsMargins(m + 22, m + 18, m + 22, m + 18)
        root.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(10)
        icon = QLabel("↓")
        icon.setObjectName("dlIcon")
        icon.setFixedSize(30, 30)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.addWidget(icon)
        self.title = QLabel(self._title_text())
        self.title.setObjectName("dlTitle")
        head.addWidget(self.title, 1)
        self.close_btn = QPushButton("×")
        self.close_btn.setObjectName("dlClose")
        self.close_btn.setFixedSize(22, 22)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self._on_close_clicked)
        head.addWidget(self.close_btn)
        root.addLayout(head)

        root.addSpacing(12)
        self.sub = QLabel(self._sub_text())
        self.sub.setObjectName("dlSub")
        self.sub.setWordWrap(True)
        root.addWidget(self.sub)

        root.addSpacing(16)
        self.pct = QLabel("0%")
        self.pct.setObjectName("dlPct")
        root.addWidget(self.pct)

        root.addSpacing(6)
        self.bar = QProgressBar()
        self.bar.setObjectName("dlBar")
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setFixedHeight(8)
        self.bar.setTextVisible(False)
        root.addWidget(self.bar)

        root.addSpacing(9)
        meta = QHBoxLayout()
        meta.setSpacing(8)
        self.left_lab = QLabel("")
        self.left_lab.setObjectName("dlMeta")
        meta.addWidget(self.left_lab)
        meta.addStretch(1)
        self.right_lab = QLabel("")
        self.right_lab.setObjectName("dlMeta")
        meta.addWidget(self.right_lab)
        root.addLayout(meta)

        root.addSpacing(18)
        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.main_btn = QPushButton("暂停")
        self.main_btn.setObjectName("primaryBtn")
        self.main_btn.setMinimumHeight(34)
        self.main_btn.setMinimumWidth(98)
        self.main_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.main_btn.clicked.connect(self._on_main_clicked)
        btns.addWidget(self.main_btn)
        btns.addStretch(1)
        self.bg_btn = QPushButton("后台运行")
        self.bg_btn.setObjectName("ghostBtn")
        self.bg_btn.setMinimumHeight(34)
        self.bg_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bg_btn.clicked.connect(self._to_background)
        btns.addWidget(self.bg_btn)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setObjectName("dangerBtn")
        self.cancel_btn.setMinimumHeight(34)
        self.cancel_btn.setMinimumWidth(76)
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self._on_cancel)
        btns.addWidget(self.cancel_btn)
        root.addLayout(btns)

        self.setStyleSheet(QSS + """
/* 透明是为了让圆角外的区域露出阴影/壁纸；字体必须再写一次 ——
   这条 QDialog 规则覆盖了 QSS 里那条（同选择器），不带上字体就会落到
   MS Sans Serif，中文全变成方块。 */
QDialog {
    background: transparent;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
}
QLabel#dlIcon {
    background: #E4EDFC; color: #2F6BEE; border-radius: 9px;
    font-size: 15px; font-weight: 700;
}
QLabel#dlTitle { font-size: 14px; font-weight: 600; color: #161C28; }
QLabel#dlSub { font-size: 12px; color: #5F6570; line-height: 165%; }
QLabel#dlPct { font-size: 22px; font-weight: 600; color: #2F6BEE; }
QLabel#dlMeta { font-size: 10.5px; color: #5F6570; }
QLabel#dlWarn { font-size: 12px; color: #EC5F53; }

QProgressBar#dlBar { background: #EDF2FB; border: none; border-radius: 4px; }
QProgressBar#dlBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4A85FF, stop:1 #6E6BFF);
    border-radius: 4px;
}
QProgressBar#dlBar[paused="true"]::chunk { background: #A9BEE0; }
QProgressBar#dlBar[failed="true"]::chunk { background: #EC5F53; }

QPushButton#primaryBtn { padding: 8px 18px; font-size: 12.5px; }
QPushButton#dangerBtn { padding: 8px 18px; font-size: 12.5px; }
QPushButton#ghostBtn {
    background: #FFFFFF; border: 1px solid #D8E3F5; border-radius: 8px;
    padding: 8px 16px; font-size: 12.5px; color: #161C28;
}
QPushButton#ghostBtn:hover { border-color: #2F6BEE; color: #2F6BEE; }
QPushButton#dlClose {
    background: transparent; border: none; border-radius: 11px;
    color: #8A919E; font-size: 15px;
}
QPushButton#dlClose:hover { background: #F0F3F8; color: #161C28; }
""")
        self._sync_buttons()
        self.adjustSize()

    # ---------------------------------------------------------------- 文案
    def _name(self) -> str:
        return self.spec.name if self.spec is not None else "模型"

    def _title_text(self) -> str:
        if self._state == "load":
            return f"正在加载 {self._name()}"
        return f"正在下载 {self._name()}"

    def _sub_text(self) -> str:
        if self._state == "load":
            return "模型已下载完成，正在载入内存（首次约 5~20 秒）。"
        # 总大小已经在下方的「已下载 / 总量」里，这里不再重复一遍
        return "只需下载一次，之后可完全离线使用。"

    # ---------------------------------------------------------------- 外部接线
    def attach(self, worker: SwitchWorker) -> None:
        """把下载线程接到页面上（后台重新打开时也走这里）。"""
        self._worker = worker
        worker.stage.connect(self.set_stage)
        worker.nbytes.connect(self._on_bytes)
        worker.ready.connect(self._on_ready)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_cancelled)
        # 阶段信号只在切换时发一次；页面是后台重新打开的就不会再收到，
        # 所以按 worker 当前阶段把界面摆好
        if getattr(worker, "_stage", "download") == "load":
            self.set_stage("load")
        if worker.paused:
            self._set_paused(True)

    def set_stage(self, stage: str) -> None:
        if self._state in ("done", "failed", "cancelled"):
            return
        self._state = "load" if stage == "load" else "download"
        if self.sub.styleSheet():
            self.sub.setStyleSheet("")      # 清掉失败态的红色覆盖
        self.title.setText(self._title_text())
        self.sub.setText(self._sub_text())
        if self._state == "load":
            self.bar.setValue(100)
            self.pct.setText("100%")
            self.left_lab.setText("下载完成")
            self.right_lab.setText("正在载入模型…")
        self._sync_buttons()

    # ---------------------------------------------------------------- 进度
    def _on_bytes(self, name: str, got: int, total: int) -> None:
        if self._state not in ("download", "paused"):
            return
        # 主模型 / mmproj 分别下载：用 spec 里的固定大小把两段拼成一条总进度，
        # 否则进度条会在下 mmproj 时从 100% 掉回 0。
        base = 0
        if self.spec is not None and self.spec.mmproj and name == self.spec.mmproj:
            base = self.spec.size_mb * 1048576
        cur = base + got
        now = time.monotonic()
        if self._t_last > 0 and cur > self._cur_last:
            dt = now - self._t_last
            if dt > 0.05:
                inst = (cur - self._cur_last) / dt
                self._speed = inst if self._speed <= 0 else self._speed * 0.7 + inst * 0.3
        self._t_last = now
        self._cur_last = max(cur, self._cur_last)

        total_all = self._total_all or (base + total)
        pct = 100.0 * cur / total_all if total_all > 0 else 0.0
        pct = max(0.0, min(100.0, pct))
        self.bar.setValue(int(pct))
        self.pct.setText(f"{pct:.0f}%")
        self.left_lab.setText(f"{_gb_text(cur)} / {_gb_text(total_all)}")
        if self._paused:
            self.right_lab.setText("已暂停")
        elif self._speed > 0:
            eta = _eta_text((total_all - cur) / self._speed)
            self.right_lab.setText(f"{_gb_text(self._speed)}/s · {eta}".strip(" ·"))

    # ---------------------------------------------------------------- 按钮
    def _sync_buttons(self) -> None:
        st = self._state
        self.main_btn.setVisible(True)
        self.main_btn.setEnabled(True)
        self.cancel_btn.setObjectName("dangerBtn")   # 先复位，失败态再覆盖成 ghostBtn
        self.cancel_btn.setText("取消")
        self.cancel_btn.setVisible(True)
        self.cancel_btn.setEnabled(True)
        self.bg_btn.setVisible(True)
        if st == "download":
            self.main_btn.setText("暂停")
            self.main_btn.setObjectName("primaryBtn")
        elif st == "paused":
            self.main_btn.setText("继续下载")
            self.main_btn.setObjectName("primaryBtn")
        elif st == "load":
            self.main_btn.setVisible(False)     # 加载不可中断，没必要给主按钮
            self.bg_btn.setVisible(True)
        elif st == "canceling":
            self.main_btn.setVisible(False)
            self.main_btn.setEnabled(False)
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setText("正在取消…")
        elif st == "done":
            self.main_btn.setText("开始对话")
            self.main_btn.setObjectName("primaryBtn")
            self.bg_btn.setVisible(False)
            self.cancel_btn.setVisible(False)
        elif st == "failed":
            self.main_btn.setText("重试")
            self.main_btn.setObjectName("primaryBtn")
            self.bg_btn.setVisible(False)
            self.cancel_btn.setText("关闭")
            self.cancel_btn.setObjectName("ghostBtn")
        for b in (self.main_btn, self.cancel_btn, self.bg_btn):
            s = b.style()
            if s is not None:
                s.unpolish(b)
                s.polish(b)
        self._paint_state()

    def _paint_state(self) -> None:
        self.bar.setProperty("paused", "true" if self._state == "paused" else "false")
        self.bar.setProperty("failed", "true" if self._state == "failed" else "false")
        s = self.bar.style()
        if s is not None:
            s.unpolish(self.bar)
            s.polish(self.bar)
        self.bar.update()

    def _set_paused(self, on: bool) -> None:
        if self._state not in ("download", "paused"):
            return
        self._paused = on
        self._state = "paused" if on else "download"
        if on:
            self.title.setText(f"已暂停 · {self._name()}")
            self.right_lab.setText("已暂停")
        else:
            self.title.setText(self._title_text())
            self.right_lab.setText("")      # 别把「已暂停」留在恢复之后
            # 恢复后重新采样速度：暂停那段的空白时间不能算进速率
            self._t_last = 0.0
            self._cur_last = 0.0
            self._speed = 0.0
        self._sync_buttons()

    def _on_main_clicked(self) -> None:
        if self._state in ("download", "paused"):
            w = self._worker
            if w is None:
                return
            if self._state == "paused":
                w.resume()
                self._set_paused(False)
            else:
                w.pause()
                self._set_paused(True)
        elif self._state == "done":
            p = self.parentWidget()
            if p is not None:          # 把聊天窗口提到前面，光标落在输入框
                p.raise_()
                p.activateWindow()
            self.close()
        elif self._state == "failed":
            self.retry_requested.emit(self.mid)

    def _on_cancel(self) -> None:
        if self._state == "failed":
            self.close()
            return
        w = self._worker
        self._state = "canceling"
        self.title.setText(f"正在取消 · {self._name()}")
        self.right_lab.setText("")
        self._sync_buttons()
        if w is not None:
            w.cancel()          # 会走进「继续显示后台」还是关闭，看 worker 的 cancelled 信号

    def _on_close_clicked(self) -> None:
        if self._state in ("download", "paused"):
            self._to_background()
        else:
            self.close()

    def _to_background(self) -> None:
        self.background.emit()
        self.hide()

    # ---------------------------------------------------------------- 结果
    def _on_ready(self, _mid: str) -> None:
        self._state = "done"
        self._paused = False
        self._speed = 0.0
        self.pct.setVisible(True)
        self.title.setText(f"{self._name()} 已就绪")
        self.sub.setText("已下载并加载完成，可以开始对话了。")
        self.bar.setValue(100)
        self.pct.setText("100%")
        self.left_lab.setText(f"已保存到本机 · {_gb_text(self._total_all)}"
                              if self._total_all else "已保存到本机")
        self.right_lab.setText("")
        self._sync_buttons()

    def _on_failed(self, msg: str) -> None:
        self._state = "failed"
        self._paused = False
        self.pct.setVisible(False)          # 失败时那个百分比没意义，只留进度条残值
        self.title.setText(f"{self._name()} 下载失败")
        text = str(msg or "").strip().replace("\n", " ")
        self.sub.setText((text[:180] + "…") if len(text) > 180 else (text or "网络异常，已下载的部分会保留。"))
        self.sub.setObjectName("dlWarn")
        self.sub.setStyleSheet("font-size:12px; color:#EC5F53; line-height:160%;")
        self.right_lab.setText("已下载的部分会保留")
        self._sync_buttons()

    def _on_cancelled(self) -> None:
        if self._state == "canceling":
            # 取消 = 放弃这次下载：.part 和已下好的文件都删掉，别在磁盘上留半截。
            # 「加载中」取消只是停止加载，绝不能碰已经装好的模型文件。
            if self._need_download and self.spec is not None:
                try:
                    from lylaunch.engine import downloads as _dl
                    _dl.cleanup_model_dir(self.spec)
                except Exception:
                    pass
            self.close()

    # ---------------------------------------------------------------- 窗口
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.adjustSize()
        p = self.parentWidget()
        if p is not None and p.isVisible():
            g = p.frameGeometry()
            self.move(g.x() + (g.width() - self.width()) // 2,
                      g.y() + max(40, (g.height() - self.height()) // 2 - 30))

    def paintEvent(self, _e) -> None:  # noqa: N802
        # 白卡 + 一圈柔和外投影（和主窗口同一套：多层半透明描边做羽化）
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = 14
        pad = self.PAD
        rect = self.rect()
        inner = rect.adjusted(pad, pad, -pad, -pad)
        sh = QPainterPath()
        sh.addRoundedRect(inner.x(), inner.y() + 3, inner.width(), inner.height(), r, r)
        p.setBrush(Qt.BrushStyle.NoBrush)
        layers = 2 * pad
        for i in range(1, layers + 1):
            t = 1 - i / layers
            p.setPen(QPen(QColor(40, 60, 100, int(7 * t * t)), i))
            p.drawPath(sh)
        path = QPainterPath()
        path.addRoundedRect(inner.x(), inner.y(), inner.width(), inner.height(), r, r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FFFFFF"))
        p.drawPath(path)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor("#DCE5F3"), 1))
        p.drawPath(path)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 无边框窗口：按住卡片任意空白处可拖动（走系统移动，避免自己算偏移）
        if event.button() == Qt.MouseButton.LeftButton:
            wh = self.windowHandle()
            if wh is not None:
                wh.startSystemMove()
                return
        super().mousePressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.dismissed.emit()
        super().closeEvent(event)


class ChatWorker(QThread):
    delta = Signal(str)      # 正文增量
    think = Signal(str)      # 思考过程增量（模型不思考时不会有）
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, engine: eng.Engine, messages: list[dict],
                 max_tokens: int, temperature: float,
                 images: list[str] | None = None, thinking: bool = False) -> None:
        super().__init__()
        self._engine = engine
        self._messages = messages
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._images = images or []
        self._thinking = thinking
        self._cancelled = False
        self.elapsed = 0.0
        self.reasoning = ""      # 收尾时带上完整思考过程（界面折叠展示 + 存历史）

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        buf: list[str] = []
        rbuf: list[str] = []
        t0 = time.time()
        try:
            for kind, piece in self._engine.chat_stream_events(
                    self._messages, self._max_tokens, self._temperature,
                    enable_thinking=self._thinking, images=self._images):
                if self._cancelled:
                    break
                if kind == "think":
                    rbuf.append(piece)
                    self.think.emit(piece)
                else:
                    buf.append(piece)
                    self.delta.emit(piece)
            self.elapsed = time.time() - t0
            self.reasoning = "".join(rbuf)
            self.done.emit("".join(buf))
        except Exception as e:  # noqa: BLE001
            self.elapsed = time.time() - t0
            self.reasoning = "".join(rbuf)
            self.failed.emit(f"{e}")


# ────────────────────────── 主对话框 ──────────────────────────
class SendButton(QPushButton):
    """发送按钮：圆形底由 QSS 画，三角自己画，好把三个尖角磨圆。

    为什么不用 ▲ 字符：字形本身就是锐角，QSS 也压不出圆角；这里用「粗描边 + 填充」
    的老办法 —— 给一个缩小的三角配 RoundJoin 的粗笔，形状会向外扩半个笔宽，
    顶点自然变成半径 = 笔宽一半的圆弧，尺寸、位置、圆角都能精确控住。
    """

    _W = 14.0        # 目标三角宽（含圆角外扩后的最终尺寸）
    _H = 12.0        # 目标三角高
    _ROUND = 1.6     # 顶点圆角半径：再大就显得圆钝，再小又回到尖角
    _DY = 0.8        # 视觉微调：三角重心偏上，整体往下压一点才看着居中

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)          # QSS 的渐变圆底
        if self.text():
            return
        color = QColor("#FFFFFF" if self.isEnabled() else "#C2C8D2")
        # 描边会把形状向外撑出 _ROUND，所以基础三角先缩回去
        hw = (self._W - self._ROUND * 2) / 2
        hh = (self._H - self._ROUND * 2) / 2
        cx = self.width() / 2
        cy = self.height() / 2 + self._DY
        path = QPainterPath()
        path.addPolygon(QPolygonF([
            QPointF(cx, cy - hh),                 # 尖
            QPointF(cx + hw, cy + hh),            # 右下
            QPointF(cx - hw, cy + hh),            # 左下
        ]))
        path.closeSubpath()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(color, self._ROUND * 2)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(color)
        p.drawPath(path)
        p.end()


class StopButton(QPushButton):
    """停止按钮：和 SendButton 共用同一套渐变圆底（QSS），中间画白色圆角方块。

    不用 "■" 字符：字形是直角、各字体渲染还不一致，QSS 也压不出圆角；
    drawRoundedRect 能把边长和圆角精确控住。设计要求「和发送按键
    相同的背景，只是中间的发送变成白色的圆角方形」。
    """

    _S = 11.0        # 方块边长
    _R = 2.5         # 圆角半径

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)          # QSS 的渐变圆底
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # PySide6 不隐式把 float 转 int，四个几何值必须显式 int()（见 DeepThinkButton）
        s = int(self._S)
        x = int((self.width() - s) / 2)
        y = int((self.height() - s) / 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FFFFFF"))
        p.drawRoundedRect(x, y, s, s, self._R, self._R)
        p.end()


class ModelPillBtn(QPushButton):
    """模型按钮：左边模型名，右边一枚细 chevron（∨ / ∧），跟面板开合翻转。

    为什么自己画而不是塞个字符：QSS 画不出 chevron；字体里的 ⌄ / ∨ 在微软雅黑上
    未必有字形（▸ 就缺过，会变成方框），而且线条粗细、圆头端点也都控不住。
    两条折线自己画最稳，hover 时颜色还能跟着文字一起变。
    """

    _CHEV_W = 9.0        # chevron 宽度
    _CHEV_H = 4.6        # 单侧高度：压扁一点才像设计稿里那种轻盈的 &lt;v&gt;
    _PAD_RIGHT = 11.0    # 距按钮右边缘
    _INK = "#7C8798"     # 静息色，比正文 #5F6570 略浅，不跟文字抢
    _INK_HOVER = "#2F6BEE"

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self._opened = False
        # 11-11：固定 30px 高，配合 QSS radius 15 正好是全圆角胶囊
        self.setFixedHeight(30)

    def set_open(self, opened: bool) -> None:
        """面板展开时翻成 ∧，让人一眼看出「再点一下是收起来」。"""
        opened = bool(opened)
        if opened != self._opened:
            self._opened = opened
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)        # 背景（QSS 圆角）+ 文字交给基类
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(self._INK_HOVER if self.underMouse() else self._INK), 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        half = self._CHEV_W / 2
        h = self._CHEV_H
        cx = self.width() - self._PAD_RIGHT - half
        cy = self.height() / 2
        if self._opened:                 # ∧
            pts = [QPointF(cx - half, cy + h / 2), QPointF(cx, cy - h / 2),
                   QPointF(cx + half, cy + h / 2)]
        else:                            # ∨
            pts = [QPointF(cx - half, cy - h / 2), QPointF(cx, cy + h / 2),
                   QPointF(cx + half, cy - h / 2)]
        p.drawPolyline(QPolygonF(pts))
        p.end()


class ChatDialog(QDialog):
    def __init__(self, api, settings, parent=None) -> None:
        super().__init__(parent)
        self._api = api
        self._settings = settings
        self._messages: list[dict] = []
        self._prep: PrepareWorker | None = None
        self._worker: ChatWorker | None = None
        self._busy = False
        self._ai_block: AiBlock | None = None
        self._pending: tuple[str, str, float, int] | None = None   # (正文, 思考, 耗时, tokens)
        self._switcher: SwitchWorker | None = None
        self._dl_dlg: "ModelDownloadDialog | None" = None      # 模型下载独立页
        self._pending_restore = None                          # 待重放的历史会话
        self._manual_stopped = False                          # 用户手动停了引擎（不再自动拉起）
        self._pending_images: list[str] | None = None         # 引擎自动拉起期间排队待发的图片
        self._attachments: list[tuple[str, str, int]] = []   # 文本附件：(文件名, 内容, 字节数)
        # 依赖布局结果的同步（输入卡宽度 / 渐隐条 / 进度线）：必须在布局跑完后
        # 才能算，构造期 resizeEvent 里 viewport 还是 0 尺寸（11-11 首开 bug）
        self._layout_sync_pending = False
        self._layout_sync_retry = 0
        self._images: list[tuple[str, str]] = []             # 图片附件：(文件名, base64)
        self._send_notice = ""                                # 本次发送的非阻断提示（看不了图等）
        # 生成中请求所属会话：切会话后所有旧回调都要丢弃，防止 A 的回答写进 B
        self._ctx_session_id: str | None = None
        self._multi: bool = False             # 侧栏多选模式
        self._checked: set[str] = set()       # 已勾选的会话 id
        self._multi_toggle_btn = None         # 第一个分组标题行上的「多选/完成」按钮
        # 后台生成：id(worker) → (原会话 id, worker)。**必须持有 worker 引用**，
        # 否则 self._worker 置 None 后 QThread 会被 GC，运行时崩溃。
        self._bg_runs: dict[int, tuple[str, object]] = {}
        self._think_overflow = False          # 本轮思考超长、已中断
        self._no_think_retry = False          # 本轮提问是否已经关思考重试过一次
        self._pending_retry = False           # 等旧线程 finished 后再重试

        self.setWindowTitle("本地 AI 对话")
        self.setObjectName("lylaunchChatDialog")   # 跨模块实例也能识别
        self.setAcceptDrops(True)
        # 0.36 11-11：做成普通应用窗口 —— 系统标题栏带最小化 / 最大化，
        # 有独立任务栏项（能真最小化到任务栏），右下角可拖着缩放。
        # 不用 Qt.Dialog 的默认 flags（那样只有关闭按钮、最小化不进任务栏）。
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setSizeGripEnabled(True)   # 右下角拖拽手柄
        # 尺寸：优先恢复上次用过的；否则默认 1360×915（约 1.49），
        # 屏幕放不下时按同比例缩到可用区域的 92%
        self.setMinimumSize(760, 560)
        saved_w = int(self._cfg("win_w", 0) or 0)
        saved_h = int(self._cfg("win_h", 0) or 0)
        if saved_w >= 760 and saved_h >= 560:
            self.resize(saved_w, saved_h)
        else:
            base_w, base_h = 1360, 915
            scr = QApplication.primaryScreen()
            if scr is not None:
                avail = scr.availableGeometry()
                scale = min(1.0, avail.width() * 0.92 / base_w,
                            avail.height() * 0.92 / base_h)
                self.resize(int(base_w * scale), int(base_h * scale))
            else:
                self.resize(base_w, base_h)
        # 深度思考开关（默认关：思考要先额外生成大量 token，会明显变慢）
        self._deep_think = bool(self._cfg("deep_think", False))
        self._build_ui()
        self.setStyleSheet(QSS)
        # 完全去掉系统标题栏：自绘导航栏 + 16px 圆角（所有插件窗口同一套外壳）。
        # 本地 AI 是普通应用窗口，要保留最小化/最大化/右下角缩放。
        from lylaunch.plugins.window_chrome import install_chrome
        install_chrome(self, "本地 AI 对话",
                       show_minmax=True, resizable=True,
                       solid=True, side_margin=8)

        self.typer = Typer(self._render_typing, self._typing_idle, self)
        # 思考阶段的状态栏计时：每秒把「正在思考… Ns」刷出来，让等待可感知
        self._think_started = 0.0
        # 本轮思考已吐出的字数（用于"思考打转"保护）
        self._think_len = 0
        self._think_clock = QTimer(self)
        self._think_clock.setInterval(1000)
        self._think_clock.timeout.connect(self._tick_think)

        port = int(self._cfg("port", eng.DEFAULT_PORT) or eng.DEFAULT_PORT)
        ctx = int(self._cfg("ctx", 0) or 0)      # 0 = 交给模型自己的默认
        mid = str(self._cfg("model_id", eng.DEFAULT_MODEL_ID) or eng.DEFAULT_MODEL_ID)
        # 配置里记的模型可能已经被删掉（用户在文件夹里手动清理过）→ 回退到第一个已安装的，
        # 否则启动只会提示「未下载模型」，用户还得自己去菜单里改
        installed_ids = [m.id for m in eng.MODELS if eng.is_installed(m.id)]
        if mid not in installed_ids and installed_ids:
            mid = installed_ids[0]
            try:
                self._api.settings_set("model_id", mid)
            except Exception:
                pass
        self.engine = eng.get_engine(_data_dir(), port, ctx, mid)
        self._sync_model_tag()
        # 恢复上次会话：配置里的 id 可能失效（换机/清数据/卸载重装），
        # 失效就回退到"最近一条会话"，并把这个 id 修正回去。
        self._session_id = str(self._cfg("session_id", "") or "")
        cur = ss.load(self._session_id) if self._session_id else None
        if cur is None:
            sessions = ss.list_sessions()
            cur = sessions[0] if sessions else None
        if cur is None:
            cur = ss.new_session(mid)   # 仅内存占位，不发消息就不产生空会话文件
        self._session_id = cur.id
        try:
            self._api.settings_set("session_id", self._session_id)
        except Exception:
            pass
        last = cur
        # 启动时重放上次的对话内容（不打字机）。
        # 之前只恢复了 id 不渲染 → 界面像"空对话"，用户再点它会被当作已在会话中拦截。
        # 历史重放必须等窗口真正显示后再做：
        # 淡入动画在未显示的窗口上不推进 → 透明度停在 0 → 消息"看不见"
        self._pending_restore = last
        self._refresh_sessions()          # 冷启动也要画出侧边栏（之前只有保存/切换时才刷新）
        QTimer.singleShot(80, self._restore_pending)
        self._sync_head_title()
        _cancel_release()
        if self._cfg("auto_start", True):
            QTimer.singleShot(120, self._prepare)

    def _cfg(self, key: str, default):
        try:
            v = self._api.settings_get(key, None)
            if v is not None:
                return v
        except Exception:
            pass
        return self._settings.get(key, default)

    # ---------- 侧边栏宽度
    def _init_split_width(self) -> None:
        """侧边栏固定 216px（Claude 版式）。"""
        w = 216
        total = max(760, self.width())
        self.split.setSizes([w, max(320, total - w)])

    def _save_split_w(self) -> None:
        try:
            self._api.settings_set("sidebar_w", int(self.split.sizes()[0]))
        except Exception:
            pass

    # ---------- UI
    def _build_ui(self) -> None:
        # 原型 .ai-classic 是「上：可拖双栏 / 下：横跨全宽的单一状态栏」的竖排结构
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 左右两列放进 QSplitter：中间那条线可以左右拖动改侧边栏宽度。
        # handle 宽 6px 但背景透明，静息时看到的仍是侧边栏右边那根 1px 细线，
        # 鼠标靠近才高亮，不会平白多出一条粗灰槽。
        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.setObjectName("split")
        self.split.setHandleWidth(6)
        self.split.setChildrenCollapsible(False)
        root.addWidget(self.split, 1)

        # 侧边栏：新会话 + 历史列表 + 删除
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(160)      # 与原型 ai-splitter 的拖拽范围一致
        sidebar.setMaximumWidth(420)      # ⚠️ 别改回 min=max=216：那样分隔条会失去拖拽能力
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(6, 14, 10, 12)   # 左边距 12→6：别和外壳留边叠成一大段空白
        sl.setSpacing(8)

        self.new_btn = NewChatButton("新会话")
        self.new_btn.clicked.connect(self._new_session)
        sl.addWidget(self.new_btn)

        self.sess_scroll = QScrollArea()
        self.sess_scroll.setObjectName("sessScroll")
        self.sess_scroll.setWidgetResizable(True)
        self.sess_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sess_scroll.setFrameShape(QFrame.Shape.NoFrame)
        sess_inner = QWidget()
        sess_inner.setObjectName("sessInner")
        self.sess_list = QVBoxLayout(sess_inner)
        self.sess_list.setContentsMargins(0, 0, 0, 0)
        self.sess_list.setSpacing(3)
        self.sess_list.addStretch(1)
        self.sess_scroll.setWidget(sess_inner)
        sl.addWidget(self.sess_scroll, 1)

        # 多选操作条：只在多选模式出现（不占日常空间）
        self.multi_bar = QWidget()
        mb = QHBoxLayout(self.multi_bar)
        mb.setContentsMargins(0, 2, 0, 2)
        mb.setSpacing(6)
        self.multi_count = QLabel("已选 0 条")
        self.multi_count.setObjectName("sideStatus")
        mb.addWidget(self.multi_count, 1)
        for _text, _slot in (("全选", self._select_all_sessions),
                             ("删除", self._ask_delete_selected)):
            b = QPushButton(_text)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                "QPushButton{background:#FFFFFF; border:1px solid #D8E3F5;"
                " border-radius:8px; color:#38414F; font-size:11px; padding:4px 9px;}"
                "QPushButton:hover{background:#E4EDFC; color:#2F6BEE;}")
            b.clicked.connect(_slot)
            mb.addWidget(b)
        self.multi_bar.setVisible(False)
        sl.addWidget(self.multi_bar)

        self.side_status = QLabel("")
        self.side_status.setObjectName("sideStatus")
        self.side_status.setWordWrap(True)
        sl.addWidget(self.side_status)
        self.split.addWidget(sidebar)

        # 右列：消息区 + 输入卡片
        right = QWidget()
        right.setMinimumWidth(320)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        self.split.addWidget(right)

        # 侧边栏不随窗口变宽，多出来的宽度全给内容区；宽度按上次拖动恢复
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        self._init_split_width()
        # 拖动很频繁：防抖 400ms 再落盘，别一秒写几十次配置
        self._split_save = QTimer(self)
        self._split_save.setSingleShot(True)
        self._split_save.setInterval(400)
        self._split_save.timeout.connect(self._save_split_w)
        self.split.splitterMoved.connect(lambda *_: self._split_save.start())

        # 顶栏（Claude 版式）：左=标题+副标题（当前模型），右=状态胶囊+模型开关
        head = QWidget()
        head.setObjectName("head")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(26, 10, 26, 10)
        hl.setSpacing(10)

        hleft = QWidget()
        hll = QVBoxLayout(hleft)
        hll.setContentsMargins(0, 0, 0, 0)
        hll.setSpacing(3)
        htop = QHBoxLayout()
        htop.setSpacing(6)
        htitle = QLabel("本地 AI 对话")
        htitle.setObjectName("headTitle")
        htop.addWidget(htitle)
        # 「本地 Qwen」徽章已撤：模型名由副标题「当前模型：xxx」表达，徽章是重复信息
        htop.addStretch(1)
        hll.addLayout(htop)
        self.head_sub = QLabel("")
        self.head_sub.setObjectName("headSub")
        hll.addWidget(self.head_sub)
        hl.addWidget(hleft, 1)

        # 引擎状态胶囊（Claude 档：描边浅胶囊，圆点 + 文本）
        eng = QWidget()
        eng.setObjectName("engPill")
        engl = QHBoxLayout(eng)
        engl.setContentsMargins(9, 4, 10, 4)
        engl.setSpacing(6)
        self.dot = QLabel()
        self.dot.setObjectName("dotIdle")
        self.dot.setFixedSize(7, 7)
        engl.addWidget(self.dot)
        self.eng_text = QLabel("正在准备模型…")
        self.eng_text.setObjectName("engText")
        engl.addWidget(self.eng_text)
        hl.addWidget(eng)

        # 引擎开关：llama-server 常驻会占数 GB 内存，用户要能自己停/起
        self.power_btn = QPushButton("启动模型")
        self.power_btn.setObjectName("powerBtn")
        self.power_btn.setMinimumWidth(96)
        self.power_btn.setFixedHeight(32)     # 32 高 + radius 16 = 真胶囊（同翻译插件）
        self.power_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.power_btn.clicked.connect(self._toggle_engine)
        hl.addWidget(self.power_btn)

        self.model_btn = ModelPillBtn()
        self.model_btn.setObjectName("modelBtn")
        self.model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.model_btn.clicked.connect(self._show_model_menu)
        # 原型 .ai-classic-head 横跨整个窗口（在 .plug-split 之前），侧栏在它下方，
        # 不是只在右列。插到 root 最前，顺序才是「头部 / 双栏 / 状态栏」。
        root.insertWidget(0, head)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("scroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.body = QWidget()
        self.body.setObjectName("body")
        self.body.setMaximumWidth(CONTENT_MAX_W)
        self.msg = QVBoxLayout(self.body)
        # 原型 Claude 档 .ai-thread：padding 34px 0 22px，消息列与输入卡同宽 720 对齐
        self.msg.setContentsMargins(0, 34, 0, 22)
        self.msg.setSpacing(36)
        self._spacer = QWidget()
        self._spacer.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
        self._spacer.setVisible(False)
        self.msg.addWidget(self._spacer)
        self.scroll.setWidget(self.body)
        # 底部渐隐：对话滚到底时柔和淡出，不再被输入卡硬生生截断
        self._fade = FadeOverlay(self.scroll.viewport())
        self._fade.raise_()
        rv.addWidget(self.scroll, 1)

        # 空状态：11-11 优化层次 —— 图标放大并加细描边、标题加大、
        # 图标/标题与副标题/建议之间分别留白，chip 改淡底胶囊（QSS #chip）。
        self.empty = QWidget()
        ev = QVBoxLayout(self.empty)
        ev.setContentsMargins(0, 0, 0, 0)
        ev.setSpacing(10)
        ev.addStretch(1)
        # 52×52 圆角 18 方块（--plugin-bg 底 + 1px --border 描边）+ 26px
        # sparkle 线性图标。旧版 52px 灰色渐变圆球视觉过于朴素，勿回退。
        logo = QLabel()
        logo.setFixedSize(64, 64)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # 白底卡片 + 产品 logo（和托盘 / 任务栏同一套几何，设计说明：
        # 原来那块浅蓝底 + sparkle 线性图标是"通用 AI 符号"，认不出是 LyLaunch）。
        # 只留白底、**不要描边**：白底在浅色页面上本来就看不见，加一圈 1px 边框
        # 反而让 logo 像贴在一个小方框里（设计说明：还是有点框）。
        _logo_qss = "background:#FFFFFF; border-radius:18px;"
        logo.setStyleSheet(_logo_qss)
        # 让标签自己把图缩到 64×64：不管 pixmap 是 64 还是 160（QIcon 取图行为
        # 受 DPR / 可用尺寸影响），都不会出现"图比标签大 → 居中裁掉一圈"的问题。
        # 正方形图等比缩放，不会变形。
        logo.setScaledContents(True)
        try:
            from lylaunch.tray import _make_default_icon
            logo.setPixmap(_make_default_icon(160).pixmap(160, 160))
        except Exception:
            # 兜底：拿不到 logo 模块时退回原来的星标，别让欢迎页空一块
            logo.setScaledContents(False)
            logo.setText("✦")
            logo.setStyleSheet(_logo_qss + " color:#2F6BEE; font-size:26px;")
        except Exception:
            # 兜底：拿不到 logo 模块时退回原来的星标，别让欢迎页空一块
            logo.setText("✦")
            logo.setStyleSheet(_logo_qss + " color:#2F6BEE; font-size:26px;")
        ev.addWidget(logo, 0, Qt.AlignmentFlag.AlignHCenter)
        ev.addSpacing(10)               # 图标 → 标题
        big = QLabel("有什么可以帮你？")
        big.setObjectName("emptyTitle")
        big.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ev.addWidget(big)
        self.empty_sub = QLabel("Qwen3-4B · 不联网 · 内容不出这台机器")
        sub = self.empty_sub
        sub.setObjectName("emptySub")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 未下载时的提示较长，窄窗口会自动折行，别把整页撑出横向滚动条
        sub.setWordWrap(True)
        ev.addWidget(sub)
        ev.addSpacing(22)               # 副标题 → 建议 chip
        # 三条示例横排一行（总宽约 590px，消息列放得下）：宽度随文案、只统一高度
        chip_row = QHBoxLayout()
        chip_row.setSpacing(8)
        chip_row.addStretch(1)
        for _s in SUGGESTIONS:
            chip = SuggestChip(_s)
            chip.setMinimumHeight(32)
            chip.clicked.connect(lambda _c=False, t=_s: self._use_suggestion(t))
            chip_row.addWidget(chip)
        chip_row.addStretch(1)
        ev.addLayout(chip_row)
        ev.addStretch(1)
        self.empty.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.msg.insertWidget(0, self.empty)

        # 输入卡片：宽度由 resizeEvent 按「消息列实宽 × COMPOSER_SCALE」动态设置，
        # 两侧 stretch 保证居中。不再写死常量 —— 最大化 / 缩放窗口后固定宽度
        # 会和消息列错位，看起来像 bug（设计说明反馈）。
        wrap = QWidget()
        self._composer_wrap = wrap
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(0)

        self.card = QFrame()
        self.card.setObjectName("card")
        self.card.setProperty("focus", "false")
        # 设计说明：第一版四周均匀蓝色光晕「像发光、不自然」，改成自然投影 ——
        # 中性深灰蓝（不带蓝味）、主要向下偏移、模糊收敛，贴近原型定稿
        # box-shadow: 0 2px 12px rgba(31,41,61,.05)。Qt 的 blurRadius 约为 CSS
        # blur 的 2 倍，所以 12px → 24；alpha 取 22（.05 在浅色底上几乎看不见）。
        # 配合消息区底部渐隐，消除内容被硬切的生硬感。
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(24)
        shadow.setColor(QColor(31, 41, 61, 22))
        shadow.setOffset(0, 2)
        self.card.setGraphicsEffect(shadow)

        cl = QVBoxLayout(self.card)
        # 原型 Claude 档 .ai-composer-card：padding 12px 14px、radius 14
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(2)

        # 附件区：只显示文件卡片，不把内容塞进输入框
        self.attach_box = QWidget()
        self.attach_row = QHBoxLayout(self.attach_box)
        self.attach_row.setContentsMargins(2, 0, 0, 4)
        self.attach_row.setSpacing(6)
        self.attach_box.setVisible(False)
        cl.addWidget(self.attach_box)

        self.input = AttachmentTextEdit(self._add_pasted_image, self._attach_paths)
        self.input.setObjectName("input")
        self.input.setPlaceholderText("问点什么…")
        self.input.setFixedHeight(INPUT_MIN_H)
        self.input.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        cl.addWidget(self.input)

        tools = QHBoxLayout()
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(6)
        self.attach_btn = QPushButton("+")
        self.attach_btn.setObjectName("toolBtn")
        self.attach_btn.setFixedSize(28, 28)
        self.attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        tools.addWidget(self.attach_btn)
        self.attach_btn.clicked.connect(self._pick_file)
        # 深度思考开关：默认关；只有会产生思考过程的模型才可点
        self.deep_btn = DeepThinkButton("深度思考")
        self.deep_btn.setChecked(self._deep_think)
        self.deep_btn.clicked.connect(self._toggle_deep)
        tools.addWidget(self.deep_btn)
        tools.addWidget(self.model_btn)
        # 设计说明：三个切换类按钮全靠左、挨着「+」；发送/停止仍留在最右
        tools.addStretch(1)
        # 三角由 SendButton 自己画（能磨圆三个角），所以这里不设文字
        self.send_btn = SendButton()
        self.send_btn.setObjectName("send")
        self.send_btn.setFixedSize(34, 34)
        self.send_btn.setEnabled(False)
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        tools.addWidget(self.send_btn)

        # 11-11：暂停键换成和发送键同一套蓝色渐变圆底，中间白色圆角方块由
        # StopButton 自绘；旧版是淡粉底 + 红色 "■" 字符，和发送键不像一套
        self.stop_btn = StopButton()
        self.stop_btn.setObjectName("stop")
        self.stop_btn.setFixedSize(34, 34)
        self.stop_btn.setVisible(False)
        self.stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        tools.addWidget(self.stop_btn)
        cl.addLayout(tools)
        wl.addWidget(self.card)

        self.progress = QProgressBar(self.card)
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(2)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        # Claude 档 .ai-progress：left/right 各 14px，与输入卡 14px 内边距对齐
        self.progress.setGeometry(14, 0, self.card.width() - 28, 2)
        self.progress.raise_()

        # 两侧 stretch 让输入卡居中，宽度由 resizeEvent 设 wrap.setFixedWidth()；
        # 底部留 10px 再接状态栏
        self._wrap_row = QHBoxLayout()
        self._wrap_row.setContentsMargins(0, 0, 0, 10)
        self._wrap_row.addStretch(1)
        self._wrap_row.addWidget(wrap)
        self._wrap_row.addStretch(1)
        rv.addLayout(self._wrap_row)

        # 底部单一状态栏（原型 .ph-status）：横跨侧栏与聊天区，绿点 + 状态 + 右侧提示。
        # Claude 档是 border-top:0、10.5px，所以不要画上边框，也不要像旧版那样
        # 把它塞进 720 居中的输入卡里（那样只在右列、还被居中对齐）。
        status_bar = QWidget()
        status_bar.setObjectName("statusBar")
        status_bar.setFixedHeight(28)
        sb = QHBoxLayout(status_bar)
        sb.setContentsMargins(14, 0, 14, 8)
        sb.setSpacing(8)
        self.status_dot = QLabel()
        self.status_dot.setObjectName("dotReady")
        self.status_dot.setFixedSize(6, 6)
        sb.addWidget(self.status_dot)
        self.status = QLabel("正在准备模型…")
        self.status.setObjectName("status")
        sb.addWidget(self.status, 1)
        # 下载页被退到后台时的入口（平时不占位）：点一下把下载页叫回前台
        self.dl_btn = QPushButton("查看下载进度")
        self.dl_btn.setObjectName("copyBtn")
        self.dl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dl_btn.setVisible(False)
        self.dl_btn.clicked.connect(self._show_download_page)
        sb.addWidget(self.dl_btn)
        # 右侧不再放「内容不出这台机器」：隐私说明已并入左侧状态文案，两边都写重复。
        # 状态栏保持「绿点 + 一句话」。
        root.addWidget(status_bar)

        self.send_btn.clicked.connect(self._send)
        self.stop_btn.clicked.connect(self._stop)
        self.input.installEventFilter(self)
        self.input.textChanged.connect(self._autosize_input)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 首次显示时布局刚跑完，立刻排一次同步，不用等用户缩放窗口
        self._schedule_layout_sync()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # ⚠️ 构造期 / 尺寸变化那一瞬间，viewport 还没被布局分配尺寸，直接读
        # width()/height() 会拿到 0 或旧值 —— 首次打开时输入卡宽度算死、
        # 渐隐条被算到 y = 0-30 跑到窗口上方（11-11 实测）。统一延后到
        # 事件循环下一轮（布局已完成）再算。
        self._schedule_layout_sync()

    def _schedule_layout_sync(self) -> None:
        """防抖：连续 resize 只排一次，避免拖拽时堆一堆定时器。"""
        if getattr(self, "_layout_sync_pending", False):
            return
        self._layout_sync_pending = True
        QTimer.singleShot(0, self._sync_layout_dependent)

    def _sync_layout_dependent(self) -> None:
        """布局完成后统一重算所有依赖 viewport / card 尺寸的宽度与绝对定位。"""
        self._layout_sync_pending = False
        sc = getattr(self, "scroll", None)
        if sc is None:
            return
        vp = sc.viewport()
        vp_w, vp_h = vp.width(), vp.height()
        if vp_w <= 0 or vp_h <= 0:
            # 布局还没真完成：有限重试，别无限自循环
            if getattr(self, "_layout_sync_retry", 0) < 3:
                self._layout_sync_retry = getattr(self, "_layout_sync_retry", 0) + 1
                self._schedule_layout_sync()
            return
        self._layout_sync_retry = 0

        # ① 输入卡宽度 = 消息列实宽 × COMPOSER_SCALE。
        # 上限用 viewport 宽（右列实宽），不能用 self.width()：输入卡在右列、
        # 左边还有可拖侧栏，用整窗宽会在窄窗口撑出边界。
        wrap = getattr(self, "_composer_wrap", None)
        if wrap is not None:
            col_w = min(CONTENT_MAX_W, vp_w)
            target = min(int(col_w * COMPOSER_SCALE), vp_w)
            wrap.setFixedWidth(max(320, int(target)))

        # ② 渐隐条贴消息 viewport 底部（绝对定位，不占布局空间）
        fade = getattr(self, "_fade", None)
        if fade is not None:
            fade.setGeometry(0, vp_h - FadeOverlay.H, vp_w, FadeOverlay.H)
            fade.raise_()

        # ③ 生成进度线贴输入卡顶部内侧（隐藏时不占高度）
        pr = getattr(self, "progress", None)
        card = getattr(self, "card", None)
        if pr is not None and card is not None:
            pr.setGeometry(14, 0, card.width() - 28, 2)

    # ---------- 附件
    def _pick_file(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "选择要一起发送的文件", "",
            "文本/代码 (*.txt *.md *.py *.json *.csv *.log *.yaml *.yml *.ini *.cfg "
            "*.html *.css *.xml *.sql *.java *.go *.rs *.c *.cpp *.h *.bat *.ps1 *.sh *.toml);;"
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp);;所有文件 (*.*)")
        if path:
            self._attach_path(path)

    def _attach_path(self, path: str) -> None:
        from pathlib import Path as _P
        p = _P(path)
        if not p.is_file():
            return
        ext = p.suffix.lower()
        if ext == ".pdf":
            self._attach_pdf(p)
            return
        if ext in IMG_EXTS:
            b64 = self._encode_image(p)
            if b64:
                self._add_image(p.name, b64)
                self._notice_if_not_vision(f"图片「{p.name}」")
            return
        if ext in doc_inputs.DOC_EXTS:
            # Excel / Word：本地解析成文本或 Markdown。解析不出来也不拦 ——
            # 附一条占位说明，消息照样能发出去，由用户自己补充或换模型。
            text, label, hint = doc_inputs.parse(str(p))
            if not text:
                self._add_attachment(p.name, f"（{p.name} 本地解析失败：{hint}）",
                                     p.stat().st_size)
                self.status.setText(f"「{p.name}」没读到文字，已作为占位附件加入 · {hint}")
                return
            self._add_attachment(p.name, text, p.stat().st_size)
            self.status.setText(
                f"已附加 {label}「{p.name}」（本地解析约 {len(text)} 字）· 直接提问即可")
            return
        if ext not in TEXT_EXTS:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "不支持的文件类型",
                f"「{p.name}」不是文本/代码/文档文件。\n"
                "目前支持文本代码、图片，以及 PDF / Excel / Word 文档。")
            return
        try:
            size = p.stat().st_size
            if size > MAX_ATTACH_BYTES:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.information(
                    self, "文件太大",
                    f"「{p.name}」有 {size / 1024:.0f}KB，超过 {MAX_ATTACH_BYTES // 1024}KB 上限。\n"
                    "可以只贴需要的片段。")
                return
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "读取失败", f"{p.name}：{e}")
            return
        omitted = 0
        if len(text) > MAX_ATTACH_CHARS:
            omitted = len(text) - MAX_ATTACH_CHARS
            text = text[:MAX_ATTACH_CHARS]
        self._add_attachment(p.name, text.rstrip(), size)
        if omitted:
            self.status.setText(
                f"「{p.name}」较长，只带入前 {MAX_ATTACH_CHARS} 字（省略 {omitted} 字）· "
                f"超出部分会撑爆模型上下文")

    def _notice_if_not_vision(self, what: str) -> None:
        """当前模型看不了图时只提示、不阻断；发送阶段会自动把图片剥掉。"""
        if self.engine.spec.vision:
            return
        self.status.setText(
            f"{what}已附加 · 当前「{self.engine.spec.name}」看不了图，"
            "发送时会自动只带文字；想让模型看图请切到 Qwen3-VL-8B")

    def _attach_pdf(self, p) -> None:
        """PDF = 文字层 + 页面图，双通道，不再因「抽不出文字层」拒收。

        llama-server 只收 image_url，收不了 PDF 二进制；所谓「模型能识别 PDF」
        实际就是把页面渲染成图片再喂给视觉模型。这里用 QtPdf 做这一步：
        文字层准且省 token，页面图让模型看到图表 / 公式 / 版式，扫描件也能发。
        """
        text, _label, hint = doc_inputs.parse(str(p))
        size = p.stat().st_size
        if text:
            self._add_attachment(p.name, text, size)
        pages, note = doc_inputs.pdf_page_images(str(p))
        added = 0
        for idx, img in pages:
            b64 = self._encode_qimage(img)
            if b64:
                self._add_image(f"PDF · {p.name} · 第{idx + 1}页", b64)
                added += 1
        if not text and not added:
            # 连页面都渲染不出来也绝不拒收：附一条说明，消息照样能发出去
            self._add_attachment(p.name, f"（无法解析此 PDF：{hint}）", size)
        if added and not self.engine.spec.vision:
            self._notice_if_not_vision(f"「{p.name}」页面图 {added} 张")
            return
        bits: list[str] = []
        if text:
            bits.append(f"文字层 {len(text)} 字")
        if added:
            bits.append(f"页面图 {added} 张")
        if note:
            bits.append(note)
        self.status.setText(
            f"已附加 PDF「{p.name}」（{' · '.join(bits) if bits else '仅占位'}）· 直接提问即可")

    def _encode_image(self, p) -> str:
        """从文件读图 → base64。"""
        from PySide6.QtGui import QImage
        img = QImage(str(p))
        if img.isNull():
            self.status.setText(f"「{p.name}」不是可读的图片")
            return ""
        return self._encode_qimage(img)

    def _encode_qimage(self, img) -> str:
        """压到长边 1600 再转 base64（原图动辄上千万像素，视觉 token 会炸上下文）。"""
        import base64
        from PySide6.QtCore import QBuffer, QIODevice
        if img.isNull():
            return ""
        if max(img.width(), img.height()) > 1600:
            img = img.scaled(1600, 1600, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "JPEG", 85)
        data = bytes(buf.data())
        return base64.b64encode(data).decode()

    def _add_image(self, name: str, b64: str) -> None:
        if any(n == name for n, _b in self._images):
            self.status.setText(f"「{name}」已经在图片里了")
            return
        self._images.append((name, b64))
        self._render_attachments()
        kb = len(b64) * 3 // 4 // 1024
        self.status.setText(f"已附加图片「{name}」（约 {kb}KB，已压缩）· 直接提问即可")
        self.input.setFocus()

    def _remove_image(self, name: str) -> None:
        self._images = [i for i in self._images if i[0] != name]
        self._render_attachments()

    def _add_pasted_image(self, img) -> None:
        """Ctrl+V 粘贴的图片（截图/复制的图）直接当图片附件，不再因模型不看图就拒绝。"""
        b64 = self._encode_qimage(img)
        if not b64:
            self.status.setText("这张图片读不出来")
            return
        n = 1 + sum(1 for name, _b in self._images if name.startswith("粘贴"))
        self._add_image(f"粘贴的图片 {n}", b64)
        self._notice_if_not_vision(f"粘贴的图片 {n}")

    def _attach_paths(self, paths: list[str]) -> None:
        """批量附加（拖入多个文件时用）。"""
        for one in paths:
            self._attach_path(one)

    # ---------- 附件列表
    def _add_attachment(self, name: str, text: str, size: int) -> None:
        if any(n == name for n, _t, _s in self._attachments):
            self.status.setText(f"「{name}」已经在附件里了")
            return
        self._attachments.append((name, text, size))
        self._render_attachments()
        size_txt = f"{size} B" if size < 1024 else f"{size / 1024:.1f} KB"
        self.status.setText(f"已附加「{name}」（{size_txt}）· 直接提问即可，内容会自动带上")
        self.input.setFocus()

    def _remove_attachment(self, name: str) -> None:
        self._attachments = [a for a in self._attachments if a[0] != name]
        self._render_attachments()

    def _render_attachments(self) -> None:
        while self.attach_row.count():
            it = self.attach_row.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()

                w.deleteLater()
        for name, _text, size in self._attachments:
            size_txt = f"{size} B" if size < 1024 else f"{size / 1024:.1f} KB"
            self.attach_row.addWidget(AttachChip(name, size_txt, self._remove_attachment))
        for name, b64 in self._images:
            kb = len(b64) * 3 // 4 // 1024
            self.attach_row.addWidget(
                AttachChip(f"图 · {name}", f"{kb} KB", self._remove_image, key=name))
        self.attach_row.addStretch(1)
        self.attach_box.setVisible(bool(self._attachments or self._images))

    def _clear_attachments(self) -> None:
        self._attachments = []
        self._images = []
        self._render_attachments()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self._attach_path(url.toLocalFile())
                event.acceptProposedAction()
                return

    def _autosize_input(self) -> None:
        doc_h = int(self.input.document().size().height()) + 16
        # 下限必须与初始高度 INPUT_MIN_H 一致，否则一打字输入框反而变矮
        self.input.setFixedHeight(max(INPUT_MIN_H, min(168, doc_h)))

    def _set_card_focus(self, focused: bool) -> None:
        self.card.setProperty("focus", "true" if focused else "false")
        st = self.card.style()
        if st is not None:
            st.unpolish(self.card)
            st.polish(self.card)
        self.card.update()

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.input:
            et = event.type()
            if et == event.Type.KeyPress:
                e: QKeyEvent = event  # type: ignore[assignment]
                if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    mods = e.modifiers()
                    if mods & (Qt.KeyboardModifier.ControlModifier
                               | Qt.KeyboardModifier.ShiftModifier):
                        return False          # 交给输入框插入换行
                    self._send()              # 裸回车直接发送
                    return True
            elif et == event.Type.FocusIn:
                self._set_card_focus(True)
            elif et == event.Type.FocusOut:
                self._set_card_focus(False)
        return super().eventFilter(obj, event)

    # ---------- 消息
    def _fade_in(self, w: QWidget) -> None:
        try:
            def _clear_fx() -> None:
                # 控件可能已被 deleteLater() 销毁（切会话/清空），此时不能再碰它。
                # QTimer 以 w 为 parent 会在销毁时一起消失，这里再兜一层异常。
                try:
                    w.setGraphicsEffect(None)
                except RuntimeError:
                    pass

            eff = QGraphicsOpacityEffect(w)
            w.setGraphicsEffect(eff)
            anim = QPropertyAnimation(eff, b"opacity", w)
            anim.setDuration(220)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.finished.connect(_clear_fx)
            anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
            # 兜底：动画若因窗口未显示等原因没推进，400ms 后强制恢复可见。
            # QTimer 以 w 为 parent → w 销毁时定时器一并销毁，回调不会触发。
            t = QTimer(w)
            t.setSingleShot(True)
            t.timeout.connect(_clear_fx)
            t.start(400)
        except Exception:
            pass

    def _scroll_bottom(self) -> None:
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum()))

    def _add_user(self, text: str, cards: list[dict] | None = None) -> None:
        # 注意：不能用 isVisible() 判断——窗口 show 之前它恒为 False，
        # 启动时重放历史就会漏掉隐藏动作，空状态把消息全挤没。
        self.empty.setVisible(False)
        self._spacer.setVisible(True)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addStretch(1)
        h.addWidget(UserBubble(text, cards))
        self.msg.insertWidget(self.msg.count() - 1, row)
        self._fade_in(row)
        self._scroll_bottom()

    def _add_ai(self, on_copy, model_name: str = "", on_regen=None) -> AiBlock:
        blk = AiBlock(on_copy, model_name, on_regen)
        self.msg.insertWidget(self.msg.count() - 1, blk)
        self._fade_in(blk)
        self._scroll_bottom()
        return blk

    def _is_bg_sender(self) -> bool:
        """这个信号是不是后台（已切走的）worker 发来的。"""
        src = self.sender()
        return (src is not None and id(src) in self._bg_runs
                and src is not self._worker)

    def _on_think(self, piece: str) -> None:
        """思考增量：只喂当前这条 AI 气泡；切过会话直接丢弃，不污染新会话。

        **没开「深度思考」时不展示**：这类带思维链的模型（Qwen3.5 蒸馏版等）
        即使请求里写了 `enable_thinking: false`、模板不认时照样往外吐推理，
        展开给用户看就是一大段英文自言自语（用户反馈截图）。
        """
        if self._is_bg_sender() or self._ctx_session_id != self._session_id:
            return                      # 后台生成不喂打字机、不占当前状态
        if not self._think_clock.isActive():
            # 第一次收到思考：开始计时，让「好像卡住了」变成「在思考 N 秒」
            self._think_started = time.time()
            self._think_clock.start()
            self.status.setText("正在思考… 0s（可随时点停止）")
        self._think_len += len(piece)
        if self._deep_think and self._ai_block is not None:
            try:
                self._ai_block.append_think(piece)
            except RuntimeError:
                self._ai_block = None    # 切会话时控件已销毁
        # 思考打转保护：开了深度思考也一样要限（设计说明：不能一直思考
        # 导致不输出回答）。中断后由 _on_done 自动关思考重发一次。
        if (self._think_len >= _THINK_CHAR_LIMIT and not self._think_overflow
                and self._worker is not None and self._worker.isRunning()):
            self._think_overflow = True
            self._worker.cancel()
            self.status.setText(
                f"思考已超过 {self._think_len} 字 · 准备改为直接作答…")

    def _on_delta(self, piece: str) -> None:
        """正文增量：思考结束，把状态栏从「正在思考」切回「正在生成」。"""
        if self._is_bg_sender() or self._ctx_session_id != self._session_id:
            return
        if self._think_clock.isActive():
            self._think_clock.stop()
            self.status.setText("正在生成…")
        self.typer.feed(piece)

    def _render_typing(self, shown: str, more: bool) -> None:
        if self._ai_block is not None:
            try:
                self._ai_block.render_typing(shown, more)
            except RuntimeError:
                self._ai_block = None   # 切会话时控件已销毁，停掉后续渲染
        self._scroll_bottom()

    def _typing_idle(self) -> None:
        """打字机把队列吐完才收尾（显示用量与复制），避免结尾突然跳全。"""
        if self._pending is None:
            return
        full, reasoning, elapsed, tokens = self._pending
        self._pending = None
        mine = self._ctx_session_id == self._session_id
        self._ctx_session_id = None
        if not mine:
            # 生成途中切了会话：这条回答属于旧会话，绝不 append 到新会话的
            # _messages，也不触发保存（旧会话已在切换前保存过）。
            return
        blk, self._ai_block = self._ai_block, None
        if blk is not None:
            try:
                if full.strip():
                    blk.set_final(full, elapsed, tokens, reasoning)
                    msg: dict = {"role": "assistant", "content": full}
                    if reasoning:
                        # 思考过程随消息存进会话 JSON：重开会话也能折叠还原
                        msg["reasoning"] = reasoning
                    self._messages.append(msg)
                else:
                    blk.set_placeholder("（没有内容）")
            except RuntimeError:
                # 控件在切会话/清空时已被销毁，直接丢掉这次收尾
                self._pending = None
                return
        self.status.setText("内容不出这台机器")
        self._set_dot("dotReady")
        self._save_current()
        self._scroll_bottom()
        # 窗口开着时也要能自动释放引擎（默认 300s），否则 llama-server 一直占几 GB
        idle = int(self._cfg("idle_release", 300) or 300)
        if idle > 0 and self.engine.ready():
            _schedule_release(self.engine, idle, self._on_idle_stopped)

    def _copy_text(self, text: str) -> None:
        try:
            self._api.clipboard_write(text)
            self.status.setText("已复制到剪贴板")
        except Exception:
            self.status.setText("复制失败")

    def _clear_ui(self, show_empty: bool = True) -> None:
        """只清界面与内存里的消息（切换/新建会话时用）。

        show_empty=False 用于"切到已有会话"：避免空状态页先显示一帧再被隐藏。
        """
        # 先停异步收尾：打字机定时器 / 生成线程可能在控件销毁后才回调，
        # 那时访问 blk 会抛 "wrapped C/C++ object has been deleted"。
        try:
            self.typer.reset()
        except Exception:
            pass
        self._pending = None
        self._ai_block = None
        # 让这一刻之前所有在途请求作废：生成回调、排队中的图片、状态提示
        # 都属于「上一个会话」，切过去之后一个都不能留。
        self._ctx_session_id = None
        self._pending_images = None
        self._send_notice = ""
        self._think_clock.stop()

        for i in reversed(range(self.msg.count())):
            w = self.msg.itemAt(i).widget()
            if w is self._spacer or w is self.empty:
                continue
            if w is not None:
                w.hide()
                w.deleteLater()
        self._messages = []
        self._clear_attachments()
        self.empty.setVisible(show_empty)
        self._spacer.setVisible(not show_empty)

    def _restore_pending(self) -> None:
        """窗口显示后重放上次的对话（只做一次）。"""
        last, self._pending_restore = getattr(self, "_pending_restore", None), None
        if last is None or not last.messages:
            return
        self._messages = copy.deepcopy(last.messages)
        self._spacer.setVisible(True)
        self.empty.setVisible(False)
        mname = eng.MODEL_BY_ID.get(last.model_id, eng.MODELS[0]).name
        for m in self._messages:
            role = m.get("role")
            ui = m.get("_ui") or {}
            # 新消息用 _ui.text（只有用户真正写的话，不含附件全文），
            # 并从 _ui.cards 恢复缩略图卡片；旧会话无 _ui 时退回原 content 保持兼容。
            content = str(ui.get("text") or m.get("content") or "")
            if role == "user":
                self._add_user(content, ui.get("cards"))
            elif role == "assistant":
                blk = self._add_ai(self._copy_text, mname)
                blk.set_final(content, 0.0, 0, str(m.get("reasoning") or ""))
                blk.set_meta("已完成", "历史")
        self._scroll_bottom()

    @staticmethod
    def _fmt_date(ts: float) -> str:
        """今天只显示时间，今年显示月-日，跨年显示完整日期。"""
        try:
            lt = time.localtime(ts)
            now = time.localtime()
        except Exception:
            return ""
        if (lt.tm_year, lt.tm_yday) == (now.tm_year, now.tm_yday):
            return time.strftime("%H:%M", lt)
        if lt.tm_year == now.tm_year:
            return time.strftime("%m-%d %H:%M", lt)
        return time.strftime("%Y-%m-%d", lt)

    def _remember_session(self) -> None:
        try:
            self._api.settings_set("session_id", self._session_id)
        except Exception:
            pass

    def _clear_chat(self) -> None:
        """删掉当前会话记录并开一条新的空会话（顶栏「清空」按钮已移除，功能保留备用）。

        之前只清了内存/界面、不删文件 → 重新打开内容又回来了，等于没清。
        """
        self._dismiss_pop()
        if self._busy:
            self._stop()
        old = self._session_id
        self._clear_ui()
        removed, _freed = ss.delete(old)
        self._session_id = ""
        self._remember_session()
        self._refresh_sessions()
        self._sync_head_title()
        extra = f" · 清理了 {removed} 张图片缓存" if removed else ""
        self.status.setText(f"已删除这条会话及其全部内容{extra} · 内容不出这台机器")

    # ---------- 会话（历史）
    def _refresh_sessions(self) -> None:
        """重画侧边栏：整批更新，不逐帧重绘（避免闪白）。

        按「置顶 / 今天 / 昨天 / 7天内 / 30天内」分组，每组前面加灰色小标题。
        """
        inner = self.sess_scroll.widget()
        inner.setUpdatesEnabled(False)
        try:
            while self.sess_list.count():
                it = self.sess_list.takeAt(0)
                w = it.widget()
                if w is not None:
                    w.hide()
                    w.deleteLater()
            sessions = ss.list_sessions()

            # 分组：置顶单独一组，其余按日期分
            pinned = [s for s in sessions if s.pinned]
            others = [s for s in sessions if not s.pinned]

            groups: list[tuple[str, list]] = []
            if pinned:
                groups.append(("置顶", pinned))

            # 日期分组
            now = time.time()
            today_start = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
            yesterday_start = today_start - 86400
            week_start = today_start - 7 * 86400
            month_start = today_start - 30 * 86400

            today_items = [s for s in others if s.updated >= today_start]
            yesterday_items = [s for s in others if yesterday_start <= s.updated < today_start]
            week_items = [s for s in others if week_start <= s.updated < yesterday_start]
            month_items = [s for s in others if month_start <= s.updated < week_start]
            older_items = [s for s in others if s.updated < month_start]

            if today_items:
                groups.append(("今天", today_items))
            if yesterday_items:
                groups.append(("昨天", yesterday_items))
            if week_items:
                groups.append(("7天内", week_items))
            if month_items:
                groups.append(("30天内", month_items))
            if older_items:
                groups.append(("更早", older_items))

            self._multi_toggle_btn = None
            first_group = True
            for group_name, group_sessions in groups:
                # 分组标题。多选入口放在**第一个分组那一行**的右侧：
                # 有置顶就是「置顶」栏，没有置顶自然就是最新的「今天」栏
                # （设计说明：别占头部，跟着分组标题走）。
                sec = QLabel(group_name)
                sec.setObjectName("sessGroup")
                if first_group:
                    bar = QWidget()
                    hl = QHBoxLayout(bar)
                    hl.setContentsMargins(0, 0, 2, 0)   # 右侧留 2px，图标不贴边
                    hl.setSpacing(4)
                    hl.addWidget(sec, 1)
                    btn = QPushButton("")
                    btn.setIcon(_multi_select_icon(self._multi))
                    btn.setIconSize(QSize(20, 20))
                    btn.setFixedSize(24, 24)      # 留 2px 边，别贴着侧栏右缘被裁
                    btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    btn.setToolTip("完成多选" if self._multi else "多选会话（可批量删除）")
                    btn.setStyleSheet(
                        "QPushButton{background:transparent; border:none; padding:0;}"
                        "QPushButton:hover{background:transparent;}")
                    btn.clicked.connect(self._toggle_multi)
                    hl.addWidget(btn)
                    self._multi_toggle_btn = btn
                    self.sess_list.addWidget(bar)
                    first_group = False
                else:
                    self.sess_list.addWidget(sec)
                # 会话行
                for sess in group_sessions:
                    cur = sess.id == self._session_id
                    row = SessionRow(sess.id, self._title_of(sess, cur),
                                     self._fmt_date(sess.updated), cur)
                    row.clicked.connect(self._open_session)
                    row.delete_requested.connect(self._ask_delete_session)
                    row.rename_requested.connect(self._rename_session)
                    row.menu_requested.connect(self._show_row_menu)
                    row.checked_changed.connect(self._on_row_toggled)
                    row.set_multi(self._multi, sess.id in self._checked)
                    self.sess_list.addWidget(row)

            if not sessions:
                tip = QLabel("还没有对话\n点上面的「+ 新会话」开始")
                tip.setObjectName("sessEmpty")
                tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self.sess_list.addWidget(tip)
            self.sess_list.addStretch(1)
            self.side_status.setText(f"共 {len(sessions)} 条会话")
        finally:
            inner.setUpdatesEnabled(True)

    @staticmethod
    def _title_of(sess, cur: bool = False) -> str:
        mark = "● " if cur else ""
        if getattr(sess, "pinned", False):
            # ★（U+2605）在微软雅黑里有字形（inFont 实测 True）。
            # 别用 📌 / ▸ / ✦ 这类：inFont 为 False，会渲染成方框。
            mark += "★ "
        return f"{mark}{sess.title}"

    def _new_session(self) -> None:
        self._dismiss_pop()
        # 当前本来就是空会话（没有任何消息/附件）→ 不再新建，避免堆一堆空会话
        if not self._messages and not self._attachments and not self._images:
            self.input.setFocus()
            self.status.setText("已经是新会话了，直接提问吧")
            return
        self._save_current(touch=False)      # 开新会话不该把旧会话顶到最前
        sess = ss.new_session(self.engine.model_id)
        # ⚠️ 空会话**不落盘**：旧版 ss.save(sess) 会让侧栏堆一堆「新会话」空条目，
        # 用户只是切了一下会话就多一条（设计说明反馈）。第一条消息产生时
        # _save_current() 才会写盘，那时才有必要进历史列表（ChatGPT 同款行为）。
        # new_session() 只造对象不写文件，id 已生成；_save_current 会回写 id。
        self._session_id = sess.id
        self._messages = []
        self._clear_ui()
        self._remember_session()
        self._refresh_sessions()
        self._sync_head_title()
        self.status.setText("新会话 · 内容不出这台机器")
        self.input.setFocus()

    def _open_session(self, sid: str, save_current: bool = True,
                      force: bool = False) -> None:
        self._dismiss_pop()
        if sid == self._session_id and not force:
            return
        # 先把当前会话完整落盘再切：此刻 _session_id 仍是旧会话的 id，
        # _save_current() 只会写回旧文件，不会污染目标会话（旧注释搞反了）。
        # 覆盖「生成被中断」「输入了但没发」这些没走到自动保存的情况。
        # 删当前会话后的跳转要传 save_current=False，否则刚删的文件会被重建。
        # 正在生成时切走：**不停 worker、也不落地半截**（设计定稿）。
        # 让它继续在后台跑完，由 _on_done 把完整回答写回原会话文件；
        # 这里只记下它属于哪个会话。_busy 保持 True，新会话暂时不能发消息，
        # 但「停止」按钮仍可用（用户想中断随时能点）。
        generating = (self._worker is not None and self._worker.isRunning())
        if generating and not force:
            # 把这条生成转入后台：worker 继续跑，但不再占用当前窗口的状态。
            # 信号仍连在本对话框上，靠 sender() 区分「后台」和「当前」。
            w = self._worker
            _sid = self._ctx_session_id or self._session_id
            # ⚠️ 关键：把后台 worker 的信号**全部断开主对话框的槽**。
            # 跨线程队列信号里 sender() 不总可靠，后台的 think/delta 会串进
            # _on_think 累计字数 → 触发 3000 字超限 cancel() → 把当前会话
            # 的生成一起打断（设计说明：切会话后回答消失）。
            # 改成只连后台专属回调，后台与当前会话彻底隔离。
            for _sig in (w.delta, w.think, w.done, w.failed, w.finished):
                try:
                    _sig.disconnect()
                except Exception:
                    pass    # 某个信号本来就没连，不能中断后面的 disconnect
            w.done.connect(
                lambda text, ww=w, ssid=_sid: self._on_bg_done(ww, ssid, text))
            w.failed.connect(
                lambda msg, ssid=_sid: self._on_bg_failed(ssid, msg))
            w.finished.connect(lambda ww=w: self._bg_runs.pop(id(ww), None))
            self._bg_runs[id(w)] = (_sid, w)
            self._worker = None
            self._busy = False          # 新会话可以立刻继续聊天
            self.send_btn.setVisible(True)
            self.send_btn.setEnabled(True)
            self.stop_btn.setVisible(False)
            self.typer.reset()
            self._pending = None
            self._ai_block = None
            self._ctx_session_id = None
            self._think_clock.stop()
        if save_current:
            if not generating:
                # 没在生成（例如排版收尾中）才把未完成的回答落地
                self._flush_streaming_message()
            # 兜底落盘，不刷新时间戳（没产生新内容）
            self._save_current(touch=False)
        sess = ss.load(sid)
        if sess is None:
            self._refresh_sessions()
            return
        self._session_id = sid
        # 目标会话本身是空的（之前新建过但没发消息）→ 显示空状态页，
        # 而不是一片空白。旧版无条件 hidden，用户反馈「提示直接消失」。
        # empty / _spacer 的显隐统一交给 _clear_ui，别在这里再手动覆盖。
        self._clear_ui(show_empty=not sess.messages)
        # 深拷贝：不让界面与已加载的 Session 对象共享嵌套 dict（_ui.cards）
        self._messages = copy.deepcopy(sess.messages)
        # 重放历史（不打字机，直接成品渲染）
        mname = eng.MODEL_BY_ID.get(sess.model_id, eng.MODELS[0]).name
        for m in self._messages:
            role = m.get("role")
            ui = m.get("_ui") or {}
            # 新消息用 _ui.text（只有用户真正写的话，不含附件全文），
            # 并从 _ui.cards 恢复缩略图卡片；旧会话无 _ui 时退回原 content 保持兼容。
            content = str(ui.get("text") or m.get("content") or "")
            if role == "user":
                self._add_user(content, ui.get("cards"))
            elif role == "assistant":
                blk = self._add_ai(self._copy_text, mname)
                blk.set_final(content, 0.0, 0, str(m.get("reasoning") or ""))
                blk.set_meta("历史", mname)
        self._remember_session()
        self._refresh_sessions()
        self._sync_head_title()
        if self._bg_runs:
            self.status.setText("另一条会话在后台生成中 · 这边可以正常聊天")
        else:
            self.status.setText(f"已打开「{sess.title}」· 内容不出这台机器")
        self._scroll_bottom()

    def _flush_streaming_message(self) -> None:
        """把正在流式输出的回答落成旧会话里的一条消息（切会话 / 关闭前调用）。

        流式期间这条回答只在界面上的临时气泡里，`_messages` 还没它；
        切换会话会整区重建，不先落地就等于回话凭空消失。内容不完整是正常的 ——
        相当于一条"被中断的回答"，切回来还能看到已经生成的部分。
        """
        blk = getattr(self, "_ai_block", None)
        pending = self._pending
        # 先把队列里剩的字一次性吐完（会回调 render_typing 更新 _full），
        # 再取文本 —— 顺序反了就会丢掉还没打出来的那一截。
        try:
            self.typer.finish_now()
        except Exception:
            pass
        pending_text = str(pending[0] or "") if pending else ""
        typer_text = str(getattr(self.typer, "_shown", "") or "")
        blk_text = ""
        try:
            blk_text = str(getattr(blk, "_full", "") or "")
        except Exception:
            pass
        # worker 已完成的完整文本 > 打字机已吐出的 > 气泡快照，三者兜底
        full = pending_text or typer_text or blk_text
        if not full.strip():
            return                      # 一个字都没出来，没有可保存的内容
        reasoning = str(pending[1] or "") if pending else ""
        self._pending = None
        self._ctx_session_id = None     # 让随后到达的 _typing_idle 直接跳过
        msg: dict = {"role": "assistant", "content": full}
        if reasoning:
            msg["reasoning"] = reasoning
        self._messages.append(msg)

    def _delete_session(self, sid: str) -> None:
        """真正执行删除（确认已由 PopConfirm 完成）。"""
        if sid == self._session_id and self._busy:
            self._stop()          # 生成中删当前会话：先停，避免回复写进别的会话
        removed, _freed = ss.delete(sid)
        if sid == self._session_id:
            sessions = ss.list_sessions()
            if sessions:
                # 当前会话刚被删，跳转时绝不能保存，否则会把它重建出来
                self._open_session(sessions[0].id, save_current=False)
            else:
                # 删光了：回到空白状态，不落盘新会话（否则看起来"删了又冒出来"）
                self._clear_ui()
                self._session_id = ""
                self._refresh_sessions()
                self._sync_head_title()
        else:
            self._refresh_sessions()                 # 删非当前：只刷列表
        self._remember_session()
        extra = f" · 清理了 {removed} 张图片缓存" if removed else ""
        self.status.setText(f"会话已删除（消息与附件随之清除）{extra}")

    def _dismiss_pop(self) -> None:
        """关掉可能残留的删除/重命名气泡（避免它跟着切会话一起浮着）。

        顺手 deleteLater：气泡是对话框的子控件，只 hide 不销毁的话，每弹一次
        就永久往对话框里堆一个 QFrame，长时间使用会慢慢涨内存。
        """
        pop, self._pop = getattr(self, "_pop", None), None
        if pop is not None:
            pop.hide()
            pop.deleteLater()

    def _show_row_menu(self, sid: str, anchor) -> None:
        self._dismiss_pop()
        sess = ss.load(sid)
        self._pop = RowMenu(self, sid, bool(sess.pinned) if sess else False)
        self._pop.rename.connect(lambda sid_: self._rename_session(sid_, anchor))
        self._pop.pin.connect(self._toggle_pin)
        self._pop.delete.connect(self._ask_delete_session)
        self._pop.open_folder.connect(self._open_session_folder)
        self._pop.backup.connect(self._backup_session)
        self._pop.multi.connect(self._enter_multi)
        self._pop.popup_at(anchor)

    # ---------- 侧栏：多选删除 / 打开位置 / 单独备份 ----------
    def _toggle_multi(self) -> None:
        self._multi = not self._multi
        if not self._multi:
            self._checked.clear()
        self.multi_bar.setVisible(self._multi)
        self._refresh_sessions()          # 每次刷新会按 _multi 重建标题行按钮
        self._update_multi_count()

    def _enter_multi(self, sid: str) -> None:
        """行菜单「批量选择」：进入多选，并先勾上这一行。"""
        if not self._multi:
            self._toggle_multi()
        self._checked.add(sid)
        self._refresh_sessions()
        self._update_multi_count()

    def _on_row_toggled(self, sid: str, on: bool) -> None:
        if on:
            self._checked.add(sid)
        else:
            self._checked.discard(sid)
        self._update_multi_count()

    def _update_multi_count(self) -> None:
        self.multi_count.setText(f"已选 {len(self._checked)} 条")

    def _select_all_sessions(self) -> None:
        self._checked = {x.id for x in ss.list_sessions()}
        self._refresh_sessions()
        self._update_multi_count()

    def _ask_delete_selected(self) -> None:
        ids = list(self._checked)
        if not ids:
            self.status.setText("还没有勾选会话")
            return
        self._dismiss_pop()
        self._pop = PopConfirm(
            self, f"删除选中的 {len(ids)} 条会话？", "删除后无法恢复。")
        self._pop.confirmed.connect(lambda: self._do_delete_selected(ids))
        self._pop.popup_at(self._multi_toggle_btn or self.multi_bar)

    def _do_delete_selected(self, ids: list[str]) -> None:
        if self._busy and self._session_id in ids:
            self._stop()
        cur_deleted = self._session_id in ids
        for sid in ids:
            ss.delete(sid)
        self._checked.clear()
        self._multi = False
        self.multi_bar.setVisible(False)
        if cur_deleted:
            rest = ss.list_sessions()
            if rest:
                self._open_session(rest[0].id, save_current=False)
            else:
                self._clear_ui()
                self._session_id = ""
                self._sync_head_title()
        self._refresh_sessions()
        self._remember_session()
        self.status.setText(f"已删除 {len(ids)} 条会话")

    def _open_session_folder(self, sid: str) -> None:
        """打开这条会话 JSON 所在的文件夹（文件存在就顺手选中它）。"""
        try:
            root = ss._root()
            root.mkdir(parents=True, exist_ok=True)
            p = ss._path(sid) if sid else root
            if p.is_file():
                subprocess.Popen(f'explorer.exe /select,"{p}"', shell=True)
            else:
                subprocess.Popen(f'explorer.exe "{root}"', shell=True)
            self.status.setText("已打开会话存放位置")
        except Exception as e:
            self.status.setText(f"打开失败：{e}")

    def _backup_session(self, sid: str) -> None:
        """把单条会话导出成 Markdown（人读）或 JSON（原始结构）。"""
        import json
        from PySide6.QtWidgets import QFileDialog
        sess = ss.load(sid)
        if sess is None:
            self.status.setText("这条会话还没有内容可备份")
            return
        safe = "".join(c if c not in '\\/:*?"<>|' else "_" for c in sess.title)[:40]
        path, chosen = QFileDialog.getSaveFileName(
            self, "备份会话", (safe or "会话") + ".md",
            "Markdown (*.md);;JSON (*.json)")
        if not path:
            return
        try:
            if path.lower().endswith(".json") or "json" in (chosen or "").lower():
                data = json.dumps(dict(sess.__dict__), ensure_ascii=False, indent=2)
            else:
                data = self._session_to_markdown(sess)
            with open(path, "w", encoding="utf-8") as f:
                f.write(data)
            self.status.setText(f"已备份到 {os.path.basename(path)}")
        except Exception as e:
            self.status.setText(f"备份失败：{e}")

    @staticmethod
    def _session_to_markdown(sess) -> str:
        from datetime import datetime
        def ts(t: float) -> str:
            try:
                return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
            except Exception:
                return ""
        out = [f"# {sess.title}", "",
               f"- 模型：{sess.model_id}",
               f"- 创建：{ts(sess.created)}",
               f"- 更新：{ts(sess.updated)}", ""]
        for m in sess.messages or []:
            ui = m.get("_ui") or {}
            text = str(ui.get("text") or m.get("content") or "")
            role = m.get("role")
            if role == "user":
                out += ["## 用户", "", text, ""]
            elif role == "assistant":
                reasoning = str(m.get("reasoning") or "")
                if reasoning:
                    out += ["<details><summary>思考过程</summary>", "",
                            reasoning, "", "</details>", ""]
                out += ["## AI", "", text, ""]
        return "\n".join(out)

    def _toggle_pin(self, sid: str) -> None:
        """置顶 / 取消置顶。只改 pinned，不动 updated，所以组内先后保持不变。"""
        pinned = ss.toggle_pin(sid)
        self._refresh_sessions()
        self.status.setText("已置顶，会固定在列表最前" if pinned else "已取消置顶")

    def _ask_delete_session(self, sid: str, anchor) -> None:
        self._dismiss_pop()          # 先关掉旧气泡，避免事件过滤器残留
        # Popup 窗口必须持有引用，否则函数返回后 Python 对象被回收、气泡立刻消失
        self._pop = PopConfirm(self, "删除这条会话？", "删除后无法恢复。")
        self._pop.confirmed.connect(lambda: self._delete_session(sid))
        self._pop.popup_at(anchor)

    def _rename_session(self, sid: str, anchor) -> None:
        self._dismiss_pop()
        sess = ss.load(sid)
        self._pop = RenamePopup(self, sid, sess.title if sess else "", self._save_rename)
        self._pop.popup_at(anchor)

    def _save_rename(self, sid: str, title: str) -> None:
        if ss.rename(sid, title):
            self._refresh_sessions()
            self._sync_head_title()
            self.status.setText(f"已重命名为「{title}」")
        else:
            self.status.setText("重命名失败")

    def _sync_head_title(self) -> None:
        """副标题只报当前模型。

        当前会话的标题**不在这里重复**：左侧列表已经高亮当前行，再写一遍
        既占地方又和左侧信息打架（2026-11-10 设计要求）。
        """
        short = "本地模型"
        try:
            spec = self.engine.spec
            short = spec.short_name or spec.name
        except Exception:
            pass
        self.head_sub.setText(f"当前模型：{short}")

    def _save_current(self, touch: bool = True) -> None:
        """把当前会话写回它的 JSON。

        touch=False 用于「切走前的兜底保存」：此时并没有产生新内容，
        不该刷新 updated —— 否则点一下会话，它就会被顶到列表最前面，
        用户看到的就是「左侧会话列表乱掉」。
        """
        if not self._messages:
            return
        # 生成中途切过会话时，任何延迟回调都不能把内容写进新会话。
        # 令牌本身由 _clear_ui() 清空，这里再兜一层，防止保存落到错误 sid。
        if self._ctx_session_id is not None and self._ctx_session_id != self._session_id:
            return
        sess = ss.load(self._session_id) or ss.new_session(self.engine.model_id)
        # 内容一个字都没变（比如只是切了个会话）→ 不写盘、不动 updated，
        # 列表顺序就不会无缘无故跳动。深比较有成本，但只在保存时机跑，可接受。
        if sess.messages == self._messages and sess.model_id == self.engine.model_id:
            return
        sess.model_id = self.engine.model_id
        sess.messages = copy.deepcopy(self._messages)   # 不与界面/其它会话共享 dict 引用
        try:
            ss.save(sess, touch=touch)
            # 关键：回写 id。否则文件不存在时 new_session 会生成新 id 保存，
            # _session_id 却还指旧的 → 每轮保存漂移出新文件，历史就乱了
            self._session_id = sess.id
        except Exception:
            pass
        # 按引用计数回收没人用的大图（异常退出留下的孤儿）。完整历史不再被
        # 裁剪，所以正常对话里的图片引用会一直保留，不会被误删。
        try:
            ss.prune_previews()
        except Exception:
            pass
        self._refresh_sessions()
        self._sync_head_title()

    # ---------- 模型切换
    def _model_panel_open(self) -> bool:
        p = getattr(self, "_model_panel", None)
        try:
            return p is not None and p.isVisible()
        except RuntimeError:      # 面板已被销毁
            return False

    def _sync_model_tag(self) -> None:
        spec = self.engine.spec
        # 箭头不再写进文字里：交给 ModelPillBtn 自绘，开合时只翻方向、不改字宽，
        # 按钮不会因为多一个字符而抖动。文字只显示核心版本（全称在模型面板里）
        self.model_btn.setText(spec.short_name or spec.name)
        self.model_btn.set_open(self._model_panel_open())
        try:
            name = spec.short_name or spec.name
            # 模型还没下载时，"不联网 / 内容不出这台机器" 说得太早了 ——
            # 用户此刻最需要知道的是去哪儿把它下下来。
            if self.engine.is_installed(spec.id):
                self.empty_sub.setText(f"{name} · 不联网 · 内容不出这台机器")
            else:
                # 别只说「X 还没下载」：用户此刻最需要的是入口 + 选模型的依据
                self.empty_sub.setText(
                    "当前还未下载本地模型，点右上角「下载模型」"
                    "（请按自身显卡显存选择合适的模型）")
        except Exception:
            pass
        self._sync_deep_btn()

    # ---------- 深度思考
    def _sync_deep_btn(self) -> None:
        """按当前模型是否会产生思考过程，决定这个开关能不能点。"""
        btn = getattr(self, "deep_btn", None)
        if btn is None:
            return
        ok = bool(self.engine.spec.reasoning)
        if not ok and btn.isChecked():
            # 换到不会思考的模型：静默关掉，免得用户以为开了却没效果
            btn.setChecked(False)
            self._deep_think = False
        btn.setEnabled(ok)

    def _toggle_deep(self, checked: bool) -> None:
        self._deep_think = bool(checked) and self.engine.spec.reasoning
        # 模型不产生思考过程时，按钮要弹回关闭态，别让实心主色骗人说「已开启」
        if self._deep_think != checked:
            self.deep_btn.setChecked(self._deep_think)
        try:
            self._api.settings_set("deep_think", self._deep_think)
        except Exception:
            pass
        self.status.setText(
            "深度思考已开：会先思考再回答，慢一些但更严谨"
            if self._deep_think else "深度思考已关：直接作答，响应更快")

    def _tick_think(self) -> None:
        """思考阶段每秒刷新一次状态栏，让"卡住"变成"在思考"。"""
        if not self._busy:
            self._think_clock.stop()
            return
        sec = int(time.time() - self._think_started)
        self.status.setText(f"正在思考… {sec}s（可随时点停止）")

    def _show_model_menu(self) -> None:
        # 一次鼠标点击有时会以两次 clicked 到达（按下 + 收尾各算一次），
        # 第二次会把刚弹出来的面板立刻关掉 —— 用户看到的就是「闪一下」。
        # 用「弹出还没落地」这个标志挡住：第二次 clicked 一定发生在同一次
        # 事件派发里，早于下面那个 singleShot，所以这里必然命中。
        # （原来用 0.28 秒时间窗不行：面板首次打开本身要 300ms+，等它显示完窗口已过期。）
        if getattr(self, "_panel_opening", False):
            return
        if getattr(self, "_model_panel", None) is None:
            self._model_panel = ModelPanel(self)
            # 面板收起（点空白 / 选完模型）时把箭头翻回朝下
            self._model_panel.closed.connect(self._sync_model_tag)
        if self._model_panel.isVisible():
            self._model_panel.hide()        # 再点一次 = 收起
            return
        # 另外也不能在 clicked 槽里直接 show()：Qt.Popup 会把这次点击的收尾
        # 判成「点了窗口外面」。推到下一轮事件循环，等点击彻底处理完再弹。
        self._panel_opening = True
        QTimer.singleShot(0, self._open_model_panel)

    def _open_model_panel(self) -> None:
        self._panel_opening = False
        panel = getattr(self, "_model_panel", None)
        if panel is None or panel.isVisible():
            return
        panel.popup_at(self.model_btn)
        self._sync_model_tag()              # 展开后箭头朝上

    def model_ctx(self, mid: str) -> int:
        """模型实际使用的上下文：用户设置优先，其次清单默认。"""
        overrides = self._cfg("ctx_overrides", {}) or {}
        if isinstance(overrides, dict) and mid in overrides:
            try:
                return int(overrides[mid])
            except Exception:
                pass
        spec = eng.MODEL_BY_ID.get(mid)
        return spec.ctx if spec else eng.CTX_DEFAULT

    def model_ctx_set(self, mid: str, val: int) -> None:
        overrides = dict(self._cfg("ctx_overrides", {}) or {})
        overrides[mid] = int(val)
        try:
            self._api.settings_set("ctx_overrides", overrides)
        except Exception:
            pass
        if mid == self.engine.model_id:
            self.engine.ctx = int(val)      # 下次启动用新值
            name = eng.MODEL_BY_ID[mid].name
            self.status.setText(f"{name} 上下文改为 {val} · 下次切换/重开生效")

    def cancel_model_download(self) -> None:
        """让 SwitchWorker 在下一次进度回调里抛 Cancelled（已下载部分保留，可续传）。"""
        sw = getattr(self, "_switcher", None)
        if sw is not None and sw.isRunning():
            sw.cancel()
            self.status.setText("正在取消下载…（已下载部分会保留，下次继续）")
        self._refresh_model_state()

    def _refresh_model_state(self) -> None:
        """下载 / 安装 / 卸载 / 取消后，重排模型面板分组与状态徽章。"""
        panel = getattr(self, "_model_panel", None)
        if panel is None or not panel.isVisible():
            return
        mid = panel._detail_id
        panel._rebuild()
        if mid:
            panel.info.show_for(mid, panel._rows.get(mid))

    def model_uninstall(self, mid: str) -> None:
        spec = eng.MODEL_BY_ID.get(mid)
        if spec is None:
            return
        if getattr(self, "_busy", False):
            self.status.setText("正在生成或切换模型，等这一轮结束再卸载")
            return
        was_current = mid == self.engine.model_id
        # 0.37 修复：只有一个已下载模型时也必须能卸载。不再要求「先切换模型」，
        # 卸载前主动停引擎，卸载后把 model_id 挪到另一个已安装模型；没有就留提示。
        extra = ("这是当前正在用的模型，卸载会先停掉它；\n"
                 "之后再聊天需要重新下载或切换到别的模型。\n\n") if was_current else ""
        if not ConfirmDialog.ask(
                self, "卸载模型",
                extra + f"卸载「{spec.name}」？\n\n会删除约 {spec.size_mb / 1024:.1f}GB 模型文件，"
                f"之后还能重新安装。",
                ok_text="卸载", danger=True):
            return
        if was_current:
            try:
                self.engine.stop()
            except Exception:
                pass
        # 0.39.1：删所有副本（engine + 旧目录硬链接），不再只 rmtree engine 目录
        ok, msg = self.engine.remove_model(mid)
        if was_current:
            others = [m.id for m in eng.MODELS
                      if m.id != mid and self.engine.is_installed(m.id)]
            if others:
                self.engine.model_id = others[0]
                try:
                    self._api.settings_set("model_id", others[0])
                except Exception:
                    pass
                self.status.setText(
                    f"已卸载 {spec.name} · 已切换到 {eng.MODEL_BY_ID[others[0]].name}")
            else:
                self.status.setText(
                    f"已卸载 {spec.name} · 当前没有可用模型，聊天前请先下载")
        else:
            self.status.setText(f"已卸载 {spec.name}")
        panel = getattr(self, "_model_panel", None)
        if panel is not None:
            try:
                panel.info.hide()
            except Exception:
                pass
        self._refresh_model_state()

    def _open_models_dir(self) -> None:
        """用资源管理器打开模型根目录（方便手动清理）。"""
        root = eng.models_root()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        try:
            os.startfile(str(root))  # noqa: S606  Windows 专用
        except Exception:
            try:
                subprocess.Popen(["explorer", str(root)])
            except Exception as e:  # noqa: BLE001
                self.status.setText(f"打开模型文件夹失败：{e}")

    def _choose_model(self, mid: str) -> None:
        if self._busy:
            self.status.setText("正在生成，等这一轮结束再切换")
            return
        if self._switcher is not None and self._switcher.isRunning():
            # 已经在下载 / 切换中：直接把下载页叫回前台，而不是再开一条线程
            self._show_download_page()
            self.status.setText("正在切换，稍等")
            return
        installed = self.engine.is_installed(mid)
        if mid == self.engine.model_id and installed and self.engine.ready():
            self.status.setText(f"已经在用 {eng.MODEL_BY_ID[mid].name}")
            return

        spec = eng.MODEL_BY_ID[mid]
        # 0.39.5：不再弹「需要先下载 4.2GB，现在开始安装吗」——下载页本身就是确认：
        # 大小、速度、剩余时间、暂停、取消都在上面，误点也能随时退。
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress.setValue(0)
        self._set_dot("dotBusy")
        self.send_btn.setEnabled(False)
        self.send_btn.setVisible(False)
        self.stop_btn.setVisible(True)      # 下载中也能取消
        self.stop_btn.setEnabled(True)
        head = "安装" if not installed else "切换"
        self.status.setText(f"正在{head} {spec.name}…")

        self._switcher = SwitchWorker(self.engine, mid, not installed)
        self._switcher.progress.connect(self._on_progress)
        self._switcher.ready.connect(self._on_switched)
        self._switcher.failed.connect(self._on_switch_failed)
        self._switcher.cancelled.connect(self._on_switch_cancelled)
        self._switcher.finished.connect(self._on_switch_finished)
        if not installed:
            self._open_download_page(mid, self._switcher)
        self._switcher.start()
        self._refresh_model_state()

    # ---------- 独立下载页
    def _open_download_page(self, mid: str, worker: SwitchWorker) -> None:
        """打开（或复用）模型下载页，并把当前 worker 接上去。"""
        old = getattr(self, "_dl_dlg", None)
        if old is not None:
            try:
                old.dismissed.disconnect()
            except Exception:
                pass
            old.deleteLater()
        dlg = ModelDownloadDialog(self, mid, need_download=worker._dl)
        dlg.background.connect(self._on_download_background)
        dlg.retry_requested.connect(self._retry_download)
        dlg.dismissed.connect(self._on_download_dismissed)
        dlg.attach(worker)
        self._dl_dlg = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        self._sync_dl_entry(True)

    def _show_download_page(self) -> None:
        dlg = getattr(self, "_dl_dlg", None)
        if dlg is not None:
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
            return
        sw = getattr(self, "_switcher", None)
        if sw is not None and sw.isRunning() and getattr(sw, "_dl", False):
            self._open_download_page(sw._mid, sw)

    def _retry_download(self, mid: str) -> None:
        dlg = getattr(self, "_dl_dlg", None)
        if dlg is not None:
            dlg.dismissed.disconnect()
            dlg.close()
            dlg.deleteLater()
            self._dl_dlg = None
        self._choose_model(mid)

    def _on_download_background(self) -> None:
        self._sync_dl_entry(True)
        self.status.setText("模型正在后台下载 · 点右侧「查看下载进度」可回到下载页")

    def _on_download_dismissed(self) -> None:
        self._dl_dlg = None
        self._sync_dl_entry(False)

    def _sync_dl_entry(self, show: bool) -> None:
        btn = getattr(self, "dl_btn", None)
        if btn is not None:
            btn.setVisible(bool(show))

    def _on_switch_cancelled(self) -> None:
        self.progress.setVisible(False)
        self.send_btn.setVisible(True)
        self.stop_btn.setVisible(False)
        self._sync_dl_entry(False)
        self._set_dot("dotIdle")
        name = eng.MODEL_BY_ID[self._switcher._mid].name if self._switcher else "模型"
        self.status.setText(f"已取消下载 {name} · 已下载部分保留，下次接着下")
        self._sync_power()
        self._refresh_model_state()

    def _on_switch_finished(self) -> None:
        self.progress.setVisible(False)
        self.send_btn.setVisible(True)
        self.stop_btn.setVisible(False)
        # 下载页还在（可能被退到后台）就留着「查看下载进度」入口，等用户自己关掉
        self._sync_dl_entry(getattr(self, "_dl_dlg", None) is not None)
        if self.engine.ready():
            self._set_dot("dotReady")
        self._sync_power()
        self._refresh_model_state()

    def _on_switched(self, mid: str) -> None:
        self.progress.setVisible(False)
        self.send_btn.setVisible(True)
        self.send_btn.setEnabled(True)
        self._set_dot("dotReady")
        self._sync_model_tag()
        try:
            self._api.settings_set("model_id", mid)
        except Exception:
            pass
        mem = self.engine.memory_mb()
        self.status.setText(f"{self.engine.spec.name} 就绪 · 占用约 {mem}MB")
        self._sync_power()
        self.input.setFocus()
        self._refresh_model_state()

    def _on_switch_failed(self, msg: str) -> None:
        self.progress.setVisible(False)
        self.send_btn.setVisible(True)
        self._set_dot("dotError")
        self._sync_power()
        self._refresh_model_state()
        # 下载页还开着：失败原因和「重试」都在页面上，再往对话区插一条错误气泡
        # 就是重复报警（还挤掉真正在看的内容）。
        if getattr(self, "_dl_dlg", None) is not None:
            self.status.setText("下载失败 · 可在下载页点「重试」")
            return
        self.status.setText("切换失败")
        blk = self._add_ai(self._copy_text)
        blk.set_error(msg)

    def _use_suggestion(self, text: str) -> None:
        self.input.setPlainText(text)
        self._autosize_input()
        self._send()

    def _set_dot(self, name: str) -> None:
        """顶栏引擎胶囊、底部状态栏两颗圆点跟引擎状态同步。"""
        for dot in (self.dot, getattr(self, "status_dot", None)):
            if dot is None or dot.objectName() == name:
                continue
            dot.setObjectName(name)
            st = dot.style()
            if st is not None:
                st.unpolish(dot)
                st.polish(dot)
            dot.update()

    # ---------- 引擎（手动启停 / 空闲释放）
    def _sync_power(self) -> None:
        """把顶栏开关、状态点、发送按钮与引擎真实状态对齐。"""
        r = self.engine.ready()
        self.power_btn.setEnabled(True)
        self.power_btn.setText("停止模型" if r else "启动模型")
        self.power_btn.setProperty("on", "true" if r else "false")
        self._sync_eng_text(r)
        st = self.power_btn.style()
        if st is not None:
            st.unpolish(self.power_btn)
            st.polish(self.power_btn)
        if r:
            self._set_dot("dotReady")
            self.send_btn.setEnabled(True)
        else:
            self._set_dot("dotIdle")
            self.send_btn.setEnabled(False)

    def _sync_eng_text(self, ready: bool | None = None) -> None:
        """把顶栏引擎胶囊的文本和真实状态对齐（圆点颜色由 _set_dot 管）。

        0.36 起胶囊只报状态、不再拼模型名：模型名已在副标题「当前模型：xxx」
        和输入卡胶囊里显示，胶囊再写一遍会把右侧按钮挤得很靠外（设计要求）。
        """
        if not hasattr(self, "eng_text"):
            return
        if ready is None:
            try:
                ready = self.engine.ready()
            except Exception:
                ready = False
        self.eng_text.setText("模型已就绪" if ready else "模型未启动")

    def _toggle_engine(self) -> None:
        if not self.engine.has_files():
            # 没模型时这颗按钮显示「下载模型」：直接开模型面板，
            # 别再走一遍启动流程（否则又回到「启动中…→未下载模型」的循环）。
            self._show_model_menu()
            return
        if self.engine.ready():
            if self._busy:
                self.status.setText("正在生成，等这一轮结束再停止运行")
                return
            _cancel_release()                       # 已手动停，取消挂着的空闲定时器
            mem = self.engine.memory_mb()
            self.engine.stop()
            self._manual_stopped = True
            self._sync_power()
            freed = f"（释放约 {mem}MB 内存）" if mem else ""
            self.status.setText(f"模型已停止{freed} · 点「启动模型」可再开")
        else:
            if self._prep is not None and self._prep.isRunning():
                return
            self._manual_stopped = False
            self._prepare()

    def _on_idle_stopped(self) -> None:
        """窗口开着时引擎空闲自动停：同步 UI，但不标记手动停止（下次发消息会自动拉起）。"""
        self._sync_power()
        self.status.setText("模型空闲已自动停止 · 再发消息会自动拉起")

    def _ensure_engine(self) -> None:
        """引擎没起时拉起（不重复起）。"""
        if self._prep is not None and self._prep.isRunning():
            return
        self._prepare()

    def _prepare(self) -> None:
        self.power_btn.setText("启动中…")
        self.power_btn.setEnabled(False)
        if not self.engine.has_files():
            # 运行时随包内置（0.39.1 起），真缺了才算故障；模型没下载是正常状态，
            # 只给一句「未下载模型」，不弹错误框、不甩文件路径。
            if not self.engine.find_runtime():
                self._fail("推理运行时缺失，请重新安装 LyLaunch。")
            else:
                self._mark_model_missing()
            return
        self.progress.setVisible(True)
        self.status.setText("正在准备模型…")
        self.eng_text.setText("正在启动…")
        self._prep = PrepareWorker(self.engine)
        self._prep.progress.connect(self._on_progress)
        self._prep.ready.connect(self._on_ready)
        self._prep.failed.connect(self._fail)
        self._prep.model_missing.connect(self._mark_model_missing)
        self._prep.start()

    def _on_progress(self, doc: str, pct) -> None:
        # 下载类回调带的是文件名（"下载 Qwen3-4B-Q4_K_M.gguf"），状态栏别显示这种
        # 技术名：换成模型显示名 + 百分比，和下载页保持一致的口径。
        if doc.startswith("下载") and self._switcher is not None:
            spec = eng.MODEL_BY_ID.get(getattr(self._switcher, "_mid", ""))
            label = spec.name if spec else "模型"
            tail = f" · {int(pct)}%" if pct is not None else ""
            self.status.setText(f"正在下载 {label}{tail}")
        else:
            self.status.setText(doc)
        self._set_dot("dotBusy")
        if pct is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(pct))

    def _on_ready(self) -> None:
        self.progress.setVisible(False)
        self._set_dot("dotReady")
        self._sync_model_tag()
        self._sync_power()
        mem = self.engine.memory_mb()
        self.status.setText(
            f"本地运行中 · 显存/内存占用：{_mem_text(mem)} · 隐私保护：数据不离开本地")
        self.input.setFocus()
        # 引擎被空闲释放后用户又发了消息：就绪后自动把排队的请求发出去。
        # 期间若切过会话，令牌已失效 → 丢弃排队请求（否则会把旧会话的问题
        # 和图片发到新会话里，就是「串味」）。
        if self._pending_images is not None:
            imgs, self._pending_images = self._pending_images, None
            if self._ctx_session_id == self._session_id:
                self._start_completion(images=imgs)

    def _mark_model_missing(self) -> None:
        """模型没下载：只给一句安静的状态提示，不弹错误框、不显示文件路径。

        旧版这里弹「LyLaunch 出错了」并甩出一个 0.35 之前的旧目录路径
        （现在模型直接放 engine\\models\\<id>\\），既误导又像程序坏了。
        下载入口本来就在右上角模型面板里，把用户引过去即可。
        """
        self.progress.setVisible(False)
        self.power_btn.setText("下载模型")
        self.power_btn.setEnabled(True)
        self.eng_text.setText("未下载模型")
        self._set_dot("dotIdle")
        self.send_btn.setEnabled(False)
        # 这句话原来写在这里，和顶部徽章、欢迎页副标题三处重复。
        # 状态栏只报状态，具体怎么下载交给正中间的欢迎页去说。
        self.status.setText("未下载模型")
        self._sync_model_tag()

    def _fail(self, msg: str) -> None:
        self.progress.setVisible(False)
        self.status.setText("模型没起来")
        self._set_dot("dotError")
        self._sync_power()
        blk = self._add_ai(self._copy_text)
        blk.set_error(msg)

    # ---------- 对话
    def _send(self) -> None:
        if self._busy:
            return
        text = self.input.toPlainText().strip()
        if not text and not self._attachments and not self._images:
            return
        self._no_think_retry = False      # 新的一问，允许再来一次关思考重试
        # 模型文件一个都没有：直接给下载指引，不要往下走。
        # 否则会先建一个会话再在 send 阶段失败，留下「有问无答」的空对话
        # （点欢迎页建议、手打文字回车都会踩到，设计说明）。
        try:
            if not self.engine.is_installed(self.engine.model_id):
                self._mark_model_missing()
                return
        except Exception:
            pass
        # 手动停了引擎就别把消息塞进历史（否则有气泡没回答）；
        # 其余情况（空闲自动释放 / 启动中）由 _start_completion 排队 + 自动拉起。
        if self._manual_stopped and not self.engine.ready():
            self.status.setText("模型已停止 · 点右上角「启动模型」再发")
            return
        # 真正发给模型的内容：附件全文 + 用户问题
        payload = text
        if self._attachments:
            parts = [f"【文件：{n}】\n{body}\n【文件结束】"
                     for n, body, _sz in self._attachments]
            payload = "\n\n".join(parts) + (f"\n\n{text}" if text else "")
        # 只发图片 / PDF 页面、没写文字时补一句任务描述：
        # 空 text + 纯图片在 llama.cpp 上容易直接回空回答
        if not payload.strip() and self._images:
            payload = "请识别并说明这些图片 / PDF 页面的内容。"

        # 气泡里显示用户写的话 + 附件卡片（图片缩略图 / 文档类型徽章），不铺附件全文。
        # cards 是纯数据：卡片用 96×96 小图 b64，点击查看的大图落成独立文件、
        # 这里只记文件名（会话 JSON 不膨胀）。整份放进消息的 _ui 字段供历史重放；
        # 发给模型前会在 _start_completion 里剥掉 _ui，不污染 OpenAI 请求。
        cards: list[dict] = []
        for name, b64 in self._images:
            cards.append({"name": name, "kind": "image",
                          "thumb": _small_thumb_b64(b64),
                          "preview": _save_preview_file(b64),
                          "size": f"{len(b64) * 3 // 4 // 1024} KB"})
        for name, body, size in self._attachments:
            cards.append({"name": name, "kind": "file",
                          "size": _size_text(size),
                          "text": body})   # 文档卡片点开可看发给模型的解析内容

        _cancel_release()                     # 有新对话，取消挂着的空闲释放
        self.input.clear()
        self._autosize_input()
        self._add_user(text, cards)
        self._messages.append({"role": "user", "content": payload,
                               "_ui": {"text": text, "cards": cards}})
        # 完整历史原样保留：界面气泡、会话 JSON、上下文记忆、大图引用都靠它。
        # 「怕撑爆 8192 上下文」只在 _start_completion() 里对请求副本裁剪，
        # 绝不在这里动 self._messages —— 以前在这里 pop(0)，等于每问一轮
        # 就把更早的对话永久删掉，模型自然「没记忆」。
        # 必须先取快照再清空：清空后再读 self._images 就是空列表（图会丢）
        imgs = [b64 for _n, b64 in self._images]
        self._send_notice = ""
        if imgs and not self.engine.spec.vision:
            # 不阻断、不弹窗：只带文字照样能发。把图片剥掉是因为非视觉模型
            # 没有 mmproj，llama-server 收到 image_url 会直接 400。
            imgs = []
            self._send_notice = "当前模型看不了图，已只带文字"
        elif len(imgs) > MAX_SEND_IMAGES:
            imgs = imgs[:MAX_SEND_IMAGES]
            self._send_notice = f"图片较多，本次只发前 {MAX_SEND_IMAGES} 张"
        self._clear_attachments()
        self._start_completion(images=imgs)

    def _start_completion(self, images: list[str] | None = None,
                          no_think: bool = False) -> None:
        """用当前 _messages 发起一次请求（首次发送与"重新生成"共用）。

        ``no_think``：思考超长时的自动重试 —— 强制关掉 enable_thinking，
        并在最后一条 user 消息末尾附一句「直接给答案」，把预算全留给正文。
        """
        # 打上会话令牌：之后切了会话，所有回调（done / failed / 延迟保存 /
        # 排队图片）都要按这个令牌比对，不一致就整条丢弃，绝不串到新会话。
        self._ctx_session_id = self._session_id
        if not self.engine.ready():
            if self._manual_stopped:
                self.status.setText("模型已停止 · 点右上角「启动模型」再发")
                return
            # 空闲自动释放 / 首次启动中：排队，等 _on_ready 自动发出
            self._pending_images = list(images or [])
            self._ensure_engine()
            self.status.setText("模型正在启动…稍候自动发送")
            return
        self._ai_block = self._add_ai(self._copy_text, self.engine.spec.name, self._regenerate)
        self._ai_block.set_placeholder("正在思考…")
        self._think_len = 0        # 新一轮：重置思考计数
        self._think_overflow = False
        self.typer.reset()
        self._pending = None

        self._busy = True
        self.send_btn.setVisible(False)
        self.stop_btn.setVisible(True)
        self.stop_btn.setEnabled(True)
        notice = getattr(self, "_send_notice", "")
        self._send_notice = ""
        self.status.setText(
            "正在生成…" + (f" · {notice}" if notice else "")
            + (" · 已关闭思考" if no_think else ""))
        self._set_dot("dotBusy")

        # 默认 1024 对"会思考"的模型太紧：思考部分能把预算吃光，正文只剩
        # 一句「（没有内容）」。抬到 2048 给正文留足空间。
        max_tokens = int(self._cfg("max_tokens", 2048) or 2048)
        ctx_tokens = int(self.engine.ctx or self.engine.spec.ctx or 8192)
        thinking = bool(self._deep_think and self.engine.spec.reasoning) and not no_think
        if thinking:
            # 思考 + 正文共用 max_tokens；给少了正文会被思考吃光只剩一两句
            max_tokens = max(max_tokens, min(3072, max(1024, ctx_tokens // 2)))
        elif self.engine.spec.reasoning:
            # 没开思考就别占着配额，保持原来的 2048 即可
            max_tokens = max(max_tokens, 2048)
        # 请求层上下文：从完整历史按预算挑出最近若干「完整轮」，顺手剥掉 _ui。
        # 这只是副本，self._messages / 会话 JSON 一个字都不动。预算按
        # 上下文长度减去回答上限和模板开销算；中文约 1 字 1 token，偏保守。
        budget = max(800, ctx_tokens - max_tokens - 700)
        model_msgs = ss.build_context(self._messages, budget)
        if no_think:
            _append_request_hint(model_msgs, "请直接给出最终答案，不要输出思考过程")
        elif thinking and self._cfg("think_in_chinese", True):
            # 深度思考时模型常用英文自言自语；附一句让它尽量用中文想。
            # 只在请求副本里，历史/会话 JSON 不变；配置 think_in_chinese=false 可关。
            _append_request_hint(model_msgs, "请全程用中文思考，并用中文回答")
        # 直观反馈：让用户能看到这次请求真的带上了之前几轮，而不是「失忆」。
        hist_turns = max(0, sum(1 for m in model_msgs if m.get("role") == "user") - 1)
        if hist_turns:
            self.status.setText(self.status.text() + f" · 带 {hist_turns} 轮上下文")
        # 不注入任何 system 消息（原因见文件顶部说明）。历史里若本来就有 system
        # （用户手动改过会话 JSON），build_context 会原样保留在最前。
        self._worker = ChatWorker(
            self.engine, model_msgs,
            max_tokens,
            float(self._cfg("temperature", 0.7) or 0.7),
            images=images if images is not None else [],
            # 只有「模型会思考 + 用户主动开了开关」才真的让它思考
            thinking=thinking,
        )
        self._worker.delta.connect(self._on_delta)
        self._worker.think.connect(self._on_think)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _regenerate(self) -> None:
        """丢弃上一条回答，用同样的上下文重跑一次。"""
        if self._busy:
            return
        if not self._messages or self._messages[-1].get("role") != "assistant":
            return
        self._messages.pop()
        for i in reversed(range(self.msg.count())):
            w = self.msg.itemAt(i).widget()
            if isinstance(w, AiBlock):
                self.msg.removeWidget(w)
                w.hide()

                w.deleteLater()
                break
        self._start_completion()

    def _on_done(self, full: str) -> None:
        # 后台分支必须排在前面：切走前已超限的话，不能在新会话触发重试（会串台）
        src = self.sender()
        if src is not None and id(src) in self._bg_runs:
            sid, _bg_w = self._bg_runs[id(src)]
            reasoning = getattr(src, "reasoning", "") or ""
            self._finish_bg_answer(sid, full, reasoning)
            return                      # 完全不碰当前会话的 UI 状态
        if self._think_overflow and not (full or "").strip():
            self._think_overflow = False
            self._retry_think_overflow()
            return
        self._think_overflow = False
        if self._ctx_session_id != self._session_id:
            self._ctx_session_id = None
            return                      # 切过会话，这次回答整条丢弃
        elapsed = self._worker.elapsed if self._worker else 0.0
        reasoning = getattr(self._worker, "reasoning", "") if self._worker else ""
        usage = getattr(self.engine, "last_usage", {}) or {}
        tokens = int(usage.get("completion_tokens") or 0)
        self._pending = (full, reasoning, elapsed, tokens)
        if self.typer.idle:      # 队列已吐完，直接收尾；否则等打字机打完
            self._typing_idle()

    def _on_bg_done(self, worker, sid: str, full: str) -> None:
        """后台生成完成（专属连接）：只写回原会话，绝不碰当前会话 UI。"""
        reasoning = getattr(worker, "reasoning", "") or ""
        if not (full or "").strip() and reasoning.strip():
            # 后台思考吃光了配额：至少把思考过程落盘，别让这条会话看起来
            # 连回答都没了（用户切回去还能看到模型想了什么）。
            self._finish_bg_answer(
                sid, "（模型只输出了思考过程，未给出正文）", reasoning)
            return
        self._finish_bg_answer(sid, full, reasoning)

    def _on_bg_failed(self, sid: str, msg: str) -> None:
        """后台生成失败：不弹当前聊天、不动按钮，原会话保持原样。"""
        self.status.setText("后台生成未成功 · 原会话内容未受影响")

    def _finish_bg_answer(self, sid: str, full: str, reasoning: str) -> None:
        """后台生成完毕：把完整回答追加到**原会话**的 JSON，不是当前会话。

        用户已经切回那条会话时立即重放（force=True 绕过 sid 相同的早退），
        就能看到完整回答；还停别的会话时只更新状态栏，切回去自然会加载。
        """
        if not (full or "").strip():
            return
        try:
            sess = ss.load(sid)
            if sess is None:
                return
            msg: dict = {"role": "assistant", "content": full}
            if reasoning:
                msg["reasoning"] = reasoning
            sess.messages.append(msg)
            ss.save(sess)             # 有新内容 → 刷新 updated，会话排到最前
        except Exception as e:
            self.status.setText(f"回答写回失败：{e}")
            return
        if self._session_id == sid and not (
                self._worker is not None and self._worker.isRunning()):
            self._open_session(sid, save_current=False, force=True)
        elif self._session_id == sid:
            self.status.setText("回答已生成完 · 这轮聊完切走再回来即可看到")
        else:
            self.status.setText("另一条会话的回答已生成完 · 切回去即可查看")
        self._refresh_sessions()

    def _retry_think_overflow(self) -> None:
        """思考把配额吃光、正文为空：清掉这条空气泡，关思考自动重发一次。

        只重试一次（_no_think_retry），否则模型又继续思考会死循环。
        """
        self.typer.reset()
        self._pending = None
        self._ctx_session_id = None
        blk, self._ai_block = self._ai_block, None
        if blk is not None:
            try:
                blk.hide()
                blk.deleteLater()
            except RuntimeError:
                pass
        if self._no_think_retry:
            self.status.setText("模型一直停在思考、没能给出答案 · 换个问题或小模型试试")
            self._set_dot("dotError")
            return
        self._no_think_retry = True
        self._pending_retry = True       # 等旧线程 finished 后再起，避免状态打架
        self.status.setText("思考过长 · 已关闭思考，正在重新作答…")

    def _on_worker_failed(self, msg: str) -> None:
        if self._is_bg_sender():
            return                      # 后台失败不打扰当前聊天
        mine = self._ctx_session_id == self._session_id
        self._ctx_session_id = None
        self.typer.reset()
        self._pending = None
        blk, self._ai_block = self._ai_block, None
        if not mine:
            return                      # 旧会话的报错也不能弹到新会话里
        if blk is not None:
            try:
                blk.set_error(f"出错了：{msg}")
            except RuntimeError:
                pass
        self.status.setText("生成失败")
        self._set_dot("dotError")

    def _on_finished(self) -> None:
        src = self.sender()
        if src is not None and id(src) in self._bg_runs:
            self._bg_runs.pop(id(src), None)
            return                      # 后台收尾：当前会话可能正在生成，别插手
        self._busy = False
        self._think_clock.stop()      # 收尾：别让状态栏还停在「正在思考 Ns」
        if self.engine.ready():
            self._set_dot("dotReady")
        self.send_btn.setVisible(True)
        self.send_btn.setEnabled(True)
        self.stop_btn.setVisible(False)
        self._worker = None
        if self._pending_retry:
            self._pending_retry = False
            QTimer.singleShot(0, lambda: self._start_completion(no_think=True))

    def _stop(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self.status.setText("已停止")
        elif self._switcher is not None and self._switcher.isRunning():
            self._switcher.cancel()
            self.status.setText("正在取消…（已下载部分会保留，下次可续传）")
            self._refresh_model_state()

    # ---------- 生命周期
    def closeEvent(self, event: QCloseEvent) -> None:
        try:
            self.typer.finish_now()
        except Exception:
            pass
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(5000)
        # 后台 worker 已转入 _bg_runs，不在这里 cancel：ChatDialog 是模块级单例，
        # close 只是 hide，回调仍能把完整回答写回原会话文件。
        if self._prep is not None and self._prep.isRunning():
            self._prep.cancel()
            self._prep.wait(3000)
        # 下载/切换线程不能留着窗口消失：QThread 没结束就析构会崩。
        # 先给用户一个明确选择，别默默把几 GB 下载掐掉。
        if self._switcher is not None and self._switcher.isRunning():
            if not ConfirmDialog.ask(
                    self, "模型还在下载",
                    "下载 / 加载还没完成。关闭窗口会中断它"
                    "（已下载的部分会保留，下次点同一模型会接着下）。\n\n确定中断并关闭吗？",
                    ok_text="中断并关闭", danger=True):
                event.ignore()
                return
            self._switcher.cancel()
            self._switcher.wait(6000)
        self._save_current()
        try:
            self._api.settings_set("session_id", self._session_id)
        except Exception:
            pass
        try:
            # 最大化时别把整屏尺寸写回，否则下次打开会变成"没最大化却占满屏"的怪窗口
            if not self.isMaximized():
                self._api.settings_set("win_w", self.width())
                self._api.settings_set("win_h", self.height())
            self._api.settings_set("sidebar_w", int(self.split.sizes()[0]))
        except Exception:
            pass
        if not self._manual_stopped and self.engine.running:
            _schedule_release(self.engine, int(self._cfg("idle_release", 300) or 300))
        else:
            _cancel_release()
        global _DLG
        _DLG = None
        super().closeEvent(event)


QSS = """
QDialog {
    background: #FFFFFF;
    /* 必须显式指定：不写的话控件继承 Windows 默认的 MS Shell Dlg 系，
       会落到 MS Sans Serif 这类 DirectWrite 渲染不了的旧点阵字体，
       控制台会一直刷 "CreateFontFaceFromHDC() failed"（无害但很吵）。 */
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
}
QWidget#head { background: transparent; }
QWidget#body, QScrollArea#scroll { background: transparent; }

/* 设计说明：按钮做长一点（左 padding 10→16、右 24→30 给 chevron 留位）、
   字号 11→13、radius 15 + 固定高 30 = 正圆角胶囊 */
QPushButton#modelBtn {
    font-size: 13px; color: #5F6570; background: #F2F6FD;
    border: none; border-radius: 15px; padding: 5px 30px 5px 16px;
}
QPushButton#modelBtn:hover { background: #E4EDFC; color: #2F6BEE; }

/* 新会话胶囊 / 深度思考的外观全部由 paintEvent 自绘（双向软投影 + 圆扩散），
   QSS 只负责不干扰：留 transparent / none，避免样式系统在自绘前再画一层。 */
QPushButton#deepBtn { background: transparent; border: none; padding: 0; }

QPushButton#ghostBtn {
    background: transparent; border: none; color: #5F6570;
    font-size: 12px; padding: 4px 8px; border-radius: 8px;
}
QPushButton#ghostBtn:hover { background: #E4EDFC; color: #2F6BEE; }
/* 顶栏「清空」：原型 .ai-mini 是白底 1px 细描边胶囊（不是无框幽灵按钮） */
/* 启停按钮与「文本翻译」插件同一套：胶囊 (32 高 / radius 16)。
   启动 = 蓝色实心主按钮；停止 = 白底蓝字 + 浅边框，hover 变红提示"会停掉进程"。 */
QPushButton#powerBtn {
    background: #2F6BEE; color: #FFFFFF; border: none; border-radius: 16px;
    padding: 6px 16px; font-size: 12px; font-weight: 600;
}
QPushButton#powerBtn:hover { background: #2359D6; }
QPushButton#powerBtn:disabled { background: #A9C1F5; color: #FFFFFF; }
/* 运行态给一层浅蓝底：和旁边白底的「模型已就绪」胶囊区分开，
   也不会像白底那样看着像"还没启用"（设计说明）。hover 仍变红。 */
QPushButton#powerBtn[on="true"] {
    background: #E8F0FE; color: #2F6BEE; border: 1px solid #BFD3FA; border-radius: 16px;
}
QPushButton#powerBtn[on="true"]:hover {
    background: #EC5F53; color: #FFFFFF; border-color: #EC5F53;
}
QPushButton#powerBtn[on="true"]:pressed {
    background: #D93A2F; color: #FFFFFF; border-color: #D93A2F;
}
QPushButton#powerBtn[on="true"]:disabled {
    color: #9AA6B8; background: #FFFFFF; border-color: #E3EBF6;
}

QWidget#head { border-bottom: 1px solid #D8E3F5; }
QLabel#headTitle { font-size: 13px; font-weight: 600; color: #161C28; }
QLabel#headSub { font-size: 11px; color: #5F6570; }
QWidget#engPill { background: transparent; border: 1px solid #D8E3F5; border-radius: 8px; }
QLabel#engText { font-size: 10.5px; color: #5F6570; }

QWidget#sidebar {
    background: rgba(255, 255, 255, 0.55); border-right: 1px solid #C3D3EE;
}
/* 可拖动的分隔条：平时透明（看到的还是 sidebar 那根 1px 细线），
   鼠标靠近才铺一层淡蓝，提示这里能左右拖 */
QSplitter#split { background: transparent; }
QSplitter#split::handle:horizontal { background: transparent; border: none; }
QSplitter#split::handle:horizontal:hover { background: rgba(47, 107, 238, 0.18); }
QPushButton#newBtn { background: transparent; border: none; padding: 0; }
QWidget#sessRow { background: transparent; border-radius: 8px; }
QWidget#sessRow:hover { background: #F2F6FD; }
QWidget#sessRowCur {
    background: #E4EDFC; border-radius: 8px;
}
QPushButton#sessBtn {
    background: transparent; border: none; text-align: left;
    font-size: 12px; color: #161C28; padding: 2px 0;
}
QWidget#sessRowCur QPushButton#sessBtn { color: #2359D6; font-weight: 600; }
QPushButton#sessBtn:hover { color: #2F6BEE; }
QPushButton#sessMore {
    background: transparent; border: none; color: #5F6570;
    font-size: 12px; border-radius: 11px;
}
QWidget#sessRow:hover QPushButton#sessMore,
QWidget#sessRowCur:hover QPushButton#sessMore { color: #5F6570; }
QPushButton#sessMore:hover { background: #E8EEFA; color: #2F6BEE; }
QPushButton#rowMenuItem, QPushButton#rowMenuItemDanger {
    background: transparent; border: none; text-align: left;
    font-size: 12px; padding: 7px 10px; border-radius: 6px;
}
QPushButton#rowMenuItem { color: #161C28; }
QPushButton#rowMenuItem:hover { background: #F2F5FB; color: #2F6BEE; }
QPushButton#rowMenuItemDanger { color: #EC5F53; }
QPushButton#rowMenuItemDanger:hover { background: #FDECEA; }
QFrame#rowMenuSep { background: #F0F2F6; border: none; }
QScrollArea#sessScroll { background: transparent; border: none; }
QWidget#sessInner { background: transparent; }
QLabel#sessEmpty {
    font-size: 11px; color: #5F6570; padding: 22px 6px; line-height: 170%;
}
QLabel#sessGroup {
    font-size: 11px; color: #5F6570; padding: 8px 8px 4px 8px; font-weight: 500;
}
QLabel#sessCur { color: #2F6BEE; font-size: 8px; }
QLabel#sessTitle { font-size: 12px; color: #161C28; }
QLabel#sessTitleCur { font-size: 12px; color: #2359D6; font-weight: 600; }
QLabel#sessDate { font-size: 9.5px; color: #5F6570; }
QLabel#sideStatus { font-size: 10.5px; color: #5F6570; line-height: 150%; }
QLabel#dotReady { background: #2FBF71; border-radius: 3px; }
QLabel#dotBusy { background: #F0A93B; border-radius: 3px; }
QLabel#dotError { background: #EC5F53; border-radius: 3px; }
QLabel#dotIdle { background: #C9CED8; border-radius: 3px; }

QLabel#emptyTitle { font-size: 22px; font-weight: 600; color: #161C28; }
QLabel#emptySub { font-size: 13px; color: #5F6570; }
QLabel#aiName { font-size: 13px; font-weight: 600; color: #161C28; }
QLabel#meta { font-size: 12px; color: #5F6570; }
QLabel#modelFoot { font-size: 11px; color: #5F6570; }
QPushButton#actBtn {
    background: transparent; border: none; color: #5F6570;
    font-size: 11.5px; padding: 1px 0;
}
QPushButton#actBtn:hover { color: #2F6BEE; }
QLabel#aiText { background: transparent; }
QTextEdit#thinkText {
    background: #F2F6FD;
    border: 1px solid #E8EDF6;
    border-radius: 8px;
    padding: 6px 9px;
    selection-background-color: #D6E4FF;
}
QPushButton#thinkBtn {
    text-align: left;
    border: none;
    background: transparent;
    color: #5F6570;
    font-size: 12px;
    padding: 2px 0;
}
QPushButton#thinkBtn:hover { color: #2F6BEE; }
/* 底部单一状态栏（原型 Claude 档 .ph-status：无上边框、10.5px 次要灰） */
QWidget#statusBar { background: transparent; }
QLabel#status { font-size: 10.5px; color: #5F6570; }

/* 与 UserBubble 自身 setStyleSheet 保持同值，避免父子样式表级联把气泡底色洗掉 */
QWidget#userBubble { background: #EDF3FF; border-radius: 12px; }
QLabel#userText { font-size: 13.5px; color: #161C28; background: transparent; }

QFrame#userAttachCard { background: #FFFFFF; border: 1px solid #D8E3F5; border-radius: 9px; }
QLabel#userAttachName { font-size: 12px; color: #161C28; background: transparent; }
QLabel#userAttachMeta { font-size: 11px; color: #5F6570; background: transparent; }

/* 建议 chip：淡蓝底胶囊（--side-bg）+ 细描边；hover 转白底主色边框/文字。
   11-11 空状态层次优化：比纯白底更柔和，和聊天气泡的 --user-bubble-bg 呼应。 */
QPushButton#chip {
    background: #F8FBFF; border: 1px solid #D8E3F5;
    border-radius: 16px; padding: 8px 16px; font-size: 11.5px; color: #38414F;
}
QPushButton#chip:hover {
    background: #E4EDFC; border: 1px solid #BFD3FA; color: #2F6BEE;
}

QFrame#card { background: #FFFFFF; border: 1px solid #D8E3F5; border-radius: 20px; }
QFrame#card[focus="true"] { border: 1px solid #BFD3FA; }

QTextEdit#input {
    background: transparent; border: none; padding: 6px 8px;
    font-size: 14px; color: #161C28; selection-background-color: #D6E4FF;
}

/* 发送 / 停止共用同一套蓝色渐变圆底；区别只在中间的自绘图形
   （SendButton 白色三角 / StopButton 白色圆角方块）。合并写，避免改一处漏一处。 */
QPushButton#send, QPushButton#stop {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #4A85FF, stop:1 #6E6BFF);
    border: none; border-radius: 17px;
}
QPushButton#send:hover, QPushButton#stop:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #3A76F5, stop:1 #5E5BF0);
}
QPushButton#send:disabled { background: #E9ECF2; color: #C2C8D2; }

QPushButton#toolBtn {
    background: transparent; border: none; border-radius: 14px;
    color: #5F6570; font-size: 17px;
}
QPushButton#toolBtn:hover { background: rgba(47,107,238,0.08); color: #2F6BEE; }

/* 气泡（PopCard）的样式统一在模块顶部的 _POPUP_QSS：PopCard 是对话框的**子控件**
   且自己调用了 setStyleSheet，Qt 只会匹配最近的祖先样式表 —— 主 QSS 里再写一份
   pop 规则永远不生效。之前这里确实多留了一整份重复（已删），改气泡配色请改
   _POPUP_QSS，别在这里加。 */

QFrame#panel { background: #FFFFFF; border: 1px solid #D8E3F5; border-radius: 12px; }
QLabel#panelTitle {
    font-size: 12.5px; color: #5F6570; font-weight: 600; padding-bottom: 2px;
}
QWidget#modelRow { background: transparent; border-radius: 8px; }
QWidget#modelRow:hover { background: #F2F6FD; }
QWidget#modelRowCur { background: #E4EDFC; border-radius: 8px; }
QWidget#modelRowMiss {
    background: transparent; border: 1px dashed #D8E3F5; border-radius: 8px;
}
QWidget#modelRowDl {
    background: #F2F6FD; border-left: 2px solid #2F6BEE; border-radius: 8px;
}
QWidget#modelRowMiss:hover, QWidget#modelRowDl:hover { background: #F2F6FD; }
QLabel#rowName { font-size: 12.5px; color: #161C28; }
QLabel#rowVram { font-size: 10.5px; color: #5F6570; }
QLabel#rowNameDim { font-size: 12px; color: #5F6570; }
QLabel#panelSection { font-size: 10.5px; color: #5F6570; padding: 4px 2px 0 2px; }
QLabel#rowTag {
    font-size: 9px; color: #2F6BEE; background: #E4EDFC;
    border-radius: 4px; padding: 1px 4px;
}
/* 模型状态徽章：当前 / 已下载 / 未下载 / 下载中（一眼看得出是否已下载） */
QLabel#mStateCur, QLabel#mStateOk, QLabel#mStateMiss, QLabel#mStateDl,
QLabel#dStateCur, QLabel#dStateOk, QLabel#dStateMiss, QLabel#dStateDl {
    font-size: 9.5px; border-radius: 4px; padding: 2px 5px;
}
QLabel#mStateCur, QLabel#dStateCur {
    background: #E4EDFC; color: #2F6BEE; font-weight: 600;
}
QLabel#mStateOk, QLabel#dStateOk { background: #F2F6FD; color: #5F6570; }
QLabel#mStateMiss, QLabel#dStateMiss {
    background: transparent; color: #5F6570; border: 1px dashed #C3D3EE;
}
QLabel#mStateDl, QLabel#dStateDl {
    background: #E4EDFC; color: #2F6BEE; font-weight: 600;
}
QPushButton#foldBtn {
    background: transparent; border: none; text-align: left;
    font-size: 11.5px; color: #5F6570; padding: 5px 8px; border-radius: 6px;
}
QPushButton#foldBtn:hover { background: #F2F6FD; color: #2F6BEE; }
QFrame#detailBox { background: #FFFFFF; border: 1px solid #D8E3F5; border-radius: 10px; }
QLabel#dName { font-size: 13px; font-weight: 600; color: #161C28; }
QLabel#dDesc { font-size: 11.5px; color: #5F6570; }
QLabel#dMeta { font-size: 10.5px; color: #5F6570; }
QLabel#ctxHint { font-size: 10px; color: #5F6570; }
QComboBox#ctxCombo {
    font-size: 11px; padding: 2px 6px; border: 1px solid #D8E3F5;
    border-radius: 6px; background: #FFFFFF; color: #161C28;
}
QComboBox#ctxCombo::drop-down { border: none; width: 16px; }
QComboBox#ctxCombo::down-arrow { image: none; width: 0; height: 0; }
QPushButton#primaryBtn {
    background: #2F6BEE; color: #FFFFFF; border: none;
    border-radius: 8px; padding: 7px; font-size: 12px;
}
QPushButton#primaryBtn:hover { background: #2359D6; }
QPushButton#primaryBtn:disabled, QPushButton#ghostBtn:disabled { color: #5F6570; }
QPushButton#dangerBtn {
    background: #FDECEA; color: #EC5F53; border: none;
    border-radius: 8px; padding: 7px; font-size: 12px;
}
QPushButton#dangerBtn:hover { background: #FBDDD9; }

QFrame#attachChip { background: #F2F6FD; border-radius: 9px; }
QLabel#attachName { font-size: 11.5px; color: #5F6570; background: transparent; }
QPushButton#attachDel {
    background: transparent; border: none; color: #5F6570;
    font-size: 12px; border-radius: 8px;
}
QPushButton#attachDel:hover { background: #E4E7EC; color: #EC5F53; }

QPushButton#copyBtn {
    background: transparent; border: none; color: #5F6570;
    font-size: 11px; padding: 2px 4px;
}
QPushButton#copyBtn:hover { color: #2F6BEE; }

QProgressBar { background: rgba(47,107,238,0.10); border: none; }
QProgressBar::chunk { background: #2F6BEE; }

QScrollBar:vertical { background: transparent; width: 4px; margin: 2px 0; }
QScrollBar::handle:vertical { background: #D8DDE6; border-radius: 2px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #C4CAD6; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
"""


# 单例：聊天窗口是"长期驻留"的页面，不是一次性弹窗
_DLG: "ChatDialog | None" = None


class Plugin(PluginBase):
    def on_load(self) -> None:
        _install_no_tooltip_filter()
        _dbg_print(f"[插件] 已加载: {self.ctx.name} v{self.ctx.manifest.version}", flush=True)
        # 启动 GC：回收历史遗留的孤儿图片（删对话 / 旧版本残留 / 异常退出留下的）。
        try:
            n, freed = ss.prune_previews()
            if n:
                print(f"[插件] 回收 {n} 张无引用的图片缓存（{freed / 1024:.0f} KB）",
                      flush=True)
        except Exception:
            pass

    def on_unload(self) -> None:
        _remove_no_tooltip_filter()
        global _DLG
        if _DLG is not None:
            try:
                _DLG.close()
            except Exception:
                pass
            _DLG = None
        eng.shutdown_all()

    def on_execute(self, query: str = "") -> None:
        """非模态打开：跨模块实例全局查重，绝不重复开新窗口。

        不能用 isinstance(w, ChatDialog)：插件模块被重复加载时，
        新模块里的 ChatDialog 是另一个类对象，匹配会失败。
        """
        global _DLG
        app = QApplication.instance()
        key = "lylaunchChatDialogInstance"
        # 主搜索框粘贴文件时把路径列表用换行拼进 query（插件 API 只有 str 通道）。
        # 只保留真实存在的路径，避免用户点插件瓦片时把搜索词当文件名附加。
        paths = [t for t in (p.strip().strip('"') for p in (query or "").splitlines())
                 if t and os.path.isfile(t)]

        def _feed(w) -> None:
            """新建或复用窗口都要把粘贴的文件附加进去，不能静默丢掉。"""
            if paths and hasattr(w, "_attach_paths"):
                try:
                    w._attach_paths(paths)
                except Exception:
                    pass

        def _reuse(w) -> bool:
            try:
                # 最小化后 show() 不会自动还原，必须 showNormal()；
                # 否则用户点了插件图标，窗口还缩在任务栏里像"打不开"
                if w.isMinimized():
                    w.showNormal()
                else:
                    w.show()
                w.raise_()
                w.activateWindow()
                return True
            except RuntimeError:
                return False

        # ① QApplication 上的动态属性（跨模块共享，最可靠）
        existing = app.property(key) if app is not None else None
        if existing is not None and _reuse(existing):
            _DLG = existing
            _feed(existing)
            return
        # ② 兜底：按 objectName 遍历顶层窗口
        for w in QApplication.topLevelWidgets():
            if w.objectName() == "lylaunchChatDialog" and _reuse(w):
                _DLG = w
                if app is not None:
                    app.setProperty(key, w)
                _feed(w)
                return

        _DLG = ChatDialog(self.ctx.api, self.ctx.settings)
        _DLG.setModal(False)
        _DLG.show()
        _DLG.raise_()
        _DLG.activateWindow()
        if app is not None:
            app.setProperty(key, _DLG)
        _feed(_DLG)
