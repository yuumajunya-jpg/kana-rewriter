"""Eager Win32 clipboard snapshots; never retain a live OLE IDataObject."""
import ctypes as C
from ctypes import wintypes as W
from contextlib import contextmanager
from dataclasses import dataclass
import time

user = C.WinDLL("user32", use_last_error=True)
kernel = C.WinDLL("kernel32", use_last_error=True)
gdi = C.WinDLL("gdi32", use_last_error=True)
ole = C.WinDLL("ole32", use_last_error=True)  # HANDLE return, not HRESULT/OleDLL

user.OpenClipboard.argtypes = [W.HWND]
user.OpenClipboard.restype = W.BOOL
user.CloseClipboard.argtypes = []
user.CloseClipboard.restype = W.BOOL
user.EmptyClipboard.argtypes = []
user.EmptyClipboard.restype = W.BOOL
user.EnumClipboardFormats.argtypes = [W.UINT]
user.EnumClipboardFormats.restype = W.UINT
user.GetClipboardData.argtypes = [W.UINT]
user.GetClipboardData.restype = W.HANDLE
user.SetClipboardData.argtypes = [W.UINT, W.HANDLE]
user.SetClipboardData.restype = W.HANDLE
user.GetClipboardSequenceNumber.restype = W.DWORD
user.GetClipboardOwner.restype = W.HWND
user.GetClipboardFormatNameW.argtypes = [W.UINT, W.LPWSTR, C.c_int]
user.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
                               C.c_int, C.c_int, C.c_int, C.c_int,
                               W.HWND, W.HMENU, W.HINSTANCE, C.c_void_p]
user.CreateWindowExW.restype = W.HWND
user.DestroyWindow.argtypes = [W.HWND]
user.PeekMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT]
kernel.GlobalLock.argtypes = [W.HGLOBAL]
kernel.GlobalLock.restype = C.c_void_p
kernel.GlobalUnlock.argtypes = [W.HGLOBAL]
kernel.GlobalFree.argtypes = [W.HGLOBAL]
kernel.GlobalFree.restype = W.HGLOBAL
kernel.GlobalAlloc.argtypes = [W.UINT, C.c_size_t]
kernel.GlobalAlloc.restype = W.HGLOBAL
ole.OleDuplicateData.argtypes = [W.HANDLE, W.WORD, W.UINT]
ole.OleDuplicateData.restype = W.HANDLE
gdi.DeleteObject.argtypes = [W.HANDLE]
gdi.DeleteEnhMetaFile.argtypes = [W.HANDLE]
gdi.DeleteMetaFile.argtypes = [W.HANDLE]


class MetafilePicture(C.Structure):
    _fields_ = [("mode", C.c_int), ("x", C.c_int), ("y", C.c_int), ("metafile", W.HANDLE)]


def pump_sent_messages():
    # PeekMessage dispatches cross-thread sent messages, including the editor's
    # WM_DESTROYCLIPBOARD for our hidden owner window. PM_NOREMOVE leaves queued
    # hotkeys for the application's main loop instead of swallowing them.
    message = W.MSG()
    user.PeekMessageW(C.byref(message), None, 0, 0, 0)


def free_data(fmt, handle):
    if not handle:
        return
    if fmt in (2, 9, 0x82):  # BITMAP, PALETTE, DSPBITMAP
        gdi.DeleteObject(handle)
    elif fmt in (14, 0x8E):  # ENHMETAFILE, DSPENHMETAFILE
        gdi.DeleteEnhMetaFile(handle)
    else:
        if fmt in (3, 0x83):  # METAFILEPICT owns both a metafile and global memory.
            pointer = kernel.GlobalLock(handle)
            if pointer:
                try:
                    picture = C.cast(pointer, C.POINTER(MetafilePicture)).contents
                    gdi.DeleteMetaFile(picture.metafile)
                finally:
                    kernel.GlobalUnlock(handle)
        kernel.GlobalFree(handle)


@dataclass
class Snapshot:
    items: list
    sequence: int

    def release(self):
        for fmt, handle in self.items:
            free_data(fmt, handle)
        self.items.clear()


class ClipboardWriteError(RuntimeError):
    def __init__(self, sequence):
        super().__init__("貼り付け用テキストをクリップボードに置けませんでした")
        self.sequence = sequence


class Clipboard:
    def __init__(self):
        # EmptyClipboard/SetClipboardData need a real owner HWND, even for eager
        # data. A STATIC message-only window never appears or steals focus.
        self.hwnd = user.CreateWindowExW(0, "STATIC", "kana-rewriter clipboard", 0,
                                        0, 0, 0, 0, W.HWND(-3), None, None, None)
        if not self.hwnd:
            raise C.WinError(C.get_last_error())

    def close(self):
        if self.hwnd:
            user.DestroyWindow(self.hwnd)
            self.hwnd = None

    @contextmanager
    def opened(self, timeout=1.0):
        deadline = time.monotonic() + timeout
        while not user.OpenClipboard(self.hwnd):
            pump_sent_messages()
            if time.monotonic() >= deadline:
                raise RuntimeError("クリップボードが使用中です。しばらく待って再実行してください")
            time.sleep(0.01)
        try:
            yield
        finally:
            if not user.CloseClipboard():
                raise RuntimeError("Win32クリップボードを閉じられませんでした")

    def snapshot(self):
        saved = Snapshot([], 0)
        try:
            with self.opened():
                # Render and duplicate all payloads BEFORE Ctrl+C changes their
                # source. OLE's DataObject tracking formats aren't payloads.
                fmt = 0
                while True:
                    C.set_last_error(0)
                    fmt = user.EnumClipboardFormats(fmt)
                    if not fmt:
                        if C.get_last_error():
                            raise C.WinError(C.get_last_error())
                        break
                    if fmt == 0x80:  # OWNERDISPLAY has no transferable data.
                        continue
                    if 0x200 <= fmt <= 0x3FF:
                        raise RuntimeError("クリップボードの独自形式を退避できません。通常の文字をコピーしてから実行してください")
                    if fmt >= 0xC000:
                        name = C.create_unicode_buffer(256)
                        user.GetClipboardFormatNameW(fmt, name, len(name))
                        if name.value in {"DataObject", "Ole Private Data"}:
                            continue
                    handle = user.GetClipboardData(fmt)
                    duplicate = ole.OleDuplicateData(handle, fmt, 0x0002) if handle else None
                    if not duplicate:
                        raise RuntimeError(f"クリップボード形式 {fmt} の退避に失敗しました（コピー前に中止）")
                    saved.items.append((fmt, duplicate))
                # Delayed rendering during snapshot may increase the sequence.
                saved.sequence = user.GetClipboardSequenceNumber()
            return saved
        except BaseException:
            saved.release()
            raise

    def read_changed_text(self, sequence):
        # Called repeatedly: an editor or clipboard manager may hold it briefly.
        if not user.OpenClipboard(self.hwnd):
            return None
        try:
            if user.GetClipboardSequenceNumber() == sequence:
                return None
            handle = user.GetClipboardData(13)
            if not handle:
                return None
            pointer = kernel.GlobalLock(handle)
            if not pointer:
                return None
            try:
                text = C.wstring_at(pointer)
            finally:
                kernel.GlobalUnlock(handle)
            # Capture AFTER GetClipboardData's delayed rendering, not before it.
            return text, user.GetClipboardSequenceNumber()
        finally:
            if not user.CloseClipboard():
                raise RuntimeError("Win32クリップボードを閉じられませんでした")

    def write_text(self, text, expected_sequence):
        """Publish the entire replacement as one CF_UNICODETEXT payload."""
        if "\x00" in text:
            raise ValueError("貼り付け文字列にNUL文字が含まれています")
        payload = text.encode("utf-16-le") + b"\x00\x00"
        handle = kernel.GlobalAlloc(2, len(payload))
        if not handle:
            raise C.WinError(C.get_last_error())
        try:
            pointer = kernel.GlobalLock(handle)
            if not pointer:
                raise C.WinError(C.get_last_error())
            try:
                C.memmove(pointer, payload, len(payload))
            finally:
                kernel.GlobalUnlock(handle)
            with self.opened():
                if user.GetClipboardSequenceNumber() != expected_sequence:
                    raise RuntimeError("貼り付け準備中にクリップボードが変更されたため中止しました")
                if not user.EmptyClipboard():
                    raise RuntimeError("貼り付け用クリップボードの初期化に失敗しました")
                if not user.SetClipboardData(13, handle):
                    # EmptyClipboard already changed the clipboard. Let the
                    # caller restore its backup, still guarded by this sequence.
                    raise ClipboardWriteError(user.GetClipboardSequenceNumber())
                handle = None  # Windows owns the payload now.
            # Closing publishes the update and may synthesize other formats.
            # Query only after CloseClipboard has finished, not in a return
            # expression inside the context manager (evaluated before close).
            return self.confirm_text(text)
        finally:
            if handle:
                kernel.GlobalFree(handle)

    def confirm_text(self, expected_text):
        """Accept format-only updates, but never a different owner or payload."""
        with self.opened():
            if user.GetClipboardOwner() != self.hwnd:
                raise RuntimeError("貼り付け前にクリップボードの所有者が変わったため中止しました")
            handle = user.GetClipboardData(13)
            pointer = kernel.GlobalLock(handle) if handle else None
            if not pointer:
                raise RuntimeError("貼り付け用クリップボードの文字列を確認できませんでした")
            try:
                if C.wstring_at(pointer) != expected_text:
                    raise RuntimeError("貼り付け前にクリップボードの文字列が変わったため中止しました")
            finally:
                kernel.GlobalUnlock(handle)
            return user.GetClipboardSequenceNumber()

    def restore(self, saved, owned_sequence):
        with self.opened():
            # Check under the clipboard lock so newer clipboard data survives.
            if user.GetClipboardSequenceNumber() != owned_sequence:
                return False
            if not user.EmptyClipboard():
                raise RuntimeError("クリップボード復元時の初期化に失敗しました")
            for index, (fmt, handle) in enumerate(saved.items):
                if not user.SetClipboardData(fmt, handle):
                    raise RuntimeError(f"クリップボード形式 {fmt} の復元に失敗しました")
                # Windows owns successful handles; never free them ourselves.
                saved.items[index] = (fmt, None)
        return True
