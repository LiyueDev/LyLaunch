"""瓦片图标的磁盘缓存：第二次开面板完全不碰 shell，也不闪白纸占位图标。

为什么需要：`extract_icon_image` 单张 3~5ms（首次摸到某个 exe 时可能到 90ms），
84 项加起来 0.6~1.2 秒，这段时间瓦片是 Qt 标准白纸图标。把结果按
「路径 + mtime」存进一个 sqlite 文件，下次开面板直接读库。

为什么是 sqlite + 原始像素而不是每张一个 PNG：
- 84 张 PNG 单文件各自都要走一次文件系统/杀软，实测单张 1.6ms（84 张 146ms 都压在
  构造里）；sqlite 一次查询读 84 行原始像素只要 ~1ms。
- 一个文件不会在用户目录里堆一堆碎图，清理也只是一条 DELETE。

失效策略：key 里带 mtime，文件改过/换过图标自然对不上；超过上限按写入时间删旧。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

_MAX_ROWS = 400
_STORE_PX = 64          # 存 64px：瓦片 32 逻辑像素 × 2 倍 DPI 够用
_DBNAME = "icon_cache.db"

_local = threading.local()

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS icons ("
    "k TEXT PRIMARY KEY, w INTEGER, h INTEGER, data BLOB, ts REAL)")


def _db_path() -> str | None:
    base = os.environ.get("LOCALAPPDATA") or ""
    if not base:
        return None
    folder = os.path.join(base, "LyLaunch")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        return None
    return os.path.join(folder, _DBNAME)


def _drop_files(path: str) -> None:
    """删掉 db 和它的 -wal/-shm（坏库自愈用；删不掉就算了）。"""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def _close(conn) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


def _open(path: str) -> sqlite3.Connection:
    """失败时自己把连接关掉再抛 —— 否则异常回溯还握着连接（帧没释放），
    文件被 Windows 锁住，`_drop_files` 删不掉，重建也必然失败。"""
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(_SCHEMA)
        # 探一次读：库里被写坏（disk I/O error）时 CREATE 能过、SELECT 立刻报错，
        # 探测放在这里才能在建连阶段就识别成「坏库」并触发重建。
        conn.execute("SELECT COUNT(1) FROM icons").fetchone()
        return conn
    except Exception:
        _close(conn)
        raise


def _conn() -> sqlite3.Connection | None:
    """每个线程一条连接（worker 线程写、UI 线程读，互不干扰）。

    自愈：库被写坏（例如另一个进程删了还在用的文件 → `disk I/O error`）
    时先关连接、删掉重建一次；再失败就把本线程的缓存标死，后续全部当 miss
    处理 —— 图标缓存只是加速手段，绝不能因为它坏掉而影响面板可用性。
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    if getattr(_local, "dead", False):
        return None
    path = _db_path()
    if not path:
        _local.dead = True
        return None
    # 第一次：直接用
    conn = None
    try:
        conn = _open(path)
        _local.conn = conn
        return conn
    except Exception:
        _close(conn)                 # 不关掉的话文件被锁住，重建也会失败
        _drop_files(path)
    # 第二次：重建后重试
    conn = None
    try:
        conn = _open(path)
        _local.conn = conn
        return conn
    except Exception:
        _close(conn)
        _local.dead = True
        return None


def _reset_conn() -> None:
    """出错的连接作废，下次调用重新开会话（会再走一遍自愈）。"""
    conn = getattr(_local, "conn", None)
    _local.conn = None
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _key(path: str) -> str | None:
    try:
        mtime = int(os.stat(path).st_mtime)
    except OSError:
        return None
    raw = f"{path.casefold()}|{mtime}".encode("utf-8", "ignore")
    return hashlib.sha1(raw).hexdigest()[:20]


def load(path: str) -> QImage | None:
    """命中返回 64px 以内的原图，miss / 出错返回 None。"""
    conn = _conn()
    key = _key(path) if conn else None
    if not conn or not key:
        return None
    try:
        row = conn.execute(
            "SELECT w, h, data FROM icons WHERE k = ?", (key,)).fetchone()
        if not row:
            return None
        w, h, data = row
        img = QImage(data, w, h, QImage.Format.Format_ARGB32)
        if img.isNull():
            return None
        return img.copy()          # QImage 不接管 bytes，必须拷一份
    except Exception:
        _reset_conn()
        return None


def save(path: str, img: QImage) -> None:
    """后台线程里调用（一条 INSERT 约 0.05ms）。"""
    conn = _conn()
    key = _key(path) if conn else None
    if not conn or not key or img is None or img.isNull():
        return
    try:
        if img.width() > _STORE_PX or img.height() > _STORE_PX:
            img = img.scaled(_STORE_PX, _STORE_PX, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        if img.format() != QImage.Format.Format_ARGB32:
            img = img.convertToFormat(QImage.Format.Format_ARGB32)
        data = img.constBits().tobytes()
        conn.execute(
            "INSERT OR REPLACE INTO icons (k, w, h, data, ts) VALUES (?, ?, ?, ?, ?)",
            (key, img.width(), img.height(), data, time.time()))
        conn.commit()
    except Exception:
        _reset_conn()


def trim(max_rows: int = _MAX_ROWS) -> int:
    """行数超上限就按写入时间删最旧的，返回删除行数。"""
    conn = _conn()
    if not conn:
        return 0
    try:
        total = conn.execute("SELECT COUNT(1) FROM icons").fetchone()[0]
        if total <= max_rows:
            return 0
        conn.execute(
            "DELETE FROM icons WHERE k IN ("
            "SELECT k FROM icons ORDER BY ts ASC LIMIT ?)", (total - max_rows,))
        conn.commit()
        return total - max_rows
    except Exception:
        _reset_conn()
        return 0


def stats() -> dict:
    """给验证脚本/诊断用：行数 + 文件大小 + 路径（读失败也返回结构，不抛）。"""
    conn = _conn()
    path = _db_path() or ""
    rows = 0
    size = 0
    if conn:
        try:
            rows = conn.execute("SELECT COUNT(1) FROM icons").fetchone()[0]
        except Exception:
            _reset_conn()
            rows = 0
    try:
        size = os.path.getsize(path) if path else 0
    except OSError:
        size = 0
    return {"path": path, "rows": rows, "bytes": size}
