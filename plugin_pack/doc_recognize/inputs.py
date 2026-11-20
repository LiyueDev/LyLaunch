"""转发：真身在 ``lylaunch/doc_core/inputs.py``（主程序共享层）。

**不要在这里写实现**，改输入适配（PDF / Excel / Word / TXT 的解析）请改
`lylaunch/doc_core/inputs.py`。
"""

from __future__ import annotations

from lylaunch.doc_core.inputs import (  # noqa: F401
    KIND_EXCEL,
    KIND_IMAGE,
    KIND_PDF,
    KIND_TEXT,
    KIND_DOCX,
    KIND_UNSUPPORTED,
    SUPPORT_HINT,
    Loaded,
    Page,
    load,
)
