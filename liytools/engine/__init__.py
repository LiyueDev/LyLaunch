"""全局本地推理引擎（llama.cpp Router 单入口调度）。

对外只暴露：EngineManager（生命周期）、RouterClient（HTTP）、registry（模型清单）。
插件不应 import 这里的进程细节，只经 PluginAPI 的 engine_* 方法。
"""
from __future__ import annotations

from .client import EngineError, RouterClient
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

__all__ = [
    "EngineManager",
    "RouterClient",
    "EngineError",
    "DEFAULT_PORT",
    "MODELS",
    "MODEL_BY_ID",
    "DEFAULT_MODEL_ID",
    "ModelDef",
    "by_cap",
    "is_installed",
    "spec_of",
]
