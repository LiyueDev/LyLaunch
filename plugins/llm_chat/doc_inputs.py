"""本地 AI 的文档附件解析：PDF 文字层 / Excel / Word → 文本或 Markdown。

刻意不 import「文档识别」插件：插件之间不直接互相引用（隔离边界），
而且 doc_recognize 是可选插件、用户可能没装。这里用标准库 + QtPdf 做精简实现。

设计取舍：只做**零大模型**的解析（Excel/Word/PDF 文字层，纯本地、100% 准确），
不拉 700MB 引擎。同时提供 pdf_page_images() 把 PDF 页面渲染成图片 —— 本地的
Qwen3-VL 这类视觉模型本来就能读页面里的图表/公式/版式，扫描件也不必再被拒收。
"""

from __future__ import annotations

import os
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

DOC_EXTS = {".pdf", ".xlsx", ".xlsm", ".docx"}
MAX_CHARS = 3000       # 与 main.py 的 MAX_ATTACH_CHARS 对齐，避免撑爆上下文
MAX_PDF_PAGES = 30

# PDF 页面图：视觉模型每页约 1~2k visual token，8192 上下文最多带 3 页才安全
MAX_PDF_IMAGE_PAGES = 3
PDF_IMAGE_LONG_EDGE = 1400

SCAN_HINT = (
    "这个 PDF 抽不出文字层（扫描件，或文字被转成了图形/轮廓）。\n"
    "已改由本地 AI 把页面渲染成图片，交给模型自己识别。"
)


def parse(path: str) -> tuple[str, str, str]:
    """返回 (解析文本, 类型标签, 提示)。失败/扫描件时文本为空、提示非空。"""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            text, hint = _pdf_text(path)
            label = "PDF"
        elif ext in (".xlsx", ".xlsm"):
            text, hint = _excel_text(path), ""
            label = "Excel 表格"
        elif ext == ".docx":
            text, hint = _docx_text(path), ""
            label = "Word 文档"
        else:
            return "", "", f"暂不支持 {ext or '这种格式'}"
    except Exception as e:  # noqa: BLE001
        return "", "", f"读取失败（{type(e).__name__}）：{e}"
    if not text or not text.strip():
        return "", label, hint or f"没从{label}里读到文字"
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f"\n\n（只带入前 {MAX_CHARS} 字，原文更长）"
    return text, label, hint


# ---------- PDF：只抽文字层 ----------
def _pdf_text(path: str) -> tuple[str, str]:
    from PySide6.QtPdf import QPdfDocument
    doc = QPdfDocument(None)   # PySide6 绑定要求显式传 parent
    err = doc.load(path)
    if err != QPdfDocument.Error.None_:
        raise ValueError(f"PDF 打不开（{err}），可能已加密或已损坏")
    n = doc.pageCount()
    if n <= 0:
        raise ValueError("PDF 里没有页面")
    parts: list[str] = []
    for i in range(min(n, MAX_PDF_PAGES)):
        txt = ""
        try:
            sel = doc.getAllText(i)
            if sel is not None and sel.isValid():
                txt = sel.text() or ""
        except Exception:
            txt = ""
        if txt.strip():
            parts.append(f"【第 {i + 1} 页】\n{txt.strip()}")
    if n > MAX_PDF_PAGES:
        parts.append(f"（只解析前 {MAX_PDF_PAGES} 页，共 {n} 页）")
    if not parts:
        return "", SCAN_HINT
    return "\n\n".join(parts), ""


# ---------- PDF：页面渲染成图片，交给视觉模型 ----------
def pdf_page_images(path: str, max_pages: int = MAX_PDF_IMAGE_PAGES,
                    long_edge: int = PDF_IMAGE_LONG_EDGE) -> tuple[list[tuple[int, Any]], str]:
    """把 PDF 前 N 页渲染成 QImage，让模型像看截图一样读版式/图表/公式。

    返回 ([(页码(0 起), QImage)], 说明)。任何失败都只返回空列表，绝不抛异常 ——
    调用方不能因为这里失败就把用户的文件拦下来（「不限制发送」）。
    页数与分辨率都做上限：视觉 token 很贵，8192 上下文塞不下整本 PDF。
    """
    try:
        from PySide6.QtCore import QSize
        from PySide6.QtPdf import QPdfDocument
        doc = QPdfDocument(None)   # PySide6 绑定要求显式传 parent
        err = doc.load(path)
        if err != QPdfDocument.Error.None_:
            return [], f"PDF 打不开（{err}）"
        n = doc.pageCount()
        if n <= 0:
            return [], "PDF 里没有页面"
        out: list[tuple[int, Any]] = []
        for i in range(min(n, max_pages)):
            pt = doc.pagePointSize(i)
            w, h = float(pt.width()), float(pt.height())
            if w <= 0 or h <= 0:
                continue
            scale = long_edge / max(w, h)
            size = QSize(max(1, round(w * scale)), max(1, round(h * scale)))
            img = doc.render(i, size)
            if not img.isNull():
                out.append((i, img))
        note = f"共 {n} 页，只渲染前 {len(out)} 页" if n > max_pages else ""
        return out, note
    except Exception as e:  # noqa: BLE001
        return [], f"页面渲染失败（{type(e).__name__}）：{e}"


# ---------- Excel：zipfile + XML → Markdown 表格 ----------
def _md_cell(text: str) -> str:
    s = (text or "").replace("\r", " ").replace("\n", "<br>").strip()
    return s.replace("|", "\\|")


def _rows_to_markdown(name: str, rows: list[list[str]]) -> str:
    clean = [r for r in rows if any((c or "").strip() for c in r)]
    if not clean:
        return f"## {name}\n\n（空工作表）"
    cols = max(len(r) for r in clean)
    norm = [(r + [""] * (cols - len(r))) for r in clean]
    out = [f"## {name}", "",
           "| " + " | ".join(_md_cell(c) for c in norm[0]) + " |",
           "| " + " | ".join(["---"] * cols) + " |"]
    out += ["| " + " | ".join(_md_cell(c) for c in r) + " |" for r in norm[1:]]
    return "\n".join(out)


def _ref_to_rc(ref: str) -> tuple[int, int]:
    col, i = 0, 0
    for ch in ref:
        if ch.isalpha():
            col = col * 26 + (ord(ch.upper()) - 64)
            i += 1
        else:
            break
    row = int(ref[i:]) if ref[i:].isdigit() else 1
    return max(0, col - 1), max(0, row - 1)


def _shared_strings(z, names) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except Exception:
        return []
    return ["".join(t.text or "" for t in si.iter(f"{_NS}t"))
            for si in root.findall(f"{_NS}si")]


def _sheet_targets(z, names) -> list[tuple[str, str]]:
    rels: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in names:
        try:
            for rel in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")):
                rid, target = rel.get("Id"), rel.get("Target")
                if rid and target:
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
                target = rels.get(sh.get(_RID) or "")
                if target and target in names:
                    out.append((sh.get("name") or "Sheet", target))
        except Exception:
            out = []
    if not out:
        out = [(n.split("/")[-1][:-4], n) for n in sorted(names)
               if n.startswith("xl/worksheets/") and n.endswith(".xml")]
    return out


def _cell_value(c, shared: list[str]) -> str:
    t, v = c.get("t"), c.find(f"{_NS}v")
    if t == "s":
        if v is None or v.text is None:
            return ""
        try:
            return shared[int(v.text)]
        except (ValueError, IndexError):
            return ""
    if t == "inlineStr":
        el = c.find(f"{_NS}is")
        return "".join(x.text or "" for x in el.iter(f"{_NS}t")) if el is not None else ""
    if t == "b":
        return "TRUE" if (v is not None and v.text == "1") else "FALSE"
    return (v.text or "") if (v is not None and v.text) else ""


def _sheet_rows(z, target: str, shared: list[str]) -> list[list[str]]:
    ws = ET.fromstring(z.read(target))
    rows: list[list[str]] = []
    for row in ws.iter(f"{_NS}row"):
        cells: list[tuple[int, str]] = []
        for c in row.findall(f"{_NS}c"):
            cells.append((_ref_to_rc(c.get("r") or "")[0], _cell_value(c, shared)))
        if not cells:
            continue
        vals = [""] * (max(i for i, _ in cells) + 1)
        for i, val in cells:
            vals[i] = val
        rows.append(vals)
    # 合并单元格：把左上角值补到整个区域（区域过大就放弃，避免表格撑爆）
    mc = ws.find(f"{_NS}mergeCells")
    if mc is not None:
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
            if (c2 - c1 + 1) * (r2 - r1 + 1) > 64 or r1 >= len(rows):
                continue
            if c1 >= len(rows[r1]):
                continue
            val = rows[r1][c1]
            for r in range(r1, min(r2 + 1, len(rows))):
                for c in range(c1, min(c2 + 1, len(rows[r]))):
                    rows[r][c] = val
    return rows


def _excel_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared = _shared_strings(z, names)
        sheets = _sheet_targets(z, names)
        if not sheets:
            raise ValueError("这个 Excel 里没找到工作表")
        parts: list[str] = []
        for sheet_name, target in sheets:
            try:
                rows = _sheet_rows(z, target, shared)
            except Exception:
                continue
            if rows:
                parts.append(_rows_to_markdown(sheet_name, rows))
    if not parts:
        raise ValueError("工作表是空的")
    return "\n\n".join(parts)


# ---------- Word：段落 + 表格 ----------
def _docx_table(tbl) -> str:
    rows: list[list[str]] = []
    for tr in tbl.findall(f"{_W_NS}tr"):
        cells = ["".join(t.text or "" for t in tc.iter(f"{_W_NS}t")).strip()
                 for tc in tr.findall(f"{_W_NS}tc")]
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


def _docx_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        if "word/document.xml" not in z.namelist():
            raise ValueError("这不是一个有效的 Word 文档（.docx）")
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(f"{_W_NS}body")
    if body is None:
        raise ValueError("Word 文档是空的")
    paras: list[str] = []
    tables: list[str] = []
    for child in body:
        if child.tag == f"{_W_NS}p":
            text = "".join(t.text or "" for t in child.iter(f"{_W_NS}t")).strip()
            if text:
                paras.append(text)
        elif child.tag == f"{_W_NS}tbl":
            md = _docx_table(child)
            if md:
                tables.append(md)
    parts = (["\n\n".join(paras)] if paras else []) + tables
    if not parts:
        raise ValueError("Word 文档里没找到文字")
    return "\n\n".join(parts)
