"""拼音搜索辅助：把中文名转成"首字母 + 全拼"，结果按名缓存。

对应 C# 的 GetPinyin / PinyinHelper（TinyPinyin.Net）。
例：微信 -> 首字母 "wx"，全拼 "weixin"。搜索时打 "wx" 或 "weixin" 都能命中。
"""

from __future__ import annotations

from functools import lru_cache

# pypinyin 本身有缓存，这里再加一层 dict 避免重复计算（与 C# _pinyinCache 等价）
_cache: dict[str, tuple[str, str]] = {}


def get_pinyin(name: str) -> tuple[str, str]:
    """返回 (首字母, 全拼)，全部小写。空名返回 ('', '')。"""
    if not name:
        return ("", "")
    if name in _cache:
        return _cache[name]
    try:
        # ⚠️ 延迟导入：pypinyin 会加载完整拼音词典，实测占 49MB 内存。
        # 启动阶段（渲染首页瓦片）用不到拼音，只有搜索才需要，所以放到这里。
        from pypinyin import Style, pinyin
        initials = "".join(
            p[0] for p in pinyin(name, style=Style.FIRST_LETTER, heteronym=False)
        ).lower()
        full = "".join(
            p[0] for p in pinyin(name, style=Style.NORMAL, heteronym=False)
        ).replace(" ", "").lower()
    except Exception:
        initials, full = "", ""
    res = (initials, full)
    _cache[name] = res
    return res
