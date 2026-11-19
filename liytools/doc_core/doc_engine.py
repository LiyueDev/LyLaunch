"""文档识别核心：模型规格 + 推理（0.39.1 起走全局 Router 引擎）。

不再自己管理 llama-server 进程：所有推理通过 liytools.engine.EngineManager
向统一 Router（127.0.0.1:18090）发请求。保留原有公共 API 形状
（Engine / get_engine / spec_of / is_installed / remove_model / shared_data_dir …），
让「图片转文字」「文档识别」两个插件改动最小。

历史：0.35/0.36 把引擎从插件上提到主程序；0.39.1 再上提到全局 Router。
"""
from __future__ import annotations

import base64
import time
from pathlib import Path

from liytools.engine import registry as _reg
from liytools.engine.client import EngineError
from liytools.engine.manager import EngineManager

RUNTIME_TAG = "b12047"
DEFAULT_PORT = 18090          # 全局 Router 端口（旧 18081 已废弃）
MAX_SIDE_FOR_MODEL = 1600     # 视觉 token 随像素面积涨，长边超过这个数容易顶爆上下文
DEFAULT_MODEL_ID = "xiaomi-ocr-0"


class Cancelled(Exception):
    """用户主动取消（下载/启动中途关掉窗口）。保留以兼容旧引用。"""


# ---- 只暴露 OCR 模型（文档识别 / 图片转文字用）----
MODELS = [m for m in _reg.MODELS if "ocr" in m.caps]
MODEL_BY_ID = {m.id: m for m in MODELS}
ModelSpec = _reg.ModelDef


def spec_of(mid: str | None = None):
    """脏值/空值安全回退到默认 OCR 模型。"""
    return MODEL_BY_ID.get(mid or "", MODELS[0])


def is_installed(spec_or_id) -> bool:
    spec = spec_of(spec_or_id) if isinstance(spec_or_id, str) else spec_or_id
    return _reg.is_installed(spec)


def models_root() -> Path:
    """统一模型目录（engine/models）。兼容旧引用。"""
    return _reg.models_dir()


def shared_data_dir() -> Path:
    """引擎工作目录（engine/）。旧代码用它做 get_engine 的 key，现在统一忽略。"""
    return _reg.engine_dir()


def remove_model(spec_or_id):
    """删除某模型的所有本地副本（engine + 旧目录硬链接），返回 (ok, msg)。"""
    spec = spec_of(spec_or_id) if isinstance(spec_or_id, str) else spec_or_id
    try:
        EngineManager.instance().unload(if_model=spec.id)
    except Exception:
        pass
    return _reg.remove_files(spec)


class Engine:
    """Router 版引擎：接口形状与旧版一致，内部走 EngineManager。"""

    def __init__(self, data_dir: Path, port: int, spec) -> None:
        self.data_dir = Path(data_dir)
        self.port = int(port)
        self.spec = spec
        self._mgr = EngineManager.instance()
        self.last_action = ""   # 兼容旧 UI 的 "reuse"/"download" 提示

    # ---- 状态
    @property
    def running(self) -> bool:
        # 旧语义 = "本模型进程在跑"；Router 下 = 本模型处于 loaded（双模型可共存）
        try:
            return self._mgr.client.status_of(self.spec.id) in ("loaded", "running", "ready")
        except Exception:
            return False

    def ready(self) -> bool:
        try:
            return self._mgr.client.status_of(self.spec.id) in ("loaded", "running", "ready")
        except Exception:
            return False

    def memory_mb(self) -> int:
        try:
            return int(self._mgr.status().get("memory_mb", 0))
        except Exception:
            return 0

    def find_runtime(self):
        return self._mgr._runtime_exe()

    def has_files(self) -> bool:
        """运行时与模型是否都已具备（只看文件，不启动进程）。"""
        return _reg.is_installed(self.spec)

    def ensure_files(self, progress=None, cancelled=None) -> None:
        """下载缺失的模型文件（运行时随包，不在这里下）。

        cancelled 必须一路传到 HTTP 读循环，否则「取消下载」只是置了个标志，
        下载线程仍会跑完（0.39.1 修复）。
        """
        from liytools.engine import downloads
        downloads.ensure_model_files(self.spec, progress, cancelled)

    def model_info(self) -> dict:
        """给界面展示的模型信息（"我正在用哪个模型 / 多大"）。"""
        import re
        spec = self.spec
        m = re.search(r"[.-](Q\d[A-Z_]*|BF16|F16|F32)\.gguf$", spec.file, re.I)
        quant = m.group(1).upper() if m else ""
        exe = self._mgr._runtime_exe()
        return {
            "id": spec.id,
            "name": spec.name,
            "note": spec.note,
            "license": spec.license,
            "quant": quant,
            "size_mb": spec.total_mb,
            "engine": f"llama.cpp {RUNTIME_TAG}",
            "files": [f for f in (spec.file, spec.mmproj) if f],
            "dir": str(_reg.models_dir() / spec.id),
            "exe": str(exe) if exe else "",
        }

    # ---- 生命周期
    def start(self, progress=None, cancelled=None) -> None:
        """文件齐 → 让本模型成为活跃模型；缺文件 → 先下载（0.39.1 补齐）。

        以前这里只有 ensure_model()，而它遇到缺文件会直接抛 ModelNotInstalled，
        所以「下载并启用」其实从没真正触发过下载。现在先 ensure_files 再起模型；
        用户取消时转成插件层一直在捕获的 Cancelled，不弹错误框。
        """
        from liytools.engine.downloads import DownloadCancelled
        try:
            self.ensure_files(progress, cancelled)
        except DownloadCancelled:
            raise Cancelled()
        self._mgr.ensure_model(self.spec.id, progress)

    def stop(self) -> None:
        """卸载本模型（若它正是活跃模型）。不杀 router。"""
        self._mgr.unload(if_model=self.spec.id)

    # ---- 推理
    def recognize(self, image_bytes: bytes, prompt: str | None = None,
                  max_tokens: int | None = None,
                  timeout: int = 600) -> tuple[str, float]:
        p = prompt if prompt is not None else self.spec.prompt
        mt = int(max_tokens or self.spec.max_tokens)
        sampling = self.spec.sampling or {}
        b64 = base64.b64encode(image_bytes).decode("ascii")
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": p},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + b64}},
        ]}]
        t0 = time.time()
        resp = self._mgr.client.chat(
            self.spec.id, msgs, timeout=timeout,
            temperature=sampling.get("temperature", 0),
            top_p=sampling.get("top_p", 0.8),
            repeat_penalty=sampling.get("repeat_penalty", 1.05),
            max_tokens=mt,
        )
        try:
            text = (resp.get("choices") or [{}])[0]["message"]["content"].strip()
        except Exception:
            text = ""
        return text, time.time() - t0


_ENGINES: dict[tuple, Engine] = {}


def get_engine(data_dir, port: int = DEFAULT_PORT,
               model_id: str = DEFAULT_MODEL_ID) -> Engine:
    mid = model_id if isinstance(model_id, str) else getattr(model_id, "id", "")
    spec = spec_of(mid)
    key = (str(data_dir), int(port), spec.id)
    eng = _ENGINES.get(key)
    if eng is None:
        eng = Engine(data_dir, port, spec)
        _ENGINES[key] = eng
    return eng


def shutdown_all() -> None:
    """停止整个 Router（程序退出 / 全量释放时用）。"""
    try:
        EngineManager.instance().shutdown()
    except Exception:
        pass
    _ENGINES.clear()
