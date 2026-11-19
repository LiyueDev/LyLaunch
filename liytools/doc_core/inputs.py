"""输入适配层：把用户丢进来的各种文件，变成"一页页可以识别的内容"。

为什么要单独一层：
界面原来只认"一张图"，用户拖个 PDF 进来会静默失败（QImage 读不了 PDF，
文件对话框的过滤器也只有图片格式）。与其在界面里堆 if/else，不如这里把
各种格式统一成 Page 列表，界面只管"一页一页往下识别"。

Page 有两种：
  · image 页 —— 需要交给模型识别（图片、扫描版 PDF）
  · text  页 —— 本身就是结构化文本，不用模型（Excel、纯文本、有文字层的 PDF）

设计取舍（都是踩过才知道的）：
1. PDF **优先抽文字层**（QtPdf 的 getAllText）：文本型 PDF 直接抽字，秒出、
   不丢精度；抽不出字（扫描件 / 拍照件）才渲染成图交给模型 —— 等于"自动 OCR"。
   反过来做（全部渲染成图再 OCR）等于把电子文本降级成图片，精度和速度都亏。
2. Excel **绝不交给模型**：xlsx 是结构化 XML，模型读截图会丢数值类型、丢公式、
   丢合并单元格，还慢十倍。标准库 zipfile + xml 就能解析成 Markdown 表格，
   零依赖、零模型、100% 准确。
3. 不引入 PyMuPDF / openpyxl：主程序打包体积和依赖都很敏感，
   这里只用 PySide6 自带的 QtPdf 和标准库。

依赖：只有 PySide6（QImage / QSize / QtPdf）和标准库，不 import 本项目的其他模块，
方便单独跑测试。
"""

from __future__ import annotations

import os
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QSize, QSizeF
from PySide6.QtGui import QImage


# ── 文件类型 ──────────────────────────────────────────────────────
KIND_IMAGE = "image"
KIND_PDF = "pdf"
KIND_EXCEL = "excel"
KIND_TEXT = "text"
KIND_DOCX = "docx"
KIND_UNSUPPORTED = "unsupported"

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".gif"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm"}
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".log", ".json", ".yaml", ".yml"}
DOCX_SUFFIXES = {".docx"}

SUPPORT_HINT = {
    KIND_IMAGE: "图片",
    KIND_PDF: "PDF",
    KIND_EXCEL: "Excel 表格",
    KIND_TEXT: "纯文本",
    KIND_DOCX: "Word 文档",
}

# 渲染 PDF 时的目标宽度（像素）。1600 与识别侧 MAX_SIDE 对齐，
# 再大只是白白多烧视觉 token（token 数随像素面积涨）。
PDF_RENDER_WIDTH = 1600
# 文字层少于这个字符数就当"没有文字层"（扫描件），转走模型识别。
MIN_TEXT_LAYER_CHARS = 24
# 单个文件最多处理多少页，防止几百页的 PDF 卡死界面。
MAX_PAGES_PER_FILE = 300


@dataclass
class Page:
    """一页内容。image 为 None 表示这页不用模型，直接用 text。"""
    label: str
    image: QImage | None = None
    text: str = ""
    source: str = ""        # 文字层 / 模型识别 / 直接解析
    chars: int = 0          # 已识别的正文字数（识别后回填）


@dataclass
class Loaded:
    """一个待处理的文件。"""
    path: str
    name: str
    kind: str = KIND_UNSUPPORTED
    pages: list[Page] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.pages)

    @property
    def needs_model(self) -> bool:
        return any(p.image is not None for p in self.pages)


def detect_kind(path: str | os.PathLike) -> str:
    """按扩展名判断类型。"""
    ext = Path(path).suffix.lower()
    if ext in IMAGE_SUFFIXES:
        return KIND_IMAGE
    if ext == ".pdf":
        return KIND_PDF
    if ext in EXCEL_SUFFIXES:
        return KIND_EXCEL
    if ext in DOCX_SUFFIXES:
        return KIND_DOCX
    if ext in TEXT_SUFFIXES:
        return KIND_TEXT
    return KIND_UNSUPPORTED


def load(path: str | os.PathLike, max_pages: int = MAX_PAGES_PER_FILE) -> Loaded:
    """读一个文件 → Loaded。失败时在 error 里写给用户看的原因，不抛异常。"""
    p = str(path)
    name = os.path.basename(p)
    kind = detect_kind(p)
    out = Loaded(path=p, name=name, kind=kind)
    try:
        if kind == KIND_IMAGE:
            out.pages = [_load_image(p)]
        elif kind == KIND_PDF:
            out.pages = _load_pdf(p, max_pages)
        elif kind == KIND_EXCEL:
            out.pages = _load_excel(p)
        elif kind == KIND_DOCX:
            out.pages = _load_docx(p)
        elif kind == KIND_TEXT:
            out.pages = _load_text(p)
        else:
            out.error = ("暂不支持这种格式；目前支持图片、PDF、Excel、"
                         "Word、TXT/Markdown/CSV")
    except Exception as e:  # noqa: BLE001
        out.error = f"读取失败（{type(e).__name__}）：{e}"
        out.pages = []
    return out


# ── 图片 ──────────────────────────────────────────────────────────
def _load_image(path: str) -> Page:
    img = QImage(path)
    if img.isNull():
        raise ValueError("图片读不出来，可能已损坏或格式不支持")
    return Page(label="整页", image=img, source="模型识别")


# ── PDF ───────────────────────────────────────────────────────────
def _load_pdf(path: str, max_pages: int) -> list[Page]:
    """PDF：先抽文字层，抽不出才渲染成图（自动 OCR）。"""
    from PySide6.QtPdf import QPdfDocument

    doc = QPdfDocument(None)     # 这个构造函数要求显式传 parent（PySide6 的绑定方式）
    err = doc.load(path)
    if err != QPdfDocument.Error.None_:
        raise ValueError(f"PDF 打不开（{err}），可能已加密或已损坏")
    try:
        n = doc.pageCount()
        if n <= 0:
            raise ValueError("PDF 里没有页面")
        pages: list[Page] = []
        for i in range(min(n, max_pages)):
            label = f"第 {i + 1} 页"
            text = ""
            try:
                sel = doc.getAllText(i)
                if sel is not None and sel.isValid():
                    text = sel.text() or ""
            except Exception:
                text = ""
            if len(text.strip()) >= MIN_TEXT_LAYER_CHARS:
                pages.append(Page(label=label, text=_tidy_text(text),
                                  source="文字层"))
                continue
            img = _render_pdf_page(doc, i)
            if img is None:
                pages.append(Page(label=label, text="", source="模型识别"))
                continue
            pages.append(Page(label=label, image=img, source="模型识别"))
        return pages
    finally:
        doc.close()


def _render_pdf_page(doc, index: int):
    """按页面真实比例渲染，避免传固定尺寸把版面拉变形。"""
    from PySide6.QtPdf import QPdfDocumentRenderOptions

    try:
        size: QSizeF = doc.pagePointSize(index)
        w = max(1, size.width())
        h = max(1, size.height())
    except Exception:
        w = h = 1.0
    scale = PDF_RENDER_WIDTH / w
    target = QSize(PDF_RENDER_WIDTH, max(1, int(round(h * scale))))
    opts = QPdfDocumentRenderOptions()
    try:
        opts.setScaledSize(target)
    except Exception:
        pass
    try:
        img = doc.render(index, target, opts)
    except Exception:
        return None
    return None if img.isNull() else img


def _tidy_text(text: str) -> str:
    """文字层清理：去掉页眉页脚式的连续空行、压缩空白。"""
    lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    for ln in lines:
        if not ln.strip():
            if out and out[-1] == "":
                continue
            out.append("")
            continue
        out.append(ln.strip())
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


# ── Excel ─────────────────────────────────────────────────────────
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _load_excel(path: str) -> list[Page]:
    """xlsx → 每个工作表一页 Markdown 表格。

    纯标准库解析（zipfile + xml），不引 openpyxl：主程序打包很忌讳重依赖，
    而且这里只需要"值"，不需要样式/公式/图表。
    """
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared = _read_shared_strings(z, names)
        sheets = _read_sheet_targets(z, names)
        if not sheets:
            raise ValueError("这个 Excel 里没找到工作表")
        pages: list[Page] = []
        for sheet_name, target in sheets:
            try:
                rows = _read_sheet_rows(z, target, shared)
            except Exception:
                continue
            if not rows:
                continue
            pages.append(Page(label=f"工作表 {sheet_name}",
                              text=_rows_to_markdown(sheet_name, rows),
                              source="直接解析"))
        if not pages:
            raise ValueError("工作表是空的")
        return pages


def _read_shared_strings(z, names) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except Exception:
        return []
    out = []
    for si in root.findall(f"{_NS}si"):
        out.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
    return out


def _read_sheet_targets(z, names) -> list[tuple[str, str]]:
    """工作表名 → 内部文件名。按 workbook.xml 的顺序，没有就按文件名排。"""
    rels: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in names:
        try:
            rr = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            for rel in rr:
                rid = rel.get("Id")
                target = rel.get("Target")
                if not rid or not target:
                    continue
                t = target.lstrip("/")
                if not t.startswith("xl/"):
                    t = "xl/" + t
                rels[rid] = t
        except Exception:
            rels = {}
    out: list[tuple[str, str]] = []
    if "xl/workbook.xml" in names:
        try:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            for sh in wb.iter(f"{_NS}sheet"):
                nm = sh.get("name") or "Sheet"
                rid = sh.get(_RID)
                target = rels.get(rid or "")
                if target and target in names:
                    out.append((nm, target))
        except Exception:
            out = []
    if not out:
        out = [(n.split("/")[-1][:-4], n) for n in sorted(names)
               if n.startswith("xl/worksheets/") and n.endswith(".xml")]
    return out


def _read_sheet_rows(z, target: str, shared: list[str]) -> list[list[str]]:
    ws = ET.fromstring(z.read(target))
    rows: list[list[str]] = []
    for row in ws.iter(f"{_NS}row"):
        cells: list[tuple[int, str]] = []
        for c in row.findall(f"{_NS}c"):
            ref = c.get("r") or ""
            idx = _col_index(ref)
            val = _cell_value(c, shared)
            cells.append((idx, val))
        if not cells:
            continue
        width = max(i for i, _ in cells) + 1
        row_vals = [""] * width
        for i, val in cells:
            row_vals[i] = val
        rows.append(row_vals)
    _fill_merged(ws, rows)
    return rows


def _cell_value(c, shared: list[str]) -> str:
    t = c.get("t")
    v = c.find(f"{_NS}v")
    if t == "s":
        if v is None or v.text is None:
            return ""
        try:
            return shared[int(v.text)]
        except (ValueError, IndexError):
            return ""
    if t == "inlineStr":
        is_el = c.find(f"{_NS}is")
        if is_el is None:
            return ""
        return "".join(x.text or "" for x in is_el.iter(f"{_NS}t"))
    if t == "b":
        return "TRUE" if (v is not None and v.text == "1") else "FALSE"
    if t == "str":
        return v.text if (v is not None and v.text) else ""
    return v.text if (v is not None and v.text) else ""


def _fill_merged(ws, rows: list[list[str]]) -> None:
    """把合并单元格的左上角值补到整个区域，表格看起来才跟原来一样。

    区域太大会把 Markdown 表格撑得极宽，这种就放弃（保持原样）。
    """
    mc = ws.find(f"{_NS}mergeCells")
    if mc is None:
        return
    for m in mc.findall(f"{_NS}mergeCell"):
        ref = m.get("ref") or ""
        if ":" not in ref:
            continue
        try:
            a, b = ref.split(":", 1)
            c1, r1 = _ref_to_rc(a)
            c2, r2 = _ref_to_rc(b)
        except Exception:
            continue
        if (c2 - c1 + 1) * (r2 - r1 + 1) > 64:
            continue
        if r1 >= len(rows) or c1 >= len(rows[r1]):
            continue
        val = rows[r1][c1]
        for r in range(r1, min(r2 + 1, len(rows))):
            row = rows[r]
            for c in range(c1, min(c2 + 1, len(row))):
                row[c] = val


def _ref_to_rc(ref: str) -> tuple[int, int]:
    """'B3' → (列2, 行3)，均 0-based。"""
    col = 0
    i = 0
    for ch in ref:
        if ch.isalpha():
            col = col * 26 + (ord(ch.upper()) - 64)
            i += 1
        else:
            break
    row = int(ref[i:]) if ref[i:].isdigit() else 1
    return max(0, col - 1), max(0, row - 1)


def _col_index(ref: str) -> int:
    return _ref_to_rc(ref)[0] if ref else 0


def _rows_to_markdown(name: str, rows: list[list[str]]) -> str:
    """行数组 → Markdown 表格。第一行当表头（Excel 本身没有表头概念，
    这是最接近人读法的折中）。"""
    clean = [r for r in rows if any((c or "").strip() for c in r)]
    if not clean:
        return f"## {name}\n\n（空工作表）"
    cols = max(len(r) for r in clean)
    norm = [(r + [""] * (cols - len(r))) for r in clean]
    out = [f"## {name}", ""]
    out.append("| " + " | ".join(_md_cell(c) for c in norm[0]) + " |")
    out.append("| " + " | ".join(["---"] * cols) + " |")
    for r in norm[1:]:
        out.append("| " + " | ".join(_md_cell(c) for c in r) + " |")
    return "\n".join(out)


def _md_cell(text: str) -> str:
    s = (text or "").replace("\r", " ").replace("\n", "<br>").strip()
    # 竖线会撞上 Markdown 表格的列分隔符，换成全角
    return s.replace("|", "｜")


# ── 纯文本 ────────────────────────────────────────────────────────
def _load_text(path: str) -> list[Page]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except Exception as e:
        raise ValueError(f"文本读取失败：{e}")
    if not raw.strip():
        raise ValueError("文件是空的")
    # Markdown 空行特别多，按空行切会被碎成几十上百「第 N 段」，阅读/复制都困难 →
    # 整篇作为一页（预览可滚动、一键复制/导出整篇）。
    if str(path).lower().endswith((".md", ".markdown")):
        return [Page(label="全文", text=raw.strip(), source="直接解析")]
    # 其它纯文本仍按空行分节，一节一页（长文档不至于一大坨）
    chunks = [c.strip() for c in raw.split("\n\n") if c.strip()]
    if not chunks:
        chunks = [raw.strip()]
    if len(chunks) > MAX_PAGES_PER_FILE:
        chunks = chunks[:MAX_PAGES_PER_FILE]
    return [Page(label=f"第 {i + 1} 段", text=c, source="直接解析")
            for i, c in enumerate(chunks)]


# ── Word ──────────────────────────────────────────────────────────
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _load_docx(path: str) -> list[Page]:
    """docx → 段落 + 表格的 Markdown（只取文本，不做样式还原）。"""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "word/document.xml" not in names:
            raise ValueError("这不是一个有效的 Word 文档（.docx）")
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(f"{_W_NS}body")
    if body is None:
        raise ValueError("Word 文档是空的")
    out: list[str] = []
    tables: list[str] = []
    for child in body:
        tag = child.tag
        if tag == f"{_W_NS}p":
            text = "".join(t.text or "" for t in child.iter(f"{_W_NS}t")).strip()
            if text:
                out.append(text)
        elif tag == f"{_W_NS}tbl":
            md = _docx_table(child)
            if md:
                tables.append(md)
    parts: list[str] = []
    if out:
        parts.append("\n\n".join(out))
    parts.extend(tables)
    if not parts:
        raise ValueError("Word 文档里没找到文字")
    return [Page(label="正文", text="\n\n".join(parts), source="直接解析")]


def _docx_table(tbl) -> str:
    rows: list[list[str]] = []
    for tr in tbl.findall(f"{_W_NS}tr"):
        cells = []
        for tc in tr.findall(f"{_W_NS}tc"):
            txt = "".join(t.text or "" for t in tc.iter(f"{_W_NS}t")).strip()
            cells.append(txt)
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    cols = max(len(r) for r in rows)
    norm = [(r + [""] * (cols - len(r))) for r in rows]
    out = ["| " + " | ".join(_md_cell(c) for c in norm[0]) + " |",
           "| " + " | ".join(["---"] * cols) + " |"]
    out += ["| " + " | ".join(_md_cell(c) for c in r) + " |" for r in norm[1:]]
    return "\n".join(out)
