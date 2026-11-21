"""转发：真身在 ``lylaunch/doc_core/doc_engine.py``（主程序共享层）。

**不要在这里写实现。**

0.35 之前这里是一份完整的引擎（约 1000 行），而内置的「图片转文字」插件装不到
可选插件里的模块，于是又复制了一份 805 行的副本，导致两套代码两个端口。
0.36 把引擎上提到主程序后，这里只留转发，插件包结构保持不变（zip 里仍是 5 个文件）。

改引擎请改 `lylaunch/doc_core/doc_engine.py`。
"""

from __future__ import annotations

from lylaunch.doc_core.doc_engine import (  # noqa: F401
    DEFAULT_MODEL_ID,
    DEFAULT_PORT,
    MAX_SIDE_FOR_MODEL,
    MODEL_BY_ID,
    MODELS,
    RUNTIME_TAG,
    Cancelled,
    Engine,
    EngineError,
    ModelSpec,
    get_engine,
    is_installed,
    models_root,
    shared_data_dir,
    remove_model,
    shutdown_all,
    spec_of,
)
