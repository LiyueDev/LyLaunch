"""统一的搜索相关度打分：应用与插件共用同一套阶梯。

为什么单独抽一份：首页搜索结果把"应用"和"插件"混在同一个列表里按相关度排序，
如果两边各写一套阈值，排序就会出现"A 明明更像、却排在 B 后面"的错位。这里把
打分收拢成单一实现，阈值改一处两边同步。

阶梯（分数越大越相关）：
    100  名称完全相等              截图 ← 截图
     92  名称前缀                  Visual Suite ← visual
     78  附加词完全相等            文本翻译 ← fy（插件的 Keywords / Id）
     74  名称拼音首字母前缀         截图 ← jt
     72  名称子串（内部命中）       ComfyUI ← fy
     68  名称全拼前缀              截图 ← jie
     62  附加词子串                图片转文字 ← 截图识别
     60  名称拼音内部
     56  附加词拼音首字母前缀       图片转文字 ← jt（关键词"截图识别"）
     52  附加词全拼前缀
     48  附加词拼音内部

几个刻意的取舍：
- "名称拼音首字母前缀"(74) 高于"名称子串"(72)：中文用户主要靠拼音输入，
  搜 set 时"设置"应该排在名字里凑巧含 set 的 "Inno Setup" 前面。
- "附加词完全相等"(78) 高于"名称子串"(72)：用户打出了插件关键词的原词
  （如 fy / 翻译），比名字里凑巧含这两个字母更可信。
- 附加词（关键词）的拼音命中整体低于名称拼音，避免"关键词里带截图识别的 OCR"
  盖过名字就叫"截图"的插件。

0 表示不命中。应用没有"附加词"，只走名称/拼音档；插件把 Keywords 和 Id 当附加词。
"""

from __future__ import annotations

from typing import Iterable

_NAME_PIN_SCORE = {3: 74, 2: 68, 1: 60}
_TERM_PIN_SCORE = {3: 56, 2: 52, 1: 48}


def pin_level(pin: str, full: str, q: str) -> int:
    """单个词的拼音匹配等级：3 首字母前缀 / 2 全拼前缀 / 1 内部命中 / 0 不命中。

    首字母允许按词（含英文词）分别前缀匹配；连写串同样支持，
    如 "图片转文字" → "tpzwz"，搜 "tpz" / "tpzwz" 都能命中。
    """
    if not q:
        return 0
    pin = pin or ""
    pin_joined = pin.replace(" ", "")
    full = full or ""
    if pin_joined.startswith(q) or any(t.startswith(q) for t in pin.split()):
        return 3
    if full.startswith(q) or any(f.startswith(q) for f in full.split()):
        return 2
    if q in pin_joined or q in full:
        return 1
    return 0


def rank_score(q: str, name: str, pinyin: str = "", pinyin_full: str = "",
               terms: Iterable[str] = ()) -> int:
    """相关度打分，0 = 不命中。

    q           已小写化的查询词
    name        显示名称（应用名 / 插件名）
    pinyin      名称的拼音首字母串（可含空格分割的英文词）
    pinyin_full 名称的拼音全拼
    terms       附加词：插件放 Keywords + Id，应用留空
    """
    q = (q or "").strip().lower()
    if not q:
        return 0

    name = (name or "").lower()
    if name == q:
        return 100
    if name.startswith(q):
        return 92

    best = 72 if (name and q in name) else 0          # 名称内部子串
    if pinyin or pinyin_full:                          # 名称拼音四档
        best = max(best, _NAME_PIN_SCORE.get(pin_level(pinyin, pinyin_full, q), 0))

    terms = [t for t in terms if t]
    if terms:
        from .pinyin import get_pinyin

        for w in terms:
            wl = w.strip().lower()
            if not wl:
                continue
            if wl == q:
                best = max(best, 78)
            elif q in wl:
                best = max(best, 62)
            else:
                # 附加词也吃拼音（关键词多为中文），逐词比对避免跨词拼串误命中
                best = max(best, _TERM_PIN_SCORE.get(pin_level(*get_pinyin(w), q), 0))
    return best
