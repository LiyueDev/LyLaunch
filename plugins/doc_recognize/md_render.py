"""转发：真身在 ``lylaunch/doc_core/md_render.py``（主程序共享层）。

**不要在这里写实现**。

注意多导出了 `_WIDGET_CSS` —— 插件界面用它给 QTextBrowser 注入样式，
`import *` 不会带下划线开头的名字，所以必须显式列出。
"""

from __future__ import annotations

from lylaunch.doc_core.md_render import (  # noqa: F401
    _WIDGET_CSS,
    clean,
    latex_to_text,
    latex_to_unicode,
    md_to_body,
    preprocess,
    to_html,
    to_plain,
)
