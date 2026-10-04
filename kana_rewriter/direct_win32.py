"""Clipboard-free messages to Unicode Edit/RichEdit controls."""
import ctypes as C
import logging
import time
from ctypes import wintypes as W

from .editor import TextState, python_offset, utf16_length
from .winapi import user, focus, KeyboardInput, send
from .input_activity import input_tick
from .timing import stage
from .waiting import sleep as poll_sleep

logger = logging.getLogger(__name__)
kernel = C.WinDLL("kernel32", use_last_error=True)
kernel.GetCurrentThreadId.restype = W.DWORD


get_window_style = getattr(user, "GetWindowLongPtrW", user.GetWindowLongW)
get_window_style.argtypes = [W.HWND, C.c_int]
get_window_style.restype = W.LPARAM
user.IsWindowUnicode.argtypes = [W.HWND]
user.IsWindowEnabled.argtypes = [W.HWND]
imm = C.WinDLL("imm32", use_last_error=True)
imm.ImmGetContext.argtypes = [W.HWND]
imm.ImmGetContext.restype = W.HANDLE
imm.ImmGetCompositionStringW.argtypes = [W.HANDLE, W.DWORD, C.c_void_p, W.DWORD]
imm.ImmGetCompositionStringW.restype = C.c_long
imm.ImmReleaseContext.argtypes = [W.HWND, W.HANDLE]


def window_identity():
    foreground, hwnd, *_ = focus()
    pid = W.DWORD()
    user.GetWindowThreadProcessId(hwnd, C.byref(pid))
    return foreground, hwnd, pid.value


def class_name(hwnd):
    name = C.create_unicode_buffer(256)
    user.GetClassNameW(hwnd, name, len(name))
    return name.value.lower()


def is_standard_edit(hwnd):
    name = class_name(hwnd)
    return name == "edit" or name.startswith("richedit")


def check_input_ready(hwnd, allow_modifiers=False):
    if not allow_modifiers and any(user.GetAsyncKeyState(k) & 0x8000 for k in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
        raise RuntimeError("修飾キーが押されているため中止しました")
    if any(user.GetAsyncKeyState(k) & 0x8000 for k in (0x01, 0x02, 0x04, 0x05, 0x06)):
        raise RuntimeError("マウスボタンが押されているため中止しました")
    # IMM is a thread-owned input context, not an authoritative composition
    # query for an external editor. For external controls TextEditPattern is
    # checked by the UIA backend when available.
    thread = user.GetWindowThreadProcessId(hwnd, None)
    if thread != kernel.GetCurrentThreadId():
        logger.debug("IME判定: 別スレッドの入力欄のためIMM直接照会を省略")
        return
    context = imm.ImmGetContext(hwnd)
    if context:
        try:
            length = imm.ImmGetCompositionStringW(context, 8, None, 0)
            logger.debug("IME判定: 自スレッドのIMM / 未確定文字列bytes=%s", length)
            if length > 0:
                raise RuntimeError("IMEの入力を確定してから変換してください（IMM）")
        finally:
            imm.ImmReleaseContext(hwnd, context)


@stage("適用/キー解放待ち")
def wait_input_release(target, trigger_keys=()):
    keys = (0x10, 0x11, 0x12, 0x5B, 0x5C) + tuple(trigger_keys)
    deadline = time.monotonic() + 2
    while True:
        if window_identity() != target:
            raise RuntimeError("キー解放待ちに入力先が変わりました")
        if not any(user.GetAsyncKeyState(key) & 0x8000 for key in keys):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("ショートカットキーを離してください")
        poll_sleep(0.005)


def unicode_events(text):
    if "\x00" in text:
        raise ValueError("入力文字列にNUL文字が含まれています")
    events = []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for character in normalized:
        if character in "\n\t":
            # Enter means an editor-defined line break. Tab changes focus in
            # many controls, so it is deliberately not synthesized.
            if character == "\t":
                raise ValueError("UIA入力ではタブを含む範囲は未対応です")
            events += [KeyboardInput(0x0D, 0, 0, 0, 0), KeyboardInput(0x0D, 0, 2, 0, 0)]
        else:
            payload = character.encode("utf-16-le")
            for offset in range(0, len(payload), 2):
                unit = int.from_bytes(payload[offset:offset + 2], "little")
                events += [KeyboardInput(0, unit, 4, 0, 0), KeyboardInput(0, unit, 6, 0, 0)]
    return events


class NativeEditor:
    validates_before_replace = True
    kind = "win32"

    def __init__(self, hwnd, config):
        self.hwnd = hwnd
        self.window = window_identity()
        self.limit = config.max_document_chars
        self.timeout_ms = int(config.editor_timeout_seconds * 1000)
        self.rich = class_name(hwnd).startswith("richedit")
        if not is_standard_edit(hwnd) or not user.IsWindowUnicode(hwnd):
            raise RuntimeError("この入力欄はUnicodeの標準Edit/RichEditではありません")
        if not user.IsWindowEnabled(hwnd) or get_window_style(hwnd, -16) & (0x800 | 0x20):
            raise RuntimeError("読み取り専用・無効・パスワード入力欄は対象外です")

    def message(self, code, wparam=0, lparam=0):
        result = C.c_size_t()
        C.set_last_error(0)
        if not user.SendMessageTimeoutW(self.hwnd, code, wparam, lparam, 0x23,
                                       self.timeout_ms, C.byref(result)):
            raise RuntimeError("入力欄が応答しないか、権限が一致しません。編集は再送しません")
        return result.value

    @stage("Win32/本文と位置取得")
    def read(self):
        length = self.message(0x000E)
        if length > self.limit * 2:
            raise RuntimeError("入力欄の本文がmax_document_charsを超えています")
        buffer = C.create_unicode_buffer(length + 1)
        self.message(0x000D, len(buffer), C.addressof(buffer))
        if self.message(0x000E) != length:
            raise RuntimeError("本文の取得中に文字列が変更されました")
        text = buffer.value
        # RichEdit character positions use CR for paragraph breaks; WM_GETTEXT
        # may expand them to CRLF. Keep that mapping out of the model backend.
        if self.rich:
            text = text.replace("\r\n", "\r")
        if len(text) > self.limit:
            raise RuntimeError("入力欄の本文がmax_document_charsを超えています")
        start, end = W.DWORD(), W.DWORD()
        self.message(0x00B0, C.addressof(start), C.addressof(end))
        return TextState(text, python_offset(text, start.value), python_offset(text, end.value))

    def select(self, state, start, end):
        self.message(0x00B1, utf16_length(state.text[:start]), utf16_length(state.text[:end]))

    @stage("Win32/選択と置換")
    def replace(self, state, start, end, result, expected_tick=None):
        tick = input_tick() if expected_tick is None else expected_tick
        if window_identity() != self.window or input_tick() != tick:
            raise RuntimeError("入力先または操作状態が変わりました")
        check_input_ready(self.hwnd)
        if self.read() != state:
            raise RuntimeError("置換直前に本文または選択位置が変わりました")
        self.select(state, start, end)
        selected = self.read()
        if selected != TextState(state.text, start, end):
            raise RuntimeError("置換対象の選択を確認できませんでした")
        check_input_ready(self.hwnd)
        if input_tick() != tick or window_identity() != self.window:
            raise RuntimeError("置換の準備中に操作があったため中止しました")
        buffer = C.create_unicode_buffer(result)
        self.message(0x00C2, 1, C.addressof(buffer))  # undo-enabled EM_REPLACESEL

    def restore_caret(self, state, caret, expected_tick=None):
        if window_identity() != self.window or (expected_tick is not None and input_tick() != expected_tick):
            raise RuntimeError("カーソル復元前に入力先が変わりました")
        self.select(state, caret, caret)

    def close(self):
        pass
