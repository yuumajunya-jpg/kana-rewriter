"""Small Win32 adapter; no keyboard hook, admin rights or third-party packages."""
import ctypes as C
from ctypes import wintypes as W


user = C.WinDLL("user32", use_last_error=True)


class KeyboardInput(C.Structure):
    _fields_ = [("vk", W.WORD), ("scan", W.WORD), ("flags", W.DWORD),
                ("time", W.DWORD), ("extra", C.c_size_t)]


class MouseInput(C.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("data", W.DWORD),
                ("flags", W.DWORD), ("time", W.DWORD), ("extra", C.c_size_t)]


class InputUnion(C.Union):
    _fields_ = [("keyboard", KeyboardInput), ("mouse", MouseInput)]


class Input(C.Structure):
    _fields_ = [("type", W.DWORD), ("value", InputUnion)]


class LastInput(C.Structure):
    _fields_ = [("size", W.UINT), ("tick", W.DWORD)]


class GuiInfo(C.Structure):
    _fields_ = [("size", W.DWORD), ("flags", W.DWORD),
                ("active", W.HWND), ("focus", W.HWND), ("capture", W.HWND),
                ("menu", W.HWND), ("move", W.HWND), ("caret", W.HWND),
                ("rect", W.RECT)]


user.GetForegroundWindow.restype = W.HWND
user.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
user.GetGUIThreadInfo.argtypes = [W.DWORD, C.POINTER(GuiInfo)]
user.GetLastInputInfo.argtypes = [C.POINTER(LastInput)]
user.GetAsyncKeyState.argtypes = [C.c_int]
user.GetAsyncKeyState.restype = C.c_short
user.SendInput.argtypes = [W.UINT, C.POINTER(Input), C.c_int]
user.RegisterHotKey.argtypes = [W.HWND, C.c_int, W.UINT, W.UINT]
user.UnregisterHotKey.argtypes = [W.HWND, C.c_int]
user.PeekMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT]
user.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
user.SendMessageTimeoutW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM,
                                    W.UINT, W.UINT, C.POINTER(C.c_size_t)]
user.SendMessageTimeoutW.restype = W.LPARAM


def send(events):
    inputs = (Input * len(events))(*(Input(1, InputUnion(keyboard=e)) for e in events))
    if user.SendInput(len(inputs), inputs, C.sizeof(Input)) != len(inputs):
        raise RuntimeError("キー送信に失敗しました。対象アプリの権限を確認してください")


def chord(*keys):
    send([KeyboardInput(k, 0, 0, 0, 0) for k in keys]
         + [KeyboardInput(k, 0, 2, 0, 0) for k in reversed(keys)])


def focus():
    hwnd = user.GetForegroundWindow()
    thread = user.GetWindowThreadProcessId(hwnd, None)
    info = GuiInfo(size=C.sizeof(GuiInfo))
    if not hwnd or not user.GetGUIThreadInfo(thread, C.byref(info)):
        raise RuntimeError("入力先を取得できません")
    return hwnd, info.focus, info.caret, tuple(getattr(info.rect, k) for k in
                                            ("left", "top", "right", "bottom"))


