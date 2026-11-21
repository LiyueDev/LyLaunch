"""作者署名（程序「设置 → 关于」页取这里）。"""

from __future__ import annotations

DEVELOPER = "LiyueDev"
PRODUCT = "LyLaunch 轻快启动"
COPYRIGHT = "Copyright (C) 2026 LiyueDev"

# 对外链接（设置 → 关于 → 链接）：源码仓库与作者主页
REPO_URL = "https://github.com/LiyueDev/LyLaunch"
HOMEPAGE_URL = "https://liyuedev.github.io/"


def about_lines(version: str) -> list[str]:
    """「关于」卡片正文：版本 + 定位 + 作者署名。"""
    return [
        f"{PRODUCT}  {version}",
        "常用软件固定 · 拼音搜索 · 快捷启动",
        "",
        f"开发者：{DEVELOPER}",
        COPYRIGHT,
    ]
