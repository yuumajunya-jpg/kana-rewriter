"""Target-window IME control; keep disabled until input is read back."""
import ctypes as C
import logging
import time

from .direct_win32 import imm, window_identity
from .winapi import user, W

logger = logging.getLogger(__name__)
imm.ImmGetDefaultIMEWnd.argtypes = [W.HWND]
imm.ImmGetDefaultIMEWnd.restype = W.HWND


class ImeSession:
    def __init__(self, target):
        self.target = target
        self.hwnd = imm.ImmGetDefaultIMEWnd(target[1])
        self.changed = False
        if not self.hwnd:
            raise RuntimeError("入力先のIME状態を確認できません。文字は送信していません")
        self.original = self.is_open()

    def message(self, command, value=0):
        result = C.c_size_t()
        if not user.SendMessageTimeoutW(self.hwnd, 0x0283, command, value, 0x23,
                                       500, C.byref(result)):
            raise RuntimeError("IME制御の応答を確認できません。文字入力は再送しません")
        return result.value

    def is_open(self):
        return bool(self.message(5))  # IMC_GETOPENSTATUS

    def set_open(self, opened):
        self.message(6, int(opened))  # IMC_SETOPENSTATUS
        deadline = time.monotonic() + 0.5
        while self.is_open() != opened:
            if window_identity() != self.target or time.monotonic() >= deadline:
                raise RuntimeError("IME状態の切り替えを確認できません。文字入力は再送しません")
            time.sleep(0.01)

    def disable(self):
        if window_identity() != self.target:
            raise RuntimeError("IME切り替え前に入力先が変わりました")
        logger.debug("IME入力制御: 元のopen=%s", self.original)
        if self.original:
            # Timeout does not prove the request had no effect.
            self.changed = True
            self.set_open(False)
        if self.is_open():
            raise RuntimeError("IMEが有効なためUnicode入力を中止しました")
        logger.debug("IME入力制御: off確認済み")

    def close(self):
        if not self.changed:
            return
        self.changed = False
        if window_identity() != self.target:
            logger.warning("入力先が変わったためIMEの復元を省略しました。元の入力先のIME状態を確認してください")
            return
        if not self.is_open():
            self.set_open(True)
        logger.debug("IME入力制御: 元のon状態へ復元済み")
