"""全局本地推理引擎（llama.cpp Router 调度）。

对外暴露：EngineManager（插件共用的 18090）、ExternalEngine（外部集成专用的 18091）、
RouterClient（HTTP）、registry（模型清单）、shutdown_all（退出时两套一起收）。
插件不应 import 这里的进程细节，只经 PluginAPI 的 engine_* 方法。
"""
from __future__ import annotations

from .client import EngineError, RouterClient
from .external import ExternalEngine, EXTERNAL_PORT
from .manager import DEFAULT_PORT, EngineManager
from .registry import (
    DEFAULT_MODEL_ID,
    MODEL_BY_ID,
    MODELS,
    ModelDef,
    by_cap,
    is_installed,
    spec_of,
)


def shutdown_all() -> None:
    """退出收口：插件那套和外部那套都要杀，别留孤儿进程占端口。"""
    for mgr in (EngineManager.instance(), ExternalEngine.instance()):
        try:
            mgr.shutdown()
        except Exception:
            pass


__all__ = [
    "EngineManager",
    "ExternalEngine",
    "RouterClient",
    "EngineError",
    "DEFAULT_PORT",
    "EXTERNAL_PORT",
    "MODELS",
    "MODEL_BY_ID",
    "DEFAULT_MODEL_ID",
    "ModelDef",
    "by_cap",
    "is_installed",
    "spec_of",
    "shutdown_all",
]
