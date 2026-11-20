"""单实例保护（对应 C# 的 Mutex + EventWaitHandle 双机制）。

① 命名 Mutex：保证全系统只有一个 LyLaunch 在跑。
② 命名内核事件 ShowSignal：第二个实例只 Set() 一下，首个实例的后台线程 WaitOne 收到后唤出窗口。
   比"广播消息"可靠（WPF/Qt 窗口常收不到广播，导致点快捷方式唤不出面板）。
"""

from __future__ import annotations

import ctypes
import threading
from ctypes.wintypes import BOOL, DWORD, HANDLE, LPCWSTR

kernel32 = ctypes.windll.kernel32

MUTEX_NAME = "Local\\LyLaunch_SingleInstance_8c5a1f2e-2b3d-4e6f-9a1c-7d4e2f8b6c10"
SHOW_EVENT_NAME = "Local\\LyLaunch_ShowSignal_8c5a1f2e-2b3d-4e6f-9a1c-7d4e2f8b6c10"

kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, BOOL, LPCWSTR]
kernel32.CreateMutexW.restype = HANDLE
kernel32.OpenMutexW.argtypes = [DWORD, BOOL, LPCWSTR]
kernel32.OpenMutexW.restype = HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, BOOL, BOOL, LPCWSTR]
kernel32.CreateEventW.restype = HANDLE
kernel32.OpenEventW.argtypes = [DWORD, BOOL, LPCWSTR]
kernel32.OpenEventW.restype = HANDLE
kernel32.SetEvent.argtypes = [HANDLE]
kernel32.SetEvent.restype = BOOL
kernel32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
kernel32.WaitForSingleObject.restype = DWORD
kernel32.CloseHandle.argtypes = [HANDLE]
kernel32.CloseHandle.restype = BOOL
kernel32.GetLastError.restype = DWORD

ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0


def is_first_instance() -> bool:
    """首个实例返回 True；已有实例在跑则返回 False。"""
    h = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def signal_existing_instance() -> None:
    """第二个实例调用：通知已有实例把窗口唤出来。"""
    h = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, SHOW_EVENT_NAME)
    if not h:
        h = kernel32.CreateEventW(None, False, False, SHOW_EVENT_NAME)
    if h:
        kernel32.SetEvent(h)
        kernel32.CloseHandle(h)


def listen_for_show(callback) -> None:
    """首个实例调用：后台线程等待"显示信号"，收到就回调（主线程里唤出窗口）。"""
    evt = kernel32.CreateEventW(None, False, False, SHOW_EVENT_NAME)
    if not evt:
        return

    def loop() -> None:
        while True:
            try:
                ret = kernel32.WaitForSingleObject(evt, INFINITE)
                if ret == WAIT_OBJECT_0:
                    callback()
            except Exception:
                break

    t = threading.Thread(target=loop, daemon=True)
    t.start()
