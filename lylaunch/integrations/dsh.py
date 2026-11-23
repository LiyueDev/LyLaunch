"""把 LyLaunch 的本地模型接入 DeepSeek Harness（dsh）。

dsh 的自定义提供商有两种落点，本模块都要认：

1. ``$DSH_HOME/settings.yaml``（web / CLI 形态，也是首次运行的导入源）
2. ``$DSH_HOME/profiles/<profile>/cordis.patch.yml``（桌面版真正吃的那一层）

桌面版第一次启动时会**把 settings.yaml 导入成 patch 层**（``- id: llm-pi-ai``
条目），并把原文件改名 ``settings.yaml.imported``。所以不能只往 settings.yaml
写 —— 导入之后那个文件已经没人读了，写了也不生效。

写文件的三条硬约束：

1. 只写自己名下那一段（用首尾哨兵注释圈住），绝不整体覆盖别人的配置；
2. 首次改动前留一份 ``*.lylaunch-bak``；
3. 写完做一次「还能不能解析」的校验，失败就把原文写回去。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..engine import EXTERNAL_PORT
from ..engine.registry import MODELS, is_installed


PROVIDER_ID = "lylaunch-local"
PROVIDER_NAME = "LyLaunch 本地模型"
PROVIDER_PLUGIN = "llm-pi-ai"
PLUGIN_PACKAGE = "@deepseek-ai/dsh-llm-pi-ai"
API_PROTOCOL = "openai-completions"
API_KEY_ENV = "LYLAUNCH_LOCAL_KEY"
# 本地 Router 不校验 Authorization；dsh 只要求这个引用能解析出非空值
API_KEY_PLACEHOLDER = "lylaunch-local"
BASE_PATH = "/v1"
BACKUP_SUFFIX = ".lylaunch-bak"

_MARK_HEAD = ">>> LyLaunch 本地模型"
_MARK_TAIL = "<<< LyLaunch 本地模型 <<<"
_BEGIN = f"# {_MARK_HEAD}：本段由 LyLaunch 维护，重新接入会覆盖，撤销会删除 >>>"
_END = f"# {_MARK_TAIL}"

# dsh 的 agent 系统提示 + 工具描述约 9.4k tokens，还要留出 maxTokens 的位置：
# 12K 窗口只剩几百 token 余量，实际一聊就 400，16K 才算跑得动。
MIN_USABLE_CONTEXT = 16384


class DshError(RuntimeError):
    """接入失败：给人看的说法，直接拿去弹窗。"""


@dataclass(frozen=True)
class ChatModel:
    """要写进 dsh 的一条模型目录项。"""

    id: str
    name: str
    context: int
    output: int
    vision: bool


@dataclass(frozen=True)
class Status:
    """设置页要展示的状态快照。"""

    home: Path
    target: Path
    mode: str                       # "patch"（桌面版实际读这层）/ "settings"
    installed: bool
    applied: bool
    models: tuple[str, ...] = ()
    backup: Path | None = None
    engine_online: bool = False     # 外部引擎（18091）在不在：不在 dsh 必然 Connection error
    base_port: int = 0              # 已写入的 baseURL 端口（0 = 没读到）
    port_mismatch: bool = False     # 写的是旧端口（例如 18090），需要重新接入
    base_ctx: int = 0               # 已写入的 contextWindow（0 = 没读到）
    ctx_mismatch: bool = False      # 写进去的窗口跟滑块档位不一致，需要重新接入


# ---------------------------------------------------------------- 路径与模型
def dsh_home() -> Path:
    raw = (os.environ.get("DSH_HOME") or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".dsh"


def settings_path() -> Path:
    return dsh_home() / "settings.yaml"


def imported_path() -> Path:
    return dsh_home() / "settings.yaml.imported"


def env_path() -> Path:
    return dsh_home() / ".env"


def backup_of(path: Path) -> Path:
    return Path(str(path) + BACKUP_SUFFIX)


def live_patch() -> Path | None:
    """桌面版实际生效的 patch 层；多个 profile 时优先 desktop。"""
    root = dsh_home() / "profiles"
    if not root.is_dir():
        return None
    found = sorted(p for p in root.glob("*/cordis.patch.yml") if p.is_file())
    for p in found:
        if p.parent.name == "desktop":
            return p
    return found[0] if found else None


def target_path() -> tuple[Path, str]:
    """该往哪儿写：导入过的（或 patch 里已有条目）走 patch，否则走 settings.yaml。"""
    patch = live_patch()
    if patch is not None:
        lines = _read_text(patch).splitlines()
        if (_has_block(lines) or _provider_key_line(lines) >= 0
                or _patch_has_entry(_read_text(patch)) or imported_path().is_file()):
            return patch, "patch"
    return settings_path(), "settings"


def api_base(port: int = EXTERNAL_PORT) -> str:
    """dsh 该连的地址：外部引擎的独立端口（不是插件那套 18090）。"""
    return f"http://127.0.0.1:{port}{BASE_PATH}"


def engine_online(port: int = EXTERNAL_PORT, timeout: float = 0.6) -> bool:
    """外部引擎在不在。dsh 直连这个端口，不在就是 Connection error。"""
    try:
        from ..engine import RouterClient
        return bool(RouterClient(port).health(timeout))
    except Exception:
        return False


def chat_models() -> list[ChatModel]:
    """能当对话模型用的本地模型。

    只收 caps 里带 ``chat`` 的：OCR / 翻译这类专用模型进了 dsh 的模型选择器
    也聊不了，选中只会得到一堆没用的输出。一个都没装时不兜底，由调用方提示。
    """
    return [_as_chat_model(m) for m in MODELS if is_installed(m) and "chat" in m.caps]


def _as_chat_model(spec) -> ChatModel:
    # 上下文取外部那套 Router 实际加载的数（设置页滑块），不是插件那套的出厂值：
    # dsh 用这个数算 token 预算，报小了会在引擎明明够用的情况下 400。
    from ..engine.registry import effective_ctx
    ctx = effective_ctx(spec, external=True)
    # 输出上限取上下文的 1/4、封顶 8192：dsh 的系统提示 + 工具描述要 ~9.4k tokens，
    # 给生成留太多会在 prompt + 输出撞 n_ctx（直接 400）；但压到 4096 又会让长回答
    # 被截断（「已达到输出 token 上限」），所以按窗口大小往上放一档。
    output = min(max(ctx // 4, 1024), 8192)
    vision = "vision" in spec.caps and bool(spec.mmproj)
    return ChatModel(spec.id, spec.display, ctx, output, vision)


# ---------------------------------------------------------------- 文本渲染
def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render(indent: int, models: list[ChatModel], port: int) -> list[str]:
    pad = " " * indent
    body = pad + "  "
    lines = [
        pad + _BEGIN,
        f"{pad}{PROVIDER_ID}:",
        f"{body}displayName: {_quote(PROVIDER_NAME)}",
        f"{body}apiKeyEnv: {API_KEY_ENV}",
        f"{body}api: {API_PROTOCOL}",
        f"{body}baseURL: {api_base(port)}",
        # llama.cpp 不认 developer 角色，输出上限也只认 max_tokens
        f"{body}compat:",
        f"{body}  supportsDeveloperRole: false",
        f"{body}  maxTokensField: max_tokens",
        f"{body}models:",
    ]
    for m in models:
        lines.append(f"{body}  - id: {m.id}")
        lines.append(f"{body}    name: {_quote(m.name)}")
        lines.append(f"{body}    contextWindow: {m.context}")
        lines.append(f"{body}    maxTokens: {m.output}")
        if m.vision:
            lines.append(f"{body}    input: [text, image]")
    lines.append(pad + _END)
    return lines


# ---------------------------------------------------------------- 行级编辑
def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _blank(line: str) -> bool:
    s = line.strip()
    return not s or s.startswith("#")


def _has_block(lines: list[str]) -> bool:
    return any(line.strip().startswith("#") and _MARK_HEAD in line for line in lines)


def _strip_block(lines: list[str]) -> tuple[list[str], bool]:
    """整段摘掉哨兵区间（含哨兵本身）。"""
    out: list[str] = []
    inside = False
    removed = False
    for line in lines:
        s = line.strip()
        if not inside and s.startswith("#") and _MARK_HEAD in s:
            inside = True
            removed = True
            continue
        if inside:
            if s.startswith("#") and _MARK_TAIL in s:
                inside = False
            continue
        out.append(line)
    return out, removed


def _provider_key_line(lines: list[str]) -> int:
    """``lylaunch-local:`` 那一行（不管有没有哨兵）。"""
    for i, line in enumerate(lines):
        if line.strip() == PROVIDER_ID + ":":
            return i
    return -1


def _strip_ours(lines: list[str]) -> tuple[list[str], bool]:
    """摘掉自己那一段：先按哨兵摘，再兜底按 provider id 摘。

    dsh 导入/保存配置时会把自己写的 YAML 重新格式化，**顺便吃掉注释** —— 哨兵
    会消失，只剩``lylaunch-local:`` 这个 id。所以 id 才是真正的锚点。
    """
    lines, removed = _strip_block(lines)
    if removed:
        return lines, True
    i_key = _provider_key_line(lines)
    if i_key < 0:
        return lines, False
    ind = _indent_of(lines[i_key])
    end = _section_end(lines, i_key, ind)
    del lines[i_key:end]
    return lines, True


def _section_end(lines: list[str], start: int, indent: int) -> int:
    """到下一个同级/更浅的键为止（空行与注释不算）。"""
    for i in range(start + 1, len(lines)):
        if _blank(lines[i]):
            continue
        if _indent_of(lines[i]) <= indent:
            return i
    return len(lines)


def _find_key(lines: list[str], start: int, end: int, key: str) -> int:
    for i in range(start, end):
        if lines[i].lstrip().startswith(key + ":"):
            return i
    return -1


def _inline_value(line: str) -> str:
    """``key: xxx  # 注释`` 里冒号后面的部分（去掉行尾注释）。"""
    rest = line.split(":", 1)[1]
    return rest.split("#", 1)[0].strip()


def _child_indent(lines: list[str], start: int, end: int, parent_indent: int) -> int:
    """块内子键的缩进。取最浅的那一档 —— 已有 provider 的字段比它的键更深，
    直接取第一条会把我们的 provider 嵌进别人里面。"""
    found = [_indent_of(l) for l in lines[start:end]
             if not _blank(l) and _indent_of(l) > parent_indent]
    return min(found) if found else parent_indent + 2


def _merge_providers(lines: list[str], i_prov: int, models: list[ChatModel],
                     port: int) -> list[str]:
    """把 provider 段落塞进 ``providers:`` 下面，返回新的行列表。"""
    value = _inline_value(lines[i_prov])
    if value and value not in ("{}",):
        raise DshError(
            "配置里的 providers 是行内写法（不是常规的块状 mapping），"
            "自动合并容易写坏它。请先改成块状写法（providers: 换行后再写各 provider）。")
    if value:
        lines[i_prov] = lines[i_prov].split(":", 1)[0] + ":"

    prov_indent = _indent_of(lines[i_prov])
    prov_end = _section_end(lines, i_prov, prov_indent)
    indent = _child_indent(lines, i_prov + 1, prov_end, prov_indent)
    # 不额外塞空行：段落自己以注释开头，读得开；塞了空行，撤销时又会留在原地
    lines[prov_end:prov_end] = _render(indent, models, port)
    return lines


def _ensure_settings(lines: list[str], models: list[ChatModel], port: int) -> list[str]:
    """settings.yaml 形态：顶层 llm-pi-ai → providers。"""
    lines, _ = _strip_ours(lines)

    i_pi = next((i for i, l in enumerate(lines)
                 if _indent_of(l) == 0 and l.lstrip().startswith(PROVIDER_PLUGIN + ":")), -1)
    if i_pi < 0:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        head = [f"{PROVIDER_PLUGIN}:", "  providers:"]
        indent = 4
        lines += head + _render(indent, models, port)
        return lines

    inline = _inline_value(lines[i_pi])
    if inline and inline not in ("{}",):
        raise DshError(
            f"settings.yaml 里的 {PROVIDER_PLUGIN} 用了行内写法（不是常规的块状 mapping），"
            f"自动合并容易写坏它。请先把那一行改回 {PROVIDER_PLUGIN}: 再点接入。")
    if inline:
        lines[i_pi] = lines[i_pi].split(":", 1)[0] + ":"

    pi_end = _section_end(lines, i_pi, 0)
    i_prov = _find_key(lines, i_pi + 1, pi_end, "providers")
    if i_prov < 0:
        lines.insert(i_pi + 1, "  providers:")
        i_prov = i_pi + 1
    return _merge_providers(lines, i_prov, models, port)


def _patch_has_entry(text: str) -> bool:
    return bool(re.search(rf"(?m)^\s*-\s*id:\s*{PROVIDER_PLUGIN}\s*$", text))


def _find_patch_entry(lines: list[str]) -> tuple[int, int]:
    """``- id: llm-pi-ai`` 那条 patch 条目的起止行号。"""
    for i, line in enumerate(lines):
        s = line.lstrip()
        if not s.startswith("- id:"):
            continue
        if s.split(":", 1)[1].strip().strip('"') != PROVIDER_PLUGIN:
            continue
        ind = _indent_of(line)
        end = len(lines)
        for j in range(i + 1, len(lines)):
            nxt = lines[j].lstrip()
            if nxt.startswith("- ") and _indent_of(lines[j]) <= ind:
                end = j
                break
        return i, end
    return -1, -1


def _ensure_patch(lines: list[str], models: list[ChatModel], port: int) -> list[str]:
    """cordis.patch.yml 形态：list 条目 - id: llm-pi-ai → config → providers。"""
    lines, _ = _strip_ours(lines)

    i_entry, entry_end = _find_patch_entry(lines)
    if i_entry < 0:
        while lines and not lines[-1].strip():
            lines.pop()
        head = [f"- id: {PROVIDER_PLUGIN}", "  config:", "    providers:"]
        indent = 6
        lines += head + _render(indent, models, port)
        return lines

    entry_indent = _indent_of(lines[i_entry])
    i_cfg = _find_key(lines, i_entry + 1, entry_end, "config")
    if i_cfg < 0:
        lines.insert(i_entry + 1, " " * (entry_indent + 2) + "config:")
        i_cfg = i_entry + 1
        entry_end += 1
    cfg_indent = _indent_of(lines[i_cfg])
    cfg_end = min(_section_end(lines, i_cfg, cfg_indent), entry_end)

    i_prov = _find_key(lines, i_cfg + 1, cfg_end, "providers")
    if i_prov < 0:
        lines.insert(i_cfg + 1, " " * (cfg_indent + 2) + "providers:")
        i_prov = i_cfg + 1
    return _merge_providers(lines, i_prov, models, port)


def _drop_empty_sections(lines: list[str]) -> list[str]:
    """撤销后把被摘空的 providers / config / patch 条目一起收走，别留半截空 mapping。"""
    for _ in range(3):
        lines, p1 = _prune_empty_map(lines, "providers")
        lines, p2 = _prune_entry_config(lines)
        lines = _drop_bare_entry(lines)
        if not (p1 or p2):
            break
    # settings.yaml 的顶层 llm-pi-ai: 空了也一并收走（行内 `{}` 之前就不动它）
    lines, _ = _prune_empty_map(lines, PROVIDER_PLUGIN)
    return lines


def _prune_empty_map(lines: list[str], key: str) -> tuple[list[str], bool]:
    changed = False
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].lstrip().startswith(key + ":"):
            continue
        if _inline_value(lines[i]):
            continue
        ind = _indent_of(lines[i])
        end = _section_end(lines, i, ind)
        if any(not _blank(l) for l in lines[i + 1:end]):
            continue
        del lines[i]
        changed = True
    return lines, changed


def _prune_entry_config(lines: list[str]) -> tuple[list[str], bool]:
    """只收拾我们自己那条 patch 条目里的 config:，别碰别人的。"""
    i_entry, entry_end = _find_patch_entry(lines)
    if i_entry < 0:
        return lines, False
    i_cfg = _find_key(lines, i_entry + 1, entry_end, "config")
    if i_cfg < 0 or _inline_value(lines[i_cfg]):
        return lines, False
    if any(not _blank(l) for l in lines[i_cfg + 1:entry_end]):
        return lines, False
    del lines[i_cfg]
    return lines, True


def _drop_bare_entry(lines: list[str]) -> list[str]:
    i_entry, entry_end = _find_patch_entry(lines)
    if i_entry < 0:
        return lines
    if any(not _blank(l) for l in lines[i_entry + 1:entry_end]):
        return lines
    return lines[:i_entry] + lines[entry_end:]


# ---------------------------------------------------------------- 读写
def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig") if path.is_file() else ""


def _read(path: Path) -> tuple[str, str, bool]:
    """返回（正文, 换行符, 是否带 BOM）——写回时三样都要还原。"""
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if b"\r\n" in raw else "\n"
    return text, newline, raw.startswith(b"\xef\xbb\xbf")


def _write(path: Path, text: str, newline: str, bom: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.replace("\r\n", "\n").replace("\n", newline).encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _parses(text: str) -> bool:
    """能不能被 YAML 解析。PyYAML 不一定在（仓库没声明它），有就顺手验一下。"""
    try:
        import yaml  # noqa: PLC0415 - 可选依赖，缺了就跳过校验
    except Exception:
        return True
    try:
        yaml.safe_load(text) if text.strip() else None
    except Exception:
        return False
    return True


def _ensure_env_line() -> None:
    path = env_path()
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if re.search(rf"(?m)^\s*{API_KEY_ENV}\s*=", text):
        return
    sep = "" if not text or text.endswith("\n") else "\n"
    _write(path, f"{text}{sep}{API_KEY_ENV}={API_KEY_PLACEHOLDER}\n", "\n")


def _drop_env_line() -> None:
    path = env_path()
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    kept = [l for l in text.splitlines()
            if l.strip() != f"{API_KEY_ENV}={API_KEY_PLACEHOLDER}"]
    after = "\n".join(kept)
    if not after.strip():
        path.unlink()
        return
    if after != text.rstrip("\r\n"):
        _write(path, after + "\n", "\n")


# ---------------------------------------------------------------- 对外操作
def _own_range(lines: list[str]) -> tuple[int, int]:
    """我们自己那一段的行区间（有哨兵就按哨兵，没有就按 provider id 段落）。"""
    start, stop = 0, len(lines)
    i_key = _provider_key_line(lines)
    if i_key >= 0:
        start = i_key
        stop = _section_end(lines, i_key, _indent_of(lines[i_key]))
    mark = next((i for i, l in enumerate(lines)
                 if l.strip().startswith("#") and _MARK_HEAD in l), -1)
    if mark >= 0:
        start = mark
        stop = next((i for i in range(mark + 1, len(lines))
                     if lines[i].strip().startswith("#") and _MARK_TAIL in lines[i]), len(lines))
    return start, stop


def _provider_model_ids(lines: list[str]) -> list[str]:
    """我们那条 provider 下登记的模型 id。"""
    start, stop = _own_range(lines)
    ids: list[str] = []
    for line in lines[start:stop]:
        s = line.strip()
        if s.startswith("- id:"):
            ids.append(s.split(":", 1)[1].strip().strip('"'))
    return ids


_BASE_PORT_RE = re.compile(r"baseURL:\s*\"?[^\"\n]*:(\d+)")
_CTX_RE = re.compile(r"contextWindow:\s*(\d+)")


def _written_base_port(lines: list[str]) -> int:
    """读已写入的 baseURL 端口；读不到返回 0（例如还没接入）。"""
    for line in lines:
        m = _BASE_PORT_RE.search(line)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return 0
    return 0


def _written_ctx(lines: list[str]) -> int:
    """读已写入的 contextWindow（取我们那段里最小的那个）；读不到返回 0。"""
    start, stop = _own_range(lines)
    found: list[int] = []
    for line in lines[start:stop]:
        m = _CTX_RE.search(line)
        if m:
            found.append(int(m.group(1)))
    return min(found) if found else 0


def _ctx_mismatch(lines: list[str]) -> bool:
    """逐模型比对已写入的 contextWindow 与当前该有的值。

    只看最小值不够：滑块从 64K 拉到 112K 后，9B 应该变 112K、VL 仍被封在 64K，
    此时「最小值」前后都是 64K，差异会被吞掉。这里按 id 一个一个对。
    """
    want = {m.id: m.context for m in chat_models()}
    if not want:
        return False
    start, stop = _own_range(lines)
    cur_id = ""
    for line in lines[start:stop]:
        s = line.strip()
        if s.startswith("- id:"):
            cur_id = s.split(":", 1)[1].strip().strip('"')
            continue
        m = _CTX_RE.search(line)
        if m and cur_id in want and int(m.group(1)) != want[cur_id]:
            return True
    return False


def probe() -> Status:
    """给设置页显示用的状态；不抛异常，读不到就当没装。"""
    home = dsh_home()
    target, mode = target_path()
    lines = _read_text(target).splitlines()
    applied = _has_block(lines) or _provider_key_line(lines) >= 0
    models = tuple(_provider_model_ids(lines)) if applied else ()
    bak = backup_of(target)
    base_port = _written_base_port(lines)
    base_ctx = _written_ctx(lines)
    return Status(
        home=home,
        target=target,
        mode=mode,
        installed=home.is_dir(),
        applied=applied,
        models=models,
        backup=bak if bak.is_file() else None,
        engine_online=engine_online(),
        base_port=base_port,
        port_mismatch=bool(applied and base_port and base_port != EXTERNAL_PORT),
        base_ctx=base_ctx,
        ctx_mismatch=bool(applied and _ctx_mismatch(lines)),
    )


def apply(models: list[ChatModel] | None = None, port: int = EXTERNAL_PORT) -> Status:
    """写入 / 刷新 provider 段落。只动自己那段，返回写后的状态。"""
    picked = list(models) if models is not None else chat_models()
    if not picked:
        raise DshError("本地还没有可接入的对话模型（Qwen3.5-9B、Qwen3-4B 这类）。"
                       "先在「模型」页下载一个再试。")

    target, mode = target_path()
    existed = target.is_file()
    text, newline, bom = _read(target) if existed else ("", "\n", False)
    before_ok = _parses(text)

    lines = text.splitlines()
    had_block = _has_block(lines) or _provider_key_line(lines) >= 0
    build = _ensure_patch if mode == "patch" else _ensure_settings
    merged = build(lines, picked, port)
    new_text = "\n".join(merged).rstrip() + "\n"
    if before_ok and not _parses(new_text):
        raise DshError(
            f"写出来的 {target.name} 校验没通过，已保持原文件不动：\n{target}\n\n"
            "多半是文件里原本就有格式问题，先手工确认一下再接入。")

    if existed and new_text != text and not had_block:
        # 只在「文件里还没有我们那段」时留底：重复接入不能把带我们内容的版本当原文
        bak = backup_of(target)
        if not bak.exists():
            bak.write_bytes(target.read_bytes())

    _write(target, new_text, newline, bom)

    # 另一处（settings.yaml / patch）如果还留着我方旧段落，一并摘掉，避免两份同时生效
    for other in (settings_path(), live_patch()):
        if other is None or other == target or not other.is_file():
            continue
        other_text, other_nl, other_bom = _read(other)
        other_lines, removed = _strip_ours(other_text.splitlines())
        if removed:
            rest = "\n".join(other_lines).rstrip()
            _write(other, rest + "\n" if rest else "", other_nl, other_bom)

    _ensure_env_line()
    return probe()


def revert() -> Status:
    """摘掉 LyLaunch 写的那一段（连带占位密钥那一行）。其余配置原样保留。"""
    for path in (live_patch(), settings_path()):
        if path is None or not path.is_file():
            continue
        text, newline, bom = _read(path)
        lines, removed = _strip_ours(text.splitlines())
        if not removed:
            continue
        lines = _drop_empty_sections(lines)
        new_text = "\n".join(lines).rstrip()
        new_text = (new_text + "\n") if new_text else ""
        bak = backup_of(path)
        if new_text:
            _write(path, new_text, newline, bom)
        elif bak.is_file():
            # 整份文件都是我们写出来的 → 原文回填
            path.write_bytes(bak.read_bytes())
        else:
            path.unlink()
    _drop_env_line()
    return probe()
