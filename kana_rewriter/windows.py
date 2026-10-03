"""Small Win32 adapter; no keyboard hook, admin rights or third-party packages."""
import ctypes as C
from ctypes import wintypes as W
import time

from .core import Capture
from .text import split_at_caret
from .clipboard import Clipboard, pump_sent_messages

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
user.GetClipboardSequenceNumber.restype = W.DWORD
user.RegisterHotKey.argtypes = [W.HWND, C.c_int, W.UINT, W.UINT]
user.UnregisterHotKey.argtypes = [W.HWND, C.c_int]
user.PeekMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT]


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


class Desktop:
    def __init__(self):
        self.clipboard = None

    def close(self):
        if self.clipboard is not None:
            self.clipboard.close()
            self.clipboard = None

    def stamp(self):
        info = LastInput(C.sizeof(LastInput), 0)
        if not user.GetLastInputInfo(C.byref(info)):
            raise C.WinError(C.get_last_error())
        return focus(), info.tick, user.GetClipboardSequenceNumber()

    def copy_selection(self):
        target = focus()
        if self.clipboard is None:
            self.clipboard = Clipboard()
        saved = self.clipboard.snapshot()
        owned = None
        try:
            if focus() != target:
                raise RuntimeError("コピー前に入力先が変わりました")
            chord(0x11, 0x43)  # Ctrl+C; never clear the clipboard as a sentinel.
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                if focus() != target:
                    raise RuntimeError("コピー中に入力先が変わりました")
                pump_sent_messages()
                changed = self.clipboard.read_changed_text(saved.sequence)
                if changed is not None:
                    text, owned = changed
                    return text
                time.sleep(0.01)
            raise RuntimeError("選択文字をコピーできません。編集可能な文字を選択してください")
        finally:
            try:
                if owned is not None:
                    self.clipboard.restore(saved, owned)
            finally:
                saved.release()

    def capture(self, mode, max_chars):
        # Wait for the trigger's modifier keys to be released.
        target = focus()
        deadline = time.monotonic() + 2
        while any(user.GetAsyncKeyState(k) & 0x8000 for k in (0x10, 0x11, 0x12, 0x4B, 0x4A)):
            if time.monotonic() > deadline:
                raise RuntimeError("ショートカットキーを離してください")
            time.sleep(0.01)
        if focus() != target:
            raise RuntimeError("入力先が変わりました")
        if mode == "line":
            chord(0x10, 0x24)  # Shift+Home: app-defined visible line start.
            time.sleep(0.05)
            before = self.copy_selection()
            # Collapse to the start of the copied prefix, then select the whole
            # visible line. This obtains right context even when it is empty.
            chord(0x25)  # Left collapses the selection to its beginning.
            chord(0x10, 0x23)  # Shift+End
            time.sleep(0.05)
            text = self.copy_selection()
            if not before or not text.startswith(before):
                raise RuntimeError("行とカーソル位置を対応づけられません。選択範囲モードを使用してください")
            region = split_at_caret(text, len(before))
        else:
            text = self.copy_selection()
            region = None
        source = region.target if region is not None else text
        if not source.strip() or len(source) > max_chars:
            raise ValueError("対象が空、または文字数上限を超えています")
        if len(text) > 10000:
            raise ValueError("取得した行が長すぎます。選択範囲モードを使用してください")
        return Capture(text, self.stamp(), region)

    def replace(self, text, target):
        if focus() != target:
            raise RuntimeError("入力先が変わりました")
        if any(user.GetAsyncKeyState(k) & 0x8000 for k in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
            raise RuntimeError("修飾キーが押されているため中止しました")
        data = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-16-le")
        events = []
        for i in range(0, len(data), 2):
            unit = int.from_bytes(data[i:i + 2], "little")
            if unit == 10:
                unit = 13  # Text controls expect carriage returns for line breaks.
            # Unicode input replaces the selection without touching the clipboard.
            events.extend([KeyboardInput(0, unit, 4, 0, 0), KeyboardInput(0, unit, 6, 0, 0)])
        send(events)
