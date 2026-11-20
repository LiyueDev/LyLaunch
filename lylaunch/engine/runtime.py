"""全局引擎的 Windows 底层工具：Job Object、pid、内存、端口健康、本地 HTTP 开销。

从 lylaunch/doc_core/doc_engine.py 抽取（原三份重复实现的合并）。
只做 OS 层操作，不涉及业务逻辑。
"""
from __future__ import annotations

import ctypes
import subprocess
import urllib.request
from ctypes import wintypes

# 绕过可能劫持 127.0.0.1 的系统代理（公司网络 / 抓包工具会把本地请求转走）
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def local_open(req, timeout: float = 10):
    return _LOCAL_OPENER.open(req, timeout=timeout)


# ---------------------------------------------------------------- Job Object
class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOB_BASIC_LIMIT(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOB_EXT_LIMIT(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOB_BASIC_LIMIT),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    """GetProcessMemoryInfo 用的结构（读进程占用，界面提示"释放能省多少"）。"""
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def bind_kill_on_close_job(pid: int):
    """把进程绑进 KILL_ON_JOB_CLOSE 的 Job（返回句柄，必须持有到进程结束）。

    ⚠️ Windows Job 的成员默认被后代继承，所以只需把 **router 父进程** 绑进来，
    它 spawn 的每个模型 child 会被一起收走（除非 child 显式 CREATE_BREAKAWAY_FROM_JOB）。
    """
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOB_EXT_LIMIT()
        info.BasicLimitInformation.LimitFlags = 0x00002000   # KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        hproc = k32.OpenProcess(0x0100 | 0x0001, False, pid)   # SET_QUOTA | TERMINATE
        if not hproc:
            return None
        ok = k32.AssignProcessToJobObject(job, hproc)
        k32.CloseHandle(hproc)
        return job if ok else None
    except Exception:
        return None


# ---------------------------------------------------------------- pid / kill
def pid_alive(pid: int) -> bool:
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        h = k32.OpenProcess(0x1000, False, pid)     # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = wintypes.DWORD()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return code.value == 259                     # STILL_ACTIVE
    except Exception:
        return False


def kill_pid(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, creationflags=0x08000000, timeout=15)
    except Exception:
        pass


def kill_tree(pid: int) -> None:
    """杀整个进程树。router 模式下只 terminate 父进程会让模型 child 变成孤儿，
    必须 /T 把子进程一起收掉。"""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, creationflags=0x08000000, timeout=20)
    except Exception:
        pass


# ---------------------------------------------------------------- memory
def memory_mb(pid: int) -> int:
    """进程当前工作集（MB）；读不到返回 0。"""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        h = k32.OpenProcess(0x1000, False, pid)      # QUERY_LIMITED_INFORMATION
        if not h:
            return 0
        psapi = ctypes.WinDLL("psapi")
        info = _PROCESS_MEMORY_COUNTERS_EX()
        info.cb = ctypes.sizeof(info)
        ok = psapi.GetProcessMemoryInfo(h, ctypes.byref(info), info.cb)
        k32.CloseHandle(h)
        return round(info.WorkingSetSize / 1048576) if ok else 0
    except Exception:
        return 0


# ---------------------------------------------------------------- port
def port_health(port: int, timeout: float = 2) -> bool:
    """llama-server 的 /health 200 判定（router 与 child 共用同一套探针）。"""
    try:
        with local_open(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------- 显存
_VRAM_CACHE: int | None = None


def vram_total_mb() -> int:
    """独显总显存（MB）。

    只有 NVIDIA 能可靠读取（nvidia-smi：System32 下自带，无需装驱动工具）。
    核显 / AMD / 读不到时返回 0 —— 调用方按「未知 = 保守」处理（少驻留一个模型）。
    首调约 100~300ms，之后走缓存。
    """
    global _VRAM_CACHE
    if _VRAM_CACHE is not None:
        return _VRAM_CACHE
    mb = 0
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, timeout=6, creationflags=0x08000000)
        if out.returncode == 0:
            toks = out.stdout.decode("utf-8", "ignore").replace(",", " ").split()
            vals = [int(t) for t in toks if t.isdigit() and int(t) > 0]
            if vals:
                mb = max(vals)
    except Exception:
        mb = 0
    _VRAM_CACHE = mb
    return mb
