"""本地 AI 插件的引擎层（0.39.1 起走全局 Router）。

不再自己起 llama-server：qwen3 / qwen3.5 / deepseek 等模型都通过统一 Router（18090）推理。
保留插件用惯的接口：``Engine`` / ``get_engine`` / ``MODELS`` / ``download_model`` / ``shutdown_all``。

⚠️ 关键修复：请求体必须带 ``model`` 字段（原版缺失 → Router 无法路由、静默用错模型）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from lylaunch.engine import downloads as _dl
from lylaunch.engine import registry as _reg
from lylaunch.engine.client import EngineError, ModelNotInstalled  # noqa: F401
from lylaunch.engine.manager import EngineManager

DEFAULT_PORT = 18090          # 全局 Router 端口（旧 18082 已废弃）
CTX_DEFAULT = 8192
DEFAULT_MODEL_ID = "qwen3-4b"

_CHAT_IDS = ("qwen3-4b", "qwen3-1.7b", "qwen3-vl-8b", "bonsai-25-27b",
             "qwen35-9b-v4-distill", "lfm25-8b-a1b", "deepseek-r1-14b")
_MS = "https://modelscope.cn/models/{repo}/resolve/master/{name}"


class Cancelled(Exception):
    """用户主动取消下载/启动。保留以兼容旧引用。"""


class ModelSpec:
    """兼容旧字段的规格（数据取自 registry.ModelDef）。"""

    def __init__(self, d) -> None:
        self.id = d.id
        self.name = d.display
        self.file = d.file
        self.size_mb = round(d.size / 1048576)
        self.repo = d.repo
        self.note = d.note
        self.mmproj = d.mmproj
        self.mmproj_size_mb = round(d.mmproj_size / 1048576)
        self.ctx = d.ctx
        self.reasoning = bool(d.reasoning)
        self.short_name = d.short_name

    @property
    def vision(self) -> bool:
        return bool(self.mmproj)

    @property
    def url(self) -> str:
        return _MS.format(repo=self.repo, name=self.file) if self.repo else ""

    @property
    def mmproj_url(self) -> str:
        return _MS.format(repo=self.repo, name=self.mmproj) if (self.mmproj and self.repo) else ""


MODELS = [ModelSpec(_reg.MODEL_BY_ID[i]) for i in _CHAT_IDS if i in _reg.MODEL_BY_ID]
MODEL_BY_ID = {m.id: m for m in MODELS}


def spec_of(model_id: str | None = None) -> ModelSpec:
    return MODEL_BY_ID.get(model_id or DEFAULT_MODEL_ID,
                           MODEL_BY_ID.get(DEFAULT_MODEL_ID, MODELS[0]))


def models_root() -> Path:
    return _reg.models_dir()


def is_installed(model_id: str) -> bool:
    return _reg.is_installed(_reg.spec_of(model_id))


def download_model(spec: ModelSpec, progress=None, cancelled=None,
                   paused=None, on_bytes=None) -> Path:
    """下载主模型（视觉模型连 mmproj）。运行时随包，不在这里下。

    paused / on_bytes 是 0.39.5 加的：独立下载页需要「暂停 / 续传」和真实字节
    （算速度、剩余时间）。不传就退回旧行为，老调用点无需改动。
    """
    _dl.ensure_model_files(_reg.spec_of(spec.id), progress, cancelled,
                           paused, on_bytes)
    return _reg.models_dir() / spec.id / spec.file


class Engine:
    """Router 版聊天引擎：接口与旧版一致，内部走 EngineManager。"""

    def __init__(self, data_dir, port: int = DEFAULT_PORT, ctx: int = CTX_DEFAULT,
                 model_id: str = DEFAULT_MODEL_ID) -> None:
        self.data_dir = Path(data_dir)
        self.port = int(port)
        self.ctx = ctx
        self.model_id = model_id if model_id in MODEL_BY_ID else DEFAULT_MODEL_ID
        self.last_usage: dict = {}
        self._mgr = EngineManager.instance()

    @property
    def spec(self) -> ModelSpec:
        return MODEL_BY_ID.get(self.model_id, MODELS[0])

    # ---------- 文件 / 状态（兼容旧接口）
    def find_runtime(self):
        return self._mgr._runtime_exe()

    def model_path(self, model_id: str | None = None) -> Path:
        mid = model_id or self.model_id
        s = MODEL_BY_ID.get(mid)
        return _reg.models_dir() / mid / (s.file if s else "")

    def mmproj_path(self, model_id: str | None = None) -> Path | None:
        mid = model_id or self.model_id
        s = MODEL_BY_ID.get(mid)
        if not s or not s.mmproj:
            return None
        return _reg.models_dir() / mid / s.mmproj

    def is_installed(self, model_id: str) -> bool:
        return _reg.is_installed(_reg.spec_of(model_id))

    def has_files(self) -> bool:
        return _reg.is_installed(_reg.spec_of(self.model_id))

    @property
    def running(self) -> bool:
        # 旧语义 = "本模型进程在跑"；Router 下 = 本模型处于 loaded（双模型可共存）
        try:
            return self._mgr.client.status_of(self.model_id) in ("loaded", "running", "ready")
        except Exception:
            return False

    def health_ok(self) -> bool:
        try:
            return self._mgr.client.health()
        except Exception:
            return False

    def ready(self) -> bool:
        return self.running

    def memory_mb(self) -> int:
        try:
            return int(self._mgr.status().get("memory_mb", 0))
        except Exception:
            return 0

    def start(self, progress=None) -> None:
        """确保 self.model_id 成为活跃模型（必要时 Router 按 models-max/LRU 调度）。"""
        self._mgr.ensure_model(self.model_id, progress)

    def stop(self) -> None:
        self._mgr.unload(if_model=self.model_id)

    def remove_model(self, model_id: str | None = None):
        """卸载并删除该模型所有本地副本（engine + 旧目录），返回 (ok, msg)。"""
        mid = model_id or self.model_id
        try:
            self._mgr.unload(if_model=mid)
        except Exception:
            pass
        return _reg.remove_files(_reg.spec_of(mid))

    # ---------- 对话（流式）
    def chat_stream_events(self, messages: list[dict], max_tokens: int = 1024,
                           temperature: float = 0.7, enable_thinking: bool = False,
                           images: list[str] | None = None
                           ) -> Iterator[tuple[str, str]]:
        """流式对话，逐段 yield ("think"|"text", piece)。"""
        msgs: list[dict] = messages
        if images:
            msgs = [dict(m) for m in messages]
            last = msgs[-1]
            parts: list[dict] = [{"type": "text", "text": str(last.get("content") or "")}]
            for b64 in images:
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
            last["content"] = parts

        self.last_usage = {}
        for kind, payload in self._mgr.client.chat_stream(
                self.model_id, msgs, timeout=600,
                max_tokens=max_tokens, temperature=temperature,
                chat_template_kwargs={"enable_thinking": bool(enable_thinking)}):
            if kind == "usage":
                self.last_usage = payload
                continue
            yield (kind, payload)

    def chat_stream(self, messages: list[dict], max_tokens: int = 1024,
                    temperature: float = 0.7, enable_thinking: bool = False,
                    images: list[str] | None = None) -> Iterator[str]:
        """只要正文的便捷版（预热等场景）。"""
        for kind, piece in self.chat_stream_events(
                messages, max_tokens, temperature, enable_thinking, images):
            if kind == "text":
                yield piece


_ENGINES: dict[int, Engine] = {}


def get_engine(data_dir, port: int = DEFAULT_PORT, ctx: int = CTX_DEFAULT,
               model_id: str = DEFAULT_MODEL_ID) -> Engine:
    key = int(port)
    e = _ENGINES.get(key)
    if e is None:
        e = Engine(data_dir, port, ctx, model_id)
        _ENGINES[key] = e
    elif model_id in MODEL_BY_ID:
        e.model_id = model_id        # SwitchWorker 会改 model_id 后 start
    return e


def shutdown_all() -> None:
    """卸载所有聊天模型（不杀 Router）。逐个判断，双模型共存时也能全清。"""
    try:
        mgr = EngineManager.instance()
        for one in mgr.loaded_ids():
            if one in MODEL_BY_ID:
                mgr.unload(if_model=one)
    except Exception:
        pass
