"""会话存储：每个会话一个 JSON 文件，放在 llm_chat/sessions/ 下。

- list_sessions()  按更新时间倒序（侧边栏用）
- load(id) / save() / delete(id)
- build_context()  从完整历史挑出「这次发给模型」的部分（只读，不改历史）
- 标题取第一条用户消息的前 24 个字

上下文刻意**不写额外的记忆文件**：完整对话就在会话 JSON 的 messages 里，
发给模型前临时裁剪一份副本即可。这样「删会话」天然等于「删掉它的全部记忆」，
不会留下需要单独回收的摘要/缓存文件。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "LyLaunch" / "llm_chat" / "sessions"


@dataclass
class Session:
    id: str
    title: str
    model_id: str
    created: float
    updated: float
    messages: list = field(default_factory=list)
    pinned: bool = False          # 置顶：排序时固定在最前
    title_manual: bool = False    # 用户手动改过名 → save() 不再用首条消息覆盖


def _path(sid: str) -> Path:
    return _root() / f"{sid}.json"


def new_session(model_id: str, messages: list | None = None) -> Session:
    now = time.time()
    return Session(id=uuid.uuid4().hex[:12], title="新会话", model_id=model_id,
                   created=now, updated=now, messages=messages or [])


# ── 请求上下文构建（只产副本，绝不改持久化）─────────────────
MAX_CONTEXT_TURNS = 12


def _msg_chars(m: dict) -> int:
    c = m.get("content")
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):          # 多模态 parts（正常不会存进 JSON）
        return sum(len(str(p.get("text") or ""))
                   for p in c if isinstance(p, dict))
    return len(str(c or ""))


def build_context(messages: list, budget_chars: int,
                  max_turns: int = MAX_CONTEXT_TURNS) -> list[dict]:
    """从完整会话历史构造「这一次发给模型」的消息列表，绝不修改入参。

    以前 _send() 直接 pop(0) 裁剪 self._messages，而 _save_current() 又把
    裁剪后的列表写回 JSON —— 等于每问一轮就永久删掉更早的对话，模型自然
    「没记忆」，界面上旧图片卡片还会因为引用丢失被 GC 删掉大图。

    规则：
    - 只按「完整轮」从最旧往新丢，保证结果第一条一定是 user（system 除外）
    - 当前这一轮永远保留；旧版裁剪留下的孤立 assistant 会被跳过
    - 单条消息过长（附件全文）单独截断，保留开头
    - 返回干净副本：剥掉 _ui（界面元数据，不能进 OpenAI 兼容请求）
    """
    sys_msgs = [dict(m) for m in messages
                if isinstance(m, dict) and m.get("role") == "system"]
    body = [m for m in messages
            if isinstance(m, dict) and m.get("role") != "system"]

    while body and body[0].get("role") != "user":
        body.pop(0)                  # 兼容旧版裁剪留下的孤儿 assistant

    turns: list[list[dict]] = []
    cur: list[dict] = []
    for m in body:
        if m.get("role") == "user" and cur:
            turns.append(cur)
            cur = []
        cur.append(m)
    if cur:
        turns.append(cur)

    picked: list[list[dict]] = []
    used, count = 0, 0
    for turn in reversed(turns):
        cost = sum(_msg_chars(m) for m in turn)
        if count >= max_turns or (picked and used + cost > budget_chars):
            break
        picked.append(turn)
        used += cost
        count += 1
    picked.reverse()

    out: list[dict] = []
    for m in sys_msgs + [m for t in picked for m in t]:
        # _ui 是界面元数据；reasoning 是历史思考过程 —— 两者都不能发给模型
        clean = {k: v for k, v in m.items() if k not in ("_ui", "reasoning")}
        c = clean.get("content")
        if isinstance(c, str) and len(c) > budget_chars:
            clean["content"] = c[:budget_chars] + "\n…（这条消息过长，已截断）"
        out.append(clean)
    return out


_FILE_BLOCK = re.compile(r"【文件：[^】]+】.*?【文件结束】\s*", re.S)
_FILE_NAME = re.compile(r"【文件：([^】]+)】")


def title_from(messages: list) -> str:
    """会话标题：取第一条用户消息里的「真正问题」，剥掉附件全文。

    否则带附件的会话标题会变成「【文件：xxx.py】def ...」这种一长串代码。
    """
    for m in messages:
        if m.get("role") == "user" and m.get("content"):
            raw = str(m["content"])
            t = _FILE_BLOCK.sub("", raw).replace("\n", " ").strip()
            if not t:
                names = _FILE_NAME.findall(raw)
                if names:
                    t = "附件：" + "、".join(names[:2])
            if t:
                return t[:24] + ("…" if len(t) > 24 else "")
    return "新会话"


def save(s: Session, touch: bool = True) -> None:
    """写盘。

    touch=False 时不刷新 updated —— 用于「切换会话前的兜底保存」。
    否则点一下会话就会把上一个会话的时间戳刷成现在，它立刻跳到列表最前，
    用户看到的就是「点一下左侧列表就乱掉」。
    """
    if touch:
        s.updated = time.time()
    # 只有没手动改过名的会话才用首条消息自动生成标题（否则重命名会被覆盖）
    if s.messages and not s.title_manual:
        s.title = title_from(s.messages)
    _root().mkdir(parents=True, exist_ok=True)
    _path(s.id).write_text(json.dumps(asdict(s), ensure_ascii=False, indent=1),
                           encoding="utf-8")


def load(sid: str) -> Session | None:
    try:
        d = json.loads(_path(sid).read_text(encoding="utf-8"))
        return Session(id=d["id"], title=d.get("title", "会话"),
                       model_id=d.get("model_id", "qwen3-4b"),
                       created=d.get("created", 0), updated=d.get("updated", 0),
                       messages=d.get("messages", []),
                       pinned=bool(d.get("pinned", False)),
                       title_manual=bool(d.get("title_manual", False)))
    except Exception:
        return None


def rename(sid: str, title: str) -> bool:
    """只改标题，不动 updated（重命名不该把会话顶到最前）。

    同时打上 title_manual 标记：否则下一次 save() 会拿首条消息重新生成标题，
    用户手动改的名字就白改了。
    """
    f = _path(sid)
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        d["title"] = (title or "").strip()[:40] or "新会话"
        d["title_manual"] = True
        f.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except Exception:
        return False


def toggle_pin(sid: str) -> bool:
    """置顶 / 取消置顶，返回切换后的状态。

    刻意不动 updated：置顶只该改变分组，不该顺带改它在组内的时间先后。
    """
    f = _path(sid)
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        d["pinned"] = not bool(d.get("pinned", False))
        f.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        return bool(d["pinned"])
    except Exception:
        return False


# 大图预览缓存：与 main.py 的 _PREVIEW_DIR 指向同一目录（sessions 的同级 images/）。
# 会话 JSON 里只存文件名，删会话必须顺带回收没人引用的图片，否则会攒孤儿文件。
_PREVIEW_NAME = re.compile(r"^p_[0-9a-f]{12}\.jpg$")


def preview_root() -> Path:
    return _root().parent / "images"


def referenced_previews() -> set[str] | None:
    """收集仍被任一会话 _ui.cards 引用的预览图文件名。

    任何一个会话 JSON 读不出来就返回 None —— 让调用方放弃清理（宁可留孤儿，
    也不能因为解析失败误删别的会话还在用的图）。
    """
    refs: set[str] = set()
    root = _root()
    if not root.is_dir():
        return refs
    for f in root.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            messages = d.get("messages", [])
            if not isinstance(messages, list):
                return None
        except Exception:
            return None
        for m in messages:
            if not isinstance(m, dict):
                continue
            ui = m.get("_ui") or {}
            if not isinstance(ui, dict):
                continue
            cards = ui.get("cards", []) or []
            if not isinstance(cards, list):
                continue
            for c in cards:
                if isinstance(c, dict) and c.get("preview"):
                    refs.add(Path(str(c["preview"])).name)
    return refs


def prune_previews() -> tuple[int, int]:
    """删掉没有任何会话引用的预览图，返回 (删除个数, 释放字节数)。

    只处理本程序生成的 p_<12位hex>.jpg，绝不碰目录里的其它文件；
    单文件失败不影响整体，也不把异常抛给调用方。
    """
    img_dir = preview_root()
    if not img_dir.is_dir():
        return (0, 0)
    keep = referenced_previews()
    if keep is None:          # 有会话读不出来 → 放弃清理，避免误删
        return (0, 0)
    removed = freed = 0
    for f in img_dir.iterdir():
        try:
            if not f.is_file() or not _PREVIEW_NAME.match(f.name) or f.name in keep:
                continue
            size = f.stat().st_size
            f.unlink()
            removed += 1
            freed += size
        except Exception:
            pass
    return removed, freed


def delete(sid: str) -> tuple[int, int]:
    """删会话 JSON，并回收不再被任何会话引用的大图缓存。

    返回 (删除图片数, 释放字节数)。消息正文、附件卡片、缩略图、文档解析文本
    全都存在这个 JSON 里，删文件即全部消失，不需要额外清理。
    仍被别的会话引用的大图会保留（引用计数），不会误删。
    """
    try:
        _path(sid).unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    # JSON 删掉后做一次引用计数清理：该会话独有的大图会被回收。
    # prune_previews() 自身有 fail-safe：任一会话 JSON 读不出来就放弃清理，
    # 宁可留孤儿也不能误删别的会话还在用的图。
    try:
        return prune_previews()
    except Exception:
        return (0, 0)


def list_sessions() -> list[Session]:
    out: list[Session] = []
    root = _root()
    if not root.is_dir():
        return out
    for f in root.glob("*.json"):
        s = load(f.stem)
        if s is not None:
            out.append(s)
    # 置顶的固定在最前，组内再按更新时间倒序
    out.sort(key=lambda s: (bool(s.pinned), s.updated), reverse=True)
    return out
