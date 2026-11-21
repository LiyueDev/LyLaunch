"""主搜索框「粘贴图片 / 文件」的载荷解析。

独立于 clipboard.py：后者只负责纯文本历史（changed=Signal(str)），
不要改动它。本模块把 QMimeData 解析成可交给插件的本地文件路径列表。

插件系统的调用面只有 ``query: str`` 一个字符串通道，因此剪贴板里的图片数据
必须先落成临时 PNG，再用路径传给插件（与截图插件 screenshot->ocr 的做法一致）。
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from dataclasses import dataclass, field

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage

from . import paths

KIND_IMAGE = "image"
KIND_FILES = "files"
KIND_UNKNOWN = "unknown"

# 与 plugins/ocr/main.py 的 IMG_EXTS、plugins/doc_recognize/inputs.py 对齐。
# 主程序侧自己维护一份：严禁 import 插件源码（插件与基础功能隔离）。
IMG_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".gif",
})
DOC_EXTS = frozenset({
    ".pdf", ".xlsx", ".xlsm", ".docx",
    ".txt", ".md", ".csv", ".log", ".json", ".yaml", ".yml",
})

# 临时 PNG 落点：程序自有数据目录，便于启动时扫残留、避开系统 Temp 被清理。
PASTE_TMP_DIR = os.path.join(str(paths.DATA_DIR), "paste_tmp")


@dataclass
class PastePayload:
    """一次粘贴 / 唤出感知得到的载荷。

    ``temp_files`` 只记录本载荷自己落盘的临时 PNG；清理时**绝不**删除
    ``paths`` 里来自资源管理器的用户原始文件。
    """

    kind: str = KIND_UNKNOWN
    paths: list[str] = field(default_factory=list)
    temp_files: list[str] = field(default_factory=list)
    is_image: bool = False
    is_doc: bool = False
    source: str = ""       # "paste"(Ctrl+V) | "sense"(唤出感知)
    summary: str = ""      # 给空状态提示用的短摘要

    @property
    def first_path(self) -> str:
        return self.paths[0] if self.paths else ""

    @property
    def usable(self) -> bool:
        return self.kind != KIND_UNKNOWN and bool(self.paths)

    def cleanup(self) -> None:
        """只删自己落的临时文件，不动用户原始文件。"""
        for p in self.temp_files:
            try:
                os.remove(p)
            except Exception:
                pass
        self.temp_files.clear()


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def _save_image(qimg: QImage) -> str:
    """把 QImage 落成唯一命名的临时 PNG，返回路径；失败返回空串。

    截图插件用的是固定名 lylaunch_shot.png（单图可覆盖）；这里支持多图 /
    多次粘贴，必须用 pid + 毫秒 + uuid 保证不互相覆盖。
    全程 str 路径（非 ASCII 用户名），不用 bytes。
    """
    try:
        os.makedirs(PASTE_TMP_DIR, exist_ok=True)
        name = f"p_{os.getpid()}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.png"
        p = os.path.join(PASTE_TMP_DIR, name)
        if not qimg.save(p, "PNG"):
            return ""
        # 落盘后回读校验，避免中文路径下静默写失败
        if not os.path.isfile(p) or QImage(p).isNull():
            return ""
        return p
    except Exception:
        return ""


def _as_qimage(value) -> QImage:
    """imageData() 在 Qt 里可能返回 QImage / QPixmap / QByteArray。"""
    if isinstance(value, QImage):
        return value
    if hasattr(value, "toImage"):          # QPixmap
        return value.toImage()
    try:
        return QImage(value)
    except Exception:
        return QImage()


def parse_mime(md) -> PastePayload:
    """解析剪贴板 QMimeData，优先级对齐 OCR 插件的 _image_from_clipboard。

    ① hasUrls()  → 资源管理器里复制的文件（可多文件）
    ② hasImage() → Win+Shift+S 截图 / 复制的图片数据 → 落临时 PNG
    ③ hasText()  → 少数工具复制的是文件绝对路径文本
    ④ 其余（纯文本）→ KIND_UNKNOWN，调用方据此放行给 QLineEdit 默认粘贴
    """
    if md is None:
        return PastePayload(kind=KIND_UNKNOWN)

    # ① 复制的文件
    try:
        if md.hasUrls():
            files = []
            for u in md.urls():
                if u.isLocalFile():
                    p = u.toLocalFile()
                    if p and os.path.isfile(p):
                        files.append(p)
            if files:
                exts = [_ext(p) for p in files]
                return PastePayload(
                    kind=KIND_IMAGE if (len(files) == 1 and exts[0] in IMG_EXTS) else KIND_FILES,
                    paths=files,
                    is_image=all(e in IMG_EXTS for e in exts),
                    is_doc=any(e in DOC_EXTS for e in exts),
                    summary=(os.path.basename(files[0]) if len(files) == 1
                             else f"{len(files)} 个文件"),
                )
    except Exception:
        pass

    # ② 剪贴板图片数据
    try:
        if md.hasImage():
            qimg = _as_qimage(md.imageData())
            if qimg is not None and not qimg.isNull():
                p = _save_image(qimg)
                if p:
                    return PastePayload(
                        kind=KIND_IMAGE, paths=[p], temp_files=[p],
                        is_image=True, summary="1 张剪贴板图片",
                    )
    except Exception:
        pass

    # ③ 路径文本兜底
    try:
        if md.hasText():
            t = (md.text() or "").strip().strip('"')
            if t and os.path.isfile(t):
                e = _ext(t)
                return PastePayload(
                    kind=KIND_IMAGE if e in IMG_EXTS else KIND_FILES,
                    paths=[t],
                    is_image=e in IMG_EXTS,
                    is_doc=e in DOC_EXTS,
                    summary=os.path.basename(t),
                )
    except Exception:
        pass

    return PastePayload(kind=KIND_UNKNOWN)


def clip_fingerprint(md) -> str:
    """只对图片 / 文件生成非空指纹；纯文本返回空串（不参与唤出感知）。

    用于「复制一次只感知一次」：指纹未变时，之后每次唤出都不再弹动作候选，
    避免用户复制过文件后正常搜索被永久打扰。
    """
    try:
        if md is None:
            return ""
        if md.hasUrls():
            ps = sorted(u.toLocalFile() for u in md.urls() if u.isLocalFile())
            return "U:" + "|".join(ps) if ps else ""
        if md.hasImage():
            # 坑：setImageData(QImage) 之后 md.data("application/x-qt-image") 返回的
            # 是空 QByteArray（Qt 内部用 QVariant 存 QImage，不序列化到 data()）。
            # 若拿它算 md5，所有图片指纹都相同 → 换一张新图也不会重新感知。
            # 这里把 QImage 编码成 PNG 字节再算，内容/尺寸任一不同都能区分。
            img = _as_qimage(md.imageData())
            if img is None or img.isNull():
                return "I:null"
            ba = QByteArray()
            buf = QBuffer(ba)
            if not buf.open(QIODevice.OpenModeFlag.WriteOnly):
                return f"I:{img.width()}x{img.height()}"
            img.save(buf, "PNG")
            buf.close()
            return "I:" + hashlib.md5(bytes(ba)).hexdigest()
        return ""
    except Exception:
        return ""


def cleanup_stale_temp_files(max_age_seconds: int = 86400) -> None:
    """启动时扫掉上次运行残留的临时 PNG（只扫本程序自建目录）。"""
    try:
        if not os.path.isdir(PASTE_TMP_DIR):
            return
        now = time.time()
        for name in os.listdir(PASTE_TMP_DIR):
            if not name.startswith("p_") or not name.lower().endswith(".png"):
                continue
            p = os.path.join(PASTE_TMP_DIR, name)
            try:
                if now - os.path.getmtime(p) > max_age_seconds:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass
