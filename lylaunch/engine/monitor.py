"""引擎运行监控：采样、请求埋点、7 天历史。

数据来源（2026-11-25 实测，别按老文档猜）：
1. `router /models` —— `data[i].status.value` = loaded / unloaded；**`status.args`
   里有 child 的 `--port`**（child 是独立 llama-server，可以直连）和 `--ctx-size`。
2. `child /props` —— `n_ctx` / `total_slots` / `model_alias` / `model_path` /
   `endpoint_metrics|props|slots`。
3. `child /slots` —— 每 slot 有 `n_ctx` / `is_processing` / `n_prompt_tokens` /
   `n_prompt_tokens_processed` / `n_prompt_tokens_cache`；**生成中 `next_token[0]`
   里有 `n_decoded` / `n_remain`**，T/s 就用它差分。
4. `router /props` 的 n_ctx 恒为 0、`router /slots` 直接 400 —— router 层这两样
   都不能用，必须走 child。
5. GPU：`nvidia-smi` 在 `C:\\Windows\\System32\\` 下，**PATH 里没有**（`runtime.
   vram_total_mb()` 之前因此一直返回 0，已在本轮一并修）。

两套引擎的区别：18090（插件）的请求都经 `RouterClient`，token 用量能逐请求精确
入库；18091（dsh 直连）拿不到请求明细，只有进程级采样，且 T/s 由 slot 差分估算。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from . import runtime

TTL_DAYS = 7
DB_NAME = "engine_stats.db"
SAMPLE_FIELDS = (
    "ts, port, running, model, ctx, total_slots, busy, prompt_tok, decoded, "
    "cache_tok, tps, vram_used, vram_total, gpu_util, gpu_temp"
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples(
  ts REAL, port INTEGER, running INTEGER, model TEXT, ctx INTEGER, total_slots INTEGER,
  busy INTEGER, prompt_tok INTEGER, decoded INTEGER, cache_tok INTEGER, tps REAL,
  vram_used INTEGER, vram_total INTEGER, gpu_util INTEGER, gpu_temp INTEGER);
CREATE TABLE IF NOT EXISTS requests(
  ts REAL, origin INTEGER, model TEXT, prompt_tok INTEGER, out_tok INTEGER,
  dur_ms REAL, ttft_ms REAL, tps REAL, ok INTEGER);
CREATE INDEX IF NOT EXISTS samples_port_ts ON samples(port, ts);
CREATE INDEX IF NOT EXISTS requests_origin_ts ON requests(origin, ts);
"""

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_local = threading.local()
_GPU_CACHE: dict = {"ts": 0.0, "data": {}}
_GPU_LOCK = threading.Lock()


# ---------------------------------------------------------------- 路径
def db_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "LyLaunch" / DB_NAME


# ---------------------------------------------------------------- HTTP
def _get(port: int, path: str, timeout: float = 0.35) -> dict | list | None:
    """读一个端点；连不上/超时返回 None（监控不能把界面拖住）。"""
    try:
        with _OPENER.open(f"http://127.0.0.1:{int(port)}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore") or "{}")
    except Exception:
        return None


# ---------------------------------------------------------------- GPU
def gpu(timeout: float = 3.0, cache_ms: int = 1500) -> dict:
    """一张卡的实时占用：{name,total_mb,used_mb,util,temp,power}；读不到返回 {}。

    nvidia-smi 一次约 30~60 ms（首调 100~300 ms），这里缓存 1.5 s，
    2 s 一次的采样不会重复起进程。
    """
    now = time.time()
    with _GPU_LOCK:
        if _GPU_CACHE["ts"] and (now - _GPU_CACHE["ts"]) * 1000 < cache_ms:
            return dict(_GPU_CACHE["data"])
    data: dict = {}
    exe = runtime.nvidia_smi()
    if exe:
        try:
            out = subprocess.run(
                [exe, "--query-gpu=name,memory.total,memory.used,utilization.gpu,"
                      "temperature.gpu,power.draw", "--format=csv,noheader,nounits"],
                capture_output=True, timeout=timeout, creationflags=0x08000000)
            if out.returncode == 0:
                line = out.stdout.decode("utf-8", "ignore").strip().splitlines()[0]
                parts = [p.strip() for p in line.split(",")]
                data = {
                    "name": parts[0],
                    "total_mb": _num(parts[1]),
                    "used_mb": _num(parts[2]),
                    "util": _num(parts[3]),
                    "temp": _num(parts[4]),
                    "power": _num(parts[5]),
                }
        except Exception:
            data = {}
    with _GPU_LOCK:
        _GPU_CACHE["ts"] = now
        _GPU_CACHE["data"] = dict(data)
    return data


def _num(text: str) -> int:
    try:
        return int(float(str(text).strip()))
    except Exception:
        return 0


# ---------------------------------------------------------------- 采样
class EngineMonitor:
    """一个实例管一套引擎的采样；UI 侧每 2 s 调一次 `sample()`。"""

    def __init__(self, origin: int) -> None:
        self.origin = int(origin)          # 18090 / 18091
        self._last: dict = {}              # 差分用：id_task / n_decoded / ts
        self._dead = False                 # 库彻底不可用时静默跳过

    # ---------- 库 ----------
    def _conn(self) -> sqlite3.Connection | None:
        if self._dead:
            return None
        conn = getattr(_local, "conn", None)
        if conn is not None:
            return conn
        try:
            path = db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            conn.commit()
            _local.conn = conn
            return conn
        except Exception:
            self._dead = True
            return None

    def trim(self, now: float | None = None) -> None:
        """清掉 7 天前的采样与请求记录。"""
        conn = self._conn()
        if conn is None:
            return
        cutoff = (now or time.time()) - TTL_DAYS * 86400
        try:
            conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            conn.execute("DELETE FROM requests WHERE ts < ?", (cutoff,))
            conn.commit()
        except Exception:
            pass

    # ---------- 采样 ----------
    def sample(self, timeout: float = 0.35) -> dict:
        ts = time.time()
        out = {
            "ts": ts, "port": self.origin, "running": False, "model": "", "ctx": 0,
            "total_slots": 0, "busy": False, "prompt_tok": 0, "decoded": 0,
            "cache_tok": 0, "tps": None, "vram_used": 0, "vram_total": 0,
            "gpu_util": 0, "gpu_temp": 0, "loaded": False,
        }
        models = _get(self.origin, "/models", timeout=timeout)
        if not models:
            self._fill_gpu(out)
            self._save(out)
            return out
        out["running"] = True
        rows = models.get("data") if isinstance(models, dict) else None
        entry = None
        for m in rows or []:
            status = (m.get("status") or {})
            if str(status.get("value", "")).lower() == "loaded":
                entry = m
                break
        if entry is None:
            self._fill_gpu(out)
            self._save(out)
            return out
        out["model"] = str(entry.get("id") or "")
        out["loaded"] = True
        args = list((entry.get("status") or {}).get("args") or [])
        child = 0
        ctx_args = 0
        if "--port" in args:
            try:
                child = int(args[args.index("--port") + 1])
            except Exception:
                child = 0
        if "--ctx-size" in args:
            try:
                ctx_args = int(args[args.index("--ctx-size") + 1])
            except Exception:
                ctx_args = 0
        out["ctx"] = ctx_args

        props = _get(child, "/props", timeout=timeout) if child else None
        if isinstance(props, dict):
            dgs = props.get("default_generation_settings") or {}
            out["ctx"] = _num(dgs.get("n_ctx")) or ctx_args
            out["total_slots"] = _num(props.get("total_slots"))

        slots = _get(child, "/slots", timeout=timeout) if child else None
        if isinstance(slots, list) and slots:
            slot = next((s for s in slots if s.get("is_processing")), slots[0])
            out["busy"] = bool(slot.get("is_processing"))
            out["prompt_tok"] = _num(slot.get("n_prompt_tokens"))
            out["cache_tok"] = _num(slot.get("n_prompt_tokens_cache"))
            nxt = (slot.get("next_token") or [{}])
            out["decoded"] = _num((nxt[0] if nxt else {}).get("n_decoded"))
            out["tps"] = self._diff(slot, ts, out["decoded"])
            out["task"] = _num(slot.get("id_task"))

        g = gpu()
        out["vram_used"] = g.get("used_mb", 0)
        out["vram_total"] = g.get("total_mb", 0)
        out["gpu_util"] = g.get("util", 0)
        out["gpu_temp"] = g.get("temp", 0)
        out["gpu_name"] = g.get("name", "")
        self._save(out)
        return out

    @staticmethod
    def _fill_gpu(out: dict) -> None:
        """没加载模型时也带上整卡显存/温度（面板不能只显示一行 0）。"""
        g = gpu()
        out["vram_used"] = g.get("used_mb", 0)
        out["vram_total"] = g.get("total_mb", 0)
        out["gpu_util"] = g.get("util", 0)
        out["gpu_temp"] = g.get("temp", 0)
        out["gpu_name"] = g.get("name", "")

    def _diff(self, slot: dict, ts: float, decoded: int) -> float | None:
        """用 `next_token.n_decoded` 差分算输出速度（t/s）。

        换了任务（`id_task` 变了）就重新起算，避免把两次请求串在一起；
        解码数回落（新任务从 0 开始）同样返回 None。
        """
        task = _num(slot.get("id_task"))
        prev = self._last.get(self.origin)
        self._last[self.origin] = {"ts": ts, "task": task, "decoded": decoded}
        if not prev or not slot.get("is_processing"):
            return None
        dt = ts - prev["ts"]
        dd = decoded - prev["decoded"]
        if dt <= 0.05 or dd <= 0 or prev["task"] != task or dd > 2000:
            return None
        return round(dd / dt, 1)

    def _save(self, row: dict) -> None:
        conn = self._conn()
        if conn is None:
            return
        try:
            conn.execute(
                "INSERT INTO samples(ts, port, running, model, ctx, total_slots, busy,"
                " prompt_tok, decoded, cache_tok, tps, vram_used, vram_total, gpu_util,"
                " gpu_temp) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["ts"], row["port"], int(row["running"]), row["model"], row["ctx"],
                 row["total_slots"], int(row["busy"]), row["prompt_tok"], row["decoded"],
                 row["cache_tok"], row["tps"] or 0, row["vram_used"], row["vram_total"],
                 row["gpu_util"], row["gpu_temp"]),
            )
            conn.commit()
        except Exception:
            pass

    # ---------- 请求埋点（18090 专属） ----------
    def record_request(self, model: str, prompt_tok: int, out_tok: int,
                       dur_ms: float, ttft_ms: float | None = None,
                       ok: bool = True) -> None:
        """一次推理请求落库。必须吞掉一切异常：埋点不能影响真实请求。"""
        conn = self._conn()
        if conn is None:
            return
        tps = None
        try:
            gen_ms = (dur_ms - (ttft_ms or 0))
            if out_tok and gen_ms > 50:
                tps = round(out_tok / (gen_ms / 1000.0), 1)
        except Exception:
            tps = None
        try:
            conn.execute(
                "INSERT INTO requests(ts, origin, model, prompt_tok, out_tok, dur_ms,"
                " ttft_ms, tps, ok) VALUES (?,?,?,?,?,?,?,?,?)",
                (time.time(), self.origin, model, int(prompt_tok), int(out_tok),
                 float(dur_ms), float(ttft_ms or 0), tps or 0, int(ok)),
            )
            conn.commit()
        except Exception:
            pass

    # ---------- 查询 ----------
    def summary(self) -> dict:
        """7 天内：请求数、prompt/输出 token 合计、平均 t/s。"""
        conn = self._conn()
        base = {"count": 0, "prompt_tok": 0, "out_tok": 0, "avg_tps": 0.0}
        if conn is None:
            return base
        cutoff = time.time() - TTL_DAYS * 86400
        try:
            row = conn.execute(
                "SELECT COUNT(1), COALESCE(SUM(prompt_tok),0), COALESCE(SUM(out_tok),0),"
                " COALESCE(AVG(NULLIF(tps,0)),0) FROM requests"
                " WHERE origin=? AND ts>=?", (self.origin, cutoff)).fetchone()
            return {"count": int(row[0] or 0), "prompt_tok": int(row[1] or 0),
                    "out_tok": int(row[2] or 0), "avg_tps": round(float(row[3] or 0), 1)}
        except Exception:
            return base

    def history(self, limit: int = 20) -> list[dict]:
        conn = self._conn()
        if conn is None:
            return []
        cutoff = time.time() - TTL_DAYS * 86400
        try:
            rows = conn.execute(
                "SELECT ts, model, prompt_tok, out_tok, dur_ms, tps, ok FROM requests"
                " WHERE origin=? AND ts>=? ORDER BY ts DESC LIMIT ?",
                (self.origin, cutoff, int(limit))).fetchall()
        except Exception:
            return []
        return [{"ts": r[0], "model": r[1], "prompt_tok": r[2], "out_tok": r[3],
                 "dur_ms": r[4], "tps": r[5], "ok": bool(r[6])} for r in rows]

    def series(self, field: str, limit: int = 60) -> list[float]:
        """最近 N 个采样点的某一列（给曲线用）；只取连续的同一 port。"""
        if field not in ("tps", "vram_used", "gpu_util", "gpu_temp", "decoded"):
            return []
        conn = self._conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT %s FROM samples WHERE port=? ORDER BY ts DESC LIMIT ?" % field,
                (self.origin, int(limit))).fetchall()
        except Exception:
            return []
        return [float(r[0] or 0) for r in reversed(rows)]

    def last_sample(self) -> dict | None:
        conn = self._conn()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT %s FROM samples WHERE port=? ORDER BY ts DESC LIMIT 1" % SAMPLE_FIELDS,
                (self.origin,)).fetchone()
        except Exception:
            return None
        if not row:
            return None
        keys = [k.strip() for k in SAMPLE_FIELDS.split(",")]
        return dict(zip(keys, row))


_instances: dict[int, EngineMonitor] = {}


def instance(origin: int) -> EngineMonitor:
    """按端口取单例（设置窗反复开关也只建一次）。"""
    if origin not in _instances:
        _instances[origin] = EngineMonitor(origin)
    return _instances[origin]
