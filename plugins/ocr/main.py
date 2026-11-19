"""图片转文字插件（LyLaunch 默认插件）。

用法：点插件瓦片 → 弹窗自动读取剪贴板里的截图（用设置里的截图快捷键截的图就在剪贴板，也可 Ctrl+V）→ 点「开始识别」
→ 结果列表展示，一键「复制全部」。也可点「选择图片」识别本地图片，或直接把图片拖进窗口。

引擎：RapidOCR（**rapidocr 3.x**，内置 PP-OCRv6 small）——PaddleOCR 模型转 ONNX，
     纯离线、模型随包内置（det 9.5MB + rec 20.3MB + cls 0.6MB）、中英文识别准确。
     CPU 推理，秒级出结果，不需要 GPU。
     ⚠️ 包名变迁：老的 `rapidocr-onnxruntime`（PP-OCRv3）官方已停止维护，
     本插件只支持新包 `rapidocr` 3.x（内置 PP-OCRv6），不再回退旧包。
     ⚠️ 依赖必须显式收集进打包（rapidocr 及其 onnxruntime/opencv/omegaconf/colorlog 等），
     并在 PyInstaller hiddenimports 里带上 `rapidocr`。
     模型随包走（collect_all 会连 models/ 一起收进 _MEIPASS），无需像翻译插件那样单独下载。

识别质量（v0.2 起）：
- **小图放大**：短边不足 1000px 的图（比如只截了一小块）会先放大再识别，
  小字识别率明显提升；超长边 > 2400px 会缩小，避免超清大图 det 阶段拖到几十秒。
- **置信度过滤**：`min_confidence`（plugin.json）真正生效，低于阈值的候选行直接丢掉，
  不再把引擎误判的噪声吐给用户（v0.1 这个配置是死的，写了不生效）。
- **阅读顺序还原**：按行聚类（行高 60% 容差）再按 x 排序，解决 PP-OCR 原始结果
  在多列/左中右排版时顺序错乱的问题；同一行的相邻片段按间距和中西文边界补空格。
- **送识别图与结果都可见**：预览缩略图 + 状态栏显示原图/送识别尺寸、行数、耗时。

能力边界：只通过 self.ctx.api 使用 clipboard / config / invoke_plugin；识别过程纯本地，不联网。
"""

from __future__ import annotations

import gc
import os
import re
import statistics
import tempfile
import time
import base64

from PySide6.QtCore import Qt, QThread, QTimer, Signal, QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QCloseEvent, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QLabel,
    QFileDialog,
    QComboBox,
)
from PySide6.QtWidgets import QApplication

from liytools.plugins.base import PluginBase
from liytools.plugins import theme
from liytools.plugins.plugin_icons import plugin_icon_svg, render_plugin_svg


def _dbg_print(*args, **kwargs):
    """调试输出已停用；保留调用以维持语句结构。"""
    pass


# 可选的第二引擎：复用「文档识别」已下载的 Xiaomi-OCR-0（本地大模型）。
# 单独成模块而不是 import doc_engine，是因为「文档识别」是可选插件，
# 没装时那个模块根本不存在；而且 doc_engine 是扁平模块名，容易和别的插件串味。
try:
    import ocr_model_engine as moe      # 同目录，manager 已把插件目录塞进 sys.path
except Exception as _e:                  # 缺文件 / 缺依赖都不该让插件打不开
    moe = None
    print(f"[OCR] 模型识别模块不可用：{type(_e).__name__}: {_e}", flush=True)


# 两个引擎的分工：
#   PP-OCRv6（rapidocr，随包 30MB）—— 默认。CPU、约 1 秒、纯文本行级识别，日常截图够用。
#   模型识别（Xiaomi-OCR-0）—— 可选。复用「文档识别」下好的权重，表格/公式/手写更准，
#   但每次打开要起一次进程（约 3~10 秒）、常驻约 1GB，所以只在用户主动选时才用，
#   而且窗口一关就停掉（见 OcrDialog._stop_model_engine）。

# 支持的图片后缀（剪贴板文件路径 / 拖入文件 / 文件选择器共用）
IMG_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff")

# 引擎下拉框右侧的小三角（10×7 灰）。
# 为什么要自带：theme.DIALOG_QSS 覆盖了 QComboBox::drop-down 子控件，默认箭头就没了；
# 而 QSS 又画不出三角（用 border 折腾出来的实测是个小方块）。内嵌 base64 而不是放
# 插件目录，是因为打包脚本只收 plugins/**/*.py，外部图片文件会被漏掉。
_ARROW_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAoAAAAHCAYAAAAxrNxjAAAACXBIWXMAAA7EAAAOxAGVKw4bAAAA"
    "lklEQVQYlX2MoQ6CUBiFv3ulYQcLm82ncPMl7hwUyAQadGjM5si3+hKMB7HYhG7F3+BwzDFOOufb"
    "t6MuV3sY35xZyUZzcwLPvT+eryOo07ImXeBtK22MGXFUCAwL1oCjQmPMqAHyNOkFFQEyvxJUlKdJ"
    "D6AnWmRxK1D+LCiLLG6nrWcP7HduBdKBdN++krqxft1Y/59/AIzLKyLJAXyuAAAAAElFTkSuQmCC")


def _shot_hotkey_text() -> str:
    """当前设置的截图快捷键文案（默认 Alt+A，用户可在设置里改）。

    每次开窗口读一次配置文件即可 —— 设置页改完立刻落盘，重开插件就是新值。
    """
    try:
        from liytools import config
        return config.load_screenshot_hotkey().display or "Alt+A"
    except Exception:
        return "Alt+A"


def _arrow_icon_path() -> str:
    """把小三角落盘到可写目录并返回正斜杠路径（QSS 的 url() 不认反斜杠）。"""
    try:
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        d = os.path.join(base, "LyLaunch", "ocr")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "arrow_down.png")
        if not os.path.isfile(p):
            with open(p, "wb") as f:
                f.write(base64.b64decode(_ARROW_PNG_B64))
        return p.replace("\\", "/")
    except Exception:
        return ""

# 送进引擎的尺寸区间：小于 _MIN_SIDE 放大（小字更准），大于 _MAX_SIDE 缩小（避免超慢）
_MIN_SIDE = 1000
_MAX_SIDE = 2400
_MAX_UPSCALE = 3.0


# ---------- 图像预处理 ----------
def _prepare(img: QImage, upscale: bool = True) -> QImage:
    """按尺寸区间缩放，并统一成 RGB888 存盘格式。

    为什么：OCR 的 det（文字检测）对分辨率敏感——截图只有 300×80 时模型几乎抓不到
    文本框；而 4K 截图走一遍检测要十几秒还不一定更准。缩放到 [1000, 2400] 这个区间
    是实测最划算的档位。
    """
    w, h = img.width(), img.height()
    longest = max(w, h)
    scale = 1.0
    if longest > _MAX_SIDE:
        scale = _MAX_SIDE / longest
    elif upscale and longest < _MIN_SIDE:
        scale = min(_MAX_UPSCALE, _MIN_SIDE / max(1, longest))
    if abs(scale - 1.0) > 0.01:
        img = img.scaled(
            max(1, round(w * scale)), max(1, round(h * scale)),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return img.convertToFormat(QImage.Format.Format_RGB888)


def _is_word_end(s: str) -> bool:
    return bool(s) and s[-1].isascii() and s[-1].isalnum()


def _is_word_start(s: str) -> bool:
    return bool(s) and s[0].isascii() and s[0].isalnum()


# 中日文字符范围（CJK 统一表意文字 + 日文假名）
_CJK = "\\u4e00-\\u9fff\\u3040-\\u30ff\\uac00-\\ud7af"
_RE_CJK2LATIN = re.compile(rf"([{_CJK}])([A-Za-z0-9])")
_RE_LATIN2CJK = re.compile(rf"([A-Za-z0-9])([{_CJK}])")


def _space_mixed(line: str) -> str:
    """在中西文/数字交界处补一个空格。

    PP-OCR 的识别头会把词间空格吃掉：`Hello World` 出来是 `HelloWorld`、
    `文字识别 OCR 测试` 出来是 `文字识别OCR测试`。直接贴在一起既难读，也影响
    后续翻译插件的分词。这里按中文排版惯例在「汉字 ↔ 字母/数字」之间补空格。
    """
    line = _RE_CJK2LATIN.sub(r"\1 \2", line)
    line = _RE_LATIN2CJK.sub(r"\1 \2", line)
    return line


def _assemble(items: list, min_conf: float, space_mixed: bool = True) -> tuple[str, int]:
    """把引擎原始结果整理成"人读得懂"的多行文本。

    items: [[box, text, score], ...]，box 是 4 个角点。
    1) 按置信度过滤；2) 按行聚类（行高 60% 容差）；3) 行内按 x 排序；
    4) 行内相邻片段：间距超过 0.8 个行高、或中西文/单词边界处补一个空格。
    """
    boxes: list[dict] = []
    for item in items:
        try:
            box, text, score = item[0], item[1], item[2]
        except Exception:
            continue
        text = (text or "").strip()
        if not text:
            continue
        try:
            if float(score) < min_conf:
                continue
        except Exception:
            pass
        try:
            ys = [float(p[1]) for p in box]
            xs = [float(p[0]) for p in box]
        except Exception:
            boxes.append({"top": 0.0, "bottom": 0.0, "left": 0.0, "right": 0.0,
                          "cy": 0.0, "text": text})
            continue
        boxes.append({
            "top": min(ys), "bottom": max(ys), "left": min(xs), "right": max(xs),
            "cy": sum(ys) / len(ys), "text": text,
        })
    if not boxes:
        return ("（未识别到文字）\n\n可以试试：\n"
                "· 截图时框大一点（太小的图会被放大，笔画糊掉）\n"
                "· 换一张更清晰、对比度更高的图\n"
                "· 图片里确实没有文字时就是这个结果", 0)

    heights = [b["bottom"] - b["top"] for b in boxes]
    med_h = statistics.median(heights) or 1.0
    tol = med_h * 0.6

    boxes.sort(key=lambda b: (b["cy"], b["left"]))
    lines: list[list[dict]] = []
    cur = [boxes[0]]
    cur_cy = boxes[0]["cy"]
    for b in boxes[1:]:
        if abs(b["cy"] - cur_cy) <= tol:
            cur.append(b)
            cur_cy = sum(x["cy"] for x in cur) / len(cur)
        else:
            lines.append(cur)
            cur = [b]
            cur_cy = b["cy"]
    lines.append(cur)

    out: list[str] = []
    for ln in lines:
        ln.sort(key=lambda b: b["left"])
        parts: list[str] = []
        for i, b in enumerate(ln):
            if i == 0:
                parts.append(b["text"])
                continue
            prev = ln[i - 1]
            gap = b["left"] - prev["right"]
            need_space = gap > med_h * 0.8 or (_is_word_end(prev["text"]) and _is_word_start(b["text"]))
            parts.append((" " if need_space else "") + b["text"])
        line = "".join(parts)
        out.append(_space_mixed(line) if space_mixed else line)
    return "\n".join(out), len(boxes)


def _normalize(res) -> list:
    """把 rapidocr 3.x 的 `RapidOCROutput` 转成 `[[box, text, score], ...]`。

    新版返回值不是元组而是对象：`.boxes`(numpy, 每项 4×2 角点) / `.txts` / `.scores`。
    ⚠️ 别对这些 numpy 字段用 `or []`——那会触发数组整体布尔求值，
    直接抛 "truth value of an array ... is ambiguous"。
    """
    _boxes = getattr(res, "boxes", None)
    boxes = list(_boxes) if _boxes is not None else []
    _txts = getattr(res, "txts", None)
    txts = list(_txts) if _txts is not None else []
    _scores = getattr(res, "scores", None)
    scores = list(_scores) if _scores is not None else []
    out = []
    for i, t in enumerate(txts):
        box = boxes[i] if i < len(boxes) and boxes[i] is not None else _FLAT_BOX
        sc = scores[i] if i < len(scores) else 1.0
        out.append([box, t, sc])
    return out


# box 缺失时的兜底（4 个角点全在原点）：_assemble 仍能出文本，只是没了位置信息
_FLAT_BOX = [(0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0)]


class OcrWorker(QThread):
    """后台识别线程，避免模型加载/识别卡住 UI。"""

    result_ready = Signal(str, int, float)   # 文本 / 行数 / 耗时秒
    error_occurred = Signal(str)

    def __init__(self, image: QImage, min_conf: float = 0.5,
                 upscale: bool = True, space_mixed: bool = True,
                 out_path: str = "") -> None:
        super().__init__()
        self._image = image
        self._min_conf = min_conf
        self._upscale = upscale
        self._space_mixed = space_mixed
        self._out_path = out_path or os.path.join(
            tempfile.gettempdir(), f"lylaunch_ocr_{os.getpid()}.png")

    def run(self) -> None:
        try:
            t0 = time.perf_counter()
            img = _prepare(self._image, self._upscale)
            img.save(self._out_path, "PNG")
            ocr = get_engine()
            text, n = _assemble(_normalize(ocr(self._out_path)),
                                self._min_conf, self._space_mixed)
            self.result_ready.emit(text, n, time.perf_counter() - t0)
        except Exception as e:
            self.error_occurred.emit(f"识别失败：{type(e).__name__}: {e}")

    def cancel(self) -> None:
        """和 ModelOcrWorker 保持同样的调用形状（本引擎一次 1 秒，没地方可中断）。"""
        return


class ModelOcrWorker(QThread):
    """模型识别线程：复用「文档识别」下好的 Xiaomi-OCR-0 跑一次多模态推理。

    信号契约与 OcrWorker 完全一致 (str, int, float)，所以 _on_result 两边通用。
    多一个 progress：起引擎可能要几十秒，状态行得一直更新，否则看着像卡死。

    第三个参数模型没有「文本框段数」这个量，这里传「非空行数」——
    同样是「识别到多少内容」的度量，0 就代表没认出字。
    """

    result_ready = Signal(str, int, float)
    error_occurred = Signal(str)
    progress = Signal(str)

    def __init__(self, image: QImage, prompt: str = "", max_tokens: int = 0,
                 model_id: str = "") -> None:
        super().__init__()
        self._image = image
        self._prompt = prompt
        self._max_tokens = max_tokens
        self._model_id = model_id or (moe.DEFAULT_MODEL_ID if moe else "")
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @staticmethod
    def _to_png(img: QImage) -> bytes:
        """按视觉 token 的经济区间缩放，再编码成 PNG 字节。

        1600 长边是准度/速度的平衡点：视觉 token 随像素面积涨，4K 原图既慢又
        吃上下文；小于 800 的小截图又会让模型看不清小字，最多放大 2 倍。
        """
        w, h = img.width(), img.height()
        long_edge = max(w, h)
        scale = 1.0
        if long_edge > moe.MAX_SIDE:
            scale = moe.MAX_SIDE / long_edge
        elif long_edge < moe.MIN_SIDE:
            scale = min(2.0, moe.MIN_SIDE / max(1, long_edge))
        if abs(scale - 1.0) > 0.01:
            img = img.scaled(
                max(1, round(w * scale)), max(1, round(h * scale)),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        buf.close()
        return bytes(ba)

    def run(self) -> None:
        if moe is None:
            self.error_occurred.emit("模型识别模块缺失，请重新安装本插件")
            return
        try:
            png = self._to_png(self._image)
            if self._cancelled:
                return
            eng = get_model_engine(self._model_id)   # 走 main 的包装，顺带刷新空闲计时
            if not eng.ready():
                self.progress.emit("正在启动本地模型…（通常几秒）")
                # 新版 Router 的 start 只收 progress(doc, pct)，包一层适配 Signal(str)
                eng.start(progress=lambda d, _p=None: self.progress.emit(str(d)))
            if self._cancelled:
                return
            self.progress.emit("模型识别中…（约 2~20 秒，视显卡而定）")
            t0 = time.perf_counter()
            # prompt / max_tokens 传 None 表示「用当前模型自己的训练参数」——
            # OvisOCR2 必须用它那条长英文 prompt，塞 Xiaomi 的会明显掉质量
            md, _ = eng.recognize(png, prompt=self._prompt or None,
                                  max_tokens=self._max_tokens or None)
            text = moe.md_to_text(md)
            if self._cancelled:
                return
            n = sum(1 for ln in text.splitlines() if ln.strip())
            self.result_ready.emit(text, n, time.perf_counter() - t0)
        except Exception as e:
            if not self._cancelled:
                # EngineError 的消息本来就是给人看的一句话，别糊上 traceback
                self.error_occurred.emit(str(e) if isinstance(e, moe.EngineError)
                                         else f"识别失败：{type(e).__name__}: {e}")


class OcrDialog(QDialog):
    def __init__(self, api, settings, parent=None, initial_image: str = "") -> None:
        super().__init__(parent)
        self._api = api
        self._settings = settings
        self._worker = None            # 当前识别线程（OcrWorker / ModelOcrWorker）
        self._cur_kind = "ppocr"       # 本次识别实际用的引擎（供 _on_result 出文案）
        self._fallback_note = ""       # 本次是否发生了引擎回退
        self._closing = False          # 关窗中：回调直接 return，避免踩已销毁控件
        self._image = QImage()
        self._sent_size = (0, 0)
        self.setWindowTitle("图片转文字")
        self.resize(660, 620)        # 原型定稿：OCR 660×620（原 580×520 是旧档位）
        self.setAcceptDrops(True)
        self._build_ui()
        self.setStyleSheet(theme.DIALOG_QSS)
        from liytools.plugins.window_chrome import install_chrome
        install_chrome(self, "图片转文字", show_minmax=True, resizable=True)

        # 空闲释放引擎的定时器必须在主线程建（后台线程建的不会触发）
        global _IDLE_SECONDS
        try:
            _IDLE_SECONDS = max(0, int(self._cfg("engine_idle_release", _IDLE_SECONDS)))
        except Exception:
            pass
        _start_release_timer()

        # 打开时的自动流程：优先用传入的图片（截图插件直传），否则读剪贴板
        if initial_image and os.path.isfile(initial_image):
            self._load_image(initial_image)
            if not self._image.isNull():
                self._on_ocr()
        elif self._cfg("auto_scan_clipboard", True):
            self._try_load_clipboard()
            if not self._image.isNull():
                self._on_ocr()

    # ---------- 配置读取（持久化配置优先，其次清单默认值）----------
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
        """弹窗布局：标题行 → 预览卡 → 工具行 → 结果区 → 状态行。

        设计取舍：
        - 原来顶部那句长提示（"支持：截图快捷键…拖进这个窗口"）压成一句副标题，
          把纵向空间让给预览和识别结果；
        - 原来 5 个按钮平铺一行、主次不分，现在按流程拆两处：工具行是「读取/选择 +
          开始识别（唯一主按钮）」，结果区标题右侧只放行内轻操作「复制 / 翻译」；
        - 图片尺寸、识别段数、耗时全部收进徽章和状态行，不再挤占正文。
        """
        self.resize(660, 620)
        self.setMinimumSize(560, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 16)
        root.setSpacing(12)

        # ① 图标头部（0.36 通用壳）：40px 线性 SVG 图标块 + 大标题 + 尺寸徽章
        head = QHBoxLayout()
        head.setSpacing(12)
        icon = QLabel()
        icon.setFixedSize(40, 40)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        icon.setStyleSheet(f"background:{theme.PLUGIN_BG}; border-radius:10px;")
        icon.setPixmap(render_plugin_svg(plugin_icon_svg("ocr"), 24))
        head.addWidget(icon)
        title = QLabel("图片转文字")
        title.setProperty("role", "title")
        head.addWidget(title)
        head.addStretch(1)
        self.size_badge = QLabel("本地识别")
        self.size_badge.setProperty("role", "badge")
        self.size_badge.setToolTip("识别在本机完成，图片不会上传")
        head.addWidget(self.size_badge)
        root.addLayout(head)

        self._shot_key = _shot_hotkey_text()
        sub = QLabel(f"{self._shot_key} 截图后可直接识别，也可以 Ctrl+V 粘贴、"
                     f"选择图片，或把图片拖进窗口")
        sub.setWordWrap(True)
        sub.setProperty("role", "subtitle")
        root.addWidget(sub)

        # ② 预览卡（拖入/粘贴都指向这里）
        self.preview = QLabel("把图片拖到这里，或点下方「选择图片」")
        self.preview.setFixedHeight(190)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setProperty("role", "preview")
        root.addWidget(self.preview)

        # ③ 工具行：次要操作在左，主操作靠右
        tool = QHBoxLayout()
        tool.setSpacing(8)
        # 引擎选择放最左：先选引擎 → 再选图 → 最后识别，跟操作顺序一致
        self.engine_box = QComboBox()
        self.engine_box.addItem("PP-OCR（快）", "ppocr")
        self.engine_box.addItem("Xiaomi-OCR-0（准）", "model")
        self.engine_box.addItem("OvisOCR2（版面）", "ovis")
        self.engine_box.setToolTip(
            "PP-OCR（快）：包内 PP-OCRv6，约 1 秒，日常截图够用（默认）\n"
            "模型识别（准）：复用「文档识别」的 Xiaomi-OCR-0，表格 / 公式 / 手写更准；\n"
            "              结果自动转成纯文本，关闭窗口会自动停掉本地模型（约省 1GB 内存）\n"
            "              注意：整页都是表格时，表格外的标题可能被漏掉，可切回 PP-OCR 核对")
        _arr = _arrow_icon_path()
        if _arr:
            # 只补箭头，其余外观（圆角/边框/内边距）继续吃主题的规则
            self.engine_box.setStyleSheet(
                "QComboBox::drop-down { border: none; width: 22px;"
                " subcontrol-origin: padding; subcontrol-position: center right;"
                " background: transparent; }"
                f"QComboBox::down-arrow {{ image: url({_arr});"
                " width: 10px; height: 7px; }")
        for _idx, _kind in ((1, "model"), (2, "ovis")):
            _ok = moe is not None and moe.is_available(_KIND_MODEL[_kind])
            if _ok:
                continue
            _it = self.engine_box.model().item(_idx)
            if _it is not None:
                _it.setEnabled(False)      # 置灰：鼠标点不动、键盘也跳过
            _why = (moe.missing_reason(_KIND_MODEL[_kind]) if moe is not None
                    else "模型识别模块缺失，请重新安装本插件")
            self.engine_box.setItemData(_idx, _why or "该模型暂不可用",
                                        Qt.ItemDataRole.ToolTipRole)
        self.engine_box.blockSignals(True)   # 初始化时别触发写配置
        self.engine_box.setCurrentIndex(
            _KIND_INDEX.get(_load_engine_choice(self._api, self._settings), 0))
        self.engine_box.blockSignals(False)
        self.engine_box.currentIndexChanged.connect(self._on_engine_changed)
        tool.addWidget(self.engine_box)
        self.scan_btn = QPushButton("读取剪贴板")
        self.scan_btn.setToolTip(
            f"读取剪贴板里的图片（{self._shot_key} 截图后点这里，或直接 Ctrl+V）")
        self.file_btn = QPushButton("选择图片…")
        tool.addWidget(self.scan_btn)
        tool.addWidget(self.file_btn)
        tool.addStretch(1)
        self.ocr_btn = QPushButton("开始识别")
        self.ocr_btn.setProperty("role", "primary")
        self.ocr_btn.setMinimumWidth(104)
        self.ocr_btn.setToolTip("识别当前图片里的文字")
        tool.addWidget(self.ocr_btn)
        root.addLayout(tool)

        # ④ 结果区：标题 + 行内轻操作
        out_head = QHBoxLayout()
        out_head.setSpacing(2)
        self.result_label = QLabel("识别结果")
        self.result_label.setProperty("role", "section")
        out_head.addWidget(self.result_label)
        out_head.addStretch(1)
        self.copy_btn = QPushButton("复制")
        self.copy_btn.setProperty("role", "ghost")
        self.copy_btn.setToolTip("复制全部识别结果")
        self.translate_btn = QPushButton("翻译")
        self.translate_btn.setProperty("role", "ghost")
        self.translate_btn.setToolTip("把识别结果交给「文本翻译」")
        out_head.addWidget(self.copy_btn)
        out_head.addWidget(self.translate_btn)
        root.addLayout(out_head)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("识别结果会显示在这里")
        self.output.setMinimumHeight(130)
        root.addWidget(self.output, 1)

        # ⑤ 状态行
        self.status = QLabel("等待图片…")
        self.status.setProperty("role", "status")
        root.addWidget(self.status)

        self.scan_btn.clicked.connect(self._try_load_clipboard)
        # Ctrl+V 直接粘贴剪贴板里的图片（窗口内任意焦点都生效）
        self._paste_sc = QShortcut(QKeySequence.StandardKey.Paste, self)
        self._paste_sc.setContext(Qt.ShortcutContext.WindowShortcut)
        self._paste_sc.activated.connect(self._try_load_clipboard)
        self.file_btn.clicked.connect(self._pick_file)
        self.ocr_btn.clicked.connect(self._on_ocr)
        self.copy_btn.clicked.connect(self._on_copy)
        self.translate_btn.clicked.connect(self._on_translate)

    def _set_status(self, msg: str) -> None:
        self.status.setText(msg)

    # ---------- 引擎切换 ----------
    def _on_engine_changed(self, _index: int) -> None:
        kind = self.engine_box.currentData() or "ppocr"
        # 置灰只挡鼠标/键盘，程序化 setCurrentIndex 能绕过去，所以这里再拦一次
        if kind in _KIND_MODEL and not (moe is not None
                                        and moe.is_available(_KIND_MODEL[kind])):
            self.engine_box.blockSignals(True)
            self.engine_box.setCurrentIndex(0)
            self.engine_box.blockSignals(False)
            self._set_status((moe.missing_reason(_KIND_MODEL[kind])
                              if moe is not None else "本地模型不可用，已回到 PP-OCR"))
            return
        try:
            self._api.settings_set("engine", kind)
        except Exception:
            pass
        is_model = kind in _KIND_MODEL
        btn = getattr(self, "ocr_btn", None)      # 构造期可能还没建出来
        if btn is not None:
            btn.setToolTip("用本地大模型识别（更准，首次要等模型启动）" if is_model
                           else "识别当前图片里的文字")
        if is_model:
            mid = _KIND_MODEL[kind]
            ready = moe.get_engine(mid).ready() if moe is not None else False
            name = moe.spec_of(mid).name if moe is not None else "模型"
            self._set_status(f"已切换到 {name} · " + (
                "模型已启动" if ready else "首次识别会启动模型（约 3~10 秒）"))
        else:
            self._set_status("已切换到 PP-OCR（包内模型，约 1 秒）")

    # ---------- 载入图片 ----------
    def _image_from_clipboard(self) -> QImage:
        """剪贴板取图：优先文件/图片数据。

        ① 复制了图片文件（资源管理器里 Ctrl+C）→ mimeData 里有 urls，直接读文件（无损、不失真）
        ② 用截图快捷键截的图 → clipboard().image()
        ③ 复制的是图片绝对路径文本（少数工具会这样）→ 也认
        """
        cb = QApplication.clipboard()
        md = cb.mimeData()
        try:
            if md.hasUrls():
                for u in md.urls():
                    p = u.toLocalFile()
                    if p and os.path.isfile(p) and p.lower().endswith(IMG_EXTS):
                        img = QImage(p)
                        if not img.isNull():
                            return img
        except Exception:
            pass
        img = cb.image()
        if not img.isNull():
            return img
        try:
            txt = (cb.text() or "").strip().strip('"')
            if txt and os.path.isfile(txt) and txt.lower().endswith(IMG_EXTS):
                img = QImage(txt)
                if not img.isNull():
                    return img
        except Exception:
            pass
        return QImage()

    def _try_load_clipboard(self) -> None:
        img = self._image_from_clipboard()
        if img.isNull():
            self._image = QImage()
            self.preview.setText(
                f"剪贴板里没有图片 · 试试 {self._shot_key}、Ctrl+V 或「选择图片」")
            self.size_badge.setText("本地识别")
            self._set_status("剪贴板里没有图片")
            return
        self._set_image(img, "剪贴板图片")

    def _pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "",
            "图片 (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff);;所有文件 (*)",
        )
        if path:
            self._load_image(path)

    def _load_image(self, path: str) -> None:
        img = QImage(path)
        if img.isNull():
            self.preview.setText(f"这张图读不出来 · {os.path.basename(path)}")
            self.size_badge.setText("本地识别")
            self._set_status("图片读取失败")
            return
        self._set_image(img, os.path.basename(path))

    def _set_image(self, img: QImage, source: str) -> None:
        self._image = img
        # 预览按卡片可用宽度（约 616 逻辑像素）缩放，比原来 500×148 更清楚
        pm = QPixmap.fromImage(img).scaled(
            596, 180, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.preview.setPixmap(pm)
        w, h = img.width(), img.height()
        self.size_badge.setText(f"{w} × {h}")
        longest = max(w, h)
        if longest > _MAX_SIDE:
            scale = _MAX_SIDE / longest
        elif longest < _MIN_SIDE and self._cfg("upscale_small", True):
            scale = min(_MAX_UPSCALE, _MIN_SIDE / max(1, longest))
        else:
            scale = 1.0
        self._sent_size = (round(w * scale), round(h * scale))
        hint = f"原图 {w}×{h}"
        if abs(scale - 1.0) > 0.01:
            hint += f" → 送识别 {self._sent_size[0]}×{self._sent_size[1]}（{'放大' if scale > 1 else '缩小'}）"
        self._set_status(f"{source} · {hint}")

    def _clear_image(self) -> None:
        """移除当前图片，回到空态（识别结果一起清掉，免得图和结果对不上）。"""
        self._image = QImage()
        self._sent_size = (0, 0)
        self.preview.setPixmap(QPixmap())       # 先去掉图，再写回空态文字
        self.preview.setText(
            f"剪贴板里没有图片 · 试试 {self._shot_key}、Ctrl+V 或「选择图片」")
        self.size_badge.setText("本地识别")
        self.output.clear()
        self._set_status("已移除当前图片")

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """有图时 Esc = 移除这张图，而不是直接关掉面板。

        识别结果可能还没保存/复制，误按 Esc 关窗容易白干一场；
        没图的时候 Esc 仍按默认（关闭窗口）。
        """
        if event.key() == Qt.Key.Key_Escape and not self._image.isNull():
            self._clear_image()
            event.accept()
            return
        super().keyPressEvent(event)

    # ---------- 拖入图片 ----------
    def dragEnterEvent(self, event) -> None:
        try:
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
                return
        except Exception:
            pass
        super().dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        try:
            for u in event.mimeData().urls():
                p = u.toLocalFile()
                if p and os.path.isfile(p) and p.lower().endswith(IMG_EXTS):
                    self._load_image(p)
                    if not self._image.isNull():
                        self._on_ocr()
                    return
        except Exception:
            pass
        super().dropEvent(event)

    # ---------- 识别 ----------
    def _on_ocr(self) -> None:
        if self._image.isNull():
            self._set_status("请先加载一张图片：截图、选择文件，或直接拖进来")
            return
        # 识别中还能点「读取剪贴板 / 选择图片」，一覆盖 self._worker 旧线程就没人管了
        # （模型识别要几十秒，比 PP-OCR 更容易撞上）
        if self._worker is not None and self._worker.isRunning():
            self._set_status("上一张还在识别，请稍等…")
            return

        kind = self.engine_box.currentData() or "ppocr"
        self._fallback_note = ""
        if kind in _KIND_MODEL and not (moe is not None
                                        and moe.is_available(_KIND_MODEL[kind])):
            kind = "ppocr"                  # 模型文件没了 / 模块缺失 → 本次静默回退
            self._fallback_note = "（模型不可用，这次用 PP-OCR）"
        self._cur_kind = kind               # 整个流程内不变，供 _on_result 出文案

        self.ocr_btn.setEnabled(False)
        self.copy_btn.setEnabled(False)
        self.translate_btn.setEnabled(False)
        self.ocr_btn.setText("识别中…")
        self.output.clear()

        if kind in _KIND_MODEL:
            self._set_status("正在准备模型识别…" + self._fallback_note)
            self._worker = ModelOcrWorker(self._image,
                                          model_id=_KIND_MODEL[kind])
            self._worker.progress.connect(self._set_status)
        else:
            self._set_status("识别中…（首次使用要加载模型，约 1 秒）" + self._fallback_note)
            try:
                import rapidocr  # noqa: F401  切引擎后才可能发现依赖缺失
            except Exception as e:
                self.output.setPlainText(
                    "OCR 模型依赖未安装：\n\n"
                    f"{type(e).__name__}: {e}\n\n"
                    "请在终端运行：python -m pip install rapidocr onnxruntime opencv-python\n"
                    "或改用「模型识别」（需先在「文档识别」里下载模型）")
                self._set_status("缺少 PP-OCR 依赖，原因见结果区")
                self.ocr_btn.setEnabled(True)
                self.ocr_btn.setText("开始识别")
                self.copy_btn.setEnabled(True)
                self.translate_btn.setEnabled(True)
                return
            self._worker = OcrWorker(
                self._image,
                min_conf=float(self._cfg("min_confidence", 0.5) or 0.5),
                upscale=bool(self._cfg("upscale_small", True)),
                space_mixed=bool(self._cfg("auto_space_mixed", True)),
            )

        self._worker.result_ready.connect(self._on_result)
        self._worker.error_occurred.connect(self._on_error)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_finished(self) -> None:
        if self._closing:
            return
        self.ocr_btn.setEnabled(True)
        self.ocr_btn.setText("开始识别")
        self.copy_btn.setEnabled(True)
        self.translate_btn.setEnabled(True)
        # 丢掉已结束的线程对象，避免它（以及它引用过的引擎）拖着不释放
        self._worker = None

    def _on_result(self, text: str, n: int, seconds: float) -> None:
        if self._closing or self._image.isNull():
            return          # 图已经被 Esc 移除了，这次结果没有对应的图，直接丢

        model = self._cur_kind in _KIND_MODEL
        self.output.setPlainText(text)
        base = ""
        # _sent_size 是按 PP-OCR 的缩放规则算的，模型走自己那套（800~1600），
        # 混着用会报出一个假尺寸，所以模型分支直接显示原图尺寸
        if model:
            if not self._image.isNull():
                base = f"原图 {self._image.width()}×{self._image.height()} · "
        elif self._sent_size[0]:
            base = f"送识别 {self._sent_size[0]}×{self._sent_size[1]} · "
        if model and moe is not None:
            eng = f" · 模型 {moe.spec_of(_KIND_MODEL[self._cur_kind]).name}"
        else:
            eng = " · 模型 PP-OCRv6"
        unit = "行" if model else "段文字"
        if n:
            chars = len(text.replace("\n", "").replace("\t", "").replace(" ", ""))
            self.result_label.setText(f"识别结果 · {chars} 字")
            self._set_status(f"识别完成 · {base}{n} {unit} · 耗时 {seconds:.2f} 秒{eng}"
                             + self._fallback_note)
        else:
            self.result_label.setText("识别结果")
            self._set_status(f"未识别到文字 · {base}耗时 {seconds:.2f} 秒{eng}"
                             + self._fallback_note)

    def _on_error(self, msg: str) -> None:
        if self._closing:
            return
        self.output.setPlainText(msg)
        self._set_status("识别出错，原因见识别结果区")

    def _on_copy(self) -> None:
        text = self.output.toPlainText().strip()
        if not text or "未识别到" in text or "请先加载" in text or "识别中" in text:
            self._set_status("还没有可复制的识别结果")
            return
        self._api.clipboard_write(text)
        self._set_status(f"已复制 {len(text)} 个字符到剪贴板")

    def _on_translate(self) -> None:
        """把识别文本交给翻译插件（经 invoke_plugin 路由，插件间不直接 import）。"""
        text = self.output.toPlainText().strip()
        if not text or "未识别到" in text or "请先加载" in text or "识别中" in text:
            self._set_status("先完成识别，再点「翻译」")
            return
        self._api.invoke_plugin("translator", text)
        self.close()

    # PP-OCR 一次 1.3 秒，5 秒足够；模型识别可能几十秒，绝不能无超时干等
    # （旧版这里是无参 wait()，识别中点关闭会把主程序冻住直到识别跑完）
    _CLOSE_WAIT_MS = 5000

    def _stop_model_engine(self) -> None:
        """关窗就把模型引擎停掉。

        为什么要它：模型识别是独立进程，常驻约 1GB。原来只靠空闲计时器回收，
        而阈值 300 秒 + 每分钟才检查一次 ⇒ 实际要挂 5~6 分钟，用户看到的就是
        「窗口早关了，任务管理器里还压着 1GB」，体验上等于内存泄漏。
        实测引擎启动只要 2~3 秒，重开时再起一次完全划算。
        想用内存换速度的，可以把配置里 keep_model_engine 改成 true。
        """
        if moe is None:
            return
        try:
            if self._cfg("keep_model_engine", False):
                return
        except Exception:
            pass
        global _MODEL_LAST_USE
        try:
            was_running = moe.is_running()
            moe.release()
            _MODEL_LAST_USE = 0.0
            if was_running:
                print("[OCR] 关窗，已停止本地模型（释放约 1GB）", flush=True)
        except Exception:
            pass

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        w, self._worker = self._worker, None
        if w is not None and w.isRunning():
            try:
                w.cancel()
            except AttributeError:
                pass
            if not w.wait(self._CLOSE_WAIT_MS):
                try:
                    w.terminate()      # 兜底：线程卡在网络/模型里，只能强杀
                    w.wait(1500)
                except Exception:
                    pass
        # 线程彻底结束之后再停引擎，否则它可能还在读模型
        self._stop_model_engine()
        try:  # 清掉预处理暂存图
            p = os.path.join(tempfile.gettempdir(), f"lylaunch_ocr_{os.getpid()}.png")
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
        super().closeEvent(event)


# 进程内缓存的 OCR 引擎（懒加载）
_ENGINE = None
# ── 空闲释放 ────────────────────────────────────────────────────────────
# 实测：RapidOCR 引擎（onnxruntime session + cv2）初始化后常驻约 +73MB，
# 而且一旦加载就再也不放。对常驻托盘的启动器来说，用过一次就永久多占 73MB
# 不划算——所以空闲 _IDLE_SECONDS 秒后主动丢掉，下次用再花 0.6 秒重建。
# 模型引擎更狠：它是独立进程，常驻约 1GB，空闲必须真停，只丢 Python 对象没用。
_IDLE_SECONDS = 300          # 默认 5 分钟；0 或负数 = 不释放
# 模型引擎单独封顶：它是约 1GB 的独立进程，而重启只要 2~3 秒，
# 跟 PP-OCR（70MB、重建 0.6 秒）共用 300 秒完全不划算。
_MODEL_IDLE_SECONDS = 60
_LAST_USE = 0.0
_MODEL_LAST_USE = 0.0
_RELEASE_TIMER: QTimer | None = None

# 引擎选择的取值迁移表。
# "enhanced" 是上一版「增强识别」留下的脏值——那版删了，但用户配置里还写着，
# 不认它的话每次开窗都会报错。现在它对应到「模型识别」。
# 引擎选项 → 实际模型 id。两个模型共用同一份引擎与运行时，只是权重不同，
# 所以 get_engine 的 key 里带 model_id（否则切模型拿回来的还是旧权重）。
_KIND_MODEL = {"model": "xiaomi-ocr-0", "ovis": "ovisocr2"}
_KIND_INDEX = {"ppocr": 0, "model": 1, "ovis": 2}

_ENGINE_ALIAS = {"ppocr": "ppocr", "fast": "ppocr",
                 "model": "model", "enhanced": "model",
                 "ovis": "ovis", "ovisocr2": "ovis", "oviscr2": "ovis"}


def _load_engine_choice(api, settings) -> str:
    """读引擎选择并做一次迁移，保证返回值一定合法且可用。

    三种来源依次兜底：持久化配置 → 清单默认值 → "ppocr"。
    脏值（含旧的 enhanced）和「模型其实没装」这两种情况都会顺手写回，
    这样用户下次开窗不会再撞同一堵墙。
    """
    raw = None
    try:
        raw = api.settings_get("engine", None)
    except Exception:
        pass
    if raw is None:
        try:
            raw = settings.get("engine", "ppocr")
        except Exception:
            raw = "ppocr"
    key = str(raw).strip().lower()
    val = _ENGINE_ALIAS.get(key, "ppocr")
    if val in _KIND_MODEL and not (moe is not None
                                   and moe.is_available(_KIND_MODEL[val])):
        val = "ppocr"                      # 模型缺失 → 静默回退，绝不卡界面
    if key != val:
        try:
            api.settings_set("engine", val)
        except Exception:
            pass
    return val


def _in_main_thread() -> bool:
    """QTimer 属于创建它的线程；OcrWorker 是后台线程且没跑事件循环，
    在它里面建的定时器永远不会触发，所以定时器只能在主线程建。"""
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        return app is None or QThread.currentThread() == app.thread()
    except Exception:
        return False


def _start_release_timer() -> None:
    global _RELEASE_TIMER
    if _RELEASE_TIMER is not None or not _in_main_thread():
        return
    try:
        t = QTimer()
        t.setInterval(60_000)          # 每分钟检查一次，够用且开销可忽略
        t.timeout.connect(_release_if_idle)
        t.start()
        _RELEASE_TIMER = t
    except Exception:
        _RELEASE_TIMER = None


def _release_if_idle() -> None:
    """两个引擎各按自己的空闲时长释放。互不牵连。

    关窗时已经会把模型引擎停掉（OcrDialog._stop_model_engine），这里主要兜住
    「窗口一直开着但没在用」的情况。
    """
    global _ENGINE, _MODEL_LAST_USE
    if _IDLE_SECONDS <= 0:
        return
    now = time.monotonic()
    if _ENGINE is not None and now - _LAST_USE >= _IDLE_SECONDS:
        _ENGINE = None
        gc.collect()
        print(f"[OCR] PP-OCR 空闲 {_IDLE_SECONDS} 秒，已释放（约 70MB）", flush=True)
    model_idle = min(_IDLE_SECONDS, _MODEL_IDLE_SECONDS)
    if _MODEL_LAST_USE > 0 and now - _MODEL_LAST_USE >= model_idle:
        if moe is not None:
            try:
                moe.release()      # 真停 llama-server 进程，光丢对象不掉内存
                print(f"[OCR] 本地模型空闲 {model_idle} 秒，已停进程（约 1GB）",
                      flush=True)
            except Exception:
                pass
        _MODEL_LAST_USE = 0.0


def release_engine() -> None:
    """立即释放两个引擎（供外部调用，如设置里手动清理 / 程序退出前）。"""
    global _ENGINE, _MODEL_LAST_USE
    _ENGINE = None
    gc.collect()
    if moe is not None:
        try:
            moe.release()
        except Exception:
            pass
    _MODEL_LAST_USE = 0.0


def get_engine():
    """获取 OCR 引擎（rapidocr 3.x / PP-OCRv6），懒加载并进程内缓存。

    为什么换包：`rapidocr-onnxruntime` 官方已停止维护，新库名叫 `rapidocr`。
    实测（同一组图）平均置信度 0.814 → 0.980，小字号和低对比度改善尤其明显。
    """
    global _ENGINE, _LAST_USE
    _LAST_USE = time.monotonic()
    if _ENGINE is None:
        import logging
        # rapidocr 3.x 默认打 INFO 日志（每步都打印模型路径），开发模式下刷屏
        logging.getLogger("RapidOCR").setLevel(logging.WARNING)
        from rapidocr import RapidOCR
        _ENGINE = RapidOCR()
    return _ENGINE


def get_model_engine(model_id: str = ""):
    """模型识别引擎（llama-server 子进程），model_id 空则用默认模型。

    走这层包装只为一件事：每次取用都刷新空闲计时，别让用户正在用的引擎被
    空闲回收悄悄停掉。模块缺失时抛 RuntimeError，由调用方（worker）翻译成提示。
    """
    global _MODEL_LAST_USE
    if moe is None:
        raise RuntimeError("模型识别模块缺失，请重新安装本插件")
    _MODEL_LAST_USE = time.monotonic()
    return moe.get_engine(model_id or None)


_DLG = None   # 非模态单例：打开后仍能用主程序；关掉即销毁，下次重开


def _open_nonmodal(cls, api, settings, **kw) -> None:
    """非模态打开插件窗口：不再 exec() 阻塞主程序；重复打开只把已有窗口提到前台。"""
    global _DLG
    if _DLG is not None:
        try:
            # 截图插件又传了新图：复用窗口但重新加载（保持"打开即识别"的旧行为）
            img = kw.get("initial_image")
            if img and os.path.isfile(img) and hasattr(_DLG, "_load_image"):
                _DLG._load_image(img)
                if hasattr(_DLG, "_on_ocr"):
                    _DLG._on_ocr()
            _DLG.raise_()
            _DLG.activateWindow()
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
        _dbg_print(f"[插件] 已加载: {self.ctx.name} v{self.ctx.manifest.version}", flush=True)

    def on_execute(self, query: str = "") -> None:
        # 依赖预检：缺失时弹明确提示并安全返回，不抛异常、不静默退出。
        # 注意只在用 PP-OCR 时检查——选了模型识别就完全不碰 rapidocr。
        if _load_engine_choice(self.ctx.api, self.ctx.settings) != "model":
            try:
                import rapidocr  # noqa: F401  仅校验依赖是否可用
            except Exception as e:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.information(
                    None, "图片转文字",
                    "OCR 模型依赖未安装，插件无法运行：\n\n"
                    f"{type(e).__name__}: {e}\n\n"
                    "请在终端运行以下命令安装：\n"
                    "python -m pip install rapidocr onnxruntime opencv-python\n\n"
                    "或者在模型下拉框里改用「模型识别」"
                    "（需先在「文档识别」插件里下载模型）。",
                )
                return
        # query 可以是别的插件（截图）直接传过来的图片路径
        path = (query or "").strip().strip('"')
        if not (path and os.path.isfile(path)):
            path = ""
        _open_nonmodal(OcrDialog, self.ctx.api, self.ctx.settings, initial_image=path)
