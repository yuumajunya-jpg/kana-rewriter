"""Track only the physical half/full-width key, never text or other keys."""
import ctypes as C
from ctypes import wintypes as W
import time

from .input_activity import InputActivity, user
from .timing import stage
from .waiting import sleep as poll_sleep


class KeyboardEvent(C.Structure):
    _fields_ = [("vkCode", W.DWORD), ("scanCode", W.DWORD), ("flags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", C.c_size_t)]


class JapaneseKeyRelease(InputActivity):
    # Reuse the isolated message-loop lifecycle, but install no mouse hook.
    hook_kinds = (13,)

    def __init__(self):
        super().__init__()
        self.held = False
        self.observed = False

    def _keyboard(self, code, message, data):
        if code == 0 and data and message in (0x0100, 0x0101, 0x0104, 0x0105):
            event = C.cast(data, C.POINTER(KeyboardEvent)).contents
            # SC029 is the physical Japanese half/full-width key. Ignore
            # injected/extended keys and do not retain virtual-key values.
            # Key-up can carry a different VK after an IME state transition.
            if event.scanCode == 0x29 and not event.flags & (0x10 | 0x01):
                self.observed = True
                self.held = message in (0x0100, 0x0104)
        return user.CallNextHookEx(None, code, message, data)

    @stage("適用/半角全角キー解放待ち")
    def wait_released(self):
        deadline = time.monotonic() + 2
        while True:
            self.snapshot()  # fail closed if the hook thread stopped
            if not self.observed:
                raise RuntimeError("半角全角キーの物理入力を確認できません。日本語キーボードの半角全角キーを押し直してください")
            if not self.held:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("半角全角キーを離してください")
            poll_sleep(0.005)
