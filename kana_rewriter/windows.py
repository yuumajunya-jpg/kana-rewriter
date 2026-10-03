"""Small Win32 adapter; no keyboard hook, admin rights or third-party packages."""
import ctypes as C
from ctypes import wintypes as W
import time
import logging

from .core import Capture
from .text import split_at_caret
from .clipboard import Clipboard, ClipboardWriteError, pump_sent_messages

user = C.WinDLL("user32", use_last_error=True)
logger = logging.getLogger(__name__)


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


class Desktop:
    def __init__(self, paste_wait_seconds=0.5):
        self.clipboard = None
        self.paste_wait_seconds = paste_wait_seconds

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
        logger.debug("取得文字列=%r / 対象=%r / 左文脈=%r / 右文脈=%r",
                     text, source, region.left if region else "", region.right if region else "")
        return Capture(text, self.stamp(), region, len(before) if region is not None else None)

    def replace(self, text, target, caret=None):
        if caret is not None and (not 0 <= caret <= len(text) or "\n" in text or "\r" in text):
            raise ValueError("カーソル復元は単一の表示行に限ります")
        if focus() != target:
            raise RuntimeError("入力先が変わりました")
        if any(user.GetAsyncKeyState(k) & 0x8000 for k in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
            raise RuntimeError("修飾キーが押されているため中止しました")
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
        if self.clipboard is None:
            self.clipboard = Clipboard()
        saved = self.clipboard.snapshot()
        owned = None
        timed_out = False
        try:
            try:
                owned = self.clipboard.write_text(text, saved.sequence)
            except ClipboardWriteError as exc:
                owned = exc.sequence
                raise
            if focus() != target:
                raise RuntimeError("貼り付け準備中に入力先が変わりました")
            if any(user.GetAsyncKeyState(k) & 0x8000 for k in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
                raise RuntimeError("貼り付け準備中に修飾キーが押されたため中止しました")
            current = user.GetClipboardSequenceNumber()
            if current != owned:
                logger.debug("クリップボード番号変化=%s→%s / 所有者と文字列を再確認", owned, current)
                owned = self.clipboard.confirm_text(text)
            # The completed string is already on the clipboard. Never send its
            # characters as VK_PACKET events: the editor/IME may process those
            # individually and change its caret/composition state between them.
            name = C.create_unicode_buffer(256)
            if target[1]:
                user.GetClassNameW(target[1], name, len(name))
            class_name = name.value.lower()
            if class_name == "edit" or class_name.startswith("richedit"):
                logger.debug("貼り付け方法=同期WM_PASTE / 入力欄=%s", name.value)
                result = C.c_size_t()
                selection_start = W.DWORD()
                selection_end = W.DWORD()
                if caret is not None:
                    if not user.SendMessageTimeoutW(target[1], 0x00B0,
                            C.addressof(selection_start), C.addressof(selection_end),
                            0x23, 2000, C.byref(result)):
                        raise RuntimeError("カーソル復元用の選択位置を取得できませんでした")
                # A standard text control finishes WM_PASTE before returning,
                # so restoring the clipboard cannot race its paste request.
                if not user.SendMessageTimeoutW(target[1], 0x0302, 0, 0, 0x23, 2000, C.byref(result)):
                    # A timed-out target may still process the message later.
                    # Keep the intended payload, never retry a potentially
                    # completed edit or replace it with the old clipboard data.
                    timed_out = True
                    raise RuntimeError("入力欄の貼り付け応答を確認できませんでした。結果はクリップボードに保持しています")
                if caret is not None:
                    position = selection_start.value + len(text[:caret].encode("utf-16-le")) // 2
                    if not user.SendMessageTimeoutW(target[1], 0x00B1, position, position,
                                                    0x23, 2000, C.byref(result)):
                        raise RuntimeError("貼り付け後のカーソル位置を復元できませんでした")
            else:
                logger.debug("貼り付け方法=Ctrl+V / 復元待機=%.2f秒 / 入力欄=%s",
                             self.paste_wait_seconds, name.value or "不明")
                chord(0x11, 0x56)
                # Custom/browser controls don't expose a synchronous paste API.
                # Leave the payload available while their queued paste runs.
                deadline = time.monotonic() + self.paste_wait_seconds
                while time.monotonic() < deadline:
                    pump_sent_messages()
                    time.sleep(0.01)
                if caret is not None:
                    # End anchors the caret even if a custom editor retained
                    # the start of the selection after pasting. Visible-line
                    # navigation is the same contract as capture's Home/End.
                    if focus()[:2] != target[:2]:
                        raise RuntimeError("貼り付け後に入力先が変わったためカーソル復元を中止しました")
                    if any(user.GetAsyncKeyState(k) & 0x8000 for k in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
                        raise RuntimeError("修飾キーが押されているためカーソル復元を中止しました")
                    chord(0x23)  # End, then return across the unchanged suffix.
                    for _ in text[caret:]:
                        chord(0x25)
                logger.debug("貼り付け後のカーソル復元位置=%s", caret)
        finally:
            try:
                if owned is not None and not timed_out:
                    self.clipboard.restore(saved, owned)
            finally:
                saved.release()
