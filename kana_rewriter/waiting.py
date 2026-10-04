"""Thread-local high-resolution waits without changing global timer resolution."""
import ctypes as C
from ctypes import wintypes as W
import math
import threading
import time

kernel = C.WinDLL("kernel32", use_last_error=True)
kernel.CreateWaitableTimerExW.argtypes = [C.c_void_p, W.LPCWSTR, W.DWORD, W.DWORD]
kernel.CreateWaitableTimerExW.restype = W.HANDLE
kernel.SetWaitableTimer.argtypes = [W.HANDLE, C.POINTER(C.c_longlong), W.LONG,
                                   C.c_void_p, C.c_void_p, W.BOOL]
kernel.SetWaitableTimer.restype = W.BOOL
kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
kernel.WaitForSingleObject.restype = W.DWORD
kernel.CloseHandle.argtypes = [W.HANDLE]
kernel.CloseHandle.restype = W.BOOL
local = threading.local()


def sleep(seconds):
    if seconds <= 0:
        time.sleep(0)
        return
    if not hasattr(local, "timer"):
        local.timer = kernel.CreateWaitableTimerExW(None, None, 2, 0x100002)
    if local.timer:
        due = C.c_longlong(-math.ceil(seconds * 10_000_000))
        if kernel.SetWaitableTimer(local.timer, C.byref(due), 0, None, None, False):
            if kernel.WaitForSingleObject(local.timer, 0xFFFFFFFF) == 0:
                return
    time.sleep(seconds)


def close_timer():
    timer = getattr(local, "timer", None)
    if timer:
        kernel.CloseHandle(timer)
    if hasattr(local, "timer"):
        del local.timer
