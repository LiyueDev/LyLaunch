r"""聊天用的 Markdown → 富文本（零依赖，不引 pygments / KaTeX / MathJax）。

为什么自己写：pygments 是某依赖的传递产物，没进 requirements；Qt 富文本也不支持
MathML / MathJax，为了打包稳定、不引 WebEngine，这里做聊天场景够用的子集：

- 代码块（带轻量高亮）、标题、列表、引用、分隔线
- Markdown 表格（| --- | 分隔行）
- 数学公式：$...$ / \(...\) 行内，$$...$$ / \[...\] 块级
- 行内加粗 / 斜体 / 行内代码 / 链接

数学走「轻量可读化」：把 \frac / \sqrt / 上下标 / 希腊字母 / 运算符转成
Unicode + <sup>/<sub>。不是 KaTeX 那种完整排版，但不会再让 \frac{2}{1-x^2}
这种源码糊在屏幕上，同时零依赖、流式打字到一半也不会崩。

两个入口：
- parse_blocks(md)  → [Block]，给 AiBlock 拆成「文本 / 代码」等不同 widget
- inline_html(text) → 单段文本内的行内标记（含行内公式）
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

# ── 转义 ─────────────────────────────────────────────
def _esc(s: str) -> str:
    return html.escape(s, quote=False)


_CODE_STYLE = ("font-family:Consolas,'Courier New',monospace; background:#F2F4F8;"
               " padding:1px 4px; border-radius:4px; font-size:12.5px;")


# ── 数学：LaTeX → Qt 富文本能显示的 HTML ───────────────
# Qt 的 QTextDocument 只支持 <sup>/<sub> 这类基础标签，所以复杂结构一律
# 「可读化」而不是「排版复刻」。目标：屏幕上是 f′(x) = 2/(1−x²)，不是 \frac{2}{1-x^2}。
# 上下标一律用 <sup>/<sub> 标签：Unicode 的 ₀₁₂ 在微软雅黑里缺字形会显示成方框。
# 符号表按 key 长度降序替换，避免 \le 抢先吃掉 \leq 的前半截。
_MATH_SYMBOLS: dict[str, str] = {
    r"\times": "×", r"\cdot": "·", r"\ast": "∗", r"\div": "÷", r"\pm": "±",
    r"\mp": "∓", r"\leq": "≤", r"\geq": "≥", r"\le": "≤", r"\ge": "≥",
    r"\neq": "≠", r"\ne": "≠", r"\approx": "≈", r"\equiv": "≡",
    r"\simeq": "≃", r"\sim": "∼", r"\propto": "∝", r"\infty": "∞",
    r"\partial": "∂", r"\nabla": "∇", r"\sum": "∑", r"\prod": "∏",
    r"\int": "∫", r"\oint": "∮",
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
    r"\epsilon": "ε", r"\varepsilon": "ε", r"\zeta": "ζ", r"\eta": "η",
    r"\theta": "θ", r"\vartheta": "ϑ", r"\iota": "ι", r"\kappa": "κ",
    r"\lambda": "λ", r"\mu": "μ", r"\nu": "ν", r"\xi": "ξ", r"\pi": "π",
    r"\rho": "ρ", r"\sigma": "σ", r"\tau": "τ", r"\upsilon": "υ",
    r"\phi": "φ", r"\varphi": "φ", r"\chi": "χ", r"\psi": "ψ", r"\omega": "ω",
    r"\Gamma": "Γ", r"\Delta": "Δ", r"\Theta": "Θ", r"\Lambda": "Λ",
    r"\Xi": "Ξ", r"\Pi": "Π", r"\Sigma": "Σ", r"\Phi": "Φ", r"\Psi": "Ψ",
    r"\Omega": "Ω",
    r"\leftrightarrow": "↔", r"\Leftrightarrow": "⇔",
    r"\rightarrow": "→", r"\leftarrow": "←", r"\Rightarrow": "⇒",
    r"\Leftarrow": "⇐", r"\mapsto": "↦", r"\to": "→",
    r"\notin": "∉", r"\subseteq": "⊆", r"\supseteq": "⊇",
    r"\subset": "⊂", r"\supset": "⊃", r"\varnothing": "∅",
    r"\emptyset": "∅", r"\cup": "∪", r"\cap": "∩", r"\in": "∈", r"\ni": "∋",
    r"\forall": "∀", r"\exists": "∃", r"\neg": "¬", r"\land": "∧", r"\lor": "∨",
    r"\cdots": "⋯", r"\ldots": "…", r"\dots": "…", r"\vdots": "⋮",
    r"\ddots": "⋱", r"\angle": "∠", r"\perp": "⊥", r"\parallel": "∥",
    r"\degree": "°", r"\prime": "′",
    r"\displaystyle": "", r"\limits": "", r"\mathrm": "", r"\operatorname": "",
    r"\left": "", r"\right": "",
    r"\sin": "sin", r"\cos": "cos", r"\tan": "tan", r"\cot": "cot",
    r"\sec": "sec", r"\csc": "csc", r"\arcsin": "arcsin", r"\arccos": "arccos",
    r"\arctan": "arctan", r"\sinh": "sinh", r"\cosh": "cosh", r"\tanh": "tanh",
    r"\ln": "ln", r"\log": "log", r"\exp": "exp", r"\lim": "lim",
    r"\max": "max", r"\min": "min", r"\arg": "arg", r"\det": "det", r"\gcd": "gcd",
    r"\qquad": "  ", r"\quad": " ", r"\,": " ", r"\;": " ", r"\!": "", r"\ ": " ",
}
_SYM_ORDER = sorted(_MATH_SYMBOLS, key=len, reverse=True)

_FRAC_RE = re.compile(r"\\(?:dfrac|tfrac|frac)\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_SQRT_RE = re.compile(r"\\sqrt\s*(?:\[([^\]]*)\])?\s*\{([^{}]*)\}")
_TEXT_RE = re.compile(r"\\(?:text|mathrm|operatorname|mathbf|textit|mbox)\s*\{([^{}]*)\}")
_SUP_RE = re.compile(r"\^\s*(?:\{([^{}]*)\}|([A-Za-z0-9+\-()'′]))")
_SUB_RE = re.compile(r"_\s*(?:\{([^{}]*)\}|([A-Za-z0-9+\-()]))")
_CMD_RE = re.compile(r"\\([A-Za-z]+)")
_SIMPLE_ARG = re.compile(r"[A-Za-z0-9.']+")


def _arg_wrap(rendered: str, raw: str) -> str:
    """分数/根号的参数：简单项不加多余括号，复合项加括号。"""
    return rendered if _SIMPLE_ARG.fullmatch(raw.strip()) else f"({rendered})"


def _math_fragment(src: str, depth: int = 0, block: bool = False) -> str:
    """一段 LaTeX → 已转义的 HTML 片段（可安全塞进 QLabel）。"""
    if depth > 4:
        return _esc(src)
    stash: list[str] = []

    def hold(frag: str) -> str:
        stash.append(frag)
        return f"\x03{len(stash) - 1}\x03"

    t = src.strip()

    # ① \text{...} / \mathrm{...}：当普通文字，不做数学替换
    t = _TEXT_RE.sub(
        lambda m: hold('<span style="font-style:normal;">' + _esc(m.group(1)) + "</span>"), t)

    # ② 分数：递归渲染分子分母；多跑几轮兼容嵌套 \frac{1}{\frac{a}{b}}
    def _frac(m: re.Match) -> str:
        ra, rb = m.group(1), m.group(2)
        a = _arg_wrap(_math_fragment(ra, depth + 1), ra)
        b = _arg_wrap(_math_fragment(rb, depth + 1), rb)
        return hold(f"{a}/{b}")

    for _ in range(3):
        new = _FRAC_RE.sub(_frac, t)
        if new == t:
            break
        t = new

    # ③ 根号
    def _sqrt(m: re.Match) -> str:
        root = m.group(1)
        inner = _arg_wrap(_math_fragment(m.group(2), depth + 1), m.group(2))
        # hold 里直接放 HTML 片段即可（不会经过 _esc），不要嵌套 hold 占位符
        pre = "√" if not root else f"<sup>{_esc(root.strip())}</sup>√"
        return hold(pre + inner)

    t = _SQRT_RE.sub(_sqrt, t)

    # ④ 上下标（递归处理分组内的公式）
    def _script(m: re.Match, tag: str) -> str:
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        raw = (raw or "").strip()
        return hold(f"<{tag}>" + _math_fragment(raw, depth + 1) + f"</{tag}>")

    t = _SUP_RE.sub(lambda m: _script(m, "sup"), t)
    t = _SUB_RE.sub(lambda m: _script(m, "sub"), t)

    # ⑤ 换行 / 对齐符
    t = re.sub(r"\\\\", lambda _m: hold("<br>" if block else "；"), t)
    t = t.replace("&", "  ") if block else t.replace("&", " ")

    # ⑥ 符号表（长名优先）
    for cmd in _SYM_ORDER:
        if cmd in t:
            t = t.replace(cmd, _MATH_SYMBOLS[cmd])

    # ⑦ 未知命令去掉反斜杠、花括号只当分组
    t = _CMD_RE.sub(r"\1", t)
    t = t.replace("{", "").replace("}", "")

    t = _esc(t)
    for i, frag in enumerate(stash):
        t = t.replace(f"\x03{i}\x03", frag)
    return t


def math_html(src: str, block: bool = False) -> str:
    """公式定界符里的内容 → 带样式的 HTML。"""
    body = _math_fragment(src, 0, block=block)
    if block:
        return f'<div align="center" style="line-height:210%;">{body}</div>'
    return ('<span style="font-family:\'Cambria Math\',Cambria,Georgia,serif;'
            f' font-style:italic; font-size:14.5px; color:#161C28;">{body}</span>')


# 行内公式：\(...\) 无条件识别；$...$ 要求有数学特征，避免把 "$5 到 $10" 当公式
_MATH_INLINE_RE = re.compile(
    r"\\\((.*?)\\\)"
    r"|(?<!\\)\$(?!\$)([^\n$]+?)(?<!\\)\$",
    re.S,
)
_MATH_FEATURE_RE = re.compile(r"\\[A-Za-z]|[\\^_=]|×|÷|≤|≥|≠|±|∞|∑|∫|√|π")
_SHORT_MATH_RE = re.compile(r"[A-Za-z0-9.+\-*/()=<>]+")


def _has_math_feature(s: str) -> bool:
    r"""判断 $...$ 里到底是不是公式（\(...\) 不需要判断）。

    只靠「有没有 \\ 或 ^」会漏掉 $2x$ / $k=2$ 这类短公式；
    但也不能把 "$5 到 $10" 当公式。判据：明显的 LaTeX / 数学符号，
    或者「短、无空格、无中文、纯 ASCII 数学字符」。
    """
    if _MATH_FEATURE_RE.search(s):
        return True
    t = s.strip()
    if not t or len(t) > 30:
        return False
    if re.search(r"[\u4e00-\u9fff\s]", t):     # 中文 / 空格 → 更像价格、普通句子
        return False
    # 必须真含字母/数字/运算符：像 $...$（占位符写法）、$$ 这种纯点号不当公式
    if not re.search(r"[A-Za-z0-9=+\-*/^_]", t):
        return False
    return bool(_SHORT_MATH_RE.fullmatch(t))


def typing_html(text: str) -> str:
    """流式打字专用：藏掉「还没写完」的公式尾巴。

    Typer 一个字一个字吐，模型写到 `\\( f'(x) = \\fra` 时定界符还没闭合，
    直接渲染会把半截 LaTeX 源码糊在屏幕上。这里把未闭合部分暂时截掉，
    set_final() 会拿全文重新 parse_blocks()，内容一点都不会丢。
    """
    t = text
    for opener, closer in ((r"\(", r"\)"), (r"\[", r"\]")):
        start = t.rfind(opener)
        if start >= 0 and closer not in t[start + len(opener):]:
            t = t[:start]
    if t.count("$$") % 2 == 1:
        t = t[:t.rfind("$$")]
    lines = t.split("\n")
    if lines and lines[-1].count("$") % 2 == 1:
        lines[-1] = lines[-1][:lines[-1].rfind("$")]
        t = "\n".join(lines)
    return inline_html(t)


# ── 行内 ─────────────────────────────────────────────
def inline_html(text: str) -> str:
    """行内标记 + 行内公式。输入是原文，输出是已转义的 HTML。

    顺序很重要：代码 → 公式 → 转义 → 强调/链接 → 恢复占位符。
    先把代码和公式藏起来，里面的 *、_、\\ 才不会被 Markdown 规则误吃。
    """
    codes: list[str] = []

    def stash(m: re.Match) -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    t = re.sub(r"`([^`]+)`", stash, text)

    maths: list[str] = []

    def stash_math(m: re.Match) -> str:
        if m.group(1) is not None:                 # \( ... \)
            src = m.group(1)
        else:
            src = m.group(2)
            if not _has_math_feature(src):        # $5 / 价格，不是公式
                return m.group(0)
        maths.append(math_html(src))
        return f"\x02{len(maths) - 1}\x02"

    t = _MATH_INLINE_RE.sub(stash_math, t)

    t = _esc(t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
               r'<a href="\2" style="color:#2F6BEE; text-decoration:none;">\1</a>', t)
    for i, c in enumerate(codes):
        t = t.replace(f"\x00{i}\x00",
                      f'<span style="{_CODE_STYLE}">{_esc(c)}</span>')
    for i, f in enumerate(maths):
        t = t.replace(f"\x02{i}\x02", f)
    return t


# ── 代码高亮（tokenizer 式扫描，不二次替换）───────────────
_KW = set("""
def class import from return if else elif for while try except finally with as in not and or
None True False print lambda yield async await self break continue pass raise global nonlocal
assert del is match case type
""".split())

_C_STR = "#C0622A"
_C_CMT = "#5F6570"
_C_KW = "#8250DF"
_C_NUM = "#0550AE"


def _hl_line(line: str) -> str:
    out: list[str] = []
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == "#":                                   # 注释
            out.append(f'<span style="color:{_C_CMT};">{_esc(line[i:])}</span>')
            return "".join(out)
        if ch in "\"'":                                 # 字符串
            q, j = ch, i + 1
            while j < n:
                if line[j] == "\\":
                    j += 2
                    continue
                if line[j] == q:
                    j += 1
                    break
                j += 1
            out.append(f'<span style="color:{_C_STR};">{_esc(line[i:j])}</span>')
            i = j
            continue
        if ch.isalpha() or ch == "_":                   # 标识符 / 关键字
            j = i
            while j < n and (line[j].isalnum() or line[j] == "_"):
                j += 1
            w = line[i:j]
            out.append(f'<span style="color:{_C_KW};">{_esc(w)}</span>' if w in _KW else _esc(w))
            i = j
            continue
        if ch.isdigit():                                # 数字
            j = i
            while j < n and (line[j].isdigit() or line[j] in "._"):
                j += 1
            out.append(f'<span style="color:{_C_NUM};">{_esc(line[i:j])}</span>')
            i = j
            continue
        out.append(_esc(ch))
        i += 1
    return "".join(out)


def code_html(code: str) -> str:
    """代码块 → HTML。

    不用 <pre>：Qt 富文本里 <pre> 每行会带额外段落间距，行高不可控（会算错高度、
    出现多余滚动条）。改成显式 line-height + 把连续空格转 &nbsp;（等宽下对齐不变）。
    """
    rows = []
    for ln in code.split("\n"):
        hl = _hl_line(ln)
        hl = re.sub(r" {2,}", lambda m: "&nbsp;" * len(m.group(0)), hl)
        rows.append(hl if hl.strip() else "&nbsp;")
    body = "<br>".join(rows)
    return ('<div style="font-family:Consolas,\'Courier New\',monospace;'
            f' font-size:12.5px; line-height:155%; color:#24292F;">{body}</div>')


# ── 块解析 ───────────────────────────────────────────
@dataclass
class Block:
    kind: str          # text | code | heading | list | quote | hr | math | table
    content: str = ""
    lang: str = ""
    level: int = 0     # heading 级别 / list 是否有序
    rows: list[list[str]] = field(default_factory=list)   # table 单元格
    align: list[str] = field(default_factory=list)        # table 列对齐


_H_FENCE = re.compile(r"^\s*```\s*([\w+-]*)")
_H_MATH = re.compile(r"^\s*(\$\$|\\\[)\s*(.*)$")
_H_HEAD = re.compile(r"^(#{1,4})\s+(.*)$")
_H_ULI = re.compile(r"^\s*[-*+]\s+(.*)$")
_H_OLI = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_H_HR = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_H_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_T_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip().replace(r"\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def _is_table_head(lines: list[str], i: int) -> bool:
    """当前行有 | 且下一行是 | --- | 分隔行，才算表格。"""
    if i + 1 >= len(lines) or "|" not in lines[i]:
        return False
    if not _T_SEP.match(lines[i + 1]):
        return False
    return len(_split_row(lines[i])) >= 2


def parse_blocks(md: str) -> list[Block]:
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[Block] = []
    buf: list[str] = []
    i = 0

    def flush_para() -> None:
        nonlocal buf
        if buf:
            text = "\n".join(buf).strip()
            if text:
                blocks.append(Block("text", text))
            buf = []

    while i < len(lines):
        line = lines[i]

        m = _H_FENCE.match(line)
        if m:
            flush_para()
            lang = m.group(1)
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            blocks.append(Block("code", "\n".join(code), lang))
            continue

        # 块级公式：$$ ... $$ / \[ ... \]，可跨行
        mm = _H_MATH.match(line)
        if mm:
            flush_para()
            opener, tail = mm.group(1), mm.group(2).strip()
            closer = "$$" if opener == "$$" else r"\]"
            body: list[str] = []
            if tail:
                if closer in tail:
                    body.append(tail.split(closer, 1)[0].strip())
                    i += 1
                    if any(x.strip() for x in body):
                        blocks.append(Block("math", "\n".join(x for x in body if x.strip())))
                    continue
                body.append(tail)
            i += 1
            while i < len(lines):
                ln = lines[i]
                if closer in ln:
                    head = ln.split(closer, 1)[0].strip()
                    if head:
                        body.append(head)
                    i += 1
                    break
                body.append(ln)
                i += 1
            content = "\n".join(x for x in body if x.strip()).strip()
            if content:
                blocks.append(Block("math", content))
            continue

        # Markdown 表格
        if _is_table_head(lines, i):
            flush_para()
            head = _split_row(lines[i])
            seps = _split_row(lines[i + 1])
            cols = max(len(head), len(seps))
            align: list[str] = []
            for c in seps:
                s = c.strip()
                align.append("center" if s.startswith(":") and s.endswith(":")
                             else "right" if s.endswith(":") else "left")
            align += ["left"] * (cols - len(align))
            rows = [head + [""] * (cols - len(head))]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                r = _split_row(lines[i])
                rows.append((r + [""] * (cols - len(r)))[:cols])
                i += 1
            blocks.append(Block("table", rows=rows, align=align))
            continue

        if _H_HR.match(line):
            flush_para()
            blocks.append(Block("hr"))
            i += 1
            continue

        m = _H_HEAD.match(line)
        if m:
            flush_para()
            blocks.append(Block("heading", m.group(2).strip(), level=len(m.group(1))))
            i += 1
            continue

        m = _H_QUOTE.match(line)
        if m:
            flush_para()
            quoted = [m.group(1)]
            i += 1
            while i < len(lines):
                mm = _H_QUOTE.match(lines[i])
                if not mm:
                    break
                quoted.append(mm.group(1))
                i += 1
            blocks.append(Block("quote", "\n".join(quoted)))
            continue

        if _H_ULI.match(line) or _H_OLI.match(line):
            flush_para()
            items: list[str] = []
            ordered = bool(_H_OLI.match(line))
            while i < len(lines):
                mu = _H_ULI.match(lines[i])
                mo = _H_OLI.match(lines[i])
                if mu:
                    items.append(mu.group(1))
                elif mo:
                    items.append(mo.group(2))
                elif lines[i].strip() == "":
                    break
                elif lines[i].startswith(("  ", "\t")) and items:
                    items[-1] += " " + lines[i].strip()      # 列表续行
                else:
                    break
                i += 1
            blocks.append(Block("list", "\n".join(items), level=1 if ordered else 0))
            continue

        if line.strip() == "":
            flush_para()
            i += 1
            continue

        buf.append(line)
        i += 1

    flush_para()
    return blocks


def block_html(b: Block) -> str:
    """块 → HTML 片段（供 QLabel 使用；code 块走 QTextEdit，不在这里）。"""
    if b.kind == "heading":
        size = {1: 17, 2: 15.5, 3: 14.5, 4: 14}.get(b.level, 14)
        body = inline_html(b.content)
        return (f'<div style="font-size:{size}px; font-weight:600; color:#161C28;'
                f' line-height:165%; margin:6px 0 2px 0;">{body}</div>')
    if b.kind == "list":
        rows = []
        for idx, item in enumerate(b.content.split("\n"), 1):
            mark = f"{idx}." if b.level == 1 else "•"
            rows.append(
                f'<div style="line-height:186%; margin-left:2px;">'
                f'<span style="color:#9AA0AA;">{mark}</span>&nbsp;&nbsp;{inline_html(item)}</div>')
        return "".join(rows)
    if b.kind == "quote":
        body = inline_html(b.content).replace("\n", "<br>")
        return (f'<div style="border-left:3px solid #DDE3EC; padding:2px 0 2px 10px;'
                f' color:#5B6270; line-height:186%;">{body}</div>')
    if b.kind == "hr":
        return '<div style="border-top:1px solid #EAEDF2; margin:10px 0;"></div>'
    if b.kind == "math":
        return ('<div style="background:#F7F9FC; border:1px solid #E9EDF3;'
                ' border-radius:8px; padding:8px 10px; margin:6px 0;'
                f' font-size:15px; color:#161C28;">{math_html(b.content, block=True)}</div>')
    if b.kind == "table":
        rows = b.rows or []
        if not rows:
            return ""
        cols = max(len(r) for r in rows)
        align = (b.align + ["left"] * cols)[:cols]
        out = ['<table border="1" cellspacing="0" cellpadding="6" width="100%" '
               'bordercolor="#D8E3F5" style="font-size:13.5px; color:#161C28;">']
        for ri, row in enumerate(rows):
            cells = (row + [""] * (cols - len(row)))[:cols]
            tag = "th" if ri == 0 else "td"
            bg = ' bgcolor="#F3F6FB"' if ri == 0 else ""
            out.append("<tr>")
            for ci, cell in enumerate(cells):
                body = inline_html(cell)
                if ri == 0:
                    body = f"<b>{body}</b>"
                out.append(f'<{tag}{bg} align="{align[ci]}">{body}</{tag}>')
            out.append("</tr>")
        out.append("</table>")
        return "".join(out)
    body = inline_html(b.content).replace("\n", "<br>")
    return f'<div style="line-height:186%; font-size:14px; color:#161C28;">{body}</div>'
