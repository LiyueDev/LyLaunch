"""把模型输出的 Markdown 整理成能看的文档，并转成 HTML。

为什么需要这一层：
模型（Xiaomi-OCR-0）对整页文档的输出经常长这样——
    | 采购结算单 |
    | --- |
    | 单据编号： JS-20261030-0117 |
    | 商品名称 | 数量 | 单价 | 金额 | 备注 |
    | A4复印纸70g | 12箱 | 118.00 | 1416.00 |  |
也就是把整页当"单列表格"逐行输出，中间夹着真正的多列表格。
直接渲染的话，Markdown 要求同一张表列数一致，结果就是表格错乱、竖线满屏——
用户看到的"识别成文字有 bug"多半是这个观感问题，不是模型认错字。

这里的处理：
1. 逐块扫描表格行，把「单列行」抽出来当段落（键值对加粗），「多列行」才生成真表格；
2. 支持标题、列表、引用、代码块、粗体、行内代码的轻量解析；
3. 输出两份 HTML：给 QTextBrowser 预览用的（Qt 富文本子集），和给导出用的（完整 CSS，浏览器打开就好看）。
"""

from __future__ import annotations

import html as _html
import re

# ── LaTeX 公式：转成 Unicode 数学符号 ────────────────────────────────
# 为什么需要：模型把公式输出成 LaTeX（\int_{0}^{+\infty} ...），而 Qt 的富文本
# 组件不支持数学排版，纯文本导出更是只有一堆反斜杠。这里做"尽力而为"的转换：
# 常见符号、上下标、分式、根号都能变成看得懂的样子；实在表达不了的退回 ^(...) / _(...)。
_SYMBOLS = {
    # 希腊字母
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ",
    "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    # 运算
    "cdot": "·", "times": "×", "div": "÷", "pm": "±", "mp": "∓",
    "ast": "∗", "star": "⋆", "circ": "∘", "bullet": "•",
    "oplus": "⊕", "otimes": "⊗", "odot": "⊙",
    # 关系
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠",
    "approx": "≈", "equiv": "≡", "sim": "∼", "simeq": "≃", "propto": "∝",
    "ll": "≪", "gg": "≫", "lll": "⋘", "ggg": "⋙",
    # 箭头
    "to": "→", "rightarrow": "→", "leftarrow": "←", "gets": "←",
    "leftrightarrow": "↔", "Rightarrow": "⇒", "Leftarrow": "⇐",
    "Leftrightarrow": "⇔", "mapsto": "↦", "uparrow": "↑", "downarrow": "↓",
    "longrightarrow": "⟶", "longleftarrow": "⟵",
    # 大算符
    "int": "∫", "iint": "∬", "iiint": "∭", "oint": "∮",
    "sum": "∑", "prod": "∏", "coprod": "∐",
    "infin": "∞", "infty": "∞", "partial": "∂", "nabla": "∇",
    "lim": "lim", "sup": "sup", "inf": "inf", "max": "max", "min": "min",
    "log": "log", "ln": "ln", "exp": "exp", "sin": "sin", "cos": "cos",
    "tan": "tan", "cot": "cot", "sec": "sec", "csc": "csc",
    # 集合 / 逻辑
    "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆",
    "supset": "⊃", "supseteq": "⊇", "cup": "∪", "cap": "∩",
    "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃",
    "neg": "¬", "land": "∧", "lor": "∨",
    # 其他
    "ldots": "…", "cdots": "⋯", "dots": "…", "vdots": "⋮", "ddots": "⋱",
    "prime": "′", "angle": "∠", "perp": "⊥", "parallel": "∥",
    "therefore": "∴", "because": "∵", "degree": "°", "hbar": "ℏ", "ell": "ℓ",
    # 定界符
    "langle": "⟨", "rangle": "⟩", "lfloor": "⌊", "rfloor": "⌋",
    "lceil": "⌈", "rceil": "⌉", "vert": "|", "Vert": "‖", "|": "‖",
    "left": "", "right": "", "big": "", "Big": "", "bigg": "", "Bigg": "",
    # 间距 / 换行
    "quad": "  ", "qquad": "    ", ",": " ", ";": " ", ":": " ", "!": "",
    " ": " ", "displaystyle": "", "textstyle": "",
}

_SUP = dict(zip("0123456789+-=()nix", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱˣ"))
_SUP.update({"a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ",
             "h": "ʰ", "j": "ʲ", "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "o": "ᵒ", "p": "ᵖ",
             "r": "ʳ", "s": "ˢ", "t": "ᵗ", "u": "ᵘ", "v": "ᵛ", "w": "ʷ", "y": "ʸ",
             "z": "ᶻ"})
_SUB = dict(zip("0123456789+-=()aehijklmnoprstuvx",
                "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"))

_BRACE_ARG = re.compile(r"\{([^{}]*)\}")


def _to_script(s: str, sup: bool, html: bool) -> str:
    """上下标：HTML 模式用 <sup>/<sub>（Qt 富文本原生支持，字体一定渲染得出来）；
    纯文本模式用 ^(...) / _(...)（不用 ₀ₐₑ 这类罕见字符，雅黑里是缺字形的，会变乱码）。"""
    s = s.strip()
    if not s:
        return ""
    if html:
        tag = "sup" if sup else "sub"
        return f"<{tag}>{_html.escape(s, quote=False)}</{tag}>"
    mark = "^" if sup else "_"
    return f"{mark}({s})" if len(s) > 1 else f"{mark}{s}"


def latex_to_text(expr: str, html: bool = False) -> str:
    """LaTeX → 可读文本。

    html=True 时上下标用 <sup>/<sub>（给预览/导出用，视觉上是真正的上下标）；
    html=False 时用 ^()/( )（给纯文本导出用，任何编辑器都不会乱码）。
    """
    s = (expr or "").strip()
    s = s.replace("$$", "").replace("$", "")
    s = re.sub(r"\\begin\{[^}]*\}|\\end\{[^}]*\}", "", s)

    # \text{...} 之类：只保留内容
    for cmd in ("text", "mathrm", "mathbf", "mathit", "mathbb", "mathcal",
                "operatorname", "textbf", "boxed", "overline", "bar"):
        s = re.sub(rf"\\{cmd}\s*", "", s)

    # 先换命令（\alpha → α），再处理分式/根号/上下标，这样 {\alpha} 里的内容也已是符号
    for name in sorted(_SYMBOLS, key=len, reverse=True):
        s = re.sub(rf"\\{re.escape(name)}(?![a-zA-Z])", _SYMBOLS[name], s)

    def frac(m):
        a, b = m.group(1).strip(), m.group(2).strip()
        pa = a if len(a) <= 2 and " " not in a else f"({a})"
        pb = b if len(b) <= 2 and " " not in b else f"({b})"
        return f"{pa}/{pb}"

    for _ in range(3):
        s = re.sub(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", frac, s)

    s = re.sub(r"\\sqrt\s*\[([^\]]*)\]\s*\{([^{}]*)\}",
               lambda m: f"{_to_script(m.group(1), True, html)}√({m.group(2)})", s)
    s = re.sub(r"\\sqrt\s*\{([^{}]*)\}", lambda m: f"√({m.group(1)})", s)

    s = re.sub(r"\^\s*\{([^{}]*)\}", lambda m: _to_script(m.group(1), True, html), s)
    s = re.sub(r"\^\s*(\w)", lambda m: _to_script(m.group(1), True, html), s)
    s = re.sub(r"_\s*\{([^{}]*)\}", lambda m: _to_script(m.group(1), False, html), s)
    s = re.sub(r"_\s*(\w)", lambda m: _to_script(m.group(1), False, html), s)

    # 剩余命令去掉反斜杠，分组花括号去掉
    s = re.sub(r"\\([a-zA-Z]+)", r"\1", s)
    s = s.replace("\\", "")
    s = _BRACE_ARG.sub(r"\1", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def latex_to_unicode(expr: str) -> str:
    """兼容别名：纯文本形式。"""
    return latex_to_text(expr, html=False)


# ── 内联元素（粗体 / 行内代码 / 链接）────────────────────────────────
_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_MATH = re.compile(r"(\$\$.+?\$\$|\$[^$\n]+?\$)")
# 模型有时不用 $ 包裹，直接吐 LaTeX 环境：\begin{align*}...\end{align*}
# 这是实测踩到的（识别数学公式时就是这种输出），不认的话预览里会漏出反斜杠源码。
_MATH_ENV = re.compile(r"\\begin\{([a-zA-Z*]+)\}.*?\\end\{\1\}", re.S)


def _wrap_latex_delims(text: str) -> str:
    r"""把 \[ ... \] 和 \( ... \) 统一成 $$ / $，方便后面统一处理。"""
    text = re.sub(r"\\\[(.+?)\\\]", lambda m: f"$${m.group(1)}$$", text, flags=re.S)
    text = re.sub(r"\\\((.+?)\\\)", lambda m: f"${m.group(1)}$", text, flags=re.S)
    return text


def _math_inline_html(raw: str) -> str:
    """公式 → HTML（转义特殊字符，但保留我们自己生成的 <sup>/<sub>）。"""
    t = latex_to_text(raw, html=True)
    for tag in ("sup", "sub"):
        t = t.replace(f"<{tag}>", f"\x01{tag}\x01").replace(f"</{tag}>", f"\x01/{tag}\x01")
    t = _html.escape(t, quote=False)
    for tag in ("sup", "sub"):
        t = t.replace(f"\x01{tag}\x01", f"<{tag}>").replace(f"\x01/{tag}\x01", f"</{tag}>")
    return t


def _math_html(raw: str) -> str:
    """一个数学块/行内公式 → HTML（unicode 近似 或 保留 LaTeX 给 KaTeX）。"""
    if _MATH_MODE == "latex":
        return f'<div class="mathblock">$${raw}$$</div>'
    return f'<p class="math">{_math_inline_html(raw)}</p>'


_MATH_MODE = "unicode"   # "unicode"：公式转 Unicode 近似；"latex"：原样保留给浏览器 KaTeX


def _set_math_mode(mode: str) -> None:
    global _MATH_MODE
    _MATH_MODE = mode


def _inline_plain(text: str) -> str:
    t = _html.escape(text, quote=False)
    t = _INLINE_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", t)
    t = _BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", t)
    t = _ITALIC.sub(lambda m: f"<i>{m.group(1)}</i>", t)
    return t


def _inline(text: str) -> str:
    parts = _MATH.split(text)
    if len(parts) == 1:
        return _inline_plain(text)
    out: list[str] = []
    for i, p in enumerate(parts):
        if i % 2 == 1:      # 公式
            if _MATH_MODE == "latex":
                out.append(p)                       # 留给浏览器端的 KaTeX
            else:
                out.append('<span class="math">'
                           + _math_inline_html(p.strip().strip("$"))
                           + '</span>')
        else:
            out.append(_inline_plain(p))
    return "".join(out)


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


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c or "--") for c in cells)


def _kv_html(line: str) -> str:
    """「键：值」这种单列行，渲染成加粗键 + 值，比一整段更好读。"""
    m = re.match(r"^([^：:]{1,12})[：:]\s*(.+)$", line.strip())
    if m:
        return f'<p class="kv"><b>{_inline(m.group(1))}</b>：{_inline(m.group(2))}</p>'
    return f'<p class="line">{_inline(line.strip())}</p>'


def _table_html(rows: list[list[str]]) -> str:
    """把若干行（列数可能不齐）补成等宽表格。"""
    if not rows:
        return ""
    cols = max(len(r) for r in rows)
    norm = [r + [""] * (cols - len(r)) for r in rows]
    head = norm[0]
    body = norm[1:]
    out = ['<table cellspacing="0" cellpadding="6" border="1">']
    out.append("<tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr>")
    for r in body:
        out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
    out.append("</table>")
    return "".join(out)


def _flush_table(rows: list[list[str]], out: list[str]) -> None:
    """把一块表格行拆成「单列 → 段落」+「多列 → 表格」。"""
    pending_single: list[str] = []
    pending_multi: list[list[str]] = []

    def flush_multi() -> None:
        if pending_multi:
            out.append(_table_html(pending_multi))
            pending_multi.clear()

    for cells in rows:
        if _is_separator(cells):
            continue
        cells = [c for c in cells]
        non_empty = [c for c in cells if c]
        if len(non_empty) <= 1:
            flush_multi()
            pending_single.append(non_empty[0] if non_empty else "")
        else:
            if pending_single:
                for s in pending_single:
                    if s.strip():
                        out.append(_kv_html(s))
                pending_single.clear()
            pending_multi.append(cells)
    if pending_single:
        for s in pending_single:
            if s.strip():
                out.append(_kv_html(s))
    flush_multi()


def md_to_body(md: str) -> str:
    """Markdown（含模型那种伪表格 + LaTeX 公式）→ HTML 片段。"""
    text = _strip_repeat_loop(_normalize_ocr_tags((md or "").strip()))
    for tag in ("<md>", "</md>", "<doc>", "</doc>"):
        text = text.replace(tag, "")
    text = _wrap_latex_delims(text)
    lines = [ln.rstrip() for ln in text.splitlines()]

    out: list[str] = []
    i = 0
    in_code = False
    code_buf: list[str] = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 代码块
        if stripped.startswith("```"):
            if in_code:
                out.append("<pre><code>" + _html.escape("\n".join(code_buf)) + "</code></pre>")
                code_buf.clear()
                in_code = False
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue

        # LaTeX 数学环境：模型识别公式时会直接吐 \begin{align*}...\end{align*}
        if "\\begin{" in stripped:
            buf = [stripped]
            while "\\end{" not in buf[-1] and i + 1 < len(lines):
                i += 1
                buf.append(lines[i].strip())
            i += 1
            out.append(_math_html(" ".join(buf)))
            continue

        # 表格块（连续）
        if _is_table_row(line):
            rows: list[list[str]] = []
            while i < len(lines) and _is_table_row(lines[i]):
                rows.append(_split_row(lines[i]))
                i += 1
            _flush_table(rows, out)
            continue

        if not stripped:
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level = min(3, len(m.group(1)))
            tag = {1: "h2", 2: "h3", 3: "h4"}[level]
            out.append(f"<{tag}>{_inline(m.group(2))}</{tag}>")
            i += 1
            continue

        # 引用
        if stripped.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].strip())
                i += 1
            out.append("<blockquote>" + "<br>".join(_inline(b) for b in buf) + "</blockquote>")
            continue

        # 列表
        if re.match(r"^([-*+]|\d+\.)\s+", stripped):
            items = []
            while i < len(lines) and re.match(r"^([-*+]|\d+\.)\s+", lines[i].strip()):
                items.append(re.sub(r"^([-*+]|\d+\.)\s+", "", lines[i].strip()))
                i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(t)}</li>" for t in items) + "</ul>")
            continue

        # 普通段落：连着几行合并成一段
        buf = [stripped]
        i += 1
        while (i < len(lines) and lines[i].strip()
               and not re.match(r"^(#{1,6}\s|>|\||```|[-*+]\s|\d+\.\s)", lines[i].strip())):
            buf.append(lines[i].strip())
            i += 1
        # 普通文本一律按段落渲染：OCR 结果第一行往往不是标题，
        # 以前把首块强制升成 <h2> 会平白冒出一行巨大加粗的字（很突兀）。
        para = " ".join(buf)
        out.append(f"<p>{_inline(para)}</p>")

    if code_buf:
        out.append("<pre><code>" + _html.escape("\n".join(code_buf)) + "</code></pre>")
    return "\n".join(out)


# ── 样式：预览用（Qt 富文本子集，别用 flex/grid）──────────────────
_WIDGET_CSS = """
/* 与「本地 AI」插件的 Markdown 观感对齐：14px / 186% 行高 / 浅蓝表格描边。
   仍是 doc_core 共享层这一套渲染器，两处共用，不另起炉灶。 */
body { font-family: "Microsoft YaHei UI","Microsoft YaHei",sans-serif;
       font-size: 14px; color: #161C28; line-height: 186%; margin: 0; }
h2 { font-size: 18px; font-weight: 600; margin: 4px 0 10px 0; color: #161C28; }
h3 { font-size: 15px; font-weight: 600; margin: 12px 0 6px 0; color: #161C28; }
h4 { font-size: 14px; font-weight: 600; margin: 10px 0 4px 0; color: #3D4450; }
p { margin: 6px 0; }
p.kv { margin: 3px 0; }
blockquote { margin: 8px 0; padding-left: 10px; color: #646A73;
             border-left: 3px solid #DDE3EC; }
ul { margin: 4px 0 4px 18px; }
li { margin: 2px 0; }
table { margin: 8px 0; border-color: #D8E3F5; }
th { background: #F3F6FB; font-weight: 600; }
td, th { border: 1px solid #D8E3F5; font-size: 13px; }
code { background: #F7F9FC; font-family: Consolas, monospace; font-size: 12px; }
sup, sub { font-size: 72%; }
pre { background: #F7F9FC; padding: 8px; }
"""

# ── 样式：导出用（完整 CSS，浏览器打开即成品）──────────────────────
_BROWSER_CSS = """
:root { color-scheme: light; }
body {
  margin: 0 auto; padding: 48px 24px 72px; max-width: 820px;
  font-family: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  font-size: 15px; line-height: 1.75; color: #161C28; background: #FFFFFF;
}
h2 { font-size: 24px; margin: 0 0 20px; padding-bottom: 10px; border-bottom: 2px solid #E5E6EB; }
h3 { font-size: 18px; margin: 28px 0 10px; }
h4 { font-size: 15px; margin: 20px 0 8px; color: #3D4450; }
p { margin: 10px 0; }
p.kv b { color: #161C28; }
blockquote { margin: 16px 0; padding: 8px 16px; border-left: 4px solid #2F6BEE;
             background: #F7F8FA; color: #4E5561; }
ul { padding-left: 22px; }
li { margin: 4px 0; }
table { border-collapse: collapse; width: 100%; margin: 16px 0; font-size: 14px; }
th, td { border: 1px solid #D5D9DF; padding: 8px 10px; text-align: left; vertical-align: top; }
th { background: #F5F6F8; font-weight: 600; }
tr:nth-child(even) td { background: #FBFCFD; }
code { background: #F5F6F8; padding: 1px 5px; border-radius: 4px;
       font-family: Consolas, "Courier New", monospace; font-size: 13px; }
pre { background: #F5F6F8; padding: 14px 16px; border-radius: 8px; overflow-x: auto; }
pre code { background: none; padding: 0; }
sup, sub { font-size: 72%; }
footer { margin-top: 40px; padding-top: 12px; border-top: 1px solid #E5E6EB;
         color: #5F6570; font-size: 12px; }
@media print { body { padding: 0; max-width: none; } footer { display: none; } }
"""


# ── 模型输出预处理：按模型"方言"统一成 Markdown ───────────────────
# Xiaomi-OCR-0 输出的是它自己那套 Markdown（偶尔把整页包成单列表格），
# md_to_body() 已经能处理；OvisOCR2 输出的是 HTML 表格 + LaTeX + bbox 图片标签，
# 直接喂进渲染会剩一屏生标签，所以进 clean()/to_html() 之前先在这里统一。
# 以后加新模型，输出格式不同就加个分支，别去改通用的 md_to_body()。
_BBOX_IMG = re.compile(
    r'''<img\b[^>]*\bsrc\s*=\s*["']images/bbox_[^"']*["'][^>]*>''', re.I | re.S)
_HTML_TABLE = re.compile(r"<table\b[^>]*>.*?</table>", re.I | re.S)
_TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.I | re.S)
_CELL = re.compile(r"<(?:td|th)\b[^>]*>(.*?)</(?:td|th)>", re.I | re.S)
_ANY_TAG = re.compile(r"<[^>]+>")


def _cell_text(fragment: str) -> str:
    s = re.sub(r"<br\s*/?>", " ", fragment, flags=re.I)
    s = _ANY_TAG.sub("", s)
    # 单元格里原本的 | 会撞上 Markdown 表格的列分隔符。用全角竖线代替
    # 而不是 \| ：本文件的 _split_row() 不认转义，纯文本导出会留下一个反斜杠。
    return _html.unescape(s).strip().replace("|", "｜")


def _html_tables_to_md(text: str) -> str:
    """OvisOCR2 的 <table> → 管道表格；列数不齐补空，管道符转义。"""
    def conv(m: re.Match) -> str:
        rows: list[list[str]] = []
        for tr in _TR.findall(m.group(0)):
            cells = [_cell_text(c) for c in _CELL.findall(tr)]
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        cols = max(len(r) for r in rows)
        rows = [r + [""] * (cols - len(r)) for r in rows]
        out = [
            "| " + " | ".join(rows[0]) + " |",
            "| " + " | ".join(["---"] * cols) + " |",
        ]
        out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n\n" + "\n".join(out) + "\n\n"
    return _HTML_TABLE.sub(conv, text)


def _clean_truncated_repeats(text: str, min_text_len: int = 800,
                             max_period: int = 200, min_repeat_chars: int = 100,
                             min_repeat_times: int = 5) -> str:
    """削掉结尾的重复循环尾巴。

    OvisOCR2 在复杂页面上偶尔会陷进循环（官方推理示例里专门写了同名函数）：
    从结尾往前找最短重复单元，重复次数够了就在循环起点截掉。
    """
    n = len(text)
    if n < min_text_len:
        return text
    for unit_len in range(1, max_period + 1):
        match_len = 0
        idx = n - 1
        while idx - unit_len >= 0 and text[idx] == text[idx - unit_len]:
            match_len += 1
            idx -= 1
        total_len = match_len + unit_len
        if total_len < min_repeat_chars:
            continue
        if total_len // unit_len >= min_repeat_times:
            tail = total_len % unit_len
            return text[:n - total_len + unit_len] + (text[n - tail:] if tail else "")
    return text


# ── 识别模型的结构化标签 → 干净 Markdown ─────────────────────────
# 依据 Xiaomi-OCR-0 官方模型卡（xiaomi-research/Xiaomi-OCR-0）：
#   「Tables should be expressed in **OTSL** format」——
#   <fcel>(有内容的格) / <ecel>(空格) / <lcel>(左并) / <ucel>(上并) /
#   <xcel>(双向并) / <nl>(换行) 都是 OTSL 的正式 token，**不是垃圾标签**。
# ⚠️ 2026-11-14 修正：上一版把这些 token 直接删掉，等于把表格结构毁了
#   （整页被拍平成一串段落）。现在改为解析 OTSL → Markdown 管道表格。
# 另外 Xiaomi 偶尔还会混入 Paddle/PP-Structure 风格标签（<loc_坐标>、
# <md_header_level_N>、<list_item>、<COLSPAN/ROWSPAN>、<picture>），一并清掉。
_OTSL_TOK = re.compile(r"<(fcel|ecel|lcel|ucel|xcel|nl)\s*/?>", re.I)
_LOC_TAG = re.compile(r"<loc_[^>]*>", re.I)
_HEAD_TAG = re.compile(r"<md_header_level_(\d)\s*>", re.I)
_LIST_OPEN = re.compile(r"</?(?:unordered_list|ordered_list)\s*>", re.I)
_ITEM_OPEN = re.compile(r"<list_item[^>]*>", re.I)
_ITEM_CLOSE = re.compile(r"</list_item\s*>", re.I)
_SPAN_TAG = re.compile(r"</?(?:COLSPAN|ROWSPAN)\s*=\s*\d+\s*>", re.I)
_PIC_TAG = re.compile(r"<picture\b[^>]*>.*?</picture\s*>", re.I | re.S)
_PIC_OPEN = re.compile(r"<picture\b[^>]*>", re.I)
_BROKEN_TAG = re.compile(r"</l\s*>")


def _otsl_rows(text: str) -> list[list[str]]:
    """OTSL 文本 → 二维单元格数组。

    合并位（lcel/ucel/xcel）本身不含文本，Markdown 表格又不支持合并，
    所以这些位置留空 —— 内容已经在起始格里，读起来不丢字。
    """
    rows: list[list[str]] = []
    cur: list[str] = []
    cell: str | None = None
    pos = 0
    for m in _OTSL_TOK.finditer(text):
        seg = text[pos:m.start()]
        pos = m.end()
        tag = m.group(1).lower()
        if cell is not None:
            cell += seg
        if tag == "fcel":
            if cell is not None:
                cur.append(cell)
            cell = ""
        elif tag == "nl":
            if cell is not None:
                cur.append(cell)
                cell = None
            rows.append(cur)
            cur = []
        else:                      # ecel / lcel / ucel / xcel：空位
            if cell is not None:
                cur.append(cell)
                cell = None
            cur.append("")
    if cell is not None:
        cur.append(cell)
    if cur:
        rows.append(cur)
    return rows


def _otsl_to_md(text: str) -> str:
    """OTSL → Markdown。单列的整页（很常见）退化成段落，别画成一条竖线表格。"""
    rows = _otsl_rows(text)
    while rows and not any(c.strip() for c in rows[-1]):
        rows.pop()
    if not rows:
        return ""
    cols = max(len(r) for r in rows)
    if cols <= 1:
        return "\n\n".join(c.strip() for r in rows for c in r if c.strip())
    out: list[str] = []
    for r in rows:
        cells = [c.replace("\n", " ").replace("|", "｜").strip() for c in r]
        cells += [""] * (cols - len(cells))
        out.append("| " + " | ".join(cells) + " |")
    out.insert(1, "| " + " | ".join(["---"] * cols) + " |")
    return "\n\n" + "\n".join(out) + "\n\n"


def _normalize_ocr_tags(text: str) -> str:
    """结构化标签 → 干净 Markdown（幂等；没有标签时原样返回）。"""
    s = text or ""
    # 1) OTSL 表格 → 真表格 / 段落
    if _OTSL_TOK.search(s):
        s = _otsl_to_md(s)
    # 2) 其它方言残留（没有就不动，避免误伤正常 Markdown）
    if any(r.search(s) for r in (_LOC_TAG, _HEAD_TAG, _LIST_OPEN, _ITEM_OPEN,
                                 _SPAN_TAG, _PIC_TAG, _PIC_OPEN, _BROKEN_TAG)):
        s = _PIC_TAG.sub("", s)               # 整块图片占位直接去掉
        s = _PIC_OPEN.sub("", s)
        s = _LOC_TAG.sub("", s)               # 版面坐标：没裁图，留着没用
        s = _HEAD_TAG.sub(lambda m: "\n" + "#" * min(6, int(m.group(1))) + " ", s)
        s = _LIST_OPEN.sub("\n", s)
        s = _ITEM_OPEN.sub("\n- ", s)
        s = _ITEM_CLOSE.sub("\n", s)
        s = _SPAN_TAG.sub("", s)              # <COLSPAN=6> 之类，Markdown 表格用不上
        s = _BROKEN_TAG.sub("", s)
        s = re.sub(r"[ \t]+\n", "\n", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
    # 每行独立成段（Xiaomi 的连续文本行不这样会被挤成一坨）；
    # 但**表格行、列表行各自必须连续**，否则表格 / 列表会被空行拆散。
    out: list[str] = []
    prev_kind = ""
    for ln in (x.strip() for x in s.splitlines()):
        if not ln:
            continue
        if _is_table_row(ln):
            kind = "tbl"
        elif re.match(r"^[-*+]\s+|^\d+\.\s+", ln):
            kind = "list"
        else:
            kind = "text"
        if out and not (kind == prev_kind and kind in ("tbl", "list")):
            out.append("")
        out.append(ln)
        prev_kind = kind
    return "\n".join(out)


# ── 句子级复读检测（解码退化兜底）────────────────────────────────
# temperature=0 贪心解码 + 模板化版面（免责声明、逐条编号）会让小 VLM
# 陷入自回归循环：同一句话换个编号反复吐，直到撞满 max_tokens。
# 旧的 _clean_truncated_repeats 只认「末尾逐字重复」，
# 遇到「九、…十、…十一、…」这种编号递增的模板复读完全失效。
_SEG_SPLIT = re.compile(r"(?<=[。！？；!?;\n])")
_CN_ORD = r"[零〇一二三四五六七八九十百千两0-9]+"
_LOOP_PREFIX = re.compile(
    r"^\s*(?:第\s*" + _CN_ORD + r"\s*[条项点、.．,，)）:]|"
    r"\(?" + _CN_ORD + r"\s*[、.．,，)）:）])\s*")


def _seg_key(seg: str) -> str:
    """片段 → 归一化 key：去掉递增编号前缀与空白，只留模板正文。"""
    s = _LOOP_PREFIX.sub("", seg.strip())
    return re.sub(r"\s+", "", s)


def _strip_repeat_loop(text: str, min_key_len: int = 12,
                       min_repeats: int = 5, keep: int = 2) -> str:
    """截掉结尾的「同模板 + 递增编号」复读，保留前 keep 次（幂等）。

    例：「一、本报告不得…使用。二、本报告不得…使用。……二十三、…」
    → 只留前 2 条。正常文档里偶发重复句不会触发（要求连续 ≥5 次）。
    """
    s = text or ""
    parts = [p for p in _SEG_SPLIT.split(s) if p]
    if len(parts) < min_repeats + 2:
        return s
    keys = [_seg_key(p) for p in parts]
    # 从尾部往前找最长连续相同模板
    last = keys[-1]
    if len(last) < min_key_len:
        return s
    start = len(keys) - 1
    while start > 0 and keys[start - 1] == last:
        start -= 1
    run = len(keys) - start
    if run < min_repeats:
        return s
    return "".join(parts[:start + keep]).rstrip()


def preprocess(text: str, dialect: str = "xiaomi") -> str:
    """模型原始输出 → md_render 认得的 Markdown。dialect 来自 ModelSpec。"""
    s = _strip_repeat_loop(_normalize_ocr_tags(text or ""))
    if dialect != "ovisocr2":
        return s
    s = _BBOX_IMG.sub("", s)          # bbox 占位图我们没裁图，留着只会变成死链
    s = _html_tables_to_md(s)         # HTML 表格 → 管道表格
    s = re.sub(r"\n{3,}", "\n\n", s)  # 表格前后别留一大片空白
    return _clean_truncated_repeats(s.strip()).strip()


def clean(md: str) -> str:
    """去掉模型包裹标签与行首尾多余空格（导出 .md 时用，保持 Markdown 原样）。"""
    text = _strip_repeat_loop(_normalize_ocr_tags((md or "").strip()))
    for tag in ("<md>", "</md>", "<doc>", "</doc>", "```markdown"):
        text = text.replace(tag, "")
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if s == "```":
            continue
        lines.append(s)
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def to_plain(md: str) -> str:
    """转成纯文本：表格用两个空格分隔、公式转 Unicode、去掉所有 Markdown 标记。"""
    text = _wrap_latex_delims(clean(md))
    # 数学环境（可能跨行）先整体转成 Unicode，避免后面按行处理时漏掉
    text = _MATH_ENV.sub(lambda m: "\n" + latex_to_unicode(m.group(0)) + "\n", text).strip()
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append("")
            continue
        # 公式先转成 Unicode（不然纯文本里就是一堆 \int \cdot）
        if "$" in s:
            s = _MATH.sub(lambda m: latex_to_unicode(m.group(0).strip("$")), s)
        if _is_table_row(s):
            cells = _split_row(s)
            if _is_separator(cells):
                continue
            cells = [c for c in cells if c]
            out.append("  ".join(cells) if cells else "")
            continue
        s = re.sub(r"^#{1,6}\s+", "", s)
        s = re.sub(r"^>\s?", "", s)
        s = re.sub(r"^([-*+]|\d+\.)\s+", "", s)
        s = _INLINE_CODE.sub(r"\1", s)
        s = _BOLD.sub(r"\1", s)
        s = _ITALIC.sub(r"\1", s)
        out.append(s)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def to_html(md: str, for_browser: bool = False, title: str = "识别结果",
            model_name: str = "本地模型") -> str:
    """Markdown → HTML。for_browser=True 产出可直接保存/打开的完整网页。"""
    raw_md = md or ""
    has_math = ("$" in raw_md) or ("\\begin{" in raw_md) or ("\\(" in raw_md)
    # 导出网页时保留 LaTeX 原样，交给浏览器端的 KaTeX 排成真正的公式；
    # 预览（Qt 富文本不支持数学排版）则转成 Unicode 近似。
    _set_math_mode("latex" if for_browser else "unicode")
    try:
        body = md_to_body(md)
    finally:
        _set_math_mode("unicode")
    css = _BROWSER_CSS if for_browser else _WIDGET_CSS
    if for_browser:
        katex = ""
        if has_math:
            katex = (
                '<link rel="stylesheet" '
                'href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">\n'
                '<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>\n'
                '<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"\n'
                '        onload="renderMathInElement(document.body,{delimiters:['
                "{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false},"
                "{left:'\\\\(',right:'\\\\)',display:false}],throwOnError:false});\"></script>\n"
            )
        return (
            "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
            "<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{_html.escape(title)}</title>\n"
            f"{katex}<style>{css}</style>\n</head>\n<body>\n{body}\n"
            "<footer>由 LyLaunch · 文档识别（本地 " + _html.escape(model_name) + "）生成"
            + ("；公式由 KaTeX 排版，离线打开时公式显示为 LaTeX 源码" if has_math else "")
            + "</footer>\n</body>\n</html>\n"
        )
    return f"<html><head><style>{css}</style></head><body>{body}</body></html>"
