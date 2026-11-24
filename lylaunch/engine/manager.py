"""EngineManager：全局 Router 的单例管理者。

职责：起停 router（唯一常驻 llama-server）、按需加载/卸载模型（单活跃 + LRU 语义）、
空闲释放、异常兜底。插件只通过 PluginAPI 的 engine_* 与它交互，不再自己 Popen。

进程树：LyLaunch → llama-server(router) → 最多 1 个 child llama-server。
Job Object 绑 router pid 即可收走全部后代；退出用 taskkill /T 兜底。
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from . import registry
from .client import EngineError, ModelNotInstalled, RouterClient
from .runtime import (bind_kill_on_close_job, kill_tree, memory_mb, pid_alive,
                      port_health, port_owner_pid, vram_total_mb)

DEFAULT_PORT = 18090
EXTERNAL_PORT = 18091     # 外部集成（dsh）那套独立的 Router，见 engine/external.py
_LOADED_STATES = ("loaded", "running", "ready")
_FAILED_STATES = ("failed", "error", "error_loading")
# router 派生的 child llama-server 加载失败时会自己退出，但 router 本体还活着，
# /models 里的状态不会变成 loaded —— 旧实现会一直干等到超时。这些日志是唯一的可靠信号。
_LOAD_ERROR_MARKS = (
    "exiting due to model loading error",
    "error loading model",
    "failed to load model",
    "unable to load model",
)


class EngineManager:
    """单例（懒汉式，照抄 PluginManager.instance）。

    同一份实现管两套 Router：插件共用的 ``EngineManager``（18090）和给外部工具
    用的 ``ExternalEngine``（18091，见 engine/external.py）。区别只在构造参数：
    端口、preset 是不是外部那份、日志/pid 文件名、以及同时驻留几个模型。
    """

    _instance: "EngineManager | None" = None

    @classmethod
    def instance(cls) -> "EngineManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, port: int = DEFAULT_PORT, external: bool = False,
                 log_name: str = "engine.log", pid_name: str = "engine.pid") -> None:
        self.port = port
        self.external = external
        self._log_name = log_name
        self._pid_name = pid_name
        self.api_key = ""
        self._proc: subprocess.Popen | None = None
        self._job = None
        self._lock = threading.RLock()
        self.client = RouterClient(self.port)
        self._active: str | None = None
        self._touched: dict[str, float] = {}

    # ---------------------------------------------------------------- 路径
    def _runtime_exe(self) -> Path | None:
        # 1) 随包内置（打包后 sys._MEIPASS/runtime）
        try:
            from ..paths import resource_path
            p = Path(resource_path("runtime"))
            if (p / "llama-server.exe").is_file():
                return p / "llama-server.exe"
        except Exception:
            pass
        # 2) engine/runtime 兜底下载目录
        rt = registry.engine_dir() / "runtime"
        for pat in ("*/llama-server.exe", "llama-server.exe"):
            got = sorted(rt.glob(pat))
            if got:
                return got[0]
        # 3) 旧目录（迁移前复用本机已有那份，避免重复下载）
        root = registry._data_dir() / "models"
        for pat in ("*/runtime/*/llama-server.exe", "*/runtime/llama-server.exe"):
            got = sorted(root.glob(pat))
            if got:
                return got[0]
        return None

    def _log_path(self) -> Path:
        return registry.engine_dir() / self._log_name

    def _pid_path(self) -> Path:
        return registry.engine_dir() / self._pid_name

    def _models_max(self) -> int:
        """外部那套固定只驻留 1 个模型：显存全给 dsh，不做模型间往来切换。"""
        return 1 if self.external else self.effective_models_max()

    def _router_cmd(self, exe: Path) -> list[str]:
        preset = registry.write_preset(external=self.external)
        return [
            str(exe),
            "--models-preset", str(preset),
            "--models-max", str(self._models_max()),
            "--models-memory-margin", "512",
            "--host", "127.0.0.1", "--port", str(self.port),
            "--no-webui", "--timeout", "3600",
        ]

    # ---------------------------------------------------------------- 同时驻留几个模型
    @staticmethod
    def models_max_setting() -> str:
        """用户设置值：auto / 1 / 2。"""
        try:
            from .. import config
            return config.load_models_max()
        except Exception:
            return "auto"

    @staticmethod
    def resolve_models_max(value: str) -> int:
        """把设置值（auto/1/2）解成实际数字。auto 按显存判断，未知一律保守取 1。"""
        if value in ("1", "2"):
            return int(value)
        return 2 if vram_total_mb() >= 8192 else 1

    @staticmethod
    def effective_models_max() -> int:
        """实际传给 --models-max 的数字。"""
        return EngineManager.resolve_models_max(EngineManager.models_max_setting())

    def set_models_max(self, value: str) -> dict:
        """保存设置；若 Router 正在跑则重启使其生效（已加载模型会被卸载）。

        返回 {"changed": bool, "restarted": bool, "effective": int}。
        """
        from .. import config
        before = self.effective_models_max()
        config.save_models_max(value)
        after = self.effective_models_max()
        restarted = False
        if after != before and port_health(self.port):
            self.shutdown()          # 杀整棵树；下次插件用到模型时自动重起
            restarted = True
        return {"changed": after != before, "restarted": restarted, "effective": after}

    # ---------------------------------------------------------------- 生命周期
    def start(self, progress=None) -> None:
        """起 router（幂等）。router 本体不加载任何模型，应 1~2 秒内就绪。"""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and port_health(self.port):
                return
            if port_health(self.port):
                # 端口有人应答：只有 pid 文件指向的活进程才算"自己那套"，可以直接复用；
                # 否则是上一轮遗留的孤儿 —— 复用它等于沿用旧的 preset（改过 ctx 也不生效，
                # 实测就是这么一直卡在 16K 的）。这里把它清掉，接下来按新 preset 重起。
                pid = self._read_pid()
                if pid and pid_alive(pid):
                    return
                self._clear_stray()
            try:
                from .migrate import adopt_legacy
                adopt_legacy()
            except Exception:
                pass
            exe = self._runtime_exe()
            if not exe:
                raise EngineError("找不到 llama-server 运行时（runtime 缺失）")
            self._sweep_legacy_ports()
            self._log_path().parent.mkdir(parents=True, exist_ok=True)
            log = open(self._log_path(), "wb")
            self._proc = subprocess.Popen(
                self._router_cmd(exe), stdout=log, stderr=subprocess.STDOUT,
                creationflags=0x08000000)          # CREATE_NO_WINDOW
            try:
                self._pid_path().write_text(str(self._proc.pid), encoding="utf-8")
            except Exception:
                pass
            self._job = bind_kill_on_close_job(self._proc.pid)

            t0 = time.time()
            while time.time() - t0 < 60:
                if self._proc.poll() is not None:
                    raise EngineError("Router 启动即退出：" + self._log_tail())
                if port_health(self.port):
                    return
                if progress:
                    try:
                        progress("正在启动本地推理服务…", None)
                    except Exception:
                        pass
                time.sleep(0.3)
            raise EngineError("Router 启动超时（60 秒）：" + self._log_tail())

    def ensure_model(self, mid: str, progress=None) -> None:
        """确保 mid 成为当前唯一活跃模型；必要时先卸载旧模型。全程串行。"""
        spec = registry.spec_of(mid)
        self._check_external_exclusive()
        with self._lock:
            # 先查文件再让 router 干活：preset 里 9 个段是「预声明」的，
            # 新装机器一个模型都没下载时，router 会去加载不存在的路径并让子进程崩掉。
            problem = registry.model_files_problem(spec)
            if problem:
                # 用专门异常：插件据此只显示「未下载模型」，不弹错误框、不甩路径
                raise ModelNotInstalled(
                    f"模型 {spec.display} 不可用：{problem}。\n"
                    f"请先在对应插件的「模型」面板里下载该模型，再重试。")
            self.start(progress)
            # preset 是启动时按「当时已下载」写死的；刚下载完的模型不在里面，
            # 直接 load 会报 model not found。重写 preset + 重启 router 让它认识这个 id。
            if self._unknown_to_router(mid):
                self._safe(progress, f"正在注册新模型 {spec.display}…", None)
                self.shutdown()
                self.start(progress)
            if self.client.status_of(mid) in _LOADED_STATES:
                self._active = mid
                self._touched[mid] = time.monotonic()
                return
            # 不主动卸载旧模型：交给 Router 的 --models-max=2 + LRU，
            # 让「图片转文字」和「文档识别」用不同小模型时可以同时驻留。
            if progress:
                self._safe(progress, f"正在加载 {spec.display}…（首次约 5~20 秒）", None)
            self.client.load_model(mid)
            if not self._wait_loaded(mid, 240, progress):
                raise EngineError(f"模型 {spec.display} 加载超时（240 秒未就绪）："
                                  + self._log_tail(8))
            self._active = mid
            self._touched[mid] = time.monotonic()

    def unload(self, mid: str | None = None, if_model: str | None = None) -> None:
        """卸载模型（不杀 router）。

        **按模型粒度**：--models-max=2 时可能同时驻留两个模型，
        所以不能用 loaded_id()（只返回第一个）来判断该卸谁，否则会出现
        "想卸 B、结果发现第一个是 A、于是什么都不做" 的静默失败。

        - 指定 mid / if_model → 只卸它（处于加载态才动手，否则无操作）
        - 都不指定 → 卸掉当前所有已加载模型
        """
        target = if_model or mid
        with self._lock:
            if target:
                if self.client.status_of(target) in _LOADED_STATES:
                    self.client.unload_model(target)
                    self._wait_unloaded(target, 25)
                    if self._active == target:
                        self._active = None
                return
            loaded = self.client.loaded_ids()
            for one in loaded:
                self.client.unload_model(one)
                self._wait_unloaded(one, 25)
                if self._active == one:
                    self._active = None

    def loaded_ids(self) -> list[str]:
        """当前所有已加载模型。"""
        return self.client.loaded_ids()

    def active(self) -> str | None:
        return self.client.loaded_id() or self._active

    def status(self) -> dict:
        running = port_health(self.port)
        pid = self._proc.pid if self._proc is not None and self._proc.poll() is None else 0
        return {
            "running": running,
            "active": self.client.loaded_id() if running else None,
            "port": self.port,
            "memory_mb": memory_mb(pid) if pid else 0,
        }

    def touch(self, mid: str, idle_seconds: int = 300) -> None:
        """记录一次使用时间；实际空闲释放由上层 QTimer 调度（P6 接）。"""
        self._touched[mid] = time.monotonic()

    def release_idle(self, idle_seconds: int = 300) -> None:
        """到点卸载长时间未用的模型（逐个判断，不依赖"只有一个活跃模型"）。"""
        for one in self.client.loaded_ids():
            last = self._touched.get(one, 0.0)
            if last and (time.monotonic() - last) >= max(30, idle_seconds):
                self.unload(if_model=one)

    def shutdown(self) -> None:
        """退出收口：/T 杀整棵树（router + 所有 child），避免孤儿进程。

        ``self._proc`` 为空但 pid 文件里有活进程时也要杀 —— 那种情况是「复用了上一轮
        常驻的 Router」，只清引用不杀进程会留下一只永远占着端口的老实例（正是
        "改了 ctx 重启程序还是 16K" 的根因）。
        """
        with self._lock:
            proc = self._proc
            pid = proc.pid if proc is not None and proc.poll() is None else self._read_pid()
            self._proc = None
            self._job = None
            self._active = None
            try:
                self._pid_path().unlink()
            except Exception:
                pass
            if pid and pid_alive(pid):
                kill_tree(pid)
                if proc is not None:
                    try:
                        proc.wait(timeout=8)
                    except Exception:
                        pass

    def running(self, timeout: float = 0.6) -> bool:
        """端口上有没有 Router 在应答（不管是不是本进程拉起的）。

        ``timeout`` 是本次健康探测的上限：UI 里同步问一次要按毫秒级给
        （本机回环上"端口没监听"不会立刻 RST，而是耗满超时，默认 0.6s
        就足够让设置页卡半秒），后台线程可以放宽。
        """
        return port_health(self.port, timeout=timeout)

    def pid(self) -> int:
        """当前 Router 的 pid：优先自己持有的 Popen，其次 pid 文件；都没有返回 0。"""
        if self._proc is not None and self._proc.poll() is None:
            return self._proc.pid
        pid = self._read_pid()
        return pid if pid and pid_alive(pid) else 0

    def loaded_ctx(self) -> int:
        """已加载模型的实际 n_ctx（用来证明"改动真的生效了"）；读不到返回 0。"""
        try:
            for item in self.client.models(timeout=1.0):
                if RouterClient._status_of(item) in _LOADED_STATES:
                    meta = item.get("meta") or {}
                    if isinstance(meta, dict) and meta.get("n_ctx"):
                        return int(meta["n_ctx"])
        except Exception:
            return 0
        return 0

    # ---------------------------------------------------------------- 内部
    def _read_pid(self) -> int:
        try:
            return int(self._pid_path().read_text(encoding="utf-8").strip())
        except Exception:
            return 0

    def _clear_stray(self) -> None:
        """清掉占着自己端口、却无法由 pid 文件认领的残留 Router（只认这个端口）。"""
        stray = port_owner_pid(self.port)
        if not stray:
            return
        kill_tree(stray)
        t0 = time.time()
        while time.time() - t0 < 8:
            if not port_health(self.port, timeout=0.5):
                return
            time.sleep(0.3)

    def _check_external_exclusive(self) -> None:
        """外部引擎开启时，插件这套不许再加载模型（显存要整块留给 dsh）。"""
        if self.external:
            return
        try:
            from .. import config
            if not config.load_ext_engine():
                return
            from .external import ExternalEngine
            if not ExternalEngine.instance().running():
                return
        except Exception:
            return
        raise EngineError(
            "外部集成正在独占本地模型（设置 → 外部集成）。\n"
            "要先用翻译 / 文档识别 / 本地 AI，请在那里关掉「启用外部引擎」。")

    @staticmethod
    def _safe(cb, *args):
        try:
            cb(*args)
        except Exception:
            pass

    def _wait_loaded(self, mid: str, timeout: int, progress=None) -> bool:
        spec = registry.spec_of(mid)
        # 日志过滤关键字：文件名 / mmproj / 模型 id 任一命中才算「本模型」的日志
        keys = tuple(k for k in (spec.file, spec.mmproj, mid) if k)
        t0 = time.time()
        last_report = 0.0
        last_log_check = 0.0
        while time.time() - t0 < timeout:
            st = self.client.status_of(mid)
            if st in _LOADED_STATES:
                return True
            if st in _FAILED_STATES:
                raise EngineError(f"模型 {spec.display} 加载失败："
                                  + self._log_tail(8, only=keys))
            now = time.time()
            # 每 2 秒扫一次日志尾巴：child 进程崩了但 router 还活着时，
            # 立刻把真实原因抛出去，别让用户对着「加载超时」干等 4 分钟。
            #
            # ⚠️ 必须按模型过滤（0.39.5 修）：router 启动时会为 preset 里**每个**
            # 模型测显存，没下载的模型必然刷 "error loading model"（如 qwen3-4b /
            # xiaomi-ocr-0）。不过滤就会把「别的模型缺文件」当成「当前模型加载失败」
            # —— 实测加载 Qwen3-VL-8B 成功，却报 qwen3-4b 的错。
            if now - last_log_check >= 2:
                last_log_check = now
                tail = self._log_tail(8, only=keys)
                if tail and any(k in tail.lower() for k in _LOAD_ERROR_MARKS):
                    raise EngineError(
                        f"模型 {spec.display} 加载失败"
                        f"（本地推理进程已退出）：" + tail)
            if progress and now - last_report >= 5:
                last_report = now
                self._safe(progress, "正在加载模型…", None)
            time.sleep(1.0)
        return False

    def _wait_unloaded(self, mid: str, timeout: int) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.client.status_of(mid) not in _LOADED_STATES:
                return True
            time.sleep(0.5)
        return False

    def _log_tail(self, n: int = 5, only=None) -> str:
        """只读末尾 64KB：engine.log 会随每次模型加载持续增长，
        而加载等待期间每 2 秒就要扫一次，不能每次全量 read_text。
        每行再截到 240 字符，避免日志里偶发的超长行把错误弹窗整个塞满。

        ``only``：只保留包含任一关键字的行（str 或序列）。用于把「本模型的
        日志」从 router 的 measure 噪音里摘出来 —— 见 ``_wait_loaded``。
        """
        try:
            with self._log_path().open("rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 65536))
                data = f.read().decode("utf-8", "replace")
            lines = [x.strip() for x in data.splitlines() if x.strip()]
            if only:
                keys = [only] if isinstance(only, str) else list(only)
                low = [str(k).lower() for k in keys if k]
                if low:
                    lines = [x for x in lines if any(k in x.lower() for k in low)]
            return " / ".join(x[:240] for x in lines[-n:])
        except Exception:
            return ""

    def _unknown_to_router(self, mid: str) -> bool:
        """router 的 /models 里没有这个 id？

        有内容且确实不含它 → 模型是「下载后新增」的，需要重写 preset 并重启。
        /models 取不到或返回空时一律返回 False（别为了一个偶发超时去杀 router）。
        """
        try:
            items = self.client.models()
        except Exception:
            return False
        if not items:
            return False
        for it in items:
            got = str(it.get("id") or it.get("model") or it.get("name") or "")
            if got == mid:
                return False
        return True

    def _sweep_legacy_ports(self) -> None:
        """清掉升级前旧引擎残留的 18081/18082/18083 进程（按 pid 文件 + 存活判断，绝不盲杀）。"""
        legacy = [
            (18081, registry._data_dir() / "doc_core" / "engine.pid"),
            (18082, registry._data_dir() / "llm" / "engine.pid"),
            (18082, registry._data_dir() / "llm_chat" / "engine.pid"),
            (18083, registry._data_dir() / "llm" / "engine.pid"),
        ]
        for port, pid_file in legacy:
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
            except Exception:
                continue
            if pid_alive(pid):
                try:
                    kill_tree(pid)
                except Exception:
                    pass
