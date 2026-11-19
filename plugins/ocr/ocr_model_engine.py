r"""图片转文字插件的「模型识别」引擎 —— 薄适配层（Router 版）。

真正的实现在 ``liytools/doc_core/doc_engine.py``（主程序共享层），
0.39.1 起它不再自己起 llama-server，而是统一走全局 Router（18090）。

这里保留插件用惯的接口形状，尽量少改插件代码：
- ``Engine`` / ``get_engine()`` / ``is_running()`` / ``release()``
- ``is_available()`` / ``missing_reason()`` / ``EngineError``
- ``PROMPT`` / ``MAX_TOKENS`` / ``MIN_SIDE`` / ``MAX_SIDE`` / ``PORT``
- ``md_to_text()``
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from liytools.doc_core import doc_engine as _dc
from liytools.doc_core.text_out import md_to_text  # noqa: F401  （对外保持原名）

# 端口与「文档识别」共用：同一个 Router 实例。
PORT = _dc.DEFAULT_PORT

# 默认用 Xiaomi-OCR-0（体积小、票据/截图稳）；OvisOCR2 按页读版面更准。
DEFAULT_MODEL_ID = "xiaomi-ocr-0"
MODEL_ID = DEFAULT_MODEL_ID          # 兼容旧引用

_SPEC = _dc.MODEL_BY_ID[DEFAULT_MODEL_ID]

PROMPT = _SPEC.prompt                # Xiaomi 训练时的 prompt，别改
MAX_TOKENS = _SPEC.max_tokens
MAX_SIDE = _dc.MAX_SIDE_FOR_MODEL    # 视觉 token 随像素面积涨，1600 是准度/速度平衡点
MIN_SIDE = 800                       # 太小的图模型看不清小字，温和放大

EngineError = _dc.EngineError


# ---------------------------------------------------------------- 模型清单
def spec_of(model_id: str | None = None):
    """拿模型规格；空值/脏值回退到默认模型（别让界面因为脏配置起不来）。"""
    return _dc.spec_of(model_id or DEFAULT_MODEL_ID)


def available_models() -> list:
    """已装好的模型列表（给下拉框用）。没装的不返回。"""
    return [m for m in _dc.MODELS if _dc.is_installed(m.id)]


# ---------------------------------------------------------------- 数据目录
def data_dir() -> Path:
    """引擎工作目录（与原「文档识别」一致）。"""
    return _dc.shared_data_dir()


def models_dir() -> Path:
    return _dc.models_root()


def runtime_root() -> Path:
    return _dc.shared_data_dir() / "runtime"


# ---------------------------------------------------------------- 可用性
def is_model_installed(model_id: str | None = None) -> bool:
    return _dc.is_installed(spec_of(model_id).id)


def find_runtime() -> Path | None:
    return _dc.get_engine(data_dir(), PORT, DEFAULT_MODEL_ID).find_runtime()


def is_available(model_id: str | None = None) -> bool:
    return is_model_installed(model_id) and find_runtime() is not None


def missing_reason(model_id: str | None = None) -> str:
    """给下拉框 tooltip / 状态行用的一句话，说清「缺什么、怎么补」。"""
    spec = spec_of(model_id)
    if not is_model_installed(spec.id):
        return f"需要先在「文档识别」插件里下载 {spec.name} 模型（约 {spec.total_mb}MB）"
    if find_runtime() is None:
        return "识别运行时缺失，请重新安装程序修复"
    return ""


# ---------------------------------------------------------------- 引擎适配
class Engine:
    """把 doc_core 的 Router 版 Engine 包装成插件原本用惯的签名。

    差异只有 ``start(cancelled=...)``：新版 Router 的 start 只收 progress，
    取消判断由 ModelOcrWorker 自己检查。
    """

    def __init__(self, inner: _dc.Engine, spec) -> None:
        self._inner = inner
        self._spec = spec

    def ready(self) -> bool:
        return self._inner.ready()

    def running(self) -> bool:
        return bool(self._inner.running)

    def memory_mb(self) -> int:
        try:
            return int(self._inner.memory_mb())
        except Exception:
            return 0

    @property
    def spec(self):
        return self._spec

    def start(self, progress: Callable | None = None,
              cancelled: Callable[[], bool] | None = None) -> None:
        """确保模型就绪（必要时由 Router 卸载旧模型、加载本模型）。"""
        self._inner.start(progress)

    def stop(self) -> None:
        self._inner.stop()

    def recognize(self, image_bytes: bytes, prompt: str | None = None,
                  max_tokens: int | None = None,
                  timeout: int = 600) -> tuple[str, float]:
        """prompt / max_tokens 不传就用**当前模型**的训练参数。"""
        return self._inner.recognize(
            image_bytes,
            prompt or self._spec.prompt,
            max_tokens or self._spec.max_tokens,
            timeout=timeout,
        )


def get_engine(model_id: str | None = None) -> Engine:
    spec = spec_of(model_id)
    return Engine(_dc.get_engine(data_dir(), PORT, spec.id), spec)


def is_running(model_id: str | None = None) -> bool:
    try:
        return bool(_dc.get_engine(data_dir(), PORT, spec_of(model_id).id).running)
    except Exception:
        return False


def release(model_id: str | None = None) -> None:
    """卸载 OCR 模型，不碰翻译/聊天正在用的模型。

    Router 下"释放" = 让 Router 卸掉这个模型，而不是杀进程；
    router 本身保持常驻（其他插件可能还在用）。
    双模型可同时驻留（--models-max 2），所以按模型粒度逐个判断，不看 loaded_id()。
    """
    try:
        from liytools.engine import EngineManager
        mgr = EngineManager.instance()
        ocr_ids = {m.id for m in _dc.MODELS}
        target = spec_of(model_id).id if model_id else None
        if target:
            mgr.unload(if_model=target)
        else:
            for one in mgr.loaded_ids():
                if one in ocr_ids:
                    mgr.unload(if_model=one)
    except Exception:
        pass
