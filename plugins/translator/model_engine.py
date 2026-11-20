"""翻译插件的本地大模型引擎（0.39.1 起走全局 Router）。

不再自己起 llama-server：hy-mt2（1.8B）/ hy-mt2-7b 都通过统一 Router（18090）推理。
保留插件用惯的接口：``spec_of`` / ``get_engine`` / ``shutdown_all`` / ``SPECS`` / ``Engine``。
采样参数从旧版命令行挪到请求体（Router 的 preset 不承载采样）。
"""
from __future__ import annotations

import time
from pathlib import Path

from lylaunch.engine import registry as _reg
from lylaunch.engine.client import EngineError
from lylaunch.engine.manager import EngineManager

PORT = 18090                     # 全局 Router 端口（旧 18082/18083 已废弃）
DEFAULT_MODEL_ID = "hy-mt2"


class Cancelled(Exception):
    """用户主动取消下载/启动。保留以兼容旧引用。"""


# ---------------------------------------------------------------- prompt 模板
def _hy_prompt(text: str, src_name: str, tgt_name: str) -> str:
    """Hy-MT2 官方翻译模板：只输出译文、不要解释。语言名用全称。"""
    which = f"{src_name}文本" if src_name else "文本"
    return (f"将以下{which}翻译为{tgt_name}，注意只需要输出翻译后的结果，"
            f"不要额外解释：{text}")


_PROMPTS = {"hy-mt2": _hy_prompt, "hy-mt2-7b": _hy_prompt}


class ModelSpec:
    """兼容旧字段的规格（数据取自 registry.ModelDef）。"""

    def __init__(self, d, prompt_func) -> None:
        s = d.sampling or {}
        self.id = d.id
        self.name = d.display
        self.license = d.license
        self.quant = "Q4_K_M"
        self.gguf = d.file
        self.size = d.size
        self.sha256 = d.sha256
        self.repos = d.repos
        self.ctx = d.ctx
        self.requires_jinja = d.jinja
        self.temp = s.get("temperature", 0.7)
        self.top_p = s.get("top_p", 0.9)
        self.top_k = s.get("top_k", 20)
        self.repeat_penalty = s.get("repeat_penalty", 1.05)
        self.min_p = s.get("min_p", 0.0)
        self.max_tokens = s.get("max_tokens", 1024)
        self.port = PORT
        self._prompt = prompt_func

    @property
    def size_mb(self) -> int:
        return round(self.size / 1048576)

    def build_prompt(self, text: str, src_name: str, tgt_name: str) -> str:
        return self._prompt(text, src_name, tgt_name)


SPECS = [ModelSpec(_reg.MODEL_BY_ID[mid], fn) for mid, fn in _PROMPTS.items()
         if mid in _reg.MODEL_BY_ID]
MODEL_BY_ID = {s.id: s for s in SPECS}


def spec_of(model_id: str | None = None) -> ModelSpec:
    """空值/脏值回退到默认模型。"""
    return MODEL_BY_ID.get(model_id or DEFAULT_MODEL_ID,
                           MODEL_BY_ID.get(DEFAULT_MODEL_ID, SPECS[0]))


def models_root() -> Path:
    return _reg.models_dir()


class Engine:
    """Router 版翻译引擎：接口与旧版一致，内部走 EngineManager。"""

    def __init__(self, model_id: str) -> None:
        self._spec = spec_of(model_id)
        self._mgr = EngineManager.instance()

    @property
    def spec(self) -> ModelSpec:
        return self._spec

    @property
    def port(self) -> int:
        return PORT

    @property
    def running(self) -> bool:
        # 旧语义 = "本模型进程在跑"；Router 下 = 本模型处于 loaded（双模型可共存）
        try:
            return self._mgr.client.status_of(self._spec.id) in ("loaded", "running", "ready")
        except Exception:
            return False

    def ready(self) -> bool:
        return self.running

    def has_files(self) -> bool:
        return _reg.is_installed(_reg.spec_of(self._spec.id))

    def ensure_files(self, progress=None, cancelled=None,
                     paused=None, on_bytes=None) -> None:
        """下载缺失的模型文件（运行时随包内置，不在这里下）。

        0.39.1 全局引擎重构时给了 doc_core / llm_chat 下载能力，翻译这份漏了：
        主界面点「现在下载吗 → Yes」后直接 `start()` → `ensure_model()` 见文件不在
        就抛 ModelNotInstalled（"请先在对应插件的模型面板里下载"），于是点了几次
        都下不下来。这里补上真正的下载，并把取消统一成插件的 Cancelled。
        """
        from lylaunch.engine import downloads
        try:
            downloads.ensure_model_files(_reg.spec_of(self._spec.id),
                                         progress, cancelled, paused, on_bytes)
        except downloads.DownloadCancelled:
            raise Cancelled() from None

    def memory_mb(self) -> int:
        try:
            return int(self._mgr.status().get("memory_mb", 0))
        except Exception:
            return 0

    def start(self, progress=None) -> None:
        """确保本模型成为唯一活跃模型（必要时卸载别的）。"""
        self._mgr.ensure_model(self._spec.id, progress)

    def stop(self) -> None:
        self._mgr.unload(if_model=self._spec.id)

    def remove_model(self):
        """卸载并删除该模型的所有本地副本（engine + 旧目录），返回 (ok, msg)。"""
        try:
            self._mgr.unload(if_model=self._spec.id)
        except Exception:
            pass
        return _reg.remove_files(_reg.spec_of(self._spec.id))

    def translate(self, text: str, src_name: str, tgt_name: str) -> str:
        if not self.running:
            raise EngineError("模型没有运行（可能已被释放），请重新打开翻译")
        prompt = self._spec.build_prompt(text, src_name, tgt_name)
        resp = self._mgr.client.chat(
            self._spec.id,
            [{"role": "user", "content": prompt}],
            timeout=600,
            temperature=self._spec.temp,
            top_p=self._spec.top_p,
            top_k=self._spec.top_k,
            repeat_penalty=self._spec.repeat_penalty,
            min_p=self._spec.min_p,
            max_tokens=self._spec.max_tokens,
        )
        try:
            return (resp.get("choices") or [{}])[0]["message"]["content"].strip()
        except Exception:
            return ""


_ENGINES: dict[str, Engine] = {}


def get_engine(data_dir, port: int = PORT, model_id: str = DEFAULT_MODEL_ID) -> Engine:
    """按模型复用句柄（data_dir/port 仅为兼容旧签名，统一走 Router）。"""
    mid = model_id if isinstance(model_id, str) else getattr(model_id, "id", "")
    spec = spec_of(mid)
    eng = _ENGINES.get(spec.id)
    if eng is None:
        eng = Engine(spec.id)
        _ENGINES[spec.id] = eng
    return eng


def shutdown_all() -> None:
    """卸载所有翻译模型（不杀 Router）。逐个判断，双模型共存时也能全清。"""
    try:
        mgr = EngineManager.instance()
        for one in mgr.loaded_ids():
            if one in MODEL_BY_ID:
                mgr.unload(if_model=one)
    except Exception:
        pass
