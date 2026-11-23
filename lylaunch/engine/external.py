"""外部引擎：给 dsh 这类外部工具单独一套 Router（端口 18091）。

为什么单独一套（而不是和插件共用 18090）：
1. preset 是 Router **启动时**读进内存的，改 ctx-size 必须重启 Router；共用一个进程
   就意味着"改外部集成的上下文要打断插件正在用的模型"。
2. 外部集成要的是 32K~64K 的长上下文，插件（翻译 / OCR / 本地 AI）要的是短请求
   加省显存 —— 一个 preset 同时满足不了两边。

互斥规则：打开外部引擎时，先把插件那套已加载的模型卸掉（显存整块让给 dsh），
并让插件侧的 EngineManager 在开启期间拒绝加载（见 _check_external_exclusive）。
关闭时收掉本进程，插件恢复按需加载。
"""
from __future__ import annotations

import time

from .. import config
from . import registry
from .manager import EXTERNAL_PORT, EngineManager

__all__ = ["EXTERNAL_PORT", "ExternalEngine"]


class ExternalEngine(EngineManager):
    """外部集成专用 Router：独立端口 / preset / 日志 / pid，默认关闭。"""

    _instance: "ExternalEngine | None" = None

    def __init__(self) -> None:
        super().__init__(port=EXTERNAL_PORT, external=True,
                         log_name="engine-ext.log", pid_name="engine-ext.pid")

    # ---------------------------------------------------------------- 开关
    def enable(self, progress=None) -> bool:
        """打开：先腾显存（卸掉插件那套的模型），再起自己的 Router；成功才写盘。"""
        try:
            EngineManager.instance().unload()
        except Exception:
            pass          # 插件那套没在跑 / 端口不通都无所谓，重点是别让它占着显存
        self.start(progress)
        config.save_ext_engine(True)
        return self.running()

    def disable(self) -> None:
        """关掉：收掉本进程，插件恢复按需加载。"""
        self.shutdown()
        config.save_ext_engine(False)

    def restart(self, progress=None) -> bool:
        """重启（改完 preset 后让它按新值加载）。"""
        self.shutdown()
        self.start(progress)
        return self.running()

    # ---------------------------------------------------------------- 上下文
    def set_context(self, value: int) -> dict:
        """改外部引擎的上下文窗口：写设置 + 重写外部 preset；在跑就重启本进程。

        返回 {"changed": bool, "restarted": bool, "effective": int}。
        只动 18091 这一套，插件那套（18090）不受影响 —— 这正是拆两套的目的。
        """
        before = config.load_engine_context()
        config.save_engine_context(value)
        after = config.load_engine_context()
        restarted = False
        if after != before:
            registry.write_preset(external=True)
            if self.running():
                self.restart()
                restarted = True
        return {"changed": after != before, "restarted": restarted, "effective": after}

    # ---------------------------------------------------------------- 状态
    def state(self) -> dict:
        """设置页展示用：在线 / pid / 已加载模型的实际 n_ctx / 是否与设置一致。"""
        running = self.running()
        return {
            "running": running,
            "pid": self.pid(),
            "port": self.port,
            "n_ctx": self.loaded_ctx() if running else 0,
            "want_ctx": config.load_engine_context(),
            "enabled": config.load_ext_engine(),
        }

    def wait_ready(self, timeout: float = 70) -> bool:
        """等端口应答（enable/restart 走后台线程时，界面用它收尾）。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.running():
                return True
            time.sleep(0.3)
        return False
