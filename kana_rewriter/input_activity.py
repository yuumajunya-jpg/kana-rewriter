"""Count editing input without counting pointer motion or key releases.

Hooks only count event kinds; they never inspect, store, or suppress key data.
The message loop runs separately from blocking UIA/Win32 calls.
"""
import ctypes as C
from ctypes import wintypes as W
import threading

from .winapi import user


HOOKPROC = C.WINFUNCTYPE(W.LPARAM, C.c_int, W.WPARAM, W.LPARAM)
user.SetWindowsHookExW.argtypes = [C.c_int, HOOKPROC, W.HINSTANCE, W.DWORD]
user.SetWindowsHookExW.restype = W.HANDLE
user.CallNextHookEx.argtypes = [W.HANDLE, C.c_int, W.WPARAM, W.LPARAM]
user.CallNextHookEx.restype = W.LPARAM
user.UnhookWindowsHookEx.argtypes = [W.HANDLE]
user.GetMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT]
user.GetMessageW.restype = C.c_int
user.PostThreadMessageW.argtypes = [W.DWORD, W.UINT, W.WPARAM, W.LPARAM]
kernel = C.WinDLL("kernel32", use_last_error=True)
kernel.GetCurrentThreadId.restype = W.DWORD
kernel.GetModuleHandleW.argtypes = [W.LPCWSTR]
kernel.GetModuleHandleW.restype = W.HMODULE

# Key down (including repeats/system keys), mouse buttons and wheels.
# WM_MOUSEMOVE and key/button releases alone cannot start an edit.
KEY_MESSAGES = frozenset((0x0100, 0x0104))
MOUSE_MESSAGES = frozenset((0x0201, 0x0204, 0x0207, 0x020B, 0x020A, 0x020E))


class InputActivity:
    hook_kinds = (13, 14)

    def __init__(self):
        self.counter = 0
        self.error = None
        self.thread_id = None
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="input-activity", daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(2):
            self.close()
            raise RuntimeError("入力監視の起動がタイムアウトしました")
        self.snapshot()
        return self

    def snapshot(self):
        if self.error is not None or not self.thread.is_alive():
            raise RuntimeError("入力監視が停止しています。プログラムを再起動してください") from self.error
        return self.counter

    def _keyboard(self, code, message, data):
        if code == 0 and message in KEY_MESSAGES:
            self.counter += 1
        return user.CallNextHookEx(None, code, message, data)

    def _mouse(self, code, message, data):
        if code == 0 and message in MOUSE_MESSAGES:
            self.counter += 1
        return user.CallNextHookEx(None, code, message, data)

    def _run(self):
        hooks = []
        # Retain callbacks until both hooks have been removed.
        callbacks = (HOOKPROC(self._keyboard), HOOKPROC(self._mouse))
        try:
            message = W.MSG()
            user.PeekMessageW(C.byref(message), None, 0, 0, 0)  # create queue
            self.thread_id = kernel.GetCurrentThreadId()
            module = kernel.GetModuleHandleW(None)
            for kind, callback in zip(self.hook_kinds, callbacks):
                hook = user.SetWindowsHookExW(kind, callback, module, 0)
                if not hook:
                    raise C.WinError(C.get_last_error())
                hooks.append(hook)
            self.ready.set()
            while not self.stopping.is_set():
                result = user.GetMessageW(C.byref(message), None, 0, 0)
                if result == -1:
                    raise C.WinError(C.get_last_error())
                if result == 0:
                    break
        except Exception as exc:
            self.error = exc
        finally:
            for hook in reversed(hooks):
                user.UnhookWindowsHookEx(hook)
            self.ready.set()

    def close(self):
        self.stopping.set()
        if self.thread_id is not None and self.thread.is_alive():
            user.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)  # WM_QUIT
        if self.thread.ident is not None:
            self.thread.join(timeout=2)


_monitor = None


def input_tick():
    """An event sequence number, not the OS's last-input timestamp."""
    global _monitor
    if _monitor is None:
        _monitor = InputActivity().start()
    return _monitor.snapshot()


def close_monitor():
    global _monitor
    if _monitor is not None:
        _monitor.close()
        _monitor = None
