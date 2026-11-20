"""Markdown → 纯文本（给「图片转文字」用，粘进 Excel 能分列）。

这一套和 md_render.to_plain() 是**故意分开**的：
- 这里表格转**制表符**，粘到 Excel / WPS 会自动分列；
- md_render.to_plain() 是给文档导出用的，表格用两个空格、公式转 Unicode。

硬合成一套会牺牲其中一个场景，所以保留两份实现，只是都收进 doc_core。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- Markdown → 纯文本
# 为什么需要这一步：模型输出的是它自己那套 Markdown（表格用管道、公式用 LaTeX、
# 偶尔整页包一层 <md> 或单列表格）。用户要的是「能直接粘出去的文字」，
# 所以这里全剥掉。表格转成制表符 —— 粘进 Excel / WPS 能自动分列。
_RE_FENCE = re.compile(r"^\s*```[A-Za-z0-9_+.-]*\s*$")
_RE_HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_RE_HEAD = re.compile(r"^#{1,6}\s*")
_RE_QUOTE = re.compile(r"^(?:>\s?)+")
_RE_LIST = re.compile(r"^(?:[-*+]|\d{1,3}[.)])\s+")
_RE_IMG = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_RE_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_RE_AUTOLINK = re.compile(r"<((?:https?|mailto):[^>\s]+)>", re.I)
_RE_CODE = re.compile(r"`([^`]+)`")
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_RE_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<![\w])_([^_\n]+)_(?![\w])")
_RE_TAG = re.compile(
    r"</?(?:br|b|i|u|s|p|div|span|md|doc|hr|"
    r"table|thead|tbody|tr|td|th|sub|sup|code|pre)\b[^>]*>", re.I)
_RE_MATH_ENV = re.compile(r"\\\[(.+?)\\\]|\\\((.+?)\\\)", re.S)
_RE_FRAC = re.compile(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_RE_SQRT = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
_RE_TEXTCMD = re.compile(r"\\(?:text|mathrm|mathbf|mathit|operatorname)\s*\{([^{}]*)\}")

_LATEX_SYM = {
    "times": "×", "div": "÷", "cdot": "·", "pm": "±", "mp": "∓",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "equiv": "≡", "propto": "∝", "infty": "∞",
    "sum": "∑", "prod": "∏", "int": "∫", "partial": "∂", "nabla": "∇",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "theta": "θ", "lambda": "λ", "mu": "μ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "tau": "τ", "phi": "φ", "omega": "ω", "psi": "ψ",
    "Delta": "Δ", "Omega": "Ω", "Sigma": "Σ", "Phi": "Φ",
    "degree": "°", "rightarrow": "→", "to": "→", "leftarrow": "←",
    "forall": "∀", "exists": "∃", "in": "∈", "notin": "∉",
}


def _latex_light(s: str) -> str:
    r"""轻量 LaTeX 可读化：去掉 $ 定界符，常见符号转 Unicode，分式/根号拍平。

    不做完整公式排版（那是 md_chat 里给 AI 回复用的），这里只求「能读、能复制」。
    认不出来的命令原样保留 —— 保留 \xxx 至少没丢信息。
    """
    if "\\" not in s and "$" not in s:
        return s
    s = _RE_MATH_ENV.sub(lambda m: m.group(1) or m.group(2) or "", s)
    s = s.replace("$$", "").replace("$", "")
    s = _RE_FRAC.sub(lambda m: _frac(m.group(1), m.group(2)), s)
    s = _RE_SQRT.sub(lambda m: f"√({m.group(1).strip()})", s)
    s = _RE_TEXTCMD.sub(lambda m: m.group(1), s)
    s = re.sub(r"\\([A-Za-z]+)",
               lambda m: _LATEX_SYM.get(m.group(1), m.group(0)), s)
    return s


def _frac(num: str, den: str) -> str:
    """\\frac{a+b}{c} → (a+b)/c；\\frac{1}{2} → 1/2（简单项不加括号，别乱套）。"""
    a, b = num.strip(), den.strip()
    if not re.fullmatch(r"[\w.]+", a):
        a = f"({a})"
    if not re.fullmatch(r"[\w.]+", b):
        b = f"({b})"
    return f"{a}/{b}"


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_table_sep(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c or "--") for c in cells)


def _inline(s: str) -> str:
    """单行内的 Markdown 行内标记 → 纯文字。"""
    s = _RE_IMG.sub(lambda m: m.group(1), s)
    s = _RE_LINK.sub(lambda m: m.group(1), s)
    s = _RE_AUTOLINK.sub(lambda m: m.group(1), s)
    s = _RE_CODE.sub(lambda m: m.group(1), s)
    s = _RE_BOLD.sub(lambda m: m.group(1) or m.group(2) or "", s)
    s = _RE_ITALIC.sub(lambda m: m.group(1) or m.group(2) or "", s)
    s = _RE_TAG.sub("", s)
    s = _latex_light(s)
    # 去掉 $ / \[ \] 后常在两侧留出双空格（如「面积 S = πr² ，周长」），收成一个
    return re.sub(r"[ ]{2,}", " ", s).strip()


_RE_CELL_END = re.compile(r"</t[dh]>\s*", re.I)
_RE_CELL_BLOCK = re.compile(r"<(t[dh])\b[^>]*>(.*?)</\1>", re.I | re.S)
_RE_ROW_END = re.compile(r"</tr>\s*", re.I)
_RE_BR = re.compile(r"<br\s*/?>", re.I)


def _pre_html(text: str) -> str:
    """HTML 表格 → 制表符分隔（OvisOCR2 那种输出会直接吐 <table>）。

    不这么做的话，标签被 _RE_TAG 一键删光，整张表会粘成「姓名年龄张三28」一行。
    单元格内部的 <br> 特意换成空格而不是换行：换行会把制表符的分列结构打断。
    """
    if "<" not in text:
        return text
    s = _RE_CELL_BLOCK.sub(
        lambda m: f"<{m.group(1)}>{_RE_BR.sub(' ', m.group(2))}</{m.group(1)}>", text)
    s = _RE_BR.sub("\n", s)
    s = _RE_CELL_END.sub("\t", s)
    s = _RE_ROW_END.sub("\n", s)
    return s


def _strip_wrappers(md: str) -> str:
    s = (md or "").strip()
    for tag in ("<md>", "</md>", "<doc>", "</doc>", "```markdown", "```Markdown"):
        s = s.replace(tag, "")
    return s


def md_to_text(md: str) -> str:
    """模型输出的 Markdown → 可直接复制/粘贴的纯文本。

    规则：围栏与分隔线整行删；标题/引用/列表去符号（列表保留缩进）；行内标记剥掉；
    连续 |...| 行合成一行、用制表符分列（粘进 Excel 能直接分列）。折叠多余空行。
    """
    text = _pre_html(_strip_wrappers(md))
    out: list[str] = []
    table: list[list[str]] = []

    def flush_table() -> None:
        if table:
            # 行尾空单元格会留下尾随制表符，看不见但粘贴时多余
            out.extend("\t".join(cells).rstrip("\t") for cells in table)
            table.clear()

    for raw in text.splitlines():
        s = raw.strip()
        indent = raw[:len(raw) - len(raw.lstrip())]
        if _RE_FENCE.match(s):
            flush_table()
            continue
        if _is_table_row(s):
            cells = _split_row(s)
            if _is_table_sep(cells):
                continue
            table.append([_inline(c) for c in cells])
            continue
        flush_table()
        if not s:
            out.append("")
            continue
        if _RE_HR.match(s):
            continue
        s = _RE_HEAD.sub("", s)
        s = _RE_QUOTE.sub("", s)
        s = _RE_LIST.sub("", s)
        out.append(indent + _inline(s))

    flush_table()

    # 折叠连续空行（模型常在表格 / 公式前后留两三个空行）
    tidy: list[str] = []
    for ln in out:
        if not ln.strip() and (not tidy or not tidy[-1].strip()):
            continue
        tidy.append(ln)
    while tidy and not tidy[-1].strip():
        tidy.pop()
    return "\n".join(tidy)
