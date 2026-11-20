"""文本翻译插件（LyLaunch 第一个默认插件）。

用法：点插件瓦片 → 弹窗自动预填当前剪贴板文字 → 点「翻译」→ 显示结果 → 「复制结果」写入剪贴板。
方向：默认按**主导语言**自动判断（中 → 英 / 英 → 中），也可在操作条手动指定方向。
  自动判定不再「出现一个汉字就算中文」：英文段落里夹一两个汉字不会整段被判成中译英。

后端（见 plugin.json 的 Config.backend，可选项：offline / hy-mt2 / hy-mt2-7b；
  默认 = hy-mt2：本地大模型，离线、免 key、隐私，且**首次打开不加载重型依赖**，开得最快）：
  - "offline" : 完全离线、免 key、隐私。直接用 ctranslate2 + sentencepiece 加载 Argos 开源模型
               （本质上就是把 Argos 当 Python 库调用，不依赖 argos 包本身）。
               依赖：python -m pip install ctranslate2 sentencepiece
               模型（en_zh / zh_en，各约 70MB）按 model_search_dirs() 的顺序查找：
                 exe同目录 models/ → 包内插件 models/ → 仓库根 models/ → 用户目录
               打包发布时用 tools/fetch_translator_models.py 预下载到 models/，由安装包投放到
               exe 同目录 → 装完即可断网翻译，不必首次联网。都没找到时会**先询问用户**
               （约 145MB）再下载，下载过程有进度条。
  - "hy-mt2" / "hy-mt2-7b" : 【可选】本地大模型翻译，via llama.cpp。
               · Hy-MT2 1.8B（腾讯混元，Apache-2.0，可商用）：翻译专用、质量高，约 1.1GB。
               · Hy-MT2 7B（腾讯混元，Apache-2.0，可商用）：长句/专业内容最稳，约 4.6GB。
               都纯本地运行、不联网、隐私好；无需 pip 包 —— 推理运行时（llama.cpp）
               随主程序内置，模型首次使用时由 model_engine.py 自动下载（带进度 + SHA-256 校验），只需一次。
               进程常驻、跨窗口复用；关闭翻译窗口后空闲一段时间自动释放内存。
               0.39.1 起三个插件共用全局 Router（端口 18090），不再各起 llama-server。

2026-10-25：原 "youdao" 后端与独立的「在线翻译（有道）」插件已整体移除（用户决定不再需要），
本插件不再保留任何有道相关代码与配置项。

所有重型依赖都懒加载（用到才 import），缺哪个后端只影响那个后端，不会让整个插件崩。

能力边界：只通过 self.ctx.api 使用 clipboard / config / network，不 import 主程序其它模块——与基础功能隔离。
"""

from __future__ import annotations

import os
import gc
import sys
import time
import zipfile

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QComboBox,
    QMenu,
    QFrame,
    QSizePolicy,
    QApplication,
    QWidget,
    QProgressBar,
)
from PySide6.QtCore import Qt, QEvent, QEventLoop, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut

from lylaunch.plugins.base import PluginBase
from lylaunch.plugins import theme
from lylaunch.plugins.plugin_icons import plugin_icon_svg, render_plugin_svg

import model_engine   # 本地大模型翻译引擎（Hy-MT2 via llama.cpp）— 同目录模块，插件加载时已加入 sys.path


def _dbg_print(*args, **kwargs):
    """调试输出已停用；保留调用以维持语句结构。"""
    pass



# 翻译方向：auto = 按主导语言自动判断；另两个是手动指定
DIR_AUTO = "auto"
DIR_ZH_EN = "zh_en"
DIR_EN_ZH = "en_zh"


def _lang_counts(text: str) -> tuple[int, int]:
    """数出汉字数与拉丁字母数（标点 / 数字 / 空白不计入分母）。"""
    cjk = 0
    latin = 0
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            cjk += 1
        elif ch.isascii() and ch.isalpha():
            latin += 1
    return cjk, latin


def _has_non_chinese_script(text: str) -> bool:
    """检测是否含日语假名 / 韩语谚文等「非中文」表意文字——这些一律译中。

    没有这一步时，日语汉字会被 _lang_counts 当成「汉字」→ 误判为中文 → 译英，
    正是「翻译外语却翻成英文」的根因。
    """
    for ch in text:
        o = ord(ch)
        # 平假名 3040-309F、片假名 30A0-30FF、谚文音节 AC00-D7A3、谚文字母 1100-11FF
        if (0x3040 <= o <= 0x30FF) or (0xAC00 <= o <= 0xD7A3) or (0x1100 <= o <= 0x11FF):
            return True
    return False


def _detect_direction(text: str) -> str:
    """自动判定翻译方向（严格中英互译）：

      源是中文 → 译英（zh_en）；源是**任何其他语言**（英 / 日 / 韩 / 法 / 俄…）→ 译中（en_zh）。
      即「自动识别」= 外语统一翻成中文，只有中文才翻成英文。

      修 0.37 的两个 bug：① 英文夹一个汉字被判中译英；② 日语汉字被当成中文误译英。
      现在：含日语假名 / 韩语谚文 ⇒ 直接判为非中文 → 译中；其余按中文主导度判断。
    """
    if _has_non_chinese_script(text):
        return DIR_EN_ZH
    cjk, latin = _lang_counts(text)
    if cjk == 0:
        return DIR_EN_ZH            # 英文 / 纯数字符号 / 其他无汉字语言 → 译中
    if latin == 0:
        return DIR_ZH_EN            # 纯汉字（无拉丁、无假名谚文） → 译英
    if cjk >= 2 and cjk / (cjk + latin) >= 0.30:
        return DIR_ZH_EN            # 中文为主 → 译英
    return DIR_EN_ZH                # 英文为主夹少量汉字 → 译中


# 可选语言（下拉顺序）；source 额外支持「自动检测」= DIR_AUTO。
LANGUAGES = [
    ("zh", "中文"),
    ("en", "英语"),
    ("ja", "日语"),
    ("ko", "韩语"),
    ("de", "德语"),
    ("fr", "法语"),
    ("es", "西班牙语"),
]
LANG_NAME = dict(LANGUAGES)
# 离线（Argos）只备了中英两对模型；其它语言只能用本地大模型。
OFFLINE_LANGS = ("zh", "en")


def _detected_lang(text: str) -> str:
    """按字符构成粗判源语言：中文占多数 → zh，否则 en（离线只认这两种）。"""
    return "zh" if _detect_direction(text) == DIR_ZH_EN else "en"


def _effective_target(text: str, src: str, tgt: str) -> str:
    """真正要翻成的目标语言。

    **就是用户选的那个**（0.39.5 修）：旧实现会在「自动检测 + 检测结果=目标语言」时
    自动把目标换成另一种语言（中文原文 + 目标中文 → 翻成英文），用户看到的就是
    「我要中文，怎么给我英文」。语言相同时该直出原文，由 `_same_language()` 在入口拦，
    不该靠偷改目标语言来"避免无效翻译"。
    """
    return tgt


def _same_language(text: str, src: str, tgt: str) -> bool:
    """源和目标是否同一种语言（自动检测时按原文判）。"""
    s = src if src != DIR_AUTO else _detected_lang(text)
    return s == tgt


def _offline_pair(text: str, src: str, tgt: str) -> str:
    """离线（Argos）只支持中英互译；返回可用的 langpair，不可用返回空串。"""
    t = _effective_target(text, src, tgt)
    s = src if src != DIR_AUTO else _detected_lang(text)
    if s == t or s not in OFFLINE_LANGS or t not in OFFLINE_LANGS:
        return ""
    return f"{s}_{t}"


def _translate_preserving_lines(translate_one, text: str) -> str:
    """逐行翻译，原样保留换行与空行（模型 / 离线都靠它保证格式不丢）。

    整段一次性翻译时，模型常把多行合并或重排换行 → 「换行这种基本格式」丢失。
    逐行翻译后按原行结构拼回，空行原样保留，行数对得上。
    """
    lines = text.split("\n")
    out = []
    for ln in lines:
        if ln.strip() == "":
            out.append(ln)              # 空行原样保留
        else:
            out.append(translate_one(ln))
    return "\n".join(out)


# Argos 官方模型（R2 CDN，已实测存在）。key = "源_目标" 语言对。
ARGOS_MODELS = {
    "en_zh": "https://pub-dbae765fb25a4114aac1c88b90e94178.r2.dev/v1/translate-en_zh-1_9.argosmodel",
    "zh_en": "https://pub-dbae765fb25a4114aac1c88b90e94178.r2.dev/v1/translate-zh_en-1_9.argosmodel",
}
MODEL_VERSION = "1_9"


def writable_models_dir() -> str:
    """模型可写目录（自动下载的落点）。

    打包后程序目录通常在 C:\\Program Files 下（无写权限），且 onefile 的 sys._MEIPASS 是
    每次启动都会变的临时目录——都不能当下载落点。所以固定用用户数据目录。
    直接读环境变量而不 import lylaunch.paths，是为了守住「插件不碰主程序模块」的隔离原则。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "LyLaunch", "translator_models")


def model_search_dirs(plugin_dir: str) -> list[str]:
    """按优先级返回模型查找目录（打包/开发两种场景都覆盖）。

    打包发布后（frozen）：
      1) <exe 同目录>/models        ← 推荐：由安装包部署，模型不进 exe，启动不变慢
      2) <包内插件目录>/models      ← 若把模型一起打进了 exe（sys._MEIPASS，只读）
    开发期：
      2) plugins/translator/models  ← 同上，开发期就是仓库里这个目录
      3) <仓库根>/models            ← tools/fetch_translator_models.py 的默认下载位置
    最后：
      4) %LocalAppData%\\LyLaunch\\translator_models  ← 首次自动下载的落点（可写）
    """
    dirs: list[str] = []
    if getattr(sys, "frozen", False):
        dirs.append(os.path.join(os.path.dirname(sys.executable), "models"))
    if plugin_dir:
        dirs.append(os.path.join(plugin_dir, "models"))
        dirs.append(os.path.join(os.path.dirname(os.path.dirname(plugin_dir)), "models"))
    dirs.append(writable_models_dir())

    seen: set[str] = set()
    uniq: list[str] = []
    for d in dirs:
        key = os.path.normcase(os.path.abspath(d))
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq


# ---------- 离线引擎缓存 + 空闲释放 ----------
# 实测（2026-10-25）：加载一个 Argos 模型约 +90MB（两个方向都用过时更接近 +220MB）；
# 而**原实现每次翻译都重建 ctranslate2.Translator**，导致 0.27s/句——缓存后 0.08s/句。
# 缓存放模块级是因为 TranslateWorker 每次翻译都新建 ArgosEngine 实例，实例级缓存没用。
# 空闲 _IDLE_SECONDS 秒后主动释放：常驻托盘的启动器不该为一个辅助功能长期占着两百多 MB。
_ENGINE_CACHE: dict[str, tuple] = {}    # key=f"{model_dir}|{langpair}" -> (translator, sp, prefix)
_LAST_USE = 0.0
_IDLE_SECONDS = 300                     # 5 分钟；<=0 表示不释放
_RELEASE_TIMER = None


def _in_main_thread() -> bool:
    """QTimer 属于创建它的线程；TranslateWorker 是后台线程且没跑事件循环，
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
        t.setInterval(60_000)           # 每分钟检查一次，开销可忽略
        t.timeout.connect(_release_if_idle)
        t.start()
        _RELEASE_TIMER = t
    except Exception:
        _RELEASE_TIMER = None


def _release_if_idle() -> None:
    global _ENGINE_CACHE
    if not _ENGINE_CACHE or _IDLE_SECONDS <= 0:
        return
    if time.monotonic() - _LAST_USE < _IDLE_SECONDS:
        return
    _ENGINE_CACHE.clear()
    gc.collect()
    print(f"[翻译-离线] 空闲 {_IDLE_SECONDS} 秒，已释放模型（内存可回落）", flush=True)


def release_engine() -> None:
    """立即释放离线引擎（供外部调用，如设置里手动清理）。"""
    global _ENGINE_CACHE
    _ENGINE_CACHE.clear()
    gc.collect()


# ---------- 本地大模型空闲释放 ----------
# 大模型是独立进程（llama-server），只在用「本地大模型」后端时才起，关窗后空闲 N 分钟
# 自动释放把内存还回去。两个本地模型（Hy-MT2 1.8B / 7B）共用全局 Router，按需换载。
_MODEL_RELEASE_TIMER = None


def _cancel_model_release() -> None:
    """取消待触发的延迟释放（用户再次使用模型时调用，避免把刚用上的引擎杀掉）。"""
    global _MODEL_RELEASE_TIMER
    if _MODEL_RELEASE_TIMER is not None:
        try:
            _MODEL_RELEASE_TIMER.stop()
        except Exception:
            pass
        _MODEL_RELEASE_TIMER = None


def _schedule_model_release(seconds: int) -> None:
    """关掉翻译窗口后延迟释放全部本地大模型进程（常驻几百 MB~1GB，闲置久了要还回去）。"""
    global _MODEL_RELEASE_TIMER
    if _MODEL_RELEASE_TIMER is not None:
        try:
            _MODEL_RELEASE_TIMER.stop()
        except Exception:
            pass
    t = QTimer()
    t.setSingleShot(True)

    def _fire() -> None:
        try:
            model_engine.shutdown_all()
        except Exception:
            pass

    t.timeout.connect(_fire)
    t.start(max(30, int(seconds)) * 1000)
    _MODEL_RELEASE_TIMER = t


class ArgosEngine:
    """离线翻译引擎：直接加载 Argos 的 .argosmodel（CTranslate2 + SentencePiece）。

    不依赖 argos-translate 包本身，只用底层两个库，等价于「把 Argos 当 Python 库直接调用」。
    翻译器按「模型目录 + 语言方向」缓存并空闲释放，见上方 _ENGINE_CACHE 说明。
    """

    def __init__(self, search_dirs: list[str], download_dir: str = "") -> None:
        # search_dirs：按优先级查找已有模型（可含只读目录，如打包进 exe 的）
        self._search_dirs = [d for d in (search_dirs or []) if d]
        # download_dir：缺模型时下载到哪（必须可写）
        self._download_dir = download_dir or writable_models_dir()

    @staticmethod
    def _model_ready(model_dir: str) -> bool:
        """模型目录是否完整可用（CTranslate2 模型 + SentencePiece 分词器都在）。"""
        return os.path.isdir(os.path.join(model_dir, "model")) and os.path.isfile(
            os.path.join(model_dir, "sentencepiece.model")
        )

    def find_model(self, langpair: str) -> str:
        """在所有查找目录里找现成模型，找到返回目录，没找到返回空串。

        ⚠️ **必须把 _download_dir 也算进查找范围**：下载/解压的落点就是它，
        漏掉会导致「刚下完的模型 has_model 仍是 False」→ 每次翻译都重下 70MB
        （2026-10-25 实测踩到）。
        """
        # .argosmodel 解压后是 translate-<langpair>-1_9/ 目录
        name = f"translate-{langpair}-{MODEL_VERSION}"
        for base in [*self._search_dirs, self._download_dir]:
            if not base:
                continue
            model_dir = os.path.join(base, name)
            if self._model_ready(model_dir):
                return model_dir
        return ""

    def has_model(self, langpair: str) -> bool:
        """本地是否已有该方向的模型（**不会触发下载**）。"""
        return bool(self.find_model(langpair))

    def download_model(self, langpair: str, progress=None) -> str:
        """下载并解压模型，返回模型目录。

        progress(done_bytes, total_bytes) 可选回调：
        - 主线程用它更新进度条；
        - **返回 False 表示用户点了取消**，下载会中止并清理残包。
        下载完做大小校验（对不上 Content-Length 就是下残了），避免留下损坏模型。
        """
        import urllib.request

        name = f"translate-{langpair}-{MODEL_VERSION}"
        os.makedirs(self._download_dir, exist_ok=True)
        url = ARGOS_MODELS[langpair]
        pkg = os.path.join(self._download_dir, f"{name}.argosmodel")
        print(f"[翻译-离线] 开始下载 {langpair}：{url}", flush=True)
        # R2 CDN 对无 UA 的请求返回 403，必须带 User-Agent
        req = urllib.request.Request(url, headers={"User-Agent": "LyLaunch/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp, open(pkg, "wb") as f:
                total = int(resp.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if progress is not None and progress(got, total) is False:
                        raise RuntimeError("下载已取消")
            # 完整性校验：大小对不上说明下残了，删掉别留坏模型
            if total and os.path.getsize(pkg) != total:
                raise RuntimeError(
                    f"下载不完整（{os.path.getsize(pkg)}/{total} 字节），已清理，请重试")
        except Exception:
            try:
                os.remove(pkg)
            except OSError:
                pass
            raise

        with zipfile.ZipFile(pkg) as zf:
            zf.extractall(self._download_dir)
        # 删临时包失败不影响使用（模型已解压好），别让它把翻译搞崩
        try:
            os.remove(pkg)
        except OSError:
            pass

        model_dir = os.path.join(self._download_dir, name)
        if not self._model_ready(model_dir):
            raise RuntimeError(f"模型下载后结构异常：{model_dir}")
        return model_dir

    def ensure_model(self, langpair: str) -> str:
        """返回可用模型目录：优先用现成的，没有才下载（非 UI 场景兜底用）。

        带界面的流程请**先 has_model() 判断并询问用户**再下载——
        下载要走进度条，不能在后台线程里静默跑 70MB。
        """
        found = self.find_model(langpair)
        if found:
            return found
        return self.download_model(langpair)

    def translate(self, text: str, langpair: str) -> str:
        import ctranslate2
        import sentencepiece as spm

        global _LAST_USE
        model_dir = self.ensure_model(langpair)
        key = f"{model_dir}|{langpair}"
        _LAST_USE = time.monotonic()

        cached = _ENGINE_CACHE.get(key)
        if cached is not None:
            translator, sp, prefix = cached
        else:
            # ⚠️ 必须把分词器读成 bytes 走 model_proto，不能用 model_file 传路径：
            # sentencepiece 的 C++ 层用窄字符文件 API，路径含非 ASCII（如中文用户名
            # C:\Users\<用户名>\AppData\Local\...）会报 NOT_FOUND。读字节交给它就绕开了。
            # （ctranslate2 用宽字符 API，中文路径没问题，下面可以直接传路径。）
            with open(os.path.join(model_dir, "sentencepiece.model"), "rb") as f:
                sp = spm.SentencePieceProcessor(model_proto=f.read())
            translator = ctranslate2.Translator(
                os.path.join(model_dir, "model"),
                device="cpu",
                intra_threads=min(4, os.cpu_count() or 1),
            )
            # 读 target_prefix（中译英通常为空，英译中也为空）
            prefix = ""
            meta = os.path.join(model_dir, "metadata.json")
            if os.path.isfile(meta):
                try:
                    import json

                    prefix = (json.load(open(meta, encoding="utf-8")) or {}).get("target_prefix", "")
                except Exception:
                    prefix = ""
            _ENGINE_CACHE[key] = (translator, sp, prefix)

        tokens = sp.encode(text, out_type=str)
        target_prefix = [[prefix]] if prefix else None
        results = translator.translate_batch(
            [tokens],
            target_prefix=target_prefix,
            replace_unknowns=True,
            beam_size=4,
            length_penalty=0.2,
        )
        out = results[0].hypotheses[0]
        # sentencepiece 用 ▁(U+2581) 标记词间空格，按 Argos 惯例替换为普通空格
        result = sp.decode(out).replace("\u2581", " ")
        if prefix and result.startswith(prefix):
            result = result[len(prefix):]
        if result and result[0] == " ":
            result = result[1:]
        return result


class TranslateWorker(QThread):
    """后台翻译线程，避免下载/网络请求卡住 UI。"""

    result_ready = Signal(str)
    error_occurred = Signal(str)

    def __init__(self, text: str, backend: str, settings: dict,
                 src_lang: str = DIR_AUTO, tgt_lang: str = "zh") -> None:
        super().__init__()
        self._text = text
        self._backend = backend
        self._settings = settings
        self._src = src_lang or DIR_AUTO
        self._tgt = tgt_lang or "zh"

    def run(self) -> None:
        try:
            self.result_ready.emit(self._do_translate())
        except Exception as e:  # 网络/接口异常原样回显给用户
            self.error_occurred.emit(f"翻译失败：{e}")

    def _do_translate(self) -> str:
        src, tgt = self._src, self._tgt
        # 本地大模型（hy-mt2 / hy-mt2-7b）：Router 已在主线程准备好模型（_ensure_model），
        # 这里只发翻译请求；若被空闲释放停掉，兜底重启一次（文件在则几秒起好，不重复下载）。
        if self._backend != "offline":
            engine = model_engine.get_engine(
                model_engine.models_root(),
                model_engine.spec_of(self._backend).port, self._backend)
            try:
                engine.start(None)
            except Exception:
                pass
            if not engine.running:
                raise RuntimeError(
                    "本地大模型翻译服务未运行。请重试，或在右上角切换回「离线翻译」。")
            src_name = "" if src == DIR_AUTO else LANG_NAME.get(src, src)
            tgt_name = LANG_NAME.get(_effective_target(self._text, src, tgt), tgt)
            return _translate_preserving_lines(
                lambda t: engine.translate(t, src_name, tgt_name), self._text)
        # 离线：只支持中英互译（Argos 只有这两对模型）
        langpair = _offline_pair(self._text, src, tgt)
        if not langpair:
            raise RuntimeError(
                "离线翻译只支持中英互译。其它语言请点右上角切换到「本地大模型」后再翻。")
        custom = (self._settings.get("models_dir") or "").strip()
        dirs = model_search_dirs(self._settings.get("plugin_dir") or "")
        if custom:
            dirs.insert(0, custom)
        engine = ArgosEngine(dirs, custom)
        # 模型缺失时**不在这里偷偷下载**：下载要询问用户 + 有进度条，必须由
        # 主线程的对话框先走完流程（worker 是后台线程，不能弹窗）。
        if not engine.has_model(langpair):
            raise RuntimeError(
                "离线翻译模型未安装。请回到翻译窗口，点翻译时会提示下载（约 70MB）。"
            )
        return _translate_preserving_lines(
            lambda t: engine.translate(t, langpair), self._text)


class ModelDownloadWorker(QThread):
    """后台下载离线模型，并把进度回传主线程。

    必须开线程：70MB 下载几十秒，放主线程会把界面冻住。
    子线程不能直接碰 UI，所以进度用信号回传给 QProgressDialog。
    """

    progress = Signal(int, int)      # 已下载字节 / 总字节（总字节可能为 0）
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, engine, langpair: str, parent=None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._langpair = langpair
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            def _prog(done: int, total: int) -> bool:
                self.progress.emit(done, total)
                return not self._cancel   # 返回 False → download_model 中止并清理
            path = self._engine.download_model(self._langpair, _prog)
            self.finished_ok.emit(path)
        except Exception as e:
            self.failed.emit(str(e))


class ModelPrepareWorker(QThread):
    """后台准备本地大模型：下载运行时+模型 → 起进程 → 健康检查 → 预热。

    镜像文档识别插件的 PrepareWorker；进度回调 (doc, pct) 中 pct 为 None 表示
    不确定进度（如起服务/预热阶段），否则是下载百分比。取消时由回调抛
    model_engine.Cancelled 中断下载。
    """

    progress = Signal(str, object)      # (说明文字, 百分比|None)
    ready = Signal()
    failed = Signal(str)

    def __init__(self, engine, parent=None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    def _progress(self, doc: str, pct) -> None:
        if self._cancel:
            raise model_engine.Cancelled()
        # 下载回调带的是 gguf 文件名，进度框里换成模型显示名更好读
        if doc.startswith("下载"):
            doc = f"正在下载 {self._engine.spec.name}"
        self.progress.emit(doc, pct)

    def run(self) -> None:
        try:
            # 先真下载（缺文件时）；cancelled 必须一路传到 HTTP 读循环，
            # 否则「取消」只是置了个标志、下载照跑完（0.39.4 同类坑）。
            self._engine.ensure_files(self._progress, lambda: self._cancel)
            self._engine.start(self._progress)
            self.ready.emit()
        except model_engine.Cancelled:
            pass            # 用户主动取消，不算失败
        except model_engine.EngineError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"模型准备失败（{type(e).__name__}）：{e}")


# ---- 模型选择面板（照本地 AI 插件的模型列表：白底圆角 + 行 + 状态徽章）----
_MODEL_PANEL_QSS = """
QFrame#transModelPanel { background: #FFFFFF; border: 1px solid #D8E3F5; border-radius: 12px; }
QLabel#tpTitle { font-size: 12px; color: #5F6570; font-weight: 600; padding: 0 0 4px 2px; }
QFrame#tpRow { background: transparent; border-radius: 8px; }
QFrame#tpRow:hover { background: #F2F6FD; }
QFrame#tpRowCur { background: #E4EDFC; border-radius: 8px; }
QFrame#tpRowCur:hover { background: #DCE8FB; }
QLabel#tpRowName { font-size: 12.5px; color: #161C28; }
QLabel#tpRowSize { font-size: 10.5px; color: #5F6570; }
QLabel#tpStateCur, QLabel#tpStateOk, QLabel#tpStateMiss, QLabel#tpStateAvail {
    font-size: 9.5px; border-radius: 4px; padding: 2px 6px;
}
QLabel#tpStateCur { background: #E4EDFC; color: #2F6BEE; font-weight: 600; }
QLabel#tpStateOk, QLabel#tpStateAvail { background: #F2F6FD; color: #5F6570; }
QLabel#tpStateMiss { background: transparent; color: #5F6570; border: 1px dashed #C3D3EE; }
"""

# 模型介绍气泡（照文档识别插件的 ModelDetailBubble）：悬停某行时浮出，口语化讲清区别。
_BUBBLE_QSS = """
QFrame#tpBubble { background:#FFFFFF; border:1px solid #D8E3F5; border-radius:10px; }
QLabel#bName { color:#161C28; font-size:13px; font-weight:600; }
QLabel#bState { color:#5F6570; font-size:10px; border-radius:9px; padding:2px 9px; }
QLabel#bStateCur { color:#2F6BEE; background:#E4EDFC; border:1px solid #C3D3EE;
                   border-radius:9px; padding:2px 9px; font-size:10px; }
QLabel#bStateOk { color:#2F6BEE; font-size:10px; }
QLabel#bDesc { color:#161C28; font-size:11px; }
QLabel#bMeta { color:#5F6570; font-size:10px; }
QPushButton#bPrimary { background:#2F6BEE; color:#FFFFFF; border:none;
                      border-radius:7px; padding:6px 10px; font-size:11.5px; }
QPushButton#bPrimary:hover { background:#2359D6; }
QPushButton#bDangerBtn { background:#FDECEA; color:#EC5F53; border:none;
                        border-radius:7px; padding:6px 10px; font-size:11.5px; }
QPushButton#bDangerBtn:hover { background:#FBDDD9; }
"""

# 右上角「启动/停止模型」按钮：启动=蓝色实心、停止=浅蓝描边，两种状态一眼分清。
_POWER_START_QSS = (
    "QPushButton{background:#2F6BEE;color:#FFFFFF;border:none;border-radius:16px;"
    "padding:6px 16px;font-size:12px;font-weight:600;}"
    "QPushButton:hover{background:#2359D6;}"
    "QPushButton:disabled{background:#A9C1F5;color:#FFFFFF;}")
# 停止模型：胶囊形 + 一条浅边框；平时蓝字白底，鼠标移上去变红，移开还原。
_POWER_STOP_QSS = (
    "QPushButton{background:#FFFFFF;color:#2F6BEE;border:1px solid #D8E3F5;"
    "border-radius:16px;padding:6px 16px;font-size:12px;font-weight:600;}"
    "QPushButton:hover{background:#EC5F53;color:#FFFFFF;border-color:#EC5F53;}"
    "QPushButton:pressed{background:#D93A2F;border-color:#D93A2F;color:#FFFFFF;}"
    "QPushButton:disabled{color:#9AA6B8;border-color:#E3EBF6;background:#FFFFFF;}")
# 离线时「停止模型」外观灰掉、无 hover 反馈（点它也没反应），但保持 enabled 才能弹说明气泡。
_POWER_STOP_INERT_QSS = (
    "QPushButton{background:#FFFFFF;color:#9AA6B8;border:1px solid #E9EFF8;"
    "border-radius:16px;padding:6px 16px;font-size:12px;font-weight:600;}")

# 「正在启动模型」进度窗口：和主题一致的浅色细进度条 + ghost 取消按钮。
# （QProgressDialog 是独立窗口，拿不到 theme.DIALOG_QSS 里的 QDialog QProgressBar，必须自带。）
_MODEL_PROGRESS_QSS = """
QProgressDialog { background: #FFFFFF; }
QProgressDialog QLabel { color: #161C28; font-size: 12px; }
QProgressDialog QProgressBar {
    background: #EDF2FB; border: none; border-radius: 7px;
    min-height: 14px; max-height: 14px; color: transparent;
}
QProgressDialog QProgressBar::chunk { background: #2F6BEE; border-radius: 7px; }
QProgressDialog QPushButton {
    border: 1px solid #C3D3EE; border-radius: 8px; background: #FFFFFF;
    color: #2F6BEE; padding: 5px 16px; font-size: 12px; font-weight: 600;
}
QProgressDialog QPushButton:hover { background: #E4EDFC; border-color: #BFD3FA; }
QProgressDialog QPushButton:pressed { background: #D5E4FB; }
"""

_INPUT_PLACEHOLDER = "在这里输入或粘贴要翻译的文字…"

# 各模型的口语化介绍：note 一句话定位，points 讲区别，meta 体积/来源/许可。
_BUBBLE_INFO = {
    "offline": {
        "name": "Argos 离线模型",
        "note": "不联网、不用下大模型，打开就能翻。",
        "points": [
            "中英互译够用，速度最快、最省内存",
            "第一次下个约 70MB 的小模型，之后纯离线",
            "长句、专业词多的时候，不如大模型顺",
        ],
        "meta": "约 70MB · 免登录 · 纯离线",
        "need": "CPU 运行，不占显存",
    },
    "hy-mt2": {
        "name": "Hy-MT2 1.8B",
        "note": "腾讯专门为翻译练的大模型，速度和质量最均衡。",
        "points": [
            "翻译腔少，长句、术语翻得比离线模型准",
            "体积适中（约 1.1GB），普通电脑也跑得动",
            "要极高质量的长文/专业内容，再上 7B",
        ],
        "meta": "约 1.1GB · 腾讯混元",
        "need": "所需显存约 2.5GB",
    },
    "hy-mt2-7b": {
        "name": "Hy-MT2 7B",
        "note": "腾讯混元翻译专用 7B，本地质量最高的档。",
        "points": [
            "翻译质量超过 DeepSeek-V4-Pro / Kimi K2.6",
            "长句、专业内容、多语言都比 1.8B 更稳",
            "体积较大（约 4.6GB），启动略慢",
        ],
        "meta": "约 4.6GB · 腾讯混元",
        "need": "所需显存约 5GB",
    },
}


class ModelRow(QFrame):
    """模型列表里的一行：名称 + 体积，点击选中该模型；悬停弹介绍气泡。"""

    clicked = Signal(str)
    hovered = Signal(str)     # 鼠标进入某行 → 面板弹介绍气泡
    left = Signal(str)        # 鼠标离开某行 → 延迟收起气泡

    def __init__(self, backend: str, name: str, size_text: str,
                 current: bool) -> None:
        super().__init__()
        self._backend = backend
        self.setObjectName("tpRowCur" if current else "tpRow")
        # QWidget 子类要靠这个属性才会用 QSS 画自身背景 / hover
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(9, 7, 9, 7)
        lay.setSpacing(8)

        self._name_text = name
        self._name_lab = QLabel(name)
        self._name_lab.setObjectName("tpRowName")
        # 允许名字被压窄并打省略号，否则长名字会把面板撑宽
        self._name_lab.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._name_lab.setMinimumWidth(0)
        lay.addWidget(self._name_lab, 1)

        size_lab = QLabel(size_text)
        size_lab.setObjectName("tpRowSize")
        lay.addWidget(size_lab)

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        avail = self._name_lab.width()
        if avail > 12:
            fm = self._name_lab.fontMetrics()
            txt = fm.elidedText(self._name_text, Qt.TextElideMode.ElideRight, avail)
            if txt != self._name_lab.text():
                self._name_lab.setText(txt)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._backend)

    def enterEvent(self, e) -> None:  # noqa: N802
        self.hovered.emit(self._backend)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self.left.emit(self._backend)
        super().leaveEvent(e)


class ModelBubble(QFrame):
    """模型介绍气泡：鼠标停在哪一行，就在面板旁浮出（照文档识别插件的气泡）。

    和面板一样做成对话框的子控件；位置优先放面板右侧，放不下放左侧。
    """

    WIDTH = 252
    GAP = 8

    def __init__(self, panel) -> None:
        super().__init__(panel.parent())
        self.panel = panel
        self.setObjectName("tpBubble")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(self.WIDTH)
        self.setStyleSheet(_BUBBLE_QSS)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 13, 14, 13)
        lay.setSpacing(9)

        self.b_name = QLabel("—")
        self.b_name.setObjectName("bName")
        self.b_name.setWordWrap(True)
        lay.addWidget(self.b_name)

        self.b_note = QLabel("")
        self.b_note.setObjectName("bDesc")
        self.b_note.setWordWrap(True)
        lay.addWidget(self.b_note)

        self.b_points = QLabel("")
        self.b_points.setObjectName("bDesc")
        self.b_points.setWordWrap(True)
        self.b_points.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.b_points)

        self.b_meta = QLabel("")
        self.b_meta.setObjectName("bMeta")
        self.b_meta.setWordWrap(True)
        lay.addWidget(self.b_meta)

        # 动作按钮：本地大模型 → 未下载「下载并启用」/ 已下载「卸载模型」；离线隐藏
        self.act_btn = QPushButton("")
        self.act_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.act_btn.setVisible(False)
        self.act_btn.clicked.connect(self._on_action)
        lay.addWidget(self.act_btn)
        self._backend = None

        self.b_hint = QLabel("")
        self.b_hint.setObjectName("bMeta")
        self.b_hint.setWordWrap(True)
        lay.addWidget(self.b_hint)

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

    def schedule_hide(self) -> None:
        self._hide_timer.start()

    def enterEvent(self, e) -> None:  # noqa: N802
        self._hide_timer.stop()      # 鼠标移进气泡就别收（方便看清楚）
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self.schedule_hide()
        super().leaveEvent(e)

    def _on_action(self) -> None:
        if self._backend:
            self.panel.dlg._on_bubble_action(self._backend)

    def show_for(self, backend: str, row) -> None:
        info = _BUBBLE_INFO.get(backend)
        if info is None:
            return
        from html import escape as _esc
        self._hide_timer.stop()
        self._backend = backend
        self.b_name.setText(info["name"])
        self.b_note.setText(info["note"])
        self.b_points.setText("".join(
            f"<p style='margin:0 0 5px 0;'><span style='color:#2F6BEE;'>·</span> {_esc(p)}</p>"
            for p in info["points"]))
        self.b_meta.setText(info["meta"])
        self.b_hint.setText(f"{info['name']}，{info['need']}")
        # 动作：本地大模型未下载→「下载并启用」；已下载→「卸载模型」；离线无动作
        if backend == "offline":
            self.act_btn.setVisible(False)
        elif self.panel.dlg._hy_engine(backend).has_files():
            self.act_btn.setText("卸载模型")
            self.act_btn.setObjectName("bDangerBtn")
            self.act_btn.setVisible(True)
        else:
            self.act_btn.setText("下载并启用")
            self.act_btn.setObjectName("bPrimary")
            self.act_btn.setVisible(True)
        st = self.act_btn.style()
        if st is not None:
            st.unpolish(self.act_btn)
            st.polish(self.act_btn)
        self._place(row)
        self.show()
        self.raise_()

    def _place(self, row) -> None:
        self.adjustSize()
        dlg = self.window()
        px, pw = self.panel.x(), self.panel.width()
        py, ph = self.panel.y(), self.panel.height()
        cw, ch = self.width(), self.height()
        dw, dh = dlg.width(), dlg.height()
        row_y = row.mapTo(dlg, row.rect().topLeft()).y() - 4 if row else py
        if px + pw + self.GAP + cw <= dw - 8:      # 面板右侧放得下 → 放右边
            x, y = px + pw + self.GAP, row_y
        elif px - self.GAP - cw >= 8:              # 左侧放得下 → 放左边
            x, y = px - self.GAP - cw, row_y
        else:                                       # 窄窗口两边都放不下 → 放面板下方，别盖住列表行
            x, y = max(8, min(px, dw - cw - 8)), py + ph + self.GAP
        self.move(x, max(8, min(y, dh - ch - 8)))


class ModelPanel(QFrame):
    """模型选择面板：标题 + 三行（离线 / Hy-MT2 1.8B / Hy-MT2 7B），作为对话框的子控件。

    参照本地 AI 插件的模型列表面板（白底圆角 + 行：名称 + 体积）；不用 Qt.Popup
    顶层窗口，避免「点一下闪一下」的抢激活问题，点击外部靠事件过滤器收起。
    悬停某一行时旁边浮出 ModelBubble 介绍，下载/卸载也在气泡里。
    """

    picked = Signal(str)      # 选中某个后端 id

    # 三个可选项：backend, 名称, 体积提示（离线也给了正式名字：Argos 离线模型）
    _OPTS = [
        ("offline", "Argos 离线模型", "~70MB"),
        ("hy-mt2", "Hy-MT2 1.8B", "~1.1GB"),
        ("hy-mt2-7b", "Hy-MT2 7B", "~4.6GB"),
    ]

    def __init__(self, dialog) -> None:
        super().__init__(dialog)
        self.dlg = dialog
        self.setObjectName("transModelPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(_MODEL_PANEL_QSS)
        self.setMinimumWidth(232)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(2)
        title = QLabel("选择模型")
        title.setObjectName("tpTitle")
        outer.addWidget(title)
        self._rows_box = QVBoxLayout()
        self._rows_box.setContentsMargins(0, 0, 0, 0)
        self._rows_box.setSpacing(3)
        outer.addLayout(self._rows_box)
        self._rows: dict[str, ModelRow] = {}
        self.bubble = ModelBubble(self)      # 悬停某行时在旁边浮出介绍
        self.refresh()
        self.hide()

    def refresh(self) -> None:
        """按后端状态重建三行（当前 / 已下载 / 未下载）。"""
        self.bubble.hide()
        while self._rows_box.count():
            it = self._rows_box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        self._rows.clear()
        for backend, name, size_text in self._OPTS:
            current = (backend == self.dlg._backend)
            row = ModelRow(backend, name, size_text, current)
            row.clicked.connect(self.picked)          # signal → signal 转发
            row.hovered.connect(self._on_row_hover)
            row.left.connect(self._on_row_leave)
            self._rows_box.addWidget(row)
            self._rows[backend] = row

    def _on_row_hover(self, backend: str) -> None:
        self.bubble.show_for(backend, self._rows.get(backend))

    def _on_row_leave(self, _backend: str) -> None:
        self.bubble.schedule_hide()

    def hide(self) -> None:  # noqa: N802
        if hasattr(self, "bubble"):
            self.bubble.hide()
        super().hide()


class _AutoTranslateEdit(QPlainTextEdit):
    """原文输入框：任何「粘贴」动作（Ctrl+V / 右键粘贴 / 拖入文字 / 中键）
    都会走 insertFromMimeData —— 在这一次性捕获，粘完发信号自动翻译。

    比监听 textChanged 精确：正常打字不会触发，只有真正的粘贴会。
    """
    pasted = Signal()

    def insertFromMimeData(self, source) -> None:  # noqa: N802
        super().insertFromMimeData(source)
        try:
            text = (source.text() or "").strip() if source is not None else ""
        except Exception:
            text = ""
        if text:
            self.pasted.emit()


class TranslateDialog(QDialog):
    def __init__(self, api, settings, parent=None) -> None:
        super().__init__(parent)
        self._api = api
        self._settings = settings
        # 偏好持久化的选择（settings_get 优先），再回退到传入的内存配置，
        # 保证「右上角切换模型」后下次打开翻译窗口默认就用它（照文档识别插件做法）。
        try:
            _saved = self._api.settings_get("backend", None)
        except Exception:
            _saved = None
        # 默认离线翻译：第一次用的人通常一个本地模型都没下载，默认指到 Hy-MT2
        # 会让「翻译」按钮直接不可用，用户既以为插件坏了、也不知道还有离线模式
        # （离线包 70MB，中英互译开箱即用）。选过本地大模型的人会被 _saved 记住。
        self._backend = _saved if _saved else settings.get("backend", "offline")
        # 记住的是本地大模型、但模型文件已经不在了（卸载 / 重置数据）：这次先落到
        # 离线，否则打开就是「下载模型」+ 翻译按钮灰着，看着像坏了。
        # 只改这一次的选择，不写回配置 —— 模型一装上，下次打开自动回到他的选择。
        self._backend_fallback = False
        if self._backend != "offline":
            try:
                if not self._hy_engine(self._backend).has_files():
                    self._backend = "offline"
                    self._backend_fallback = True
            except Exception:
                pass
        self._worker: TranslateWorker | None = None
        self._prep: "ModelPrepareWorker | None" = None    # 下载 / 启动模型的后台线程
        # 用显式标志而不是 _prep.isRunning()：ready/failed 信号是在 run() 里发出的，
        # UI 收到时线程可能还在收尾，isRunning() 仍为 True → 按钮会卡在「准备中…」。
        self._prep_busy: bool = False
        self._pending_translate: bool = False            # 模型还在准备：就绪后自动接着翻
        self._busy: bool = False            # 翻译/准备模型中：屏蔽重复点击，防起多个进程
        self._offline_started: bool = True  # 离线翻译默认已启动（纯逻辑状态，可手动停用）
        self.setWindowTitle("文本翻译")
        self.setMinimumWidth(520)
        self._build_ui()
        if self._backend_fallback:
            self._set_status("原选的本地模型还没下载 · 当前用离线翻译（右上角可切回并下载）")
        self.setStyleSheet(theme.DIALOG_QSS)
        from lylaunch.plugins.window_chrome import install_chrome
        install_chrome(self, "文本翻译", show_minmax=True, resizable=True)

        # 空闲释放离线引擎：定时器必须在主线程建（后台线程建的不会触发）
        global _IDLE_SECONDS
        try:
            v = self._api.settings_get("engine_idle_release", None)
            if v is None:
                v = settings.get("engine_idle_release", _IDLE_SECONDS)
            _IDLE_SECONDS = max(0, int(v))
        except Exception:
            pass
        _start_release_timer()

    def _build_ui(self) -> None:
        """弹窗布局：标题行 → 原文卡 → 操作条 → 译文卡 → 状态行。

        设计取舍：
        - 原文/译文各自是一张卡（文本框本身就是卡片：白底 + 圆角 + 描边），
          不再用「原文（已自动预填剪贴板，可修改）：」这种长标签压在上面；
        - 「粘贴 / 清空 / 复制」下沉成行内轻按钮（ghost），主操作只有「翻译」一个；
        - 后端信息从顶部大段文字降级为操作条左侧一行小字 + 标题右侧方向徽章，
          技术细节放进 tooltip，需要时才看到。
        """
        # 装了 44px 自绘导航栏后内容区变矮，整体放大一档
        self.resize(740, 640)
        self.setMinimumSize(620, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 16)
        root.setSpacing(12)

        # ① 图标头部（0.36 通用壳）：40px 线性 SVG 图标块 + 大标题 + 方向徽章
        head = QHBoxLayout()
        head.setSpacing(12)
        icon = QLabel()
        icon.setFixedSize(40, 40)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        icon.setStyleSheet(f"background:{theme.PLUGIN_BG}; border-radius:10px;")
        icon.setPixmap(render_plugin_svg(plugin_icon_svg("translator"), 24))
        head.addWidget(icon)
        title = QLabel("文本翻译")
        title.setProperty("role", "title")
        head.addWidget(title)
        head.addStretch(1)
        # 右上角：模型切换（照文档识别插件的样式：按钮 ▾ + 状态徽章 + 手动停止）
        self.model_btn = QPushButton()
        self.model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.model_btn.setStyleSheet(
            "QPushButton{border:1px solid #C3D3EE;border-radius:16px;background:#FFFFFF;"
            "color:#161C28;padding:6px 18px;font-size:12px;}"
            "QPushButton:hover{background:#E4EDFC;border-color:#BFD3FA;}")
        self.model_btn.setMinimumWidth(132)
        self.model_btn.setFixedHeight(32)     # 固定高 32 + radius 16 → 真胶囊
        self.model_btn.setToolTip("切换翻译模型：离线翻译 / 本地大模型（Hy-MT2 1.8B、7B）")
        head.addWidget(self.model_btn)
        # 手动启动/停止模型：本地大模型常驻会占几百 MB~1GB 内存，用户要能自己关掉；
        # 离线也复用这颗按钮（默认已启动，可停用）。不再单独放状态徽章，状态由它表达。

        self.power_btn = QPushButton("启动模型")
        self.power_btn.setMinimumWidth(96)
        self.power_btn.setFixedHeight(32)     # 固定高 32 + radius 16 → 真胶囊
        self.power_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.power_btn.setToolTip("启动 / 停止本地大模型进程：启动会下载（首次）并常驻内存，停止则释放内存（不影响已下载的模型）")
        self.power_btn.setVisible(False)
        self.power_btn.clicked.connect(self._toggle_engine)
        self.power_btn.installEventFilter(self)   # 事件不参与模型面板收起判断
        head.addWidget(self.power_btn)
        # 右上角模型列表：白底圆角面板（照本地 AI），点按钮开合、点行切换
        self._model_panel = ModelPanel(self)
        self._model_panel.picked.connect(self._on_panel_picked)
        self.model_btn.clicked.connect(self._toggle_model_panel)
        root.addLayout(head)

        sub = QLabel("多种语言可互相翻译；离线只能中英互译，本地大模型支持更多语言")
        sub.setProperty("role", "subtitle")
        root.addWidget(sub)

        # 模型准备进度：和文档识别插件同一套观感（一行状态文字 + 6px 细进度条），
        # 不再弹模态 QProgressDialog —— 模态框会把整个窗口挡住，还挡不住用户去点别处。
        self.progress_box = QWidget()
        pb = QVBoxLayout(self.progress_box)
        pb.setContentsMargins(0, 0, 0, 0)
        pb.setSpacing(8)
        self.prepare_text = QLabel("正在检查翻译模型…")
        self.prepare_text.setProperty("role", "status")
        self.prepare_text.setWordWrap(True)
        pb.addWidget(self.prepare_text)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        pb.addWidget(self.progress)
        self.progress_box.setVisible(False)
        root.addWidget(self.progress_box)

        # ② 原文卡（标题行右侧放行内轻操作）
        src_head = QHBoxLayout()
        src_head.setSpacing(2)
        src_label = QLabel("原文")
        src_label.setProperty("role", "section")
        src_head.addWidget(src_label)
        src_head.addStretch(1)
        paste_btn = QPushButton("粘贴")
        paste_btn.setProperty("role", "ghost")
        paste_btn.setToolTip("用剪贴板里的文字替换原文")
        paste_btn.clicked.connect(self._on_paste)
        src_head.addWidget(paste_btn)
        clear_btn = QPushButton("清空")
        clear_btn.setProperty("role", "ghost")
        clear_btn.setToolTip("清空原文")
        clear_btn.clicked.connect(lambda: self.input.clear())
        src_head.addWidget(clear_btn)
        root.addLayout(src_head)

        self.input = _AutoTranslateEdit()
        self.input.setPlaceholderText(_INPUT_PLACEHOLDER)
        self.input.setMinimumHeight(140)
        # 粘贴进插件就自动翻译（0.43 设计要求）
        self.input.pasted.connect(self._on_input_pasted)
        # 聚焦时隐藏占位提示：否则用输入法打拼音时（组合输入不进文档）占位文字不消失、
        # 和候选/拼音叠在一起（实测 bug）。
        self.input.installEventFilter(self)
        try:
            self.input.setPlainText(self._api.clipboard_read())
        except Exception:
            pass
        if self.input.toPlainText().strip():
            self.input.selectAll()      # 预填剪贴板后全选：一输入就替换，旧文字不残留
        root.addWidget(self.input, 1)

        # ③ 操作条：源语言 ⇄ 目标语言（中间可对调），右侧唯一主按钮
        mid = QHBoxLayout()
        mid.setSpacing(8)
        # 操作条上下留白，别贴着原文框和译文框；行内控件统一 34px 高
        mid.setContentsMargins(0, 4, 0, 4)
        self.src_combo = QComboBox()
        self.src_combo.addItem("自动检测", DIR_AUTO)
        for _code, _name in LANGUAGES:
            self.src_combo.addItem(_name, _code)
        self.src_combo.setFixedWidth(112)
        self.src_combo.setFixedHeight(34)
        self.src_combo.setToolTip("源语言；「自动检测」按原文自动判断")
        mid.addWidget(self.src_combo)

        # 就是个可点击的符号，不做按钮样式（无边框/无底色），鼠标移上去变蓝
        self.swap_btn = QPushButton("⇆")
        self.swap_btn.setFixedSize(28, 34)   # 与两侧下拉/翻译按钮同高，操作条不再参差
        self.swap_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.swap_btn.setToolTip("交换源语言 / 目标语言（点一下对调）")
        self.swap_btn.setStyleSheet(
            "QPushButton{border:none;background:transparent;color:#5F6570;"
            "font-size:18px;font-weight:600;padding:0;}"
            "QPushButton:hover{color:#2F6BEE;}")
        self.swap_btn.clicked.connect(self._swap_langs)
        mid.addWidget(self.swap_btn)

        self.tgt_combo = QComboBox()
        for _code, _name in LANGUAGES:
            self.tgt_combo.addItem(_name, _code)
        self.tgt_combo.setFixedWidth(112)
        self.tgt_combo.setFixedHeight(34)
        self.tgt_combo.setToolTip("目标语言")
        mid.addWidget(self.tgt_combo)

        mid.addStretch(1)
        self.translate_btn = QPushButton("翻译")
        self.translate_btn.setProperty("role", "primary")
        self.translate_btn.setMinimumWidth(104)
        self.translate_btn.setFixedHeight(34)
        self.translate_btn.setToolTip("翻译原文（Ctrl+Enter）")
        self.translate_btn.clicked.connect(self._on_translate)
        mid.addWidget(self.translate_btn)
        root.addLayout(mid)
        self._refresh_model_ui()      # 放在 translate_btn 创建后：依后端/引擎状态决定翻译按钮可点性

        # ④ 译文卡
        out_head = QHBoxLayout()
        out_head.setSpacing(2)
        out_label = QLabel("译文")
        out_label.setProperty("role", "section")
        out_head.addWidget(out_label)
        out_head.addStretch(1)
        # 0.43：反译 —— 把（可编辑的）译文当原文再翻回去
        self.reverse_btn = QPushButton("反译")
        self.reverse_btn.setProperty("role", "ghost")
        self.reverse_btn.setToolTip(
            "把译文当作原文、方向对调后再翻一次（双向翻译）")
        self.reverse_btn.clicked.connect(self._on_reverse_translate)
        out_head.addWidget(self.reverse_btn)
        self.copy_btn = QPushButton("复制")
        self.copy_btn.setProperty("role", "ghost")
        self.copy_btn.setToolTip("复制译文到剪贴板")
        self.copy_btn.clicked.connect(self._on_copy)
        out_head.addWidget(self.copy_btn)
        root.addLayout(out_head)

        self.output = QPlainTextEdit()
        # 0.43：译文可编辑（设计要求）—— 方便顺手改词、润色后再复制/反译。
        # 错误提示也会写进这里，所以复制/反译都做了「提示语开头」的过滤。
        self.output.setReadOnly(False)
        self.output.setPlaceholderText("翻译结果会显示在这里（可直接编辑）")
        self.output.setMinimumHeight(140)
        root.addWidget(self.output, 1)

        # ⑤ 状态行
        self.status = QLabel("就绪")
        self.status.setProperty("role", "status")
        root.addWidget(self.status)

        # Ctrl+Enter 翻译（比鼠标点按钮快，符合"唤起即用"的节奏）。
        # 只注册一次：同一序列注册两个 QShortcut 会变成 ambiguous 而两个都不触发。
        sc = QShortcut(QKeySequence("Ctrl+Return"), self)
        sc.activated.connect(self._on_translate)

        # 打开时若已把剪贴板内容预填进来，等窗口真正显示后再自动翻（0.43）。
        # 延迟到 show 之后：本地大模型若没启动，_maybe_auto_translate 会只提示不硬翻，
        # 放在构造期触发会让状态栏提示被后续初始化覆盖。
        if self.input.toPlainText().strip():
            QTimer.singleShot(0, self._auto_translate_on_open)

    def _auto_translate_on_open(self) -> None:
        """首次打开带剪贴板内容时自动翻译一次（只触发一次，避免重复）。"""
        if getattr(self, "_opened_auto_done", False):
            return
        self._opened_auto_done = True
        self._maybe_auto_translate()

    def _set_status(self, msg: str) -> None:
        self.status.setText(msg)

    # ---- 右上角模型切换（照文档识别插件的样式）----
    def _label_for(self, backend: str) -> str:
        return {
            "offline": "离线翻译",
            "hy-mt2": "Hy-MT2",
            "hy-mt2-7b": "Hy-MT2 7B",
        }.get(backend, backend)

    def _toggle_model_panel(self) -> None:
        if self._model_panel.isVisible():
            self._hide_model_panel()
        else:
            self._show_model_panel()

    def _show_model_panel(self) -> None:
        self._model_panel.refresh()
        self._model_panel.adjustSize()
        self._position_model_panel()
        self._model_panel.show()
        self._model_panel.raise_()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)     # 点外部 / Esc 时收起

    def _hide_model_panel(self) -> None:
        panel = getattr(self, "_model_panel", None)
        if panel is not None and panel.isVisible():
            panel.hide()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    def _position_model_panel(self) -> None:
        """把面板放在模型按钮正下方，右对齐，并夹在对话框可见范围内。"""
        panel = self._model_panel
        btn = self.model_btn
        bottom_left = btn.mapTo(self, btn.rect().bottomLeft())
        x = bottom_left.x() + btn.width() - panel.width()
        y = bottom_left.y() + 6
        x = max(8, min(x, self.width() - panel.width() - 8))
        y = max(8, min(y, self.height() - panel.height() - 8))
        panel.move(x, y)

    def _on_panel_picked(self, backend: str) -> None:
        self._hide_model_panel()
        self._select_backend(backend)

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        # 电源按钮的事件不参与「点面板外面就收起」判断。
        # 原来这里还会在离线时用 QToolTip.showText 主动弹「不占用显存」气泡，
        # 0.39.1 按设计要求取消；但 return False 这层保护必须留着，否则面板
        # 打开时点电源按钮会被误判成「点了外面」而把面板收掉。
        if obj is getattr(self, "power_btn", None):
            return False
        # 输入框：聚焦就隐藏占位提示，失焦且为空再恢复（避免输入法拼音与占位文字重叠）
        if obj is getattr(self, "input", None):
            et = ev.type()
            if et == QEvent.Type.FocusIn:
                if not self.input.toPlainText().strip():
                    self.input.setPlaceholderText("")
            elif et == QEvent.Type.FocusOut:
                if not self.input.toPlainText().strip():
                    self.input.setPlaceholderText(_INPUT_PLACEHOLDER)
            return False
        # 模型面板打开时：点面板 / 按钮以外的地方、或按 Esc → 收起。
        panel = getattr(self, "_model_panel", None)
        if panel is not None and panel.isVisible():
            et = ev.type()
            if et == QEvent.Type.KeyPress and ev.key() == Qt.Key.Key_Escape:
                self._hide_model_panel()
                return True
            if et == QEvent.Type.MouseButtonPress:
                try:
                    gpos = ev.globalPosition().toPoint()
                except AttributeError:
                    gpos = ev.globalPos()
                widgets = [panel, self.model_btn]
                bub = getattr(panel, "bubble", None)
                if bub is not None:
                    widgets.append(bub)
                inside = False
                for w in widgets:
                    if w.isVisible() and w.rect().contains(w.mapFromGlobal(gpos)):
                        inside = True
                        break
                if not inside:
                    self._hide_model_panel()
        return False

    def _select_backend(self, new: str) -> None:
        new = new or "offline"
        if new == self._backend:
            return
        if self._busy:
            self._set_status("翻译中，请等本次翻译完成再切换模型")
            self._refresh_model_ui()
            return
        old = self._backend
        self._backend = new
        self._persist_backend(new)
        # 0.39.1：不再停旧模型，交给 Router 的 models-max/LRU 管理
        self._refresh_model_ui()

    def _row_state(self, backend: str) -> tuple[str, bool]:
        """给模型面板用：返回（状态文案, 是否当前选中）。"""
        current = (backend == self._backend)
        if backend == "offline":
            return ("当前" if current else "可用"), current
        if current:
            return "当前", True
        eng = self._hy_engine(backend)
        return ("已下载" if eng.has_files() else "未下载"), False

    def _persist_backend(self, value: str) -> None:
        # 记住这次选的后端：下次打开翻译窗口默认就用它（同时让 on_execute 的依赖预检对准）。
        # 同时更新内存配置：settings_set 可能只落盘，不保证同步刷新 ctx.settings，
        # 写入内存让「本次会话内下一次打开」也立即生效（与文档识别插件的 _cfg 思路一致）。
        try:
            self._api.settings_set("backend", value)
        except Exception:
            pass
        try:
            self._settings["backend"] = value
        except Exception:
            pass

    def _refresh_model_ui(self) -> None:
        """刷新右上角模型按钮、启停按钮，以及「翻译」按钮的可点状态。

        统一用「启动/停止模型」按钮表达状态（不再有单独的状态徽章）：
        - 离线：默认已启动，可手动停用（纯逻辑状态，不起进程）；
        - 本地大模型：未启动时翻译禁用，先点「启动模型」下载 + 起服务后才能翻，
          避免反复点翻译在后台并发起出多个 llama-server（每个几百 MB~1GB）导致 OOM。
        """
        self.model_btn.setText(f"{self._label_for(self._backend)}  ▾")
        self._ensure_lang_combos_for_backend()   # 离线只给中英，本地大模型给全部
        panel = getattr(self, "_model_panel", None)
        if panel is not None and panel.isVisible():
            panel.refresh()      # 面板开着时同步行（当前高亮在 refresh 里按 _backend 重算）
        self.power_btn.setVisible(True)
        if self._backend == "offline":
            self._offline_started = True        # 离线始终可用，禁止停止
            self._set_power(offer_stop=True)     # 文案「停止模型」
            # 保持 enabled 才能弹出 hover 气泡（Qt 里 disabled 控件不触发 tooltip）；
            # 但外观灰掉、光标是普通箭头、点击无反应 → 看起来/用起来都是「不可点」。
            self.power_btn.setEnabled(True)
            self.power_btn.setCursor(Qt.CursorShape.ArrowCursor)
            self.power_btn.setStyleSheet(_POWER_STOP_INERT_QSS)
            self.power_btn.setToolTip("离线翻译不占用显存（CPU 运行）")
            self.translate_btn.setEnabled(True)
            self.translate_btn.setToolTip("翻译原文（Ctrl+Enter）")
            return
        self.power_btn.setEnabled(True)
        self.power_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        if self._prep_busy:
            # 下载/启动进行中：这颗按钮就是状态显示器，别让用户再点一次
            self.power_btn.setText("准备中…")
            self.power_btn.setEnabled(False)
            self.translate_btn.setEnabled(False)
            self._set_power(offer_stop=False)
            self.power_btn.setText("准备中…")
            return
        eng = self._hy_engine(self._backend)
        if not eng.has_files():
            self._set_power(offer_stop=False)
            self.power_btn.setText("下载模型")     # 未下载 → 按钮显示「下载模型」
            self.power_btn.setToolTip("未下载，点击会先下载模型（几百 MB~1GB，只需一次），再启动")
            self.translate_btn.setEnabled(False)
            self.translate_btn.setToolTip(
                "请先在右上角「下载模型」，或切到「离线翻译」（约 70MB，中英互译）")
            self._set_status(
                f"{self._label_for(self._backend)} 未下载 · 点右上角「下载模型」，"
                f"或先切「离线翻译」（约 70MB，中英互译）")
        elif eng.running:
            self._set_power(offer_stop=True)
            self.power_btn.setToolTip("停止本地大模型进程释放内存（不影响已下载的模型）")
            self.translate_btn.setEnabled(True)
            self.translate_btn.setToolTip("翻译原文（Ctrl+Enter）")
        else:
            self._set_power(offer_stop=False)
            self.power_btn.setToolTip("启动本地大模型进程（已下载，几秒起好）")
            self.translate_btn.setEnabled(False)
            self.translate_btn.setToolTip("请先在右上角「启动模型」，再点翻译")

    def _set_power(self, offer_stop: bool) -> None:
        """启停按钮两种状态做出明显差异：启动=蓝色实心，停止=浅蓝描边。"""
        if offer_stop:
            self.power_btn.setText("停止模型")
            self.power_btn.setStyleSheet(_POWER_STOP_QSS)
        else:
            self.power_btn.setText("启动模型")
            self.power_btn.setStyleSheet(_POWER_START_QSS)

    def _toggle_engine(self) -> None:
        """启动 / 停止当前模型。
        - 离线：纯逻辑开关（默认已启动），停用后翻译按钮不可用；
        - 本地大模型：未启动先下载 + 起服务，已启动则停进程释放内存。"""
        if self._backend == "offline":
            return      # 离线始终可用，禁止停止
        eng = self._hy_engine(self._backend)
        if eng.running:
            eng.stop()
        else:
            self._ensure_model(self._backend)   # 已就绪则直接返回；否则下载 + 启动 + 预热（带进度）
        self._refresh_model_ui()

    # ---- 气泡里的下载 / 卸载 ----
    def _on_bubble_action(self, backend: str) -> None:
        self._hide_model_panel()      # 先收起面板/气泡，再弹下载进度或确认框
        if backend == "offline":
            return
        if self._hy_engine(backend).has_files():
            self._uninstall_model(backend)
        else:
            self._install_model(backend)

    def _install_model(self, backend: str) -> None:
        """下载并启用某个本地大模型：缺文件先下载、再起服务。

        切换是立刻就切的（模型面板的高亮先跟上），准备进度显示在窗口内；
        准备失败时按钮会恢复成「下载模型」，可以再点一次重试。
        """
        if self._backend != backend:
            self._backend = backend
            self._persist_backend(backend)
        self._pending_translate = False        # 这个是「启用」，不是「翻译」
        self._ensure_model(backend)
        self._refresh_model_ui()

    def _uninstall_model(self, backend: str) -> None:
        """卸载某个本地大模型：确认后停进程 + 删模型文件（运行时保留）。"""
        info = _BUBBLE_INFO.get(backend) or {}
        label = info.get("name") or self._label_for(backend)
        try:
            mb = model_engine.spec_of(backend).size_mb
        except Exception:
            mb = 0
        if not theme.ask_confirm(
                self, "卸载模型",
                f"卸载「{label}」？\n会先停掉模型再删除约 {mb}MB 的文件，"
                "以后要用需要重新下载。"):
            return
        try:
            self._hy_engine(backend).remove_model()
        except Exception as e:  # noqa: BLE001
            theme.show_warning(self, "卸载失败", f"删除模型文件失败：\n{e}")
            return
        self._refresh_model_ui()

    def _src_lang(self) -> str:
        """源语言：DIR_AUTO（自动检测）或具体语言代码。"""
        return self.src_combo.currentData() or DIR_AUTO

    def _tgt_lang(self) -> str:
        """目标语言代码。"""
        return self.tgt_combo.currentData() or "zh"

    @staticmethod
    def _select_combo(combo: QComboBox, data: str) -> None:
        i = combo.findData(data)
        if i >= 0:
            combo.setCurrentIndex(i)

    def _swap_langs(self) -> None:
        """交换源/目标语言；源为「自动检测」时把目标挪到源，目标回到中/英。"""
        src, tgt = self._src_lang(), self._tgt_lang()
        if src == DIR_AUTO:
            new_src = tgt
            new_tgt = "zh" if tgt != "zh" else "en"
        else:
            new_src, new_tgt = tgt, src
        self._select_combo(self.src_combo, new_src)
        self._select_combo(self.tgt_combo, new_tgt)
        if new_src != DIR_AUTO and new_src == new_tgt:
            self._set_status("源语言和目标语言相同，请换一个")

    def _lang_list_for_backend(self):
        """离线只支持中英；本地大模型支持全部语言。"""
        if self._backend == "offline":
            return [("zh", "中文"), ("en", "英语")]
        return LANGUAGES

    def _populate_lang_combos(self) -> None:
        """按当前后端的可选语言重建源/目标下拉（尽量保留原选择）。"""
        langs = self._lang_list_for_backend()
        cur_src, cur_tgt = self._src_lang(), self._tgt_lang()
        self.src_combo.blockSignals(True)
        self.src_combo.clear()
        self.src_combo.addItem("自动检测", DIR_AUTO)
        for code, name in langs:
            self.src_combo.addItem(name, code)
        self._select_combo(
            self.src_combo,
            cur_src if self.src_combo.findData(cur_src) >= 0 else DIR_AUTO)
        self.src_combo.blockSignals(False)

        self.tgt_combo.blockSignals(True)
        self.tgt_combo.clear()
        for code, name in langs:
            self.tgt_combo.addItem(name, code)
        if self.tgt_combo.findData(cur_tgt) < 0:
            cur_tgt = "zh" if self.tgt_combo.findData("zh") >= 0 else "en"
        self._select_combo(self.tgt_combo, cur_tgt)
        self.tgt_combo.blockSignals(False)

    def _ensure_lang_combos_for_backend(self) -> None:
        """后端在「离线 / 本地大模型」之间切换时，语言选项跟着变（离线只有中英）。"""
        sig = "offline" if self._backend == "offline" else "llm"
        if getattr(self, "_combo_sig", None) == sig:
            return
        self._combo_sig = sig
        self._populate_lang_combos()

    def _on_paste(self) -> None:
        try:
            text = self._api.clipboard_read() or ""
        except Exception:
            text = ""
        if text.strip():
            self.input.setPlainText(text)
            self._set_status("已粘贴剪贴板内容")
            self._maybe_auto_translate()      # 0.43：粘进来就自动翻
        else:
            self._set_status("剪贴板里没有文字")

    def _on_input_pasted(self) -> None:
        """原文框里发生粘贴（Ctrl+V / 右键 / 拖入）→ 自动翻译。"""
        self._set_status("已粘贴 · 正在自动翻译…")
        self._maybe_auto_translate()

    def _maybe_auto_translate(self) -> None:
        """粘贴后自动翻译的统一入口。

        本地大模型没启动时**不硬翻**：只提示一句，等用户启动模型后自己点「翻译」
        （否则每次粘贴都蹦「请先启动模型」，很吵）。离线后端不需要启动，直接翻。
        """
        if not self.input.toPlainText().strip():
            return
        if getattr(self, "_busy", False):
            return
        if self._backend != "offline":
            try:
                if not self._hy_engine(self._backend).running:
                    self._set_status("已粘贴 · 启动模型后点「翻译」即可")
                    return
            except Exception:
                return
        # 用 singleShot 让粘贴事件先收尾（直接调会在插入文本的中途重入）
        QTimer.singleShot(0, self._on_translate)

    def _on_reverse_translate(self) -> None:
        """反译：译文 → 原文，方向对调，再翻一次（双向翻译）。

        译文区可编辑，所以用户润色后的内容也会被拿去反译。
        """
        text = self.output.toPlainText().strip()
        if not text or text.startswith(("翻译中", "翻译失败", "首次使用", "（",
                                        "检测到", "源语言")):
            self._set_status("译文区没有可反译的内容")
            return
        self.input.setPlainText(text)
        self._swap_langs()                    # 源/目标对调（自动检测时按目标推进）
        self._set_status("已把译文作为原文，反向翻译中…")
        self._maybe_auto_translate()

    def _model_engine(self) -> ArgosEngine:
        """按当前配置构造引擎（模型查找目录顺序与 worker 里保持一致）。"""
        custom = (self._settings.get("models_dir") or "").strip()
        dirs = model_search_dirs(self._settings.get("plugin_dir") or "")
        if custom:
            dirs.insert(0, custom)
        return ArgosEngine(dirs, custom)

    def _ensure_offline_model(self, langpair: str) -> bool:
        """离线后端翻译前确保模型就绪：没有就先问用户，再带进度下载。

        为什么放在主线程做：下载要弹确认框和进度条，而 TranslateWorker 是后台
        线程，在里面弹窗会直接崩（Qt 要求 UI 操作必须在主线程）。
        """
        if self._model_engine().has_model(langpair):
            return True

        if not theme.ask_confirm(
                self, "需要下载离线翻译模型",
                "离线翻译模型尚未安装。\n\n"
                "需要下载约 70MB（只需一次，下载后即可完全断网使用）。\n\n现在下载吗？",
                yes_text="下载", no_text="暂不", default_yes=True):
            return False
        return self._download_with_progress(langpair)

    def _download_with_progress(self, langpair: str) -> bool:
        """弹进度条下载模型；返回是否成功。"""
        dlg = QProgressDialog("正在准备下载…", "取消", 0, 100, self)
        dlg.setWindowTitle("下载离线翻译模型")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setValue(0)

        worker = ModelDownloadWorker(self._model_engine(), langpair, self)
        loop = QEventLoop()
        ok = {"v": False}

        def on_prog(done: int, total: int) -> None:
            if total:
                dlg.setValue(int(done * 100 / total))
                dlg.setLabelText(
                    f"正在下载… {done / 1048576:.1f} / {total / 1048576:.1f} MB")
            if dlg.wasCanceled():
                worker.request_cancel()

        def on_ok(_path: str) -> None:
            ok["v"] = True
            loop.quit()

        def on_fail(msg: str) -> None:
            # 用户主动取消不算失败，不弹错误框
            if "取消" not in msg:
                theme.show_warning(self, "下载失败", f"离线模型下载失败：\n\n{msg}")
            loop.quit()

        worker.progress.connect(on_prog)
        worker.finished_ok.connect(on_ok)
        worker.failed.connect(on_fail)
        worker.finished.connect(loop.quit)
        worker.start()
        dlg.show()
        loop.exec()
        dlg.close()
        if worker.isRunning():
            worker.wait()
        return ok["v"]

    # ---- 本地大模型准备流程 ----
    def _hy_engine(self, model_id: str):
        """指定本地模型的引擎实例（按模型独立端口，与文档识别互不影响）。"""
        spec = model_engine.spec_of(model_id)
        return model_engine.get_engine(model_engine.models_root(), spec.port, model_id)

    def _ensure_model(self, model_id: str) -> bool:
        """模型后端翻译前确保服务就绪；返回 True 表示**现在就能翻**。

        0.39.5 起与文档识别插件统一：不再弹「现在下载吗」确认框、也不再开模态
        QProgressDialog。缺文件或进程没起都在窗口内显示进度（一行文字 + 6px 条），
        后台线程去下载/启动，本方法立刻返回 False 让调用方先别翻。
        """
        _cancel_model_release()          # 用户正在用，取消上一轮关窗后的延迟释放
        engine = self._hy_engine(model_id)
        if engine.has_files() and engine.ready():
            return True
        self._start_prepare(model_id)
        return False

    def _start_prepare(self, model_id: str) -> None:
        """后台准备模型：缺文件先下载，再起服务（进度显示在窗口内，不挡界面）。"""
        if self._prep_busy:
            return
        self._prep_busy = True
        spec = model_engine.spec_of(model_id)
        has_files = self._hy_engine(model_id).has_files()
        self.progress_box.setVisible(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.prepare_text.setText(
            f"正在加载 {spec.name}…" if has_files
            else f"首次使用需要下载 {spec.name}（约 {spec.size_mb}MB，只需一次，之后可完全离线）…")
        self.power_btn.setEnabled(False)
        self.power_btn.setText("准备中…")
        self.translate_btn.setEnabled(False)
        self._set_status(
            f"正在加载 {spec.name}…" if has_files
            else f"正在下载 {spec.name}（约 {spec.size_mb}MB）…")

        self._prep = ModelPrepareWorker(self._hy_engine(model_id), self)
        self._prep.progress.connect(self._on_prep_progress)
        self._prep.ready.connect(self._on_prep_ready)
        self._prep.failed.connect(self._on_prep_failed)
        self._prep.start()

    def _on_prep_progress(self, doc: str, pct) -> None:
        text = doc if pct is None else f"{doc}（{int(pct)}%）"
        self.prepare_text.setText(text)
        self._set_status(text)
        self.progress.setVisible(True)
        if pct is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(pct))

    def _on_prep_ready(self) -> None:
        self._prep_busy = False
        self.progress_box.setVisible(False)
        self.progress.setVisible(False)
        self.power_btn.setEnabled(True)
        self._refresh_model_ui()
        spec = model_engine.spec_of(self._backend)
        self._set_status(f"{spec.name} 已就绪 · 可以翻译了")
        # 用户是点「翻译」触发的准备：就绪后自动补上这一次翻译，不用再点一遍
        if self._pending_translate:
            self._pending_translate = False
            QTimer.singleShot(0, self._on_translate)

    def _on_prep_failed(self, msg: str) -> None:
        self._prep_busy = False
        self.progress_box.setVisible(True)
        self.progress.setVisible(False)
        self.prepare_text.setText(msg)
        self.power_btn.setEnabled(True)
        self._refresh_model_ui()
        self._set_status("模型准备失败，可再点一次右上角按钮重试")

    def _on_translate(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            self._set_status("请先输入或粘贴要翻译的文字")
            return
        src, tgt = self._src_lang(), self._tgt_lang()
        # 源=目标直接输出原文。**自动检测也要判**：中文原文 + 目标中文曾经被
        # _effective_target 偷偷改成英文（结果给出 "Test"），这里直接拦住更符合直觉。
        if _same_language(text, src, tgt):
            self.output.setPlainText(text)
            if src == DIR_AUTO:
                self._set_status(
                    f"检测到原文已经是{LANG_NAME.get(tgt, tgt)}，与目标语言相同，已直接输出原文")
            else:
                self._set_status("源语言=目标语言，已直接输出原文")
            return
        # 源语言与原文明显不符：别把中文硬喂给 en→zh 离线模型 —— 它会吐出 ***
        # 这种乱码（0.39.5 实测）。只在「纯中文」或「纯拉丁」这种能确定的情况下提示，
        # 中英混排、纯数字符号一律不插嘴（那时检测本来就不可靠）。
        if src in ("zh", "en"):
            cjk, latin = _lang_counts(text)
            if (cjk + latin) >= 2 and (cjk == 0 or latin == 0):
                got = _detected_lang(text)
                if got != src:
                    # 不一致就别翻了：直接把原文放进译文区（设计说明：
                    # 不要那一大段指引，看着累），状态栏一句话说明原因即可。
                    got_name = LANG_NAME.get(got, got)
                    self.output.setPlainText(text)
                    self._set_status(f"源语言与原文不符（像是{got_name}）· 已直接输出原文")
                    return

        # 本地大模型未启动 → 拦一层（Ctrl+Enter 快捷键也拦）
        if self._backend != "offline" and not self._hy_engine(self._backend).running:
            self._set_status("请先在右上角「启动模型」，模型就绪后再翻译")
            return
        # 防并发：翻译/准备模型期间再点翻译一律忽略。否则快速多点几下会并发进
        # _ensure_model → 并发 engine.start()，起出多个 llama-server（每个 ~1GB 常驻），
        # 内存爆掉后系统直接杀掉 LyLaunch（notify 兜底也拦不住系统 OOM 杀进程）→ 表现为闪退。
        if self._busy:
            self._set_status("正在翻译中，请稍候…")
            return
        self._busy = True
        self.translate_btn.setEnabled(False)
        started = False
        try:
            # 离线：只支持中英互译，其它语言提示改用本地大模型
            if self._backend == "offline":
                langpair = _offline_pair(text, src, tgt)
                if not langpair:
                    self.output.setPlainText(
                        "（离线翻译只支持中英互译。其它语言请点右上角切换到「本地大模型」后再翻。）")
                    self._set_status("离线只支持中英互译")
                    return
                if not self._ensure_offline_model(langpair):
                    self.output.setPlainText(
                        "（未下载离线模型，无法离线翻译。可再点一次翻译重新下载，"
                        "或在右上角切换回「离线翻译」。）")
                    self._set_status("未安装离线模型，翻译未开始")
                    return
            # 本地大模型：缺文件先询问+下载，再起服务；文件齐则直接起（都带进度）
            elif not self._ensure_model(self._backend):
                # 已经在后台下载/启动：就绪后自动接着翻这一次
                self._pending_translate = True
                spec = model_engine.spec_of(self._backend)
                self.output.setPlainText(
                    f"（{spec.name} 正在准备：首次需要下载约 {spec.size_mb}MB，"
                    "进度见上方进度条。准备好后会自动开始翻译。）"
                    if not self._hy_engine(self._backend).has_files()
                    else f"（{spec.name} 正在加载，几秒后自动开始翻译。）")
                self._set_status("模型准备中 · 就绪后自动翻译")
                return
            self.translate_btn.setText("翻译中…")
            self.output.clear()
            self._set_status("翻译中…")
            self._worker = TranslateWorker(text, self._backend, self._settings, src, tgt)
            self._worker.result_ready.connect(self._on_result)
            self._worker.error_occurred.connect(self._on_error)
            self._worker.finished.connect(self._on_worker_done)
            self._worker.start()
            started = True
        finally:
            # 只有「没真正发起翻译」的路径（空文本/下载取消/准备失败/异常）才在这里复位；
            # 发起成功时由 _on_worker_done 在后台线程结束后复位，避免提前恢复按钮。
            if not started:
                self._busy = False
                self._refresh_model_ui()
                self.translate_btn.setText("翻译")

    def _on_worker_done(self) -> None:
        self._refresh_model_ui()
        self.translate_btn.setText("翻译")
        self._busy = False

    def _on_result(self, text: str) -> None:
        self.output.setPlainText(text)
        chars = len(text.strip())
        self._set_status(f"翻译完成 · {chars} 字")

    def _on_error(self, msg: str) -> None:
        self.output.setPlainText(msg)
        self._set_status("翻译失败，原因见译文区")

    def _on_copy(self) -> None:
        text = self.output.toPlainText().strip()
        if text and not text.startswith(("翻译中", "翻译失败", "首次使用")):
            try:
                self._api.clipboard_write(text)
                self._set_status("译文已复制到剪贴板")
            except Exception:
                self._set_status("复制失败，剪贴板被占用")
        else:
            self._set_status("还没有可复制的译文")

    def closeEvent(self, event: QCloseEvent) -> None:
        self._hide_model_panel()      # 收起模型面板并移除 app 级事件过滤器
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait()
        # 下载/启动模型的线程不能留着窗口消失：WA_DeleteOnClose 会销毁窗口，
        # QThread 还在跑就是 "Destroyed while thread is still running"。
        if self._prep is not None and self._prep.isRunning():
            self._prep.request_cancel()
            self._prep.wait(6000)
        # 本地大模型进程常驻几百 MB~1GB，关窗后空闲一段时间自动释放（两个模型各自端口）
        if self._backend != "offline":
            try:
                secs = int(self._api.settings_get("engine_idle_release", 300) or 300)
            except Exception:
                secs = 300
            _schedule_model_release(secs)
        super().closeEvent(event)


_DLG = None   # 非模态单例


def _open_nonmodal(cls, api, settings, query: str = "") -> None:
    """非模态打开：不再 exec() 阻塞主程序；重复打开只把已有窗口提到前台。"""
    global _DLG
    if _DLG is not None:
        try:
            _DLG.raise_()
            _DLG.activateWindow()
            return
        except RuntimeError:
            _DLG = None
    dlg = cls(api, settings)
    if query:
        # 从搜索框带词进来也算「粘贴进入」：直接自动翻译（0.43 设计要求）。
        # 标记已处理，避免构造期那条「剪贴板预填自动翻」再触发一次。
        dlg.input.setPlainText(query)
        dlg._opened_auto_done = True
        QTimer.singleShot(0, dlg._maybe_auto_translate)
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

    def on_unload(self) -> None:
        # 关程序/禁用插件时把本地大模型进程一起清掉，不留 ~1GB 孤儿进程
        try:
            model_engine.shutdown_all()
        except Exception:
            pass

    def on_execute(self, query: str = "") -> None:
        # 依赖预检：缺失时弹明确提示并安全返回，不抛异常、不静默退出
        backend = (self.ctx.settings.get("backend") or "offline")   # 默认离线，见 __init__
        miss_cmd = {
            "offline": "ctranslate2 sentencepiece",
        }
        try:
            if backend == "offline":
                # 只查「包在不在」，**绝不 import**：ctranslate2 冷启动实测 5127ms
                # （它会把整套 C++ 运行时拖进来），在打开窗口的这条路径上同步 import
                # 会让插件明显卡住 —— 真正的 import 由后台线程在第一次翻译时做。
                import importlib.util as _iu
                missing = [m for m in ("ctranslate2", "sentencepiece")
                           if _iu.find_spec(m) is None]
                if missing:
                    raise ImportError("缺少 " + " / ".join(missing))
            else:
                # 本地大模型（hy-mt2 / hy-mt2-7b）无需 pip 依赖，运行时随包内置，模型首次使用时自动下载
                pass
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                None, "文本翻译",
                f"翻译后端【{backend}】依赖未安装，插件无法运行：\n\n"
                f"{type(e).__name__}: {e}\n\n"
                "请在终端运行以下命令安装：\n"
                f"python -m pip install {miss_cmd.get(backend, '')}",
            )
            return
        # 把插件目录透给翻译线程，供 model_search_dirs() 定位模型。
        # 打包后 plugin_dir 指向 exe 包内目录（只读），所以模型查找/下载分成了两套目录，
        # 不能再像以前那样直接往 plugin_dir 里写。
        self.ctx.settings["plugin_dir"] = self.ctx.plugin_dir
        _open_nonmodal(TranslateDialog, self.ctx.api, self.ctx.settings, query=query)
