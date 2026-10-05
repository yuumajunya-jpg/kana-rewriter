"""Observe half/full-width make/break using device Raw Input, before IME."""
import ctypes as C
from ctypes import wintypes as W
import logging
import threading
import time

from .input_activity import InputActivity, user, kernel
from .timing import stage
from .waiting import sleep as poll_sleep


logger = logging.getLogger(__name__)
WNDPROC = C.WINFUNCTYPE(W.LPARAM, W.HWND, W.UINT, W.WPARAM, W.LPARAM)


class WindowClass(C.Structure):
    _fields_ = [("style", W.UINT), ("procedure", WNDPROC), ("class_extra", C.c_int),
                ("window_extra", C.c_int), ("instance", W.HINSTANCE), ("icon", W.HICON),
                ("cursor", W.HANDLE), ("background", W.HBRUSH),
                ("menu", W.LPCWSTR), ("name", W.LPCWSTR)]


class RawDevice(C.Structure):
    _fields_ = [("page", W.USHORT), ("usage", W.USHORT), ("flags", W.DWORD), ("target", W.HWND)]


class RawHeader(C.Structure):
    _fields_ = [("kind", W.DWORD), ("size", W.DWORD), ("device", W.HANDLE), ("param", W.WPARAM)]


class RawKeyboard(C.Structure):
    _fields_ = [("scan", W.USHORT), ("flags", W.USHORT), ("reserved", W.USHORT),
                ("vk", W.USHORT), ("message", W.UINT), ("extra", W.ULONG)]


class RawPacket(C.Structure):
    _fields_ = [("header", RawHeader), ("keyboard", RawKeyboard)]


user.RegisterClassW.argtypes = [C.POINTER(WindowClass)]
user.RegisterClassW.restype = W.ATOM
user.UnregisterClassW.argtypes = [W.LPCWSTR, W.HINSTANCE]
user.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
                               C.c_int, C.c_int, C.c_int, C.c_int,
                               W.HWND, W.HMENU, W.HINSTANCE, C.c_void_p]
user.CreateWindowExW.restype = W.HWND
user.DestroyWindow.argtypes = [W.HWND]
user.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
user.DefWindowProcW.restype = W.LPARAM
user.DispatchMessageW.argtypes = [C.POINTER(W.MSG)]
user.DispatchMessageW.restype = W.LPARAM
user.RegisterRawInputDevices.argtypes = [C.POINTER(RawDevice), W.UINT, W.UINT]
user.GetRawInputData.argtypes = [W.HANDLE, W.UINT, C.c_void_p, C.POINTER(W.UINT), W.UINT]
user.GetRawInputData.restype = W.UINT
user.MapVirtualKeyW.argtypes = [W.UINT, W.UINT]
user.MapVirtualKeyW.restype = W.UINT


class JapaneseKeyRelease(InputActivity):
    # Reuse lifecycle/health checks, but install no low-level input hooks.

    def __init__(self):
        super().__init__()
        self.held = False
        self.observed = False
        self.devices_held = set()
        self.makes = self.breaks = 0
        self.window = None
        self.state_lock = threading.Lock()

    def observe(self, packet):
        event = packet.keyboard
        if packet.header.kind != 1:
            return
        # Match Microsoft's RAWKEYBOARD sample: strip the break bit from
        # MakeCode, and recover an absent scan code with MAPVK_VK_TO_VSC_EX.
        # Keep the fallback restricted to half/full-width virtual keys.
        scan = event.scan & 0x7F if event.scan <= 0xFF else event.scan
        if not event.scan and event.vk in (0xF3, 0xF4, 0x19):
            scan = user.MapVirtualKeyW(event.vk, 4)
        candidate = scan == 0x29 or event.vk in (0xF3, 0xF4, 0x19)
        accepted = bool(packet.header.device and scan == 0x29 and not event.flags & (2 | 4))
        if candidate:
            logger.debug("半角全角Raw通知: scan=0x%04X / 正規化=0x%04X / "
                         "flags=0x%04X / vk=0x%02X / message=0x%04X / "
                         "device=%s / 採用=%s", event.scan, scan, event.flags,
                         event.vk, event.message, packet.header.device, accepted)
        if not accepted:
            return
        with self.state_lock:
            if event.flags & 1:  # RI_KEY_BREAK; independent of legacy Message/VK
                self.breaks += 1
                self.devices_held.discard(packet.header.device)
            else:
                self.makes += 1
                self.devices_held.add(packet.header.device)
            self.held = bool(self.devices_held)
            self.observed = True

    def read_raw(self, handle):
        size = W.UINT()
        if user.GetRawInputData(handle, 0x10000003, None, C.byref(size), C.sizeof(RawHeader)) != 0:
            raise C.WinError(C.get_last_error())
        if not C.sizeof(RawPacket) <= size.value <= 65536:
            raise RuntimeError("半角全角キーのRaw Inputサイズが不正です")
        buffer = C.create_string_buffer(size.value)
        read = user.GetRawInputData(handle, 0x10000003, buffer, C.byref(size), C.sizeof(RawHeader))
        if read == 0xFFFFFFFF or read < C.sizeof(RawPacket):
            raise RuntimeError("半角全角キーのRaw Inputを取得できません")
        packet = RawPacket.from_buffer_copy(buffer)
        if not C.sizeof(RawPacket) <= packet.header.size <= read:
            raise RuntimeError("半角全角キーのRaw Inputが不完全です")
        self.observe(packet)

    def window_proc(self, hwnd, message, wparam, lparam):
        if message == 0x00FF:  # WM_INPUT
            try:
                self.read_raw(lparam)
            except Exception as exc:
                self.error = exc
        # DefWindowProc performs WM_INPUT cleanup; never suppress legacy input.
        return user.DefWindowProcW(hwnd, message, wparam, lparam)

    def _run(self):
        name = f"KanaRawKeyboard_{id(self)}"
        module = kernel.GetModuleHandleW(None)
        callback = WNDPROC(self.window_proc)
        atom = registered = False
        try:
            message = W.MSG()
            user.PeekMessageW(C.byref(message), None, 0, 0, 0)
            self.thread_id = kernel.GetCurrentThreadId()
            klass = WindowClass(0, callback, 0, 0, module, None, None, None, None, name)
            atom = user.RegisterClassW(C.byref(klass))
            if not atom:
                raise C.WinError(C.get_last_error())
            self.window = user.CreateWindowExW(0, name, "", 0, 0, 0, 0, 0, -3, None, module, None)
            if not self.window:
                raise C.WinError(C.get_last_error())
            device = RawDevice(1, 6, 0x100, self.window)  # keyboard + RIDEV_INPUTSINK
            if not user.RegisterRawInputDevices(C.byref(device), 1, C.sizeof(device)):
                raise C.WinError(C.get_last_error())
            registered = True
            self.ready.set()
            while not self.stopping.is_set():
                result = user.GetMessageW(C.byref(message), None, 0, 0)
                if result == -1:
                    raise C.WinError(C.get_last_error())
                if not result:
                    break
                user.DispatchMessageW(C.byref(message))
        except Exception as exc:
            self.error = exc
        finally:
            if registered:
                device = RawDevice(1, 6, 1, None)  # RIDEV_REMOVE
                user.RegisterRawInputDevices(C.byref(device), 1, C.sizeof(device))
            if self.window:
                user.DestroyWindow(self.window)
                self.window = None
            if atom:
                user.UnregisterClassW(name, module)
            self.ready.set()

    @stage("適用/半角全角キー解放待ち")
    def wait_released(self):
        deadline = time.monotonic() + 2
        while True:
            self.snapshot()  # fail closed if Raw Input monitoring stopped
            with self.state_lock:
                observed, held, makes, breaks = self.observed, self.held, self.makes, self.breaks
            if observed and not held:
                logger.debug("半角全角Raw Input: 押下=%s回 / 解放=%s回 / 押下中=False", makes, breaks)
                return
            if time.monotonic() >= deadline:
                logger.debug("半角全角Raw Input: 押下=%s回 / 解放=%s回 / 押下中=%s", makes, breaks, held)
                if not observed:
                    raise RuntimeError("半角全角キーのRaw Inputを確認できません。日本語キーボードの入力を確認してください")
                raise RuntimeError("半角全角キーを離してください")
            poll_sleep(0.005)
